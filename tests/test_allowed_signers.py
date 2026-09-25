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

import os
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
    allowed_signer_keys, public_key, signer_date, signer_options, signing_key_named,
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
        for principal, options, kind, material in entries:
            self.assertRegex(principal, r"\S")
            self.assertTrue(kind.startswith(("ssh-", "ecdsa-", "sk-")), kind)
            self.assertGreater(len(material), 32)
            # Whatever the fleet names today must be able to sign a tag.
            self.assertIn("git", signer_options(options).get("namespaces", "git"))
        self.assertIn('namespaces="git"', SIGNERS.read_text(encoding="utf-8"))

    def test_a_retired_key_keeps_its_line(self) -> None:
        """The file says so; this measures that it is true.

        An ssh signature carries no timestamp, so ssh-keygen judges
        valid-before against the clock of whoever runs it. Judged at the
        clock, retiring a key breaks every tag it ever signed -- the day of
        the retirement -- which is exactly what the line promises it will
        not do. The workflow judges at the tag's own tagger date instead.
        """
        self.assertIn("valid-before", SIGNERS.read_text(encoding="utf-8"))
        self.assertIn("retired, never deleted", SIGNERS.read_text(encoding="utf-8"))
        directory = pathlib.Path(tempfile.mkdtemp(prefix="maelys-retired-"))
        self.addCleanup(shutil.rmtree, directory, True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(directory / "key")],
                       check=True, capture_output=True)
        public = (directory / "key.pub").read_text(encoding="utf-8").strip()
        (directory / "payload").write_text("what the key signed while it was allowed to\n", encoding="utf-8")
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(directory / "key"), "-n", "git",
                        str(directory / "payload")], check=True, capture_output=True)
        retired = directory / "retired"
        retired.write_text(f'signer@example.org namespaces="git",valid-before="20250101" {public}\n',
                           encoding="utf-8")

        def verify(*options: str) -> subprocess.CompletedProcess:
            return subprocess.run(["ssh-keygen", "-Y", "verify", *options, "-f", str(retired),
                                   "-I", "signer@example.org", "-n", "git",
                                   "-s", str(directory / "payload.sig")],
                                  capture_output=True, text=True,
                                  input=(directory / "payload").read_text(encoding="utf-8"))

        # At the clock: the retirement takes the past with it.
        now = verify()
        self.assertNotEqual(now.returncode, 0)
        self.assertIn("expired", now.stdout + now.stderr)
        # At a moment the line allowed, which is what the workflow passes:
        # the replay of an old release still verifies.
        signed = verify("-O", "verify-time=20240601")
        self.assertEqual(signed.returncode, 0, signed.stderr)
        self.assertIn("Good \"git\" signature", signed.stdout + signed.stderr)

    def test_options_never_hide_the_key_and_travel_with_it(self) -> None:
        text = ('# a comment\n'
                '\n'
                'someone@example.org namespaces="git",valid-before="20260901" ssh-ed25519 AAAAOLD comment\n'
                'other@example.org ssh-ed25519 AAAANEW\n')
        self.assertEqual(allowed_signer_keys(text),
                         [("someone@example.org", 'namespaces="git",valid-before="20260901"',
                           "ssh-ed25519", "AAAAOLD"),
                          ("other@example.org", "", "ssh-ed25519", "AAAANEW")])
        self.assertEqual(signer_options('namespaces="git,file",valid-before="20260901"'),
                         {"namespaces": "git,file", "valid-before": "20260901"})
        self.assertEqual(signer_options("cert-authority"), {"cert-authority": ""})
        self.assertEqual(signer_date("20260901"), "20260901000000")
        self.assertEqual(signer_date("202609011230Z"), "20260901123000")
        # The shapes ssh-keygen refuses are refused here too, rather
        # than read as a time it never accepted.
        self.assertEqual(signer_date("20260901T1230Z"), "")
        self.assertEqual(signer_date("2026-09-01"), "")


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

    def test_a_line_ssh_keygen_would_refuse_is_refused_here_too(self) -> None:
        """The four lines that used to answer ok while ssh-keygen refused
        them: a retired key, one not yet valid, a certificate authority, and
        a key allowed in another namespace. A rule held at one end is a rule
        nothing holds between two adoptions -- here, between the push and
        the workflow, which costs a version."""
        for options, said in (('namespaces="git",valid-before="20250101"', "retired this key"),
                              ('valid-after="20300101"', "only after"),
                              ("cert-authority", "certificate authority"),
                              ('namespaces="file"', "a tag is signed in git")):
            with self.subTest(options=options):
                self.named.write_text(f"probe@example.org {options} {self.public}\n", encoding="utf-8")
                found = self.verdicts(str(self.dir / "key.pub"))
                self.assertEqual([status for status, _ in found], ["fail"], found)
                self.assertIn(said, found[0][1])
                self.assertIn("refused after the push", found[0][1])

    def test_every_shape_git_accepts_for_the_key_is_read(self) -> None:
        """A path to the public half, a path to the private one beside it,
        the literal, and git's own key:: form."""
        for declared in (str(self.dir / "key.pub"), str(self.dir / "key"),
                         self.public, "key::" + self.public):
            with self.subTest(declared=declared[:24]):
                self.assertEqual([status for status, _ in self.verdicts(declared)], ["ok"])

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
        # The file is read at the commit of the workflow this job runs,
        # which is the one the product pinned. Not github.job_workflow_sha:
        # GitHub calls that one a documentation bug, it exists as an OIDC
        # claim, and a real run measured it empty -- the pilot's v0.2.0.
        self.assertIn("job.workflow_sha", step)
        self.assertIn("job.workflow_repository", step)
        # The comment keeps the name, because it records why; what must
        # not come back is the expression.
        self.assertNotIn("${{ github.job_workflow", step)
        self.assertIn("contents/share/allowed-signers?ref=${SOCLE_SHA}", step)
        # And the socle that runs must be the one this tag pinned: a replay
        # started from a branch runs another socle, and is refused here
        # rather than by the environment after three builds.
        self.assertIn("release\\.yml@[0-9a-f]{40}", step)
        self.assertIn('"$SOCLE_SHA") ;;', step)
        self.assertIn("replay it with --ref", step)
        # Every way out is a refusal, and none falls back to GitHub's verdict.
        for guard in ('test -n "$SOCLE_SHA"', 'test -n "$SOCLE"', 'test -s "$signers"',
                      'test -s "$RUNNER_TEMP/signature"', 'test -n "$principal"'):
            self.assertIn(guard, step, guard)
        self.assertIn("ssh-keygen -Y find-principals", step)
        self.assertIn("ssh-keygen -Y verify", step)
        self.assertIn("-n git", step)
        # Judged at the moment GitHub saw the tag -- which the signer does
        # not write -- and never at the tagger date, which is inside the
        # payload the signer signs.
        self.assertIn(".verification.verified_at", step)
        self.assertNotIn("awk '/^tagger /", step)
        self.assertIn('test -n "$seen"', step)
        self.assertEqual(step.count('-O "verify-time=${signed_at}"'), 2)
        # jq -j: the payload is signed byte for byte.
        self.assertIn("jq -rj .verification.payload", step)
        self.assertRegex(step, r"set -euo pipefail")


class BackdatedTagTest(unittest.TestCase):
    """Why the moment is read from GitHub and not from the tag.

    The tagger date sits in the payload the signature covers, so it is
    authentic -- and written by whoever signs. A stolen key that backdates
    its tag walks through its own retirement, which is the opposite of what
    a retirement is for. The moment GitHub recorded is neither the clock nor
    the signer's word.
    """

    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is required")
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="maelys-backdated-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.dir / "key")],
                       check=True, capture_output=True)
        public = (self.dir / "key.pub").read_text(encoding="utf-8").strip()
        self.retired = self.dir / "retired"
        # Trusted until the first of September, retired since.
        self.retired.write_text(f'thief@example.invalid namespaces="git",valid-before="20260901000000Z" '
                                f"{public}\n", encoding="utf-8")
        repository = self.dir / "repository"
        repository.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=repository)
        for key, value in (("user.name", "Thief"), ("user.email", "thief@example.invalid"),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "true"),
                           ("gpg.format", "ssh"), ("user.signingkey", str(self.dir / "key.pub"))):
            git("config", key, value, cwd=repository)
        (repository / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        git("add", "-A", cwd=repository)
        git("commit", "-q", "-m", "one", cwd=repository)
        # Signed now, dated back to a day the key was still trusted.
        subprocess.run(["git", "-C", str(repository), "tag", "-a", "-m", "backdated", "v1.0.0"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": "2026-08-01T12:00:00+0000"})
        raw = subprocess.run(["git", "-C", str(repository), "cat-file", "tag", "v1.0.0"],
                             capture_output=True, check=True).stdout
        start = raw.index(b"-----BEGIN SSH SIGNATURE-----")
        (self.dir / "payload").write_bytes(raw[:start])
        (self.dir / "signature").write_bytes(raw[start:])

    def principals(self, at: str) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh-keygen", "-Y", "find-principals", "-O", f"verify-time={at}",
                               "-f", str(self.retired), "-s", str(self.dir / "signature")],
                              capture_output=True, text=True)

    def test_the_tagger_date_lets_a_retired_key_through(self) -> None:
        """What the payload says, and why the workflows stopped reading it."""
        tagger = [line for line in (self.dir / "payload").read_text(encoding="utf-8").splitlines()
                  if line.startswith("tagger ")][0]
        self.assertIn("1785585600", tagger, "the tag claims the first of August")
        self.assertEqual(self.principals("20260801120000Z").returncode, 0,
                         "judged at the date the signer wrote, the retirement is walked through")

    def test_the_moment_github_saw_it_does_not(self) -> None:
        """The same signature, judged at a moment the signer cannot write."""
        refused = self.principals("20260925024021Z")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("expired", refused.stdout + refused.stderr)


class SocleHoldsItselfTest(unittest.TestCase):
    """The socle publishes a formula on its own tags, and holds that
    publication to the rule it holds products to.

    A product's tag is verified by release.yml before anything is built.
    This repository does not run release.yml -- its release is the signed
    tag alone -- so without this the rule would have bound nine products and
    not the one that writes it.
    """

    FORMULA = (ROOT / ".github" / "workflows" / "formula.yml").read_text(encoding="utf-8")

    def test_the_formula_waits_for_the_tag_to_be_verified(self) -> None:
        self.assertIn("  signed:", self.FORMULA)
        self.assertIn("needs: signed", self.FORMULA)
        # The formula job is the one that pushes; it must come after.
        self.assertLess(self.FORMULA.index("  signed:"), self.FORMULA.index("  formula:"))

    def test_the_gate_holds_the_socle_to_its_own_contract(self) -> None:
        step = self.FORMULA.split("The tag is signed by a key this repository names", 1)[1]
        for guard, why in (
                ('test "v$(git show "${TAG}:VERSION")" = "$TAG"', "the tag names the VERSION its commit carries"),
                ('test "$(git cat-file -t "$TAG")" = tag', "annotated, as a release is"),
                ("git merge-base --is-ancestor", "on main, where the branch's rules stand"),
                ('git show "${TAG}:share/allowed-signers"', "the list the tag itself publishes"),
                ("ssh-keygen -Y find-principals", "a key the file names"),
                ("ssh-keygen -Y verify", "and the signature verifies"),
                (".verification.verified_at", "at the moment GitHub saw the tag"),
                ('-O "verify-time=${signed_at}"', "and never at the clock")):
            self.assertIn(guard, step, why)
        self.assertEqual(step.count('-O "verify-time=${signed_at}"'), 2)
        self.assertIn("set -euo pipefail", step)


if __name__ == "__main__":
    unittest.main()
