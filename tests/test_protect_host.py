# SPDX-License-Identifier: MPL-2.0
"""Protection writes and their verification share one stateful host."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout

import test_maelys_release as fixtures
from test_maelys_release import (ALL_LEGS, MODULE, FakeHost, FakeProtection,
                                protection_host, using_host, workflow_project)


class ProtectionHost(FakeHost):
    def __init__(self, *, classic=None, ruleset=None, drift=None):
        super().__init__()
        self.classic = copy.deepcopy(classic)
        self.ruleset = copy.deepcopy(ruleset)
        self.drift = drift
        self.events = []
        self.fallback = protection_host(("absent", None), seen=ALL_LEGS)

    def applied_rules(self):
        return [dict(rule, ruleset_id=self.ruleset["id"], ruleset_source_type="Repository")
                for rule in self.ruleset["rules"]] if self.ruleset else []

    def read(self, path):
        self.reads.append(path)
        self.events.append(("read", path))
        if path == "repos/o/r/branches/main/protection":
            return ("ok", copy.deepcopy(self.classic)) if self.classic else ("absent", None)
        if path == "repos/o/r/rules/branches/main":
            return "ok", copy.deepcopy(self.applied_rules())
        if path == "repos/o/r/rulesets/7":
            return "ok", copy.deepcopy(self.ruleset)
        return self.fallback.read(path)

    def write(self, method, endpoint, payload, cwd=None):
        body = json.loads(payload.read_text())
        self.writes.append((method, endpoint, body))
        self.events.append(("write", endpoint))
        if (method, endpoint) == ("PATCH", "repos/o/r/branches/main/protection/required_status_checks"):
            self.classic["required_status_checks"] = copy.deepcopy(body)
        elif (method, endpoint) == ("PUT", "repos/o/r/rulesets/7"):
            self.ruleset.update(copy.deepcopy(body))
            if self.drift:
                self.drift(self.ruleset)
        else:
            raise AssertionError(f"undeclared protection write: {method} {endpoint}")
        return subprocess.CompletedProcess([], 0, "", "")


class ProtectHostTest(unittest.TestCase):
    def invoke(self, host):
        out, err = io.StringIO(), io.StringIO()
        # The module and its context were built before this host was installed.
        with workflow_project() as project, using_host(host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["protect", str(project), "--apply", "--format", "json", "--compact"])
        self.assertEqual(out.getvalue() if code else err.getvalue(), "")
        result = json.loads(err.getvalue() if code else out.getvalue())
        self.assertEqual(result["contract"], "agent-cli/v2")
        self.assertEqual(result["exitCode"], code)
        self.assertEqual(len(host.writes), 1)
        return code, result

    def test_classic_apply_preserves_the_full_fixture_except_contexts(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                before = FakeProtection(["check / check (ubuntu-26.04)"], strict=strict).body
                before.update(required_pull_request_reviews={"required_approving_review_count": 2,
                              "dismiss_stale_reviews": True, "require_code_owner_reviews": True,
                              "require_last_push_approval": True},
                              required_signatures={"enabled": True},
                              required_conversation_resolution={"enabled": True},
                              restrictions={"users": [{"login": "fixture-owner"}], "teams": [], "apps": []})
                host = ProtectionHost(classic=before)
                code, result = self.invoke(host)
                self.assertEqual(code, 0, result)
                self.assertTrue(result["data"]["applied"])
                expected = copy.deepcopy(before)
                expected["required_status_checks"]["contexts"] = ALL_LEGS
                self.assertEqual(host.classic, expected)
                self.assertEqual(host.writes[0], ("PATCH",
                    "repos/o/r/branches/main/protection/required_status_checks",
                    {"strict": strict, "contexts": ALL_LEGS}))
                self.assertEqual(host.events[-2:], [
                    ("write", "repos/o/r/branches/main/protection/required_status_checks"),
                    ("read", "repos/o/r/branches/main/protection")])

    def test_ruleset_apply_preserves_the_full_fixture_except_contexts(self):
        before = copy.deepcopy(fixtures.LegRenameTest.RULESET)
        before["bypass_actors"] = [{"actor_id": 42, "actor_type": "Team", "bypass_mode": "pull_request"}]
        host = ProtectionHost(ruleset=before)
        code, result = self.invoke(host)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["applied"])
        expected = copy.deepcopy(before)
        expected["rules"][1]["parameters"]["required_status_checks"] = [
            {"context": name, "integration_id": 15368} for name in [*ALL_LEGS, "check / sanitizers"]]
        self.assertEqual(host.ruleset, expected)
        self.assertEqual(host.writes[0], ("PUT", "repos/o/r/rulesets/7",
                         {key: value for key, value in expected.items() if key not in ("id", "source_type")}))
        self.assertEqual(host.events[-2:], [("write", "repos/o/r/rulesets/7"),
                                            ("read", "repos/o/r/rules/branches/main")])

    def test_ruleset_reread_refuses_drift_in_other_rules_parameters_or_contexts(self):
        def strict_changed(ruleset):
            ruleset["rules"][1]["parameters"]["strict_required_status_checks_policy"] = True
        def rule_removed(ruleset):
            ruleset["rules"].pop(0)
        def contexts_lost(ruleset):
            ruleset["rules"][1]["parameters"]["required_status_checks"] = []
        for drift in (strict_changed, rule_removed, contexts_lost):
            with self.subTest(drift=drift.__name__):
                host = ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET, drift=drift)
                code, result = self.invoke(host)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"]["code"], "PROCESS_FAILED")
                self.assertIn("changed beyond its required checks", result["error"]["message"])
                self.assertEqual(host.events[-1], ("read", "repos/o/r/rules/branches/main"))
