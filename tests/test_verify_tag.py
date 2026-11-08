# SPDX-License-Identifier: MPL-2.0
"""The tag gate, .github/scripts/verify-tag.sh, run against a real signed tag.

release.yml runs the gate before it builds, and verify-tag.yml runs it for a
product that deploys its own way; both fetch the one file from the socle at
the commit their job runs. What these tests hold is that the script refuses
what release.yml refused before it moved there, with the same words, and
that the line it reads the pin on is the one each caller writes.

Nothing of the shell is mocked but GitHub: a fake `gh` on PATH answers the
API paths the script asks from a table, and `jq`, `git` and `ssh-keygen`
are the real ones, against a tag signed with a generated key.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = ROOT / ".github" / "scripts" / "verify-tag.sh"
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
VERIFY_TAG = (ROOT / ".github" / "workflows" / "verify-tag.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash") or "bash"
SOCLE = "maelys-dev/maelys-release"
SOCLE_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPOSITORY = "maelys-dev/maelys-fixture"

FAKE_GH = f"""#!{sys.executable}
import json, os, subprocess, sys
arguments = sys.argv[1:]
if not arguments or arguments[0] != "api":
    sys.exit("fake gh: only 'gh api' is served")
path = arguments[1]
with open(os.environ["FAKE_GH_TABLE"], encoding="utf-8") as table:
    answers = json.load(table)
if path not in answers:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
body = json.dumps(answers[path])
if "--jq" in arguments:
    expression = arguments[arguments.index("--jq") + 1]
    sys.stdout.write(subprocess.run(["jq", "-r", expression], input=body, capture_output=True,
                                    text=True, check=True).stdout)
else:
    sys.stdout.write(body + "\\n")
"""


def step(workflow: str, name: str) -> str:
    """The text of the step called NAME in WORKFLOW, up to the next step."""
    return workflow.split(f"      - name: {name}\n", 1)[1].split("\n      - ", 1)[0]


def git(directory: pathlib.Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True,
                          check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                                           "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


@unittest.skipUnless(all(shutil.which(tool) for tool in ("jq", "ssh-keygen", "git", "base64")),
                     "the gate needs jq, ssh-keygen, git and base64")
class VerifyTagScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="maelys-verify-tag-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.temp = self.dir / "runner-temp"
        self.temp.mkdir()
        self.output = self.dir / "github-output"
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.key = self.make_key("key")
        self.product = self.dir / "product"
        self.product.mkdir()
        git(self.product, "init", "-q", "-b", "main", ".")
        for key, value in (("user.name", "Signer"), ("user.email", "signer@example.org"),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "true"),
                           ("gpg.format", "ssh"), ("user.signingkey", str(self.key))):
            git(self.product, "config", key, value)
        (self.product / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        workflows = self.product / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "release.yml").write_text(
            f"jobs:\n  release:\n    uses: {SOCLE}/.github/workflows/release.yml@{SOCLE_SHA} # v9.9.9\n",
            encoding="utf-8")
        (workflows / "deploy.yml").write_text(
            f"jobs:\n  tag:\n    uses: {SOCLE}/.github/workflows/verify-tag.yml@{SOCLE_SHA} # v9.9.9\n",
            encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "commit", "-q", "-m", "one")
        git(self.product, "tag", "-a", "-m", "v1.2.3", "v1.2.3")
        self.head = git(self.product, "rev-parse", "HEAD")
        self.tag_object = git(self.product, "rev-parse", "v1.2.3")
        raw = subprocess.run(["git", "-C", str(self.product), "cat-file", "tag", "v1.2.3"],
                             capture_output=True, check=True).stdout
        start = raw.index(b"-----BEGIN SSH SIGNATURE-----")
        self.signers = f'signer@example.org namespaces="git" {(self.dir / "key.pub").read_text().strip()}\n'
        self.table = {
            f"repos/{REPOSITORY}/git/ref/tags/v1.2.3": {"object": {"type": "tag", "sha": self.tag_object}},
            f"repos/{REPOSITORY}/git/tags/{self.tag_object}": {
                "object": {"sha": self.head},
                "verification": {"verified": True, "payload": raw[:start].decode(),
                                 "signature": raw[start:].decode(), "verified_at": "2026-11-08T10:00:00Z"}},
            f"repos/{SOCLE}/contents/share/allowed-signers?ref={SOCLE_SHA}": {
                "content": base64.b64encode(self.signers.encode()).decode()},
            f"repos/{REPOSITORY}/commits/{self.head}": {"commit": {"verification": {"verified": True}}},
            f"repos/{REPOSITORY}": {"default_branch": "main"},
        }

    def make_key(self, name: str) -> pathlib.Path:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.dir / name)],
                       check=True, capture_output=True)
        return self.dir / name

    def gate(self, check: str, **env: str) -> subprocess.CompletedProcess:
        table = self.dir / "table.json"
        table.write_text(json.dumps(self.table), encoding="utf-8")
        base = {"PATH": self.path, "HOME": str(self.dir), "FAKE_GH_TABLE": str(table), "GH_TOKEN": "fake",
                "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.temp), "TAG": "v1.2.3", "SOCLE": SOCLE, "SOCLE_SHA": SOCLE_SHA,
                "PIN_FILE": ".github/workflows/release.yml", "PIN_WORKFLOW": "release.yml",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run([BASH, str(GATE), check], cwd=self.product, capture_output=True, text=True,
                              env={**base, **env})

    def outputs(self) -> dict[str, str]:
        text = self.output.read_text(encoding="utf-8") if self.output.exists() else ""
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def refused(self, done: subprocess.CompletedProcess, words: str) -> None:
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(words, done.stderr)

    # ---- tag -----------------------------------------------------------------

    def test_a_signed_annotated_tag_naming_version_and_checkout_passes(self) -> None:
        done = self.gate("tag")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs(), {"tag": "v1.2.3", "version": "1.2.3", "commit": self.head})

    def test_a_tag_that_is_not_the_version_is_refused(self) -> None:
        (self.product / "VERSION").write_text("1.2.4\n", encoding="utf-8")
        done = self.gate("tag")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.outputs(), {})

    def test_a_lightweight_tag_is_refused_with_the_words_of_release_yml(self) -> None:
        self.table[f"repos/{REPOSITORY}/git/ref/tags/v1.2.3"]["object"]["type"] = "commit"
        self.refused(self.gate("tag"), "v1.2.3 is not an annotated tag")

    def test_a_tag_github_does_not_verify_is_refused(self) -> None:
        self.table[f"repos/{REPOSITORY}/git/tags/{self.tag_object}"]["verification"]["verified"] = False
        self.refused(self.gate("tag"), "v1.2.3 is not verified by GitHub")

    def test_a_tag_naming_another_commit_is_refused(self) -> None:
        self.table[f"repos/{REPOSITORY}/git/tags/{self.tag_object}"]["object"]["sha"] = OTHER_SHA
        self.refused(self.gate("tag"), f"v1.2.3 does not name {self.head}")

    # ---- signer --------------------------------------------------------------

    def test_a_tag_signed_by_a_named_key_at_the_pinned_socle_passes(self) -> None:
        done = self.gate("signer")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"v1.2.3 is signed by signer@example.org, named by {SOCLE} at {SOCLE_SHA}", done.stdout)

    def test_a_release_without_the_pin_is_refused_as_yesterday(self) -> None:
        (self.product / ".github" / "workflows" / "release.yml").write_text("jobs: {}\n", encoding="utf-8")
        self.refused(self.gate("signer"),
                     f"v1.2.3 names no {SOCLE} commit in .github/workflows/release.yml:"
                     " the socle judging it is not the one it pinned")

    def test_a_run_of_another_socle_is_refused_with_the_replay(self) -> None:
        self.refused(self.gate("signer", SOCLE_SHA=OTHER_SHA),
                     f"this run executes {SOCLE} at {OTHER_SHA} but v1.2.3 pinned {SOCLE_SHA} :"
                     " replay it with --ref v1.2.3")

    def test_a_key_the_socle_does_not_name_is_refused(self) -> None:
        other = self.make_key("other")
        self.table[f"repos/{SOCLE}/contents/share/allowed-signers?ref={SOCLE_SHA}"]["content"] = base64.b64encode(
            f'other@example.org namespaces="git" {other.with_suffix(".pub").read_text().strip()}\n'.encode()).decode()
        done = self.gate("signer")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(f"v1.2.3 is signed by a key {SOCLE}", done.stderr)

    def test_no_allowed_signers_at_the_pinned_socle_is_refused(self) -> None:
        del self.table[f"repos/{SOCLE}/contents/share/allowed-signers?ref={SOCLE_SHA}"]
        self.refused(self.gate("signer"),
                     f"{SOCLE} at {SOCLE_SHA} has no share/allowed-signers, or this token cannot read it")

    def test_an_unknown_socle_is_refused_before_anything_is_read(self) -> None:
        self.refused(self.gate("signer", SOCLE_SHA=""), "the socle judging v1.2.3 is unknown: job.workflow_sha is empty")
        self.refused(self.gate("signer", SOCLE_SHA="main"), "the socle judging v1.2.3 is not a commit: main")
        self.refused(self.gate("signer", SOCLE=""), "the socle judging v1.2.3 is unknown: job.workflow_repository is empty")

    def test_a_tag_without_verified_at_is_refused(self) -> None:
        del self.table[f"repos/{REPOSITORY}/git/tags/{self.tag_object}"]["verification"]["verified_at"]
        self.refused(self.gate("signer"), "v1.2.3 carries no verified_at")

    def test_the_gate_reads_its_own_pin_in_the_caller(self) -> None:
        """verify-tag.yml: the line is the caller's `uses:` of the gate."""
        done = self.gate("signer", PIN_FILE=".github/workflows/deploy.yml", PIN_WORKFLOW="verify-tag.yml")
        self.assertEqual(done.returncode, 0, done.stderr)
        # A pin of release.yml in the caller is not a pin of the gate.
        self.refused(self.gate("signer", PIN_FILE=".github/workflows/release.yml", PIN_WORKFLOW="verify-tag.yml"),
                     f"v1.2.3 names no {SOCLE} commit in .github/workflows/release.yml")

    def test_the_workflow_named_must_be_a_file_name(self) -> None:
        for name in ("", "release.yml|.*", "../x.yml"):
            self.refused(self.gate("signer", PIN_WORKFLOW=name), "the gate was given no socle workflow")

    # ---- commit --------------------------------------------------------------

    def test_an_unsigned_commit_is_refused(self) -> None:
        self.table[f"repos/{REPOSITORY}/commits/{self.head}"]["commit"]["verification"]["verified"] = False
        self.refused(self.gate("commit", MODE="signed"), f"the commit {self.head} is not signed and verified by GitHub")
        self.assertEqual(self.gate("commit", MODE="signed").returncode, 1)

    def test_a_commit_that_never_landed_on_the_default_branch_is_refused(self) -> None:
        origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(self.product, "switch", "-q", "-c", "side")
        (self.product / "side").write_text("x\n", encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "commit", "-q", "-m", "side")
        landed = git(self.product, "rev-parse", "HEAD")
        git(self.product, "remote", "add", "origin", str(origin))
        git(self.product, "push", "-q", "origin", "main")
        self.table[f"repos/{REPOSITORY}/commits/{landed}"] = {"commit": {"verification": {"verified": True}}}
        self.refused(self.gate("commit", MODE="signed-on-default-branch"),
                     f"the commit {landed} is not an ancestor of main: it never landed there")
        git(self.product, "switch", "-q", "main")
        done = self.gate("commit", MODE="signed-on-default-branch")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_an_unknown_mode_is_refused(self) -> None:
        self.refused(self.gate("commit", MODE="always"),
                     "commit_verification must be none, signed or signed-on-default-branch, not always")

    # ---- caller --------------------------------------------------------------

    def test_the_caller_is_a_workflow_of_this_repository(self) -> None:
        done = self.gate("caller", WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/deploy.yml@refs/tags/v1.2.3")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs(), {"file": ".github/workflows/deploy.yml"})
        for ref, words in (("", "github.workflow_ref is empty"),
                           ("someone/else/.github/workflows/deploy.yml@refs/heads/main", "is not one of"),
                           (f"{REPOSITORY}/deploy.yml@refs/heads/main", "is not under .github/workflows"),
                           (f"{REPOSITORY}/.github/workflows/../../x.yml@refs/heads/main", "is not a plain path")):
            self.refused(self.gate("caller", WORKFLOW_REF=ref), words)

    def test_an_unknown_check_is_refused(self) -> None:
        self.refused(self.gate("everything"), "usage: verify-tag.sh tag|signer|commit|caller")


class GateWiringTest(unittest.TestCase):
    """Where each workflow runs the gate, and what it gives it."""

    def test_both_workflows_fetch_the_gate_with_the_same_step(self) -> None:
        fetch = step(RELEASE, "Fetch the tag gate of the socle")
        self.assertEqual(fetch, step(VERIFY_TAG, "Fetch the tag gate of the socle"))
        # At the commit this job runs, checked before anything is read at it.
        self.assertIn("SOCLE_SHA: ${{ job.workflow_sha }}", fetch)
        self.assertIn("contents/.github/scripts/verify-tag.sh?ref=${SOCLE_SHA}", fetch)
        self.assertLess(fetch.index('test "${#SOCLE_SHA}" = 40'), fetch.index("gh api"))
        self.assertIn('test -s "$RUNNER_TEMP/verify-tag.sh"', fetch)

    def test_each_check_runs_the_gate_after_it_is_fetched(self) -> None:
        for name, workflow, checks in (("release.yml", RELEASE, ("tag", "signer", "commit")),
                                       ("verify-tag.yml", VERIFY_TAG, ("caller", "tag", "signer", "commit"))):
            verify = workflow.split("\n  verify:\n", 1)[1].split("\n  build:\n", 1)[0]
            ran = [line.strip() for line in verify.splitlines() if 'bash "$RUNNER_TEMP/verify-tag.sh"' in line]
            self.assertEqual(ran, [f'run: bash "$RUNNER_TEMP/verify-tag.sh" {check}' for check in checks], name)
            self.assertLess(verify.index("- name: Fetch the tag gate of the socle"),
                            verify.index('bash "$RUNNER_TEMP/verify-tag.sh"'), name)
            # The words of a refusal live in the gate, not in either workflow.
            self.assertNotIn("is not an annotated tag", workflow, name)
            self.assertNotIn("ssh-keygen -Y", workflow, name)

    def test_each_workflow_names_the_line_its_pin_is_on(self) -> None:
        release = step(RELEASE, "Verify the tag was signed by a key the fleet names")
        self.assertIn("PIN_FILE: .github/workflows/release.yml", release)
        self.assertIn("PIN_WORKFLOW: release.yml", release)
        gate = step(VERIFY_TAG, "Verify the tag was signed by a key the fleet names")
        self.assertIn("PIN_FILE: ${{ steps.caller.outputs.file }}", gate)
        self.assertIn("PIN_WORKFLOW: verify-tag.yml", gate)
        self.assertIn("WORKFLOW_REF: ${{ github.workflow_ref }}", step(VERIFY_TAG, "Name the workflow that calls this gate"))

    def test_the_gate_a_product_calls_only_reads(self) -> None:
        on = VERIFY_TAG.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual([line.strip() for line in on.splitlines() if line.startswith("  ") and not line.startswith("   ")],
                         ["workflow_call:"])
        permissions = [line.strip() for line in VERIFY_TAG.splitlines()
                       if line.strip().endswith((": read", ": write")) and not line.lstrip().startswith("#")]
        self.assertEqual(permissions, ["contents: read", "contents: read"])
        self.assertNotIn("secrets", VERIFY_TAG.split("\njobs:\n", 1)[1])
        for output in ("tag", "version", "commit"):
            self.assertIn(f"value: ${{{{ jobs.verify.outputs.{output} }}}}", VERIFY_TAG)
        # The same checkout as release.yml's verify, pinned by commit.
        checkout = [line.strip() for line in RELEASE.splitlines() if "actions/checkout@" in line][0]
        self.assertIn(checkout, VERIFY_TAG)


if __name__ == "__main__":
    unittest.main()
