# SPDX-License-Identifier: MPL-2.0
"""The two release stops see the host installed after context construction."""
from __future__ import annotations

import io
import json
import pathlib
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout

import test_maelys_release as fixtures

MODULE = fixtures.MODULE


class CutHost(fixtures.FakeHost):
    """Real local Git/signatures; declared GitHub responses, no network fallback."""
    def __init__(self, product):
        super().__init__()
        self.product = product
        self.environment = {**product.env, "GIT_ALLOW_PROTOCOL": "file"}
        self.pull = None
        self.conclusion = "success"
        self.fail_verification = False
        self.events = []
        self.body = ""

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        self.events.append(("run", command))
        # The test checkout may be dirty. Only these three identity reads
        # are recorded; no command is executed in the real socle checkout.
        if cwd == MODULE.socle_root() and env is None:
            identity = {
                ("git", "status", "--porcelain", "--", "share", "bin", "VERSION"): "",
                ("git", "rev-parse", "HEAD"): "f" * 40,
                ("git", "describe", "--tags", "--exact-match"): "v9.9.9",
            }
            if tuple(command) not in identity:
                raise AssertionError(f"undeclared socle identity read: {command!r}")
            return subprocess.CompletedProcess(command, 0, identity[tuple(command)], "")
        if pathlib.Path(cwd or "/").resolve() != self.product.dir.resolve() \
                or (env is not None and env != self.environment):
            raise AssertionError(f"command outside fixture: {command!r}, {cwd}")
        if command == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/o/r.git\n", "")
        if command[:3] == ["git", "tag", "-v"] and self.fail_verification:
            return subprocess.CompletedProcess(command, 1, "", "fixture rejects signature")
        if command[0] == "git":
            # Real origin is the fixture's bare directory. Every transport
            # except file is disabled, including subcommands not listed here.
            return MODULE.Host.run(self, command, cwd=cwd, env=self.environment)
        if command[:3] == ["gh", "pr", "create"]:
            if self.pull is not None:
                raise AssertionError("a second pull request was opened")
            self.body = pathlib.Path(command[command.index("--body-file") + 1]).read_text()
            self.pull = {"number": 7, "url": "https://example.invalid/pull/7", "state": "OPEN",
                         "headRefOid": self.product.git(self.product.dir, "rev-parse", "HEAD")}
            return subprocess.CompletedProcess(command, 0, self.pull["url"], "")
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps([self.pull] if self.pull else []), "")
        if command[:3] == ["gh", "pr", "view"] and self.pull is not None:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.pull), "")
        raise AssertionError(f"undeclared command: {command!r}")

    def read(self, path):
        self.reads.append(path)
        self.events.append(("read", path))
        if path == "repos/o/r":
            return "ok", {"default_branch": "main"}
        heads = [self.pull["headRefOid"]] if self.pull else []
        if self.pull and "mergeCommit" in self.pull:
            heads.append(self.pull["mergeCommit"]["oid"])
        if path in [f"repos/o/r/commits/{sha}/check-runs?per_page=100&page=1" for sha in heads]:
            runs = ([{"name": "check", "status": "completed", "conclusion": self.conclusion}]
                    if self.conclusion else [])
            return "ok", {"total_count": len(runs), "check_runs": runs}
        if path == "repos/o/r/git/ref/tags/v1.3.0":
            return "ok", {"object": {"sha": self.product.git(self.product.dir, "rev-parse", "v1.3.0")}}
        if path.startswith("repos/o/r/git/tags/"):
            expected = self.product.git(self.product.dir, "rev-parse", "v1.3.0")
            if path != f"repos/o/r/git/tags/{expected}":
                raise AssertionError(f"wrong tag object: {path}")
            return "ok", {"verification": {"verified": True}}
        if path == "repos/o/r/actions/runs?event=push&per_page=20":
            return "ok", {"workflow_runs": []}
        if path in [f"repos/o/r/actions/runs?head_sha={sha}&per_page=100" for sha in heads]:
            return "ok", {"workflow_runs": []}
        raise AssertionError(f"undeclared GitHub read: {path}")


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is needed to sign the release fixture")
class CutHostTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CutTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.product, self.dir = self.fixture.product, self.fixture.dir
        self.assertTrue(self.fixture.signing_key())
        allowed = self.product.work / "allowed-signers"
        allowed.write_text("test@example.invalid " + (self.product.work / "signing-key.pub").read_text())
        self.product.git(self.dir, "config", "gpg.ssh.allowedSignersFile", str(allowed))
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n"
                           "## 1.2.3 — 2026-09-03\n\n- Previous.\n")
        # Cut also releases repositories with their own publication workflow.
        # Preflight's shared repository gate is covered by the API golden.
        (self.dir / ".github/workflows/release.yml").unlink()
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-qm", "prepare next entry")
        self.product.git(self.dir, "push", "-q", "origin", "main")
        self.host = CutHost(self.product)

    def invoke(self, *options):
        out, err = io.StringIO(), io.StringIO()
        # No captured host: both the loaded entry point and its context predate
        # this replacement, and the same host sees writes and verification reads.
        with fixtures.using_host(self.host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["cut", str(self.dir), "1.3.0", "--mechanism", "custom",
                                    "--format", "json", "--compact", "--poll", "5",
                                    "--timeout", "1", *options])
        self.assertEqual(self.host.writes, [])
        stream = err.getvalue().splitlines()[-1] if code == 1 else out.getvalue()
        envelope = json.loads(stream)
        self.assertEqual(envelope["contract"], "agent-cli/v2")
        self.assertEqual(envelope["exitCode"], code)
        return code, envelope

    def open_and_merge(self):
        code, envelope = self.invoke("--apply")
        self.assertEqual(code, 0, envelope)
        commit = envelope["data"]["commit"]
        self.assertEqual(self.product.git(self.fixture.origin, "rev-parse", "release/v1.3.0"), commit)
        self.assertIn("BEGIN SSH SIGNATURE", self.product.git(self.dir, "cat-file", "-p", commit))
        self.assertIn("## 1.3.0 — 2026-09-03", self.host.body)
        before = len([cmd for cmd in self.host.commands if cmd[:2] == ["git", "push"]])
        code, resumed = self.invoke()
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["data"]["commit"], commit)
        self.assertEqual(len([cmd for cmd in self.host.commands if cmd[:2] == ["git", "push"]]), before)
        self.assertEqual(len([cmd for cmd in self.host.commands if cmd[:3] == ["gh", "pr", "create"]]), 1)
        # Only the fixture merges: cut never does. Advance main afterwards to
        # prove it tags the checked merge, not the later head of the branch.
        self.product.git(self.dir, "switch", "-q", "main")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "release/v1.3.0", "-m", "merge release")
        merge = self.product.git(self.dir, "rev-parse", "HEAD")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "later main")
        self.product.git(self.dir, "push", "-q", "origin", "main")
        self.host.pull.update(state="MERGED", mergeCommit={"oid": merge})
        self.host.events.clear()
        return merge

    def assert_no_tag(self):
        self.assertEqual(self.product.git(self.dir, "tag", "--list", "v1.3.0"), "")
        self.assertEqual(self.product.git(self.fixture.origin, "tag", "--list", "v1.3.0"), "")
        self.assertFalse(any(cmd[-1] == "refs/tags/v1.3.0" for cmd in self.host.commands if cmd[:2] == ["git", "push"]))

    def test_open_resume_and_tag_the_checked_merge_even_after_main_advances(self):
        merge = self.open_and_merge()
        code, planned = self.invoke("--tag")
        self.assertEqual(code, 0, planned)
        self.assertTrue(planned["data"]["sameTree"])
        self.assert_no_tag()
        self.host.events.clear()
        code, tagged = self.invoke("--tag", "--apply")
        self.assertEqual(code, 0, tagged)
        self.assertTrue(tagged["data"]["pushed"])
        self.assertTrue(tagged["data"]["verified"])
        self.assertEqual(tagged["data"]["commit"], merge)
        self.assertEqual(self.product.git(self.fixture.origin, "rev-parse", "v1.3.0^{}"), merge)
        self.assertNotEqual(self.product.git(self.fixture.origin, "rev-parse", "main"), merge)
        checked = self.host.events.index(("read", f"repos/o/r/commits/{merge}/check-runs?per_page=100&page=1"))
        signed = next(i for i, (kind, value) in enumerate(self.host.events) if kind == "run" and value[:3] == ["git", "tag", "-s"])
        verified = self.host.events.index(("run", ["git", "tag", "-v", "v1.3.0"]))
        pushed = self.host.events.index(("run", ["git", "push", "-q", "origin", "refs/tags/v1.3.0"]))
        reread = self.host.events.index(("read", "repos/o/r/git/ref/tags/v1.3.0"))
        self.assertLess(checked, signed)
        self.assertLess(signed, verified)
        self.assertLess(verified, pushed)
        self.assertLess(pushed, reread)

    def test_red_or_missing_checks_never_create_a_tag(self):
        self.open_and_merge()
        self.host.conclusion = "failure"
        code, envelope = self.invoke("--tag", "--apply")
        self.assertEqual(code, 2, envelope)
        self.assertFalse(envelope["data"]["ready"])
        self.assert_no_tag()
        # Exercise the same tag implementation with a zero deadline; the CLI
        # deliberately requires at least a minute, unnecessary for this test.
        self.host.conclusion = ""
        with fixtures.using_host(self.host), self.assertRaises(MODULE.Failure) as raised:
            MODULE.cut_tag(fixtures.CutTest.Stub(**{"--apply": True}), self.fixture.declarations(),
                           envelope["data"], self.fixture.log, 0, 0, self.fixture.product.signers)
        self.assertEqual(raised.exception.code, "PRECONDITION_FAILED")
        self.assertIn("No check registered", raised.exception.message)
        self.assert_no_tag()

    def test_failed_signature_verification_removes_only_the_unpushed_local_tag(self):
        self.open_and_merge()
        self.host.fail_verification = True
        code, envelope = self.invoke("--tag", "--apply")
        self.assertEqual(code, 1, envelope)
        self.assertEqual(envelope["error"]["code"], "PROCESS_FAILED")
        self.assertIn("fixture rejects signature", envelope["error"]["message"])
        self.assertIn(["git", "tag", "-d", "v1.3.0"], self.host.commands)
        self.assert_no_tag()
