# SPDX-License-Identifier: MPL-2.0
"""Who may sign a Maelys release, at the two ends that read it.

GitHub's `verified` says the key belongs to some account, and every account
that may push a tag has one. `share/allowed-signers` says which key, and it
is read twice: by `preflight` and `cut`, before a tag exists, and by the
release workflow against the tag's own signature, at the socle commit the
product pinned.

The shell of that step is not mocked here: a tag is signed with a generated
key and the same `ssh-keygen -Y` sequence runs against it, so what the tests
hold is the sequence the workflow runs, not a description of it.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

from maelys_socle.release_checks import (  # noqa: E402
    allowed_signer_keys, public_key, signing_key_named,
)

SIGNERS = ROOT / "share" / "allowed-signers"
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def git(*arguments: str, cwd: pathlib.Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


class AllowedSignersFileTest(unittest.TestCase):
    def test_the_fleet_names_at_least_one_key_for_git(self) -> None:
        entries = allowed_signer_keys(SIGNERS.read_text(encoding="utf-8"))
        self.assertTrue(entries, "an empty file refuses every tag")
        for principal, kind, material in entries:
            self.assertRegex(principal, r"\S")
            self.assertTrue(kind.startswith(("ssh-", "ecdsa-", "sk-")), kind)
            self.assertGreater(len(material), 32)
        self.assertIn('namespaces="git"', SIGNERS.read_text(encoding="utf-8"))

    def test_a_retired_key_keeps_its_line(self) -> None:
        """A line removed turns a past release into one that can no longer be
        replayed, and a failed publication is replayed on its own tag."""
        text = SIGNERS.read_text(encoding="utf-8")
        self.assertIn("valid-before", text)
        self.assertIn("retired, never deleted", text)

    def test_options_never_hide_the_key(self) -> None:
        text = ('# a comment\n'
                '\n'
                'someone@example.org namespaces="git",valid-before="20260901" ssh-ed25519 AAAAOLD comment\n'
                'other@example.org ssh-ed25519 AAAANEW\n')
        self.assertEqual(allowed_signer_keys(text),
                         [("someone@example.org", "ssh-ed25519", "AAAAOLD"),
                          ("other@example.org", "ssh-ed25519", "AAAANEW")])


class SigningKeyNamedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="maelys-signers-")
        self.addCleanup(self.directory.cleanup)
        self.dir = pathlib.Path(self.directory.name)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "probe",
                        "-f", str(self.dir / "key")], check=True, capture_output=True)
        self.public = (self.dir / "key.pub").read_text(encoding="utf-8").strip()
        self.named = self.dir / "allowed-signers"
        self.named.write_text(f'probe@example.org namespaces="git" {self.public}\n', encoding="utf-8")

    def verdicts(self, declared: str, fmt: str = "ssh", signers: "pathlib.Path | None" = None):
        return signing_key_named(self.dir, declared, fmt, signers if signers else self.named)

    def test_a_named_key_passes_whichever_way_it_is_declared(self) -> None:
        for declared in (str(self.dir / "key.pub"), str(self.dir / "key"), self.public):
            with self.subTest(declared=declared[:20]):
                found = self.verdicts(declared)
                self.assertEqual([status for status, _ in found], ["ok"], found)
                self.assertIn("probe@example.org", found[0][1])

    def test_a_key_the_file_does_not_name_is_refused(self) -> None:
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "other",
                        "-f", str(self.dir / "other")], check=True, capture_output=True)
        found = self.verdicts(str(self.dir / "other.pub"))
        self.assertEqual([status for status, _ in found], ["fail"])
        self.assertIn("is not named in", found[0][1])

    def test_it_fails_closed(self) -> None:
        """No file, an empty file, a format the fleet does not verify."""
        missing = self.verdicts(str(self.dir / "key.pub"), signers=self.dir / "absent")
        self.assertEqual([status for status, _ in missing], ["fail"])
        self.assertIn("is missing", missing[0][1])
        empty = self.dir / "empty"
        empty.write_text("# nothing but a comment\n", encoding="utf-8")
        blank = self.verdicts(str(self.dir / "key.pub"), signers=empty)
        self.assertEqual([status for status, _ in blank], ["fail"])
        self.assertIn("names no key", blank[0][1])
        openpgp = self.verdicts("ABCDEF0123456789", fmt="openpgp")
        self.assertEqual([status for status, _ in openpgp], ["fail"])
        self.assertIn("openpgp", openpgp[0][1])

    def test_a_key_it_cannot_read_is_unknown_and_not_refused(self) -> None:
        """A reading that failed is not an answer: the socle says so rather
        than refusing a key it never saw."""
        found = self.verdicts(str(self.dir / "nowhere.pub"))
        self.assertEqual([status for status, _ in found], ["note"])
        self.assertIn("unknown here", found[0][1])
        self.assertEqual(public_key(self.dir, str(self.dir / "nowhere.pub")), "")

    def test_no_key_declared_says_nothing_here(self) -> None:
        # The missing user.signingkey has its own verdict beside this one.
        self.assertEqual(self.verdicts(""), [])


class ReleaseWorkflowSignatureTest(unittest.TestCase):
    """The sequence the workflow runs, against a tag signed for the test."""

    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is required")
        self.directory = tempfile.TemporaryDirectory(prefix="maelys-signed-tag-")
        self.addCleanup(self.directory.cleanup)
        self.dir = pathlib.Path(self.directory.name)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "signer",
                        "-f", str(self.dir / "key")], check=True, capture_output=True)
        repository = self.dir / "repository"
        repository.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=repository)
        for key, value in (("user.name", "Signer"), ("user.email", "signer@example.org"),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "true"),
                           ("gpg.format", "ssh"), ("user.signingkey", str(self.dir / "key.pub"))):
            git("config", key, value, cwd=repository)
        (repository / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        git("add", "-A", cwd=repository)
        git("commit", "-q", "-m", "one", cwd=repository)
        git("tag", "-a", "-m", "release 1.2.3", "v1.2.3", cwd=repository)
        self.repository = repository
        # What GitHub's verification.payload and .signature hold, byte for
        # byte: the tag object without its signature, and the armour.
        raw = subprocess.run(["git", "-C", str(repository), "cat-file", "tag", "v1.2.3"],
                             capture_output=True, check=True).stdout
        start = raw.index(b"-----BEGIN SSH SIGNATURE-----")
        (self.dir / "payload").write_bytes(raw[:start])
        (self.dir / "signature").write_bytes(raw[start:])
        self.named = self.dir / "allowed-signers"
        public = (self.dir / "key.pub").read_text(encoding="utf-8").strip()
        self.named.write_text(f'signer@example.org namespaces="git" {public}\n', encoding="utf-8")

    def keygen(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh-keygen", *arguments], capture_output=True, text=True,
                              input=(self.dir / "payload").read_text(encoding="utf-8"))

    def test_a_named_key_verifies_and_an_unnamed_one_does_not(self) -> None:
        found = self.keygen("-Y", "find-principals", "-f", str(self.named),
                            "-s", str(self.dir / "signature"))
        self.assertEqual(found.returncode, 0, found.stderr)
        principal = found.stdout.split()[0]
        self.assertEqual(principal, "signer@example.org")
        verified = self.keygen("-Y", "verify", "-f", str(self.named), "-I", principal,
                               "-n", "git", "-s", str(self.dir / "signature"))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        # ssh-keygen says it on stdout or stderr depending on its version.
        self.assertIn("Good \"git\" signature", verified.stdout + verified.stderr)

        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "other",
                        "-f", str(self.dir / "other")], check=True, capture_output=True)
        elsewhere = self.dir / "elsewhere"
        elsewhere.write_text('other@example.org namespaces="git" '
                             + (self.dir / "other.pub").read_text(encoding="utf-8").strip() + "\n",
                             encoding="utf-8")
        refused = self.keygen("-Y", "find-principals", "-f", str(elsewhere),
                              "-s", str(self.dir / "signature"))
        self.assertNotEqual(refused.returncode, 0, "a key the file does not name must not verify")

    def test_the_workflow_reads_the_socle_the_product_pinned_and_fails_closed(self) -> None:
        step = WORKFLOW.split("Verify the tag was signed by a key the fleet names", 1)[1]
        step = step.split("- name: Verify the tag's commit", 1)[0]
        # The file is read at the reusable workflow's own commit, which is
        # the one the product pinned, and from its own repository.
        self.assertIn("github.job_workflow_sha", step)
        self.assertIn("github.job_workflow_ref", step)
        self.assertIn("contents/share/allowed-signers?ref=${SOCLE_SHA}", step)
        # Every way out is a refusal, and none falls back to GitHub's verdict.
        for guard in ('test -n "$SOCLE_SHA"', 'test -n "$socle"', 'test -s "$signers"',
                      'test -s "$RUNNER_TEMP/signature"', 'test -n "$principal"'):
            self.assertIn(guard, step, guard)
        self.assertIn("ssh-keygen -Y find-principals", step)
        self.assertIn("ssh-keygen -Y verify", step)
        self.assertIn("-n git", step)
        # jq -j: the payload is signed byte for byte.
        self.assertIn("jq -rj .verification.payload", step)
        self.assertRegex(step, r"set -euo pipefail")


if __name__ == "__main__":
    unittest.main()
