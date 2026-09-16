# SPDX-License-Identifier: MPL-2.0
"""Tap plans, signed retries and refusals use the host installed after import."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_maelys_release import MODULE, FakeHost, using_host
from test_tap_workflow import FORMULA, REPOSITORY, Tap


class TapHost(FakeHost):
    """Only the fixture's bare tap and temporary clones may reach real Git."""
    def __init__(self, tap, *, style_status=0, conflict=False):
        super().__init__()
        self.tap = tap
        self.style_status = style_status
        self.conflict = conflict
        self.clones = []
        self.pushes = []
        self.styles = []
        self.signing_files = []
        self.environment = {**tap.env, "GIT_ALLOW_PROTOCOL": "file"}

    def which(self, name):
        if name != "brew":
            raise AssertionError(f"undeclared executable lookup: {name}")
        return "/fake/brew"

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if env is not None and env != self.environment:
            raise AssertionError("undeclared command environment")
        if command[:2] == ["brew", "style"] and len(command) == 3:
            formula = pathlib.Path(command[2]).resolve()
            if cwd is not None or formula.parent.parent not in self.clones:
                raise AssertionError(f"style outside the clone: {formula}")
            self.styles.append(formula.read_text())
            return subprocess.CompletedProcess(command, self.style_status, "fixture style diagnostic", "")
        if command[:3] == ["git", "clone", "-q"] and len(command) == 5:
            source = pathlib.Path(command[3].removeprefix("file://")).resolve()
            target = pathlib.Path(command[4]).resolve()
            if source != self.tap.bare.resolve() or cwd is not None:
                raise AssertionError(f"clone source is not the fixture: {source}")
            if target.parent.parent != pathlib.Path(tempfile.gettempdir()).resolve() \
                    or not target.parent.name.startswith("maelys-release-tap.") or target.name != "tap":
                raise AssertionError(f"clone destination is not temporary: {target}")
            self.clones.append(target)
        elif command[0] != "git" or pathlib.Path(cwd or "/").resolve() not in self.clones:
            raise AssertionError(f"undeclared command or directory: {command!r}, {cwd}")
        if command[:3] == ["git", "config", "user.signingkey"]:
            key = pathlib.Path(command[3])
            if key.parent != pathlib.Path(cwd).parent:
                raise AssertionError("signing key outside the temporary clone's parent")
            self.signing_files.append((key, stat.S_IMODE(key.stat().st_mode)))
        if command[:2] == ["git", "push"]:
            self.pushes.append(command)
            if self.conflict and len(self.pushes) == 1:
                competitor = self.tap.clone("competitor")
                self.tap.commit_formula(competitor, "maelys-fixture",
                                        "class MaelysFixture < Formula\n  # competing release\nend\n", push=True)
        return MODULE.Host.run(self, command, cwd=cwd, env=self.environment)


class TapHostTest(unittest.TestCase):
    def setUp(self):
        self.tap = Tap()
        self.addCleanup(self.tap.close)
        self.formula = self.tap.work / "maelys-fixture.rb"
        self.formula.write_text(FORMULA)
        self.tap.env.update(TAP_URL=f"file://{self.tap.bare}", TAP_REPOSITORY=REPOSITORY,
                            TAP_TOKEN="", TAP_SIGNING_KEY="", GIT_ALLOW_PROTOCOL="file")
        # The test's identity and signing policy do not inherit the operator's.
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            self.tap.env.pop(name, None)
        self.tap.git(self.tap.work, "config", "--global", "user.name", "Fixture Release")
        self.tap.git(self.tap.work, "config", "--global", "user.email", "release@example.invalid")
        self.tap.git(self.tap.work, "config", "--global", "commit.gpgsign", "false")
        self.initial = self.tap.git(self.tap.bare, "rev-parse", "main")

    def invoke(self, host, *options):
        out, err = io.StringIO(), io.StringIO()
        # The module and its exported handlers already exist when the host is
        # replaced. No function substitution can hide a stale host alias.
        with patch.dict(os.environ, host.environment, clear=True), using_host(host), \
                redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["tap", "maelys-fixture", "v1.2.3", str(self.formula),
                                    "--format", "json", "--compact", *options])
        self.assertEqual(out.getvalue() if code == 1 else err.getvalue(), "")
        envelope = json.loads(err.getvalue() if code == 1 else out.getvalue())
        self.assertEqual(envelope["contract"], "agent-cli/v2")
        self.assertEqual(envelope["exitCode"], code)
        self.assertEqual((host.reads, host.writes), ([], []))
        self.assertTrue(host.clones)
        self.assertTrue(all(not clone.parent.exists() for clone in host.clones))
        return code, envelope

    def test_plan_runs_style_through_the_current_host_without_pushing(self):
        host = TapHost(self.tap)
        code, envelope = self.invoke(host)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["mode"], "plan")
        self.assertTrue(envelope["data"]["changed"])
        self.assertIn("+class MaelysFixture", envelope["data"]["diff"])
        self.assertEqual(host.styles, [FORMULA])
        self.assertEqual(host.pushes, [])
        self.assertEqual(self.tap.git(self.tap.bare, "rev-parse", "main"), self.initial)

    def test_style_refusal_never_stages_or_pushes_the_formula(self):
        host = TapHost(self.tap, style_status=1)
        code, envelope = self.invoke(host, "--apply")
        self.assertEqual(code, 1, envelope)
        self.assertEqual(envelope["error"]["code"], "VALIDATION_FAILED")
        self.assertIn("fixture style diagnostic", envelope["error"]["message"])
        self.assertFalse(any(cmd[:2] in (["git", "add"], ["git", "commit"], ["git", "push"]) for cmd in host.commands))
        self.assertEqual(self.tap.git(self.tap.bare, "rev-parse", "main"), self.initial)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is needed to sign the tap fixture")
    def test_rebased_commit_stays_signed_and_the_next_apply_is_unchanged(self):
        key = self.tap.work / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        self.tap.env["TAP_SIGNING_KEY"] = key.read_text()
        self.tap.race_on_push()
        host = TapHost(self.tap)
        code, envelope = self.invoke(host, "--apply", "--skip-style")
        self.assertEqual(code, 0, envelope)
        data = envelope["data"]
        self.assertEqual(data["pushAttempts"], 2)
        self.assertEqual(data["commit"], self.tap.git(self.tap.bare, "rev-parse", "main"))
        self.assertEqual(data["install"], "brew install maelys-dev/tap/maelys-fixture")
        self.assertEqual(self.tap.subjects(), ["Update maelys-fixture to 1.2.3", "Update other to 0.1.0", "init"])
        self.assertEqual(self.tap.git(self.tap.bare, "show", "main:Formula/maelys-fixture.rb"), FORMULA.strip())
        self.assertEqual(self.tap.git(self.tap.bare, "log", "-1", "--format=%an <%ae>"),
                         "Fixture Release <release@example.invalid>")
        allowed = self.tap.work / "allowed-signers"
        allowed.write_text("release@example.invalid " + key.with_suffix(".pub").read_text())
        self.tap.git(self.tap.bare, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed}",
                     "verify-commit", data["commit"])
        self.assertEqual(len(host.pushes), 2)
        self.assertIn(["git", "fetch", "-q", "origin", "main"], host.commands)
        self.assertIn(["git", "rebase", "-q", "origin/main"], host.commands)
        self.assertEqual(host.styles, [])
        self.assertEqual(len(host.signing_files), 1)
        self.assertEqual(host.signing_files[0][1], 0o600)
        self.assertFalse(host.signing_files[0][0].exists())
        # A new host for the next invocation must also be used dynamically.
        again = TapHost(self.tap)
        code, unchanged = self.invoke(again, "--apply")
        self.assertEqual(code, 0, unchanged)
        self.assertFalse(unchanged["data"]["changed"])
        self.assertEqual(again.pushes, [])
        self.assertEqual(self.tap.git(self.tap.bare, "rev-parse", "main"), data["commit"])

    def test_rejected_pushes_stop_at_three_attempts(self):
        self.tap.reject_pushes()
        host = TapHost(self.tap)
        code, envelope = self.invoke(host, "--apply")
        self.assertEqual(code, 1, envelope)
        self.assertEqual(envelope["error"]["code"], "PROCESS_FAILED")
        self.assertIn("rejected 3 times", envelope["error"]["message"])
        self.assertEqual(len(host.pushes), 3)
        self.assertEqual(host.commands.count(["git", "fetch", "-q", "origin", "main"]), 2)
        self.assertEqual(host.commands.count(["git", "rebase", "-q", "origin/main"]), 2)
        self.assertEqual(self.tap.git(self.tap.bare, "rev-parse", "main"), self.initial)

    def test_conflicting_rebase_is_aborted_without_a_second_push(self):
        host = TapHost(self.tap, conflict=True)
        code, envelope = self.invoke(host, "--apply")
        self.assertEqual(code, 1, envelope)
        self.assertEqual(envelope["error"]["code"], "PROCESS_FAILED")
        self.assertIn("conflicts", envelope["error"]["message"])
        self.assertEqual(len(host.pushes), 1)
        self.assertIn(["git", "rebase", "--abort"], host.commands)
        self.assertEqual(self.tap.subjects(), ["Update maelys-fixture to 0.1.0", "init"])
        self.assertIn("competing release", self.tap.git(self.tap.bare, "show", "main:Formula/maelys-fixture.rb"))
