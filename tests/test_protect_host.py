# SPDX-License-Identifier: MPL-2.0
"""Protection writes and their verification share one stateful host."""
from __future__ import annotations

import copy
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import test_maelys_release as fixtures
from test_maelys_release import (ALL_LEGS, MODULE, FakeHost, FakeProtection,
                                protection_host, using_host, workflow_project)


class ProtectionHost(FakeHost):
    """A branch, its classic protection and its ruleset, changed by what is written to them.

    `view` is what `rules/branches` answers when it is not the ruleset's own
    rules: a list for an answer that differs until the ruleset is written, a
    function of the host for one that always differs in shape. The two are
    separate readings on GitHub, and a host that derived one from the other
    could not tell a command that reads the ruleset from one that trusts the
    rules of the branch. `own` is the state `rulesets/7` answers with, and
    `unreadable` the other paths GitHub does not answer for.
    """

    def __init__(self, *, classic=None, ruleset=None, drift=None, view=None, own="ok", unreadable=()):
        super().__init__()
        self.classic = copy.deepcopy(classic)
        self.ruleset = copy.deepcopy(ruleset)
        self.drift = drift
        self.view, self.own, self.unreadable = copy.deepcopy(view) if isinstance(view, list) else view, own, unreadable
        self.events = []
        self.fallback = protection_host(("absent", None), seen=ALL_LEGS)

    def applied_rules(self):
        return [dict(rule, ruleset_id=self.ruleset["id"], ruleset_source_type="Repository")
                for rule in self.ruleset["rules"]] if self.ruleset else []

    def read(self, path):
        self.reads.append(path)
        self.events.append(("read", path))
        if path in self.unreadable:
            return "unreadable", None
        if path == "repos/o/r/branches/main/protection":
            return ("ok", copy.deepcopy(self.classic)) if self.classic else ("absent", None)
        if path == "repos/o/r/rules/branches/main":
            return "ok", copy.deepcopy(self.view(self) if callable(self.view) else self.applied_rules()
                                       if self.view is None else self.view)
        if path == "repos/o/r/rulesets/7":
            return ("ok", copy.deepcopy(self.ruleset)) if self.own == "ok" else (self.own, None)
        return self.fallback.read(path)

    def write(self, method, endpoint, payload, cwd=None):
        body = json.loads(payload.read_text())
        self.writes.append((method, endpoint, body))
        self.events.append(("write", endpoint))
        if (method, endpoint) == ("PATCH", "repos/o/r/branches/main/protection/required_status_checks"):
            self.classic["required_status_checks"] = copy.deepcopy(body)
        elif (method, endpoint) == ("PUT", "repos/o/r/branches/main/protection"):
            # The whole protection, answered afterwards in GitHub's shape.
            self.classic = {key: ({"enabled": value} if isinstance(value, bool) else copy.deepcopy(value))
                            for key, value in body.items() if value is not None}
        elif (method, endpoint) == ("PUT", "repos/o/r/rulesets/7"):
            self.ruleset.update(copy.deepcopy(body))
            # Once written, the rules of the branch are the ruleset's again.
            self.view = self.view if callable(self.view) else None
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
                before = FakeProtection(["check / check (linux)"], strict=strict).body
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


CHECKS = "repos/o/r/branches/main/protection/required_status_checks"
PROTECTION = "repos/o/r/branches/main/protection"
RULESET = "repos/o/r/rulesets/7"
VIEW = "repos/o/r/rules/branches/main"
RUNS = "repos/o/r/commits/abc/check-runs?per_page=100"
NOTHING = "a check nothing produces"


def bound(names, integration=15368):
    return [{"context": name, "integration_id": integration} for name in names]


def loose(names):
    return [{"context": name} for name in names]


def ruleset_of(checks, *more):
    """The ruleset of LegRenameTest requiring CHECKS, and any further rule after it."""
    held = copy.deepcopy(fixtures.LegRenameTest.RULESET)
    held["rules"][1]["parameters"]["required_status_checks"] = checks
    held["rules"].extend(copy.deepcopy(more))
    return held


def rules_of(ruleset, identifier=7, source="Repository"):
    """A ruleset as `rules/branches` lists it."""
    return [dict(copy.deepcopy(rule), ruleset_id=identifier, ruleset_source_type=source) for rule in ruleset["rules"]]


def unbound(rules):
    """Rules whose checks name their contexts and no integration."""
    rules = copy.deepcopy(rules)
    for rule in rules:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = loose(
                check["context"] for check in rule["parameters"]["required_status_checks"])
    return rules


def requiring(checks, strict=True):
    """One more rule that requires checks, as a ruleset may hold several."""
    return {"type": "required_status_checks",
            "parameters": {"strict_required_status_checks_policy": strict, "do_not_enforce_on_create": False,
                           "required_status_checks": checks}}


@contextmanager
def bare_project():
    """A repository that calls no shared CI: the socle derives no context for it."""
    with tempfile.TemporaryDirectory(prefix="maelys-host-") as directory:
        project = pathlib.Path(directory) / "maelys-fixture"
        (project / ".github" / "workflows").mkdir(parents=True)
        yield project


class OrganisationHost(ProtectionHost):
    """A branch whose checks come from a ruleset no repository can write."""

    def applied_rules(self):
        return [dict(rule, ruleset_source_type="Organization") for rule in super().applied_rules()]


class NothingDiffersTest(unittest.TestCase):
    """A protection that already holds what the write would set is not written again.

    The environment half of `protect` reads, plans, and sends nothing when
    the environment is what it would make it. The protection half sent its
    write on every --apply: a second run reported a write that changed
    nothing, and touched the settings of a repository for no reason. Equal
    is judged member by member of the body that would be sent, and a member
    GitHub did not answer is never an equal one: the write goes as before.

    `writes` is what --apply with the same flags would send, in the plan as
    under --apply: empty where the half refuses, where a reading is missing,
    and where nothing differs -- and `nothing to change` is the last of the
    three alone. A ruleset is compared on its own reading, which only
    --apply takes: its plan names the write and never says `nothing to
    change`.
    """

    CHANGE = "protect: nothing to change\n"
    APPLY = "protect: nothing written; add --apply\n"
    NOTE = ("note     the rules of main list these checks already. Whether the ruleset itself does is read under"
            " --apply, which sends nothing when it does.\n")

    def protect(self, host, *options, text=False, project=workflow_project):
        out, err = io.StringIO(), io.StringIO()
        with project() as project, using_host(host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["protect", str(project), *options,
                                    *(["--format", "text"] if text else ["--format", "json", "--compact"])])
        if text:
            return code, out.getvalue() + err.getvalue()
        result = json.loads(err.getvalue() if code == 1 else out.getvalue())
        self.assertEqual(result["exitCode"], code)
        return code, result.get("data", result)

    def quiet(self, host, ruled=False):
        """--apply finds nothing to write, sends nothing and says so; so does the plan of a classic protection.

        The plan of a ruleset names the write it cannot rule out, and says
        which reading decides.
        """
        held = copy.deepcopy((host.classic, host.ruleset))
        for options in ((), ("--apply",)):
            with self.subTest(options=options):
                written = len(host.writes)
                code, data = self.protect(host, *options)
                self.assertEqual(code, 0, data)
                self.assertEqual(host.writes[written:], [], "a write that changes nothing was sent")
                self.assertEqual((host.classic, host.ruleset), held)
                self.assertEqual((data["written"], data["applied"]), ([], False))
                self.assertNotIn("refused", data)
                _, text = self.protect(host, *options, text=True)
                self.assertNotIn("before   ", text)
                self.assertEqual(host.writes[written:], [])
                if ruled and not options:
                    self.assertEqual((data["writes"], data["nothingToChange"]),
                                     ([{"method": "PUT", "endpoint": RULESET}], False))
                    self.assertTrue(text.endswith(self.APPLY), text)
                    self.assertIn(self.NOTE, text)
                    self.assertTrue(self.NOTE.endswith(data["note"] + "\n"))
                else:
                    self.assertEqual((data["writes"], data["nothingToChange"]), ([], True))
                    self.assertTrue(text.endswith(self.CHANGE), text)
                    self.assertNotIn("under --apply", text)

    def test_a_second_run_sends_nothing_whatever_the_first_one_wrote(self):
        for first, host in ((("PATCH", CHECKS), ProtectionHost(classic=FakeProtection(ALL_LEGS[:1], strict=True).body)),
                            (("PUT", PROTECTION), ProtectionHost()),
                            (("PUT", RULESET), ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET))):
            with self.subTest(write=" ".join(first)):
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"]), (0, True), data)
                self.assertEqual([write[:2] for write in host.writes], [first])
                for run in ("second", "third"):
                    self.quiet(host, ruled=first[1] == RULESET)
                    self.assertEqual([write[:2] for write in host.writes], [first], f"the {run} run wrote again")
                self.assertEqual(data["written"], [" ".join(first)])
                self.assertEqual([(write["method"], write["endpoint"]) for write in data["writes"]], [first])

    def test_the_required_checks_are_compared_in_any_order_and_strict_goes_back_as_it_was_read(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                host = ProtectionHost(classic=FakeProtection(ALL_LEGS[::-1], strict=strict).body)
                self.quiet(host)
                # Nothing was written, so nothing is read back either: one
                # reading of the protection for each of the four runs.
                self.assertEqual(host.reads.count(PROTECTION), 4)

    def test_the_status_checks_are_written_as_before_when_one_member_differs(self):
        for why, held in (("a context is missing", {"strict": True, "contexts": ALL_LEGS[:2]}),
                          ("a context is listed twice", {"strict": False, "contexts": [*ALL_LEGS, ALL_LEGS[0]]}),
                          ("strict was not answered", {"contexts": ALL_LEGS}),
                          ("strict is not a boolean", {"strict": None, "contexts": ALL_LEGS})):
            with self.subTest(why=why):
                classic = dict(FakeProtection([]).body, required_status_checks=held)
                body = {"strict": bool(held.get("strict")), "contexts": ALL_LEGS}
                code, plan = self.protect(ProtectionHost(classic=classic))
                self.assertEqual(plan["writes"], [{"method": "PATCH", "endpoint": CHECKS, "body": body}])
                self.assertFalse(plan["nothingToChange"])
                _, text = self.protect(ProtectionHost(classic=classic), text=True)
                self.assertTrue(text.endswith(self.APPLY), text)
                host = ProtectionHost(classic=classic)
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"]), (0, True), data)
                self.assertEqual(host.writes, [("PATCH", CHECKS, body)])
                self.assertEqual(host.events[-2:], [("write", CHECKS), ("read", PROTECTION)])
                self.assertEqual((data["writes"], data["written"]), (plan["writes"], [f"PATCH {CHECKS}"]))

    def test_the_app_bound_to_a_context_is_neither_compared_nor_sent_for_a_classic_protection(self):
        """GitHub answers `checks` beside `contexts`, each context with the
        app that must report it. The body names contexts alone, and a
        context sent that way may be bound anew: not sending keeps the
        bindings the repository has, whatever they are."""
        for why, apps in (("any source", [None, None, None]), ("one app", [15368] * 3), ("three ways", [15368, 999, None])):
            with self.subTest(why=why):
                checks = [{"context": name, "app_id": app} for name, app in zip(ALL_LEGS, apps)]
                classic = dict(FakeProtection(ALL_LEGS).body)
                classic["required_status_checks"] = dict(classic["required_status_checks"], checks=checks)
                host = ProtectionHost(classic=classic)
                self.quiet(host)
                self.assertEqual(host.classic["required_status_checks"]["checks"], checks)
        # And a write that the names need carries the names, and no binding.
        lacking = dict(FakeProtection(ALL_LEGS[:2]).body)
        lacking["required_status_checks"]["checks"] = [{"context": name, "app_id": 999} for name in ALL_LEGS[:2]]
        host = ProtectionHost(classic=lacking)
        self.protect(host, "--apply")
        self.assertEqual(host.writes, [("PATCH", CHECKS, {"strict": False, "contexts": ALL_LEGS})])

    def test_the_whole_protection_is_put_as_before_since_no_reading_can_equal_it(self):
        """It is sent only when no required checks were read, and it sets
        some: a protection that holds every other setting it carries is
        still not one that holds what it would set."""
        shaped = {key: ({"enabled": value} if isinstance(value, bool) else value)
                  for key, value in MODULE.PROTECTION_SHAPE.items() if value is not None}
        body = {**MODULE.PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": ALL_LEGS}}
        for held in (None, shaped):
            with self.subTest(exists=bool(held)):
                code, plan = self.protect(ProtectionHost(classic=held))
                self.assertEqual(plan["writes"], [{"method": "PUT", "endpoint": PROTECTION, "body": body}])
                host = ProtectionHost(classic=held)
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"]), (0, True), data)
                self.assertEqual(host.writes, [("PUT", PROTECTION, body)])
                self.assertEqual(host.events[-2:], [("write", PROTECTION), ("read", PROTECTION)])
                self.assertEqual(data["written"], [f"PUT {PROTECTION}"])

    # -- a ruleset: compared on its own reading, which only --apply takes ------------------------

    def test_a_ruleset_that_holds_its_checks_in_another_order_is_not_written(self):
        for why, checks in (("each bound to its integration", bound(ALL_LEGS[::-1])),
                            ("none bound to any", loose(ALL_LEGS[::-1]))):
            with self.subTest(why=why):
                host = ProtectionHost(ruleset=ruleset_of(checks))
                self.quiet(host, ruled=True)
                # --apply decides on the ruleset's own reading, which the
                # body would be made from; the plan never takes it.
                self.assertEqual(host.reads.count(RULESET), 2)
                self.assertEqual([event for event in host.events if event == ("read", RULESET)][:1],
                                 [("read", RULESET)])

    def test_a_ruleset_is_written_as_before_when_one_member_differs(self):
        for why, checks, sent in (
                ("a context is missing", bound(ALL_LEGS[:2]), bound(ALL_LEGS)),
                ("a context is listed twice", bound([*ALL_LEGS, ALL_LEGS[0]]), bound(ALL_LEGS)),
                ("a context is bound to no integration, and the write gives it its neighbours'",
                 [*bound(ALL_LEGS[:2]), *loose(ALL_LEGS[2:])], bound(ALL_LEGS)),
                ("an integration read as null is not one left out",
                 [dict(check, integration_id=None) for check in loose(ALL_LEGS)], loose(ALL_LEGS))):
            with self.subTest(why=why):
                held = ruleset_of(checks)
                code, plan = self.protect(ProtectionHost(ruleset=held))
                self.assertEqual(plan["writes"], [{"method": "PUT", "endpoint": RULESET}])
                _, text = self.protect(ProtectionHost(ruleset=held), text=True)
                self.assertTrue(text.endswith(self.APPLY), text)
                self.assertNotIn(self.NOTE, text)
                host = ProtectionHost(ruleset=held)
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"]), (0, True), data)
                body = {key: value for key, value in ruleset_of(sent).items() if key not in ("id", "source_type")}
                self.assertEqual(host.writes, [("PUT", RULESET, body)])
                self.assertEqual(host.events[-2:], [("write", RULESET), ("read", VIEW)])
                self.assertEqual((data["writes"], data["written"]), (plan["writes"], [f"PUT {RULESET}"]))

    def test_a_plan_does_not_read_a_ruleset_and_never_says_what_only_that_reading_can(self):
        """The rules of a branch and the ruleset they come from are two
        readings. A plan that judged on the first ended on `nothing to
        change`, and --apply, which makes the body from the second, wrote."""
        mixed = [*bound(ALL_LEGS[:2]), *loose(ALL_LEGS[2:])]
        for why, state, sends in (
                ("the rules name no integration, the ruleset binds two contexts of three",
                 lambda: ProtectionHost(ruleset=ruleset_of(mixed), view=lambda host: unbound(host.applied_rules())),
                 bound(ALL_LEGS)),
                ("the rules list every leg, the ruleset lacks one",
                 lambda: ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS[:2])),
                                        view=rules_of(ruleset_of(bound(ALL_LEGS)))), bound(ALL_LEGS)),
                ("the rules name no integration, and the ruleset binds every context",
                 lambda: ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS)),
                                        view=lambda host: unbound(host.applied_rules())), None),
                # The write changes nothing in the ruleset, and is sent: only
                # its read-back says whether the branch shows the checks.
                ("the rules lack a leg, the ruleset lists them all",
                 lambda: ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS)),
                                        view=rules_of(ruleset_of(bound(ALL_LEGS[:2])))), bound(ALL_LEGS))):
            with self.subTest(why=why):
                host = state()
                code, plan = self.protect(host)
                self.assertEqual((code, plan["writes"], plan["nothingToChange"]),
                                 (0, [{"method": "PUT", "endpoint": RULESET}], False))
                self.assertNotIn(RULESET, host.reads, "a plan read the ruleset")
                _, text = self.protect(state(), text=True)
                self.assertTrue(text.endswith(self.APPLY), text)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual(code, 0, data)
                if sends:
                    self.assertEqual([write[:2] for write in host.writes], [("PUT", RULESET)])
                    self.assertEqual(host.ruleset["rules"][1]["parameters"]["required_status_checks"], sends)
                    self.assertEqual((data["applied"], data["writes"], data["nothingToChange"]),
                                     (True, plan["writes"], False))
                else:
                    self.assertEqual((host.writes, data["applied"], data["writes"], data["nothingToChange"]),
                                     ([], False, [], True))
                    self.assertTrue(self.protect(state(), "--apply", text=True)[1].endswith(self.CHANGE))
        # And a ruleset GitHub does not answer for is an error of --apply, as it was: nothing is sent.
        state = lambda: ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS)), own="unreadable")
        code, text = self.protect(state(), text=True)
        self.assertEqual(code, 0)
        self.assertTrue(text.endswith(self.APPLY), text)
        host = state()
        code, error = self.protect(host, "--apply")
        self.assertEqual((code, error["error"]["code"], host.writes), (1, "PROCESS_FAILED", []))
        self.assertIn("ruleset 7 of o/r could not be read", error["error"]["message"])

    def test_a_ruleset_that_holds_its_checks_while_the_branch_does_not_show_them_is_written_and_fails(self):
        """The ruleset lists the three legs; the rules of the branch list
        one, or a second ruleset requires checks of its own. The write is
        sent, the read-back does not find the legs in the rules of the
        branch, and the run is an error -- on every run, as it always was.
        Decided on the ruleset's reading alone, nothing was sent and the
        run ended on `nothing to change`, exit 0: a ruleset that does not
        apply its checks to the branch, passed."""
        every = ruleset_of(bound(ALL_LEGS))
        other = rules_of(ruleset_of(loose(["lint"])), identifier=9)
        for why, view in (("the rules of the branch list one leg", lambda host: rules_of(ruleset_of(bound(ALL_LEGS[:1])))),
                          ("a second ruleset requires a check of its own", lambda host: host.applied_rules() + other)):
            with self.subTest(why=why):
                host = ProtectionHost(ruleset=every, view=view)
                for run in (1, 2, 3):
                    code, error = self.protect(host, "--apply", "--allow-narrow")
                    self.assertEqual((code, error["error"]["code"]), (1, "PROCESS_FAILED"), run)
                    self.assertIn("changed beyond its required checks, or they did not read back as written",
                                  error["error"]["message"])
                    self.assertEqual([write[:2] for write in host.writes], [("PUT", RULESET)] * run)
                    self.assertEqual(host.events[-3:], [("read", RULESET), ("write", RULESET), ("read", VIEW)])
                    code, text = self.protect(host, text=True)
                    self.assertTrue(text.endswith(self.APPLY), text)
                    self.assertNotIn(self.NOTE, text)
                self.assertEqual(host.ruleset, every)
        # And where both readings hold, nothing is sent, as nothing was.
        host = ProtectionHost(ruleset=every)
        self.quiet(host, ruled=True)

    def test_every_rule_of_a_ruleset_that_requires_checks_must_list_them(self):
        """One rule holding the checks does not make the ruleset hold
        them: the write sets them in each."""
        held, lacking = requiring(bound(ALL_LEGS)), requiring(bound(ALL_LEGS[:1]))
        checks = bound(ALL_LEGS)
        self.assertTrue(MODULE.protect.checks_held([held, held], checks))
        self.assertFalse(MODULE.protect.checks_held([held, lacking], checks))
        self.assertFalse(MODULE.protect.checks_held([lacking, held], checks))
        self.assertFalse(MODULE.protect.checks_held([{"type": "deletion"}], checks))
        host = ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS), lacking))
        _, text = self.protect(ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS), lacking)), text=True)
        self.assertNotIn(self.NOTE, text)
        self.protect(host, "--apply")
        self.assertEqual([write[:2] for write in host.writes], [("PUT", RULESET)])
        self.assertEqual([rule["parameters"]["required_status_checks"] for rule in host.ruleset["rules"][1:]],
                         [checks, checks])

    def test_a_check_that_is_not_an_object_in_the_rules_of_a_branch_does_not_stop_the_plan(self):
        """`rule_contexts` reads past it and the plan was printed; the
        comparison of the rules read it as an object, and crashed."""
        for odd, named in (("x", False), (7, False), (None, False), (["x"], False),
                           ({"context": ["x"]}, True), ({"context": {"x": 1}}, True)):
            with self.subTest(odd=odd):
                view = rules_of(ruleset_of([*bound(ALL_LEGS), odd]))
                state = lambda: ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS)), view=view)
                code, plan = self.protect(state())
                # An object that names something is read as a required
                # name, as it was: one the plan leaves out, so --apply
                # refuses and nothing is planned.
                self.assertEqual((code, plan["writes"]),
                                 (2, []) if named else (0, [{"method": "PUT", "endpoint": RULESET}]))
                code, text = self.protect(state(), text=True)
                self.assertTrue(text.endswith(self.APPLY), text)
                self.assertNotIn(self.NOTE, text)

    def test_a_ruleset_that_holds_its_checks_answers_for_them_as_one_just_written(self):
        """The exit of a run that had nothing to write is the exit of the
        run that wrote: a leg no pull request has run yet is required by the
        ruleset already, and --apply settled that on the first run."""
        host = ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS)))
        host.fallback = protection_host(("absent", None), seen=ALL_LEGS[:2])
        code, data = self.protect(host, "--apply")
        self.assertEqual(data["missingFromRuns"], ALL_LEGS[2:])
        self.assertEqual((code, host.writes, data["applied"], data["nothingToChange"]), (0, [], False, True))
        # The plan still exits 2 on it, as it did.
        self.assertEqual(self.protect(host)[0], 2)

    # -- a refusal, a missing reading: nothing sent, and never `nothing to change` -------------------

    def test_a_refusal_comes_before_any_comparison(self):
        """What --apply refused it still refuses, in the same words, and
        `writes` is empty in the plan as under --apply: nothing is sent."""
        state = lambda: ProtectionHost(classic=FakeProtection([*ALL_LEGS, NOTHING]).body)
        code, plan = self.protect(state())
        self.assertEqual((plan["writes"], plan["nothingToChange"]), ([], False))
        self.assertTrue(self.protect(state(), text=True)[1].endswith(self.APPLY))
        host = state()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, host.writes, data["applied"]), (2, [], False))
        self.assertIn(f"would stop requiring {NOTHING}", data["refused"]["message"])
        self.assertEqual((data["writes"], data["written"], data["nothingToChange"]), ([], [], False))

    def test_a_reading_that_is_not_there_is_never_nothing_to_change(self):
        """A protection that holds the proposal, and one reading GitHub
        did not give: the plan is not a reading, and says so."""
        for what, path, held in (("the rulesets", VIEW, {"classic": FakeProtection(ALL_LEGS).body}),
                                 ("the check runs of recent pull requests", RUNS,
                                  {"classic": FakeProtection(ALL_LEGS).body}),
                                 ("the check runs of recent pull requests", RUNS,
                                  {"ruleset": ruleset_of(bound(ALL_LEGS))})):
            with self.subTest(unread=what, held=list(held)):
                state = lambda: ProtectionHost(unreadable=(path,), **held)
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["writes"], plan["nothingToChange"]), ([what], [], False))
                _, text = self.protect(state(), text=True)
                self.assertIn(f"UNREAD   GitHub did not answer for {what}", text)
                self.assertTrue(text.endswith(self.APPLY), text)
                self.assertNotIn("under --apply", text)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes, data["nothingToChange"]), (2, [], False))
                self.assertIn("did not answer", data["refused"]["message"])

    # -- a plan never ends on a sentence its --apply contradicts ----------------------------------

    ENDS = {"change": CHANGE, "apply": APPLY, "refused": "protect: refused; the protection is not written\n",
            "applied": "protect: applied\n"}

    def both(self, state, *flags, project=workflow_project):
        """The plan and its --apply, each on a host of its own in the same state, in JSON and in text."""
        runs = {}
        for name, options in (("plan", flags), ("apply", (*flags, "--apply"))):
            host = state()
            code, data = self.protect(host, *options, project=project)
            _, text = self.protect(state(), *options, text=True, project=project)
            ending = next((name for name, line in self.ENDS.items() if text.endswith(line)), "error")
            runs[name] = (code, data, host, ending)
        return runs["plan"], runs["apply"]

    def hold_together(self, state, *flags, project=workflow_project):
        """What a plan says of the write is what its --apply does, with the same flags.

        `writes` is what --apply sends; `nothing to change` is --apply
        sending nothing and refusing nothing. Returns the plan, the result
        of --apply and its host.
        """
        (planned, plan, _, said), (code, applied, host, ending) = self.both(state, *flags, project=project)
        sent = [write[:2] for write in host.writes]
        named = [(write["method"], write["endpoint"]) for write in plan["writes"]]
        self.assertIn(planned, (0, 2), plan)
        self.assertEqual(said, "refused" if "refused" in plan else "change" if plan["nothingToChange"] else "apply")
        if plan["nothingToChange"] or plan["unread"] or "refused" in plan:
            self.assertEqual(plan["writes"], [])
        if "refused" in plan:
            # What a plan refuses, its --apply refuses in the same words.
            self.assertEqual((applied.get("refused"), sent), (plan["refused"], []))
        if code == 1:
            # An error replaces the result: a reading only --apply takes
            # was not given, or a write did not read back. The plan said
            # neither `nothing to change` nor that nothing would be sent.
            self.assertEqual((said, ending, bool(named)), ("apply", "error", True), applied)
            return plan, applied, host
        self.assertEqual([(write["method"], write["endpoint"]) for write in applied["writes"]], sent)
        self.assertEqual(applied["written"], [" ".join(write) for write in sent])
        for write, (_, _, body) in zip(plan["writes"], host.writes):
            self.assertEqual(write.get("body", body), body, "another body than the plan's was sent")
        if "refused" in applied:
            self.assertEqual((named, sent, ending, code, applied["nothingToChange"]), ([], [], "refused", 2, False))
            self.assertIn(said, ("apply", "refused"))
        elif sent:
            self.assertEqual((named, ending, applied["nothingToChange"]), (sent, "applied", False))
        else:
            self.assertEqual((ending, applied["nothingToChange"]), ("change", True), applied)
            # And the plan said so -- or named the write of a ruleset,
            # which only --apply reads.
            self.assertEqual(named, [] if plan["nothingToChange"] else [("PUT", RULESET)])
            self.assertEqual(said, "change" if plan["nothingToChange"] else "apply")
        return plan, applied, host

    def test_a_name_only_the_other_mechanism_requires_is_refused_and_the_plan_does_not_say_otherwise(self):
        """The two mechanisms are read in union: the one --apply writes
        holds the proposal, the other requires a name it leaves out, and
        --apply refuses to narrow. The plan plans no write, and does not
        end on `nothing to change` either."""
        theirs = ruleset_of(loose([NOTHING]))
        for why, state in (
                ("only the ruleset requires it", lambda: ProtectionHost(classic=FakeProtection(ALL_LEGS).body,
                                                                        ruleset=theirs)),
                ("only the classic protection requires it",
                 lambda: ProtectionHost(classic=FakeProtection([NOTHING]).body, ruleset=ruleset_of(bound(ALL_LEGS))))):
            with self.subTest(why=why):
                plan, applied, host = self.hold_together(state)
                self.assertEqual((plan["dropping"], plan["writes"], plan["nothingToChange"]), ([NOTHING], [], False))
                self.assertIn(f"would stop requiring {NOTHING}", applied["refused"]["message"])
                self.assertEqual(host.writes, [])
                _, text = self.protect(state(), text=True)
                self.assertIn(f"DROP     {NOTHING} is required and this plan leaves it out:"
                              " --apply refuses without --allow-narrow", text)
                self.assertTrue(text.endswith(self.APPLY), text)
                # No word of a reading --apply would not get to take.
                self.assertNotIn("under --apply", text)
        code, text = self.protect(OrganisationHost(ruleset=ruleset_of(bound(ALL_LEGS))), text=True)
        self.assertNotIn("under --apply", text)

    def test_asked_to_narrow_a_mechanism_that_holds_the_proposal_sends_nothing_run_after_run(self):
        """--allow-narrow lifts the refusal, and what it lifts it for is
        already so: the mechanism --apply writes holds the proposal. A
        write that changed nothing was sent there on every run."""
        theirs = ruleset_of(loose([NOTHING]))
        for why, host, ruled in (
                ("only the ruleset requires it",
                 ProtectionHost(classic=FakeProtection(ALL_LEGS).body, ruleset=theirs), False),
                ("only the classic protection requires it",
                 ProtectionHost(classic=FakeProtection([NOTHING]).body, ruleset=ruleset_of(bound(ALL_LEGS))), True)):
            with self.subTest(why=why):
                held = copy.deepcopy((host.classic, host.ruleset))
                for run in ("first", "second", "third"):
                    code, data = self.protect(host, "--apply", "--allow-narrow")
                    self.assertEqual((host.writes, data["applied"], data["writes"], data["nothingToChange"]),
                                     ([], False, [], True), run)
                    self.assertNotIn("refused", data)
                    self.assertTrue(self.protect(host, "--apply", "--allow-narrow", text=True)[1].endswith(self.CHANGE))
                self.assertEqual((host.classic, host.ruleset), held)
                code, plan = self.protect(host, "--allow-narrow")
                self.assertEqual(plan["nothingToChange"], not ruled)

    def test_a_leg_no_pull_request_has_run_is_refused_and_no_write_is_planned_for_it(self):
        """The refusal a flag lifts: the write is planned with
        --allow-lock, and with it alone."""
        def state():
            host = ProtectionHost(classic=FakeProtection(ALL_LEGS[:2]).body)
            host.fallback = protection_host(("absent", None), seen=ALL_LEGS[:2])
            return host
        plan, applied, host = self.hold_together(state)
        self.assertEqual((plan["missingFromRuns"], plan["writes"], plan["nothingToChange"]), (ALL_LEGS[2:], [], False))
        self.assertIn("no merged pull request of o/r has run", applied["refused"]["message"])
        plan, applied, host = self.hold_together(state, "--allow-lock")
        self.assertEqual(([write[:2] for write in host.writes], plan["writes"]),
                         ([("PATCH", CHECKS)], applied["writes"]))

    def test_an_empty_proposal_over_an_empty_list_is_refused_and_the_plan_does_not_say_otherwise(self):
        """A protection that requires checks and lists none, on a branch
        nothing was seen to run on: --apply refuses, since a protection
        that requires nothing is not what it writes. The plan plans no
        write, and does not end on `nothing to change` either."""
        def state():
            host = ProtectionHost(classic=FakeProtection([]).body)
            host.fallback = protection_host(("absent", None), seen=[])
            return host
        plan, applied, host = self.hold_together(state, project=bare_project)
        self.assertEqual((plan["proposed"], plan["required"], plan["writes"]), ([], [], []))
        self.assertIn("no check run was seen", applied["refused"]["message"])
        self.assertEqual(host.writes, [])
        self.assertTrue(self.protect(state(), text=True, project=bare_project)[1].endswith(self.APPLY))

    def states(self):
        held, extra = FakeProtection(ALL_LEGS).body, FakeProtection([*ALL_LEGS, NOTHING]).body
        every, some = ruleset_of(bound(ALL_LEGS)), ruleset_of(bound(ALL_LEGS[:2]))
        other = rules_of(ruleset_of(loose(ALL_LEGS[:1])), identifier=9)
        return {
            "a classic protection that holds the proposal": lambda: ProtectionHost(classic=held),
            "one that lacks a leg": lambda: ProtectionHost(classic=FakeProtection(ALL_LEGS[:1]).body),
            "one that requires a check nothing produces": lambda: ProtectionHost(classic=extra),
            "one whose required checks are null": lambda: ProtectionHost(classic=dict(held, required_status_checks=None)),
            "one that holds the proposal, and the rulesets unread":
                lambda: ProtectionHost(classic=held, unreadable=(VIEW,)),
            "one that holds the proposal, and the check runs unread":
                lambda: ProtectionHost(classic=held, unreadable=(RUNS,)),
            "a protection GitHub does not answer for": lambda: ProtectionHost(unreadable=(PROTECTION,)),
            "no protection": ProtectionHost,
            "no protection, beside a ruleset that is not the socle's":
                lambda: ProtectionHost(ruleset=ruleset_of(loose(["lint"]))),
            "a ruleset that holds the proposal": lambda: ProtectionHost(ruleset=every),
            "a ruleset that holds the proposal, and the check runs unread":
                lambda: ProtectionHost(ruleset=every, unreadable=(RUNS,)),
            "one that lacks a leg, and requires a job the call turns off":
                lambda: ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET),
            "one that lists the proposal in its rules and lacks a leg itself":
                lambda: ProtectionHost(ruleset=some, view=rules_of(every)),
            "one that lacks a leg in its rules and holds the proposal itself":
                lambda: ProtectionHost(ruleset=every, view=rules_of(some)),
            "one whose rules name no integration":
                lambda: ProtectionHost(ruleset=ruleset_of([*bound(ALL_LEGS[:2]), *loose(ALL_LEGS[2:])]),
                                       view=lambda host: unbound(host.applied_rules())),
            "one GitHub does not answer for itself": lambda: ProtectionHost(ruleset=every, own="unreadable"),
            "one beside another ruleset that requires checks":
                lambda: ProtectionHost(ruleset=some, view=lambda host: host.applied_rules() + other),
            "a ruleset of the organisation that holds the proposal": lambda: OrganisationHost(ruleset=every),
            "a ruleset of the organisation that lacks a leg": lambda: OrganisationHost(ruleset=some),
            "both mechanisms, each holding the proposal": lambda: ProtectionHost(classic=held, ruleset=every),
            "both, and the classic one requires more": lambda: ProtectionHost(classic=extra, ruleset=every),
            "both, and a ruleset that is not the socle's requires more":
                lambda: ProtectionHost(classic=held, ruleset=ruleset_of(loose([NOTHING])))}

    def test_no_state_ends_a_plan_on_what_its_apply_contradicts(self):
        self.assertEqual(len(self.states()), 22, "a state was named twice, and the second replaced the first")
        for name, state in self.states().items():
            for flags in ((), ("--allow-narrow",), ("--allow-lock",), ("--allow-narrow", "--allow-lock"),
                          ("--without-legs",)):
                for project in (workflow_project, bare_project):
                    with self.subTest(state=name, flags=flags, project=project.__name__):
                        self.hold_together(state, *flags, project=project)

    def test_every_member_of_the_result_is_declared(self):
        declared = next(command for command in MODULE.APP.catalog
                        if command["id"] == "protect")["outputSchema"]["properties"]
        for host, options in ((ProtectionHost(), ()), (ProtectionHost(), ("--apply",)),
                              (ProtectionHost(classic=FakeProtection(ALL_LEGS).body), ("--apply",)),
                              (ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS))), ()),
                              (ProtectionHost(ruleset=ruleset_of(bound(ALL_LEGS))), ("--apply",))):
            _, data = self.protect(host, *options)
            self.assertEqual(sorted(set(data) - set(declared)), [])
            self.assertIs(type(data["nothingToChange"]), bool)
            for write in data["writes"]:
                self.assertEqual(sorted(set(write) - set(declared["writes"]["items"]["properties"])), [])
