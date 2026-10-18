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
from test_maelys_release import (ALL_LEGS, MODULE, PACKAGE, UNPROTECTED, FakeHost, FakeProtection,
                                answered_404, protection_host, using_host, workflow_project)


class ProtectionHost(FakeHost):
    """A branch, its classic protection and its ruleset, changed by what is written to them.

    `view` is what `rules/branches` answers when it is not the ruleset's own
    rules: a list for an answer that differs until the ruleset is written, a
    function of the host for one that always differs in shape. The two are
    separate readings on GitHub, and a host that derived one from the other
    could not tell a command that reads the ruleset from one that trusts the
    rules of the branch. `own` is the state `rulesets/7` answers with,
    `unreadable` the other paths GitHub does not answer for, and `answers`
    what it answers on a path instead of what this host holds.
    """

    def __init__(self, *, classic=None, ruleset=None, drift=None, view=None, own="ok", unreadable=(), answers=None):
        super().__init__()
        self.answers = answers or {}
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
        if path in self.answers:
            return copy.deepcopy(self.answers[path])
        if path == "repos/o/r/branches/main/protection":
            return ("ok", copy.deepcopy(self.classic)) if self.classic else UNPROTECTED
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
        """It is sent only where nothing protects the branch, and it
        creates a protection: there is no reading for it to equal."""
        body = {**PACKAGE.protect.PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": ALL_LEGS}}
        code, plan = self.protect(ProtectionHost())
        self.assertEqual(plan["writes"], [{"method": "PUT", "endpoint": PROTECTION, "body": body}])
        host = ProtectionHost()
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

    def test_a_check_bound_to_no_app_stays_bound_to_none(self):
        """A check a ruleset requires from any source keeps it so, written or not.

        The write gave it the app its neighbours were bound to, and every pull
        request then waited for a report that app never sends, where another
        app -- or the status API -- reported it. A context the ruleset does
        not carry yet still takes the app its neighbours carry most often.
        """
        mixed = [*bound(ALL_LEGS[:2]), *loose(ALL_LEGS[2:])]
        self.assertEqual(PACKAGE.protect.ruleset_checks(rules_of(ruleset_of(mixed)), ALL_LEGS), mixed)
        host = ProtectionHost(ruleset=ruleset_of(mixed))
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["written"], data["nothingToChange"]), (0, [], True))
        # One leg missing: it is added bound as its neighbours are, and the
        # check bound to none stays bound to none.
        short = [*bound(ALL_LEGS[:1]), *loose(ALL_LEGS[2:])]
        self.assertEqual(PACKAGE.protect.ruleset_checks(rules_of(ruleset_of(short)), ALL_LEGS), mixed)

    def test_a_ruleset_is_written_as_before_when_one_member_differs(self):
        for why, checks, sent in (
                ("a context is missing", bound(ALL_LEGS[:2]), bound(ALL_LEGS)),
                ("a context is listed twice", bound([*ALL_LEGS, ALL_LEGS[0]]), bound(ALL_LEGS)),
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
                # The third check is bound to no app, any source may report
                # it, and the write keeps it so: the ruleset holds the checks.
                ("the rules name no integration, the ruleset binds two contexts of three",
                 lambda: ProtectionHost(ruleset=ruleset_of(mixed), view=lambda host: unbound(host.applied_rules())),
                 None),
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
        """The ruleset lists the three legs, and the rules of the branch
        that come from it list one. The write is sent, the read-back does
        not find the legs in the rules of the branch, and the run is an
        error -- on every run, as it always was. Decided on the ruleset's
        reading alone, nothing was sent and the run ended on `nothing to
        change`, exit 0: a ruleset that does not apply its checks to the
        branch, passed. Its own rules, and not those of a second ruleset
        that requires a check of its own: SeveralRulesetsTest holds that
        state, where the write is correct and reads back."""
        every = ruleset_of(bound(ALL_LEGS))
        other = rules_of(ruleset_of(loose(["lint"])), identifier=9)
        for why, view in (("the rules of the branch list one leg", lambda host: rules_of(ruleset_of(bound(ALL_LEGS[:1])))),
                          ("and a second ruleset requires a check of its own",
                           lambda host: rules_of(ruleset_of(bound(ALL_LEGS[:1]))) + other)):
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
        self.assertTrue(PACKAGE.protect.checks_held([held, held], checks))
        self.assertFalse(PACKAGE.protect.checks_held([held, lacking], checks))
        self.assertFalse(PACKAGE.protect.checks_held([lacking, held], checks))
        self.assertFalse(PACKAGE.protect.checks_held([{"type": "deletion"}], checks))
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
        other, theirs = rules_of(ruleset_of(loose(ALL_LEGS[:1])), identifier=9), ruleset_of(loose(["lint"]))
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
            "a 404 that does not say the branch is unprotected":
                lambda: ProtectionHost(answers={PROTECTION: answered_404("Not Found")}),
            "no protection": ProtectionHost,
            "no protection, and a 404 for the rules of the branch":
                lambda: ProtectionHost(answers={VIEW: answered_404("Not Found")}),
            "no protection, beside a ruleset that is not the socle's":
                lambda: ProtectionHost(ruleset=ruleset_of(loose(["lint"]))),
            "no protection, beside a ruleset that requires no check":
                lambda: ProtectionHost(ruleset=dict(every, rules=every["rules"][:1])),
            "a protection that requires no check": lambda: ProtectionHost(classic=WholeProtectionTest.HELD),
            "a protection that requires no check, beside a ruleset that is not the socle's":
                lambda: ProtectionHost(classic=WholeProtectionTest.HELD, ruleset=ruleset_of(loose(["lint"]))),
            "an answer that is no protection": lambda: ProtectionHost(answers={PROTECTION: ("ok", {})}),
            "a default branch GitHub does not name": lambda: ProtectionHost(answers={"repos/o/r": ("unreadable", None)}),
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
            "one beside another ruleset that requires a check of the socle":
                lambda: ProtectionHost(ruleset=some, view=lambda host: host.applied_rules() + other),
            "one that lacks a leg, after a ruleset that requires a check of its own":
                lambda: ProtectionHost(ruleset=some, view=lambda host: rules_of(theirs, identifier=3)
                                       + host.applied_rules()),
            "a ruleset of the organisation that holds the proposal": lambda: OrganisationHost(ruleset=every),
            "a ruleset of the organisation that lacks a leg": lambda: OrganisationHost(ruleset=some),
            "both mechanisms, each holding the proposal": lambda: ProtectionHost(classic=held, ruleset=every),
            "both, and the classic one requires more": lambda: ProtectionHost(classic=extra, ruleset=every),
            "both, and a ruleset that is not the socle's requires more":
                lambda: ProtectionHost(classic=held, ruleset=ruleset_of(loose([NOTHING])))}

    def test_no_state_ends_a_plan_on_what_its_apply_contradicts(self):
        self.assertEqual(len(self.states()), 30, "a state was named twice, and the second replaced the first")
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


class WholeProtectionTest(unittest.TestCase):
    """The whole protection is put only where nothing protects the branch.

    Reviews, linear history, the rule for administrators: set by the
    repository, with required status checks never turned on. The plan
    printed `keeps every other setting as it is`, and --apply then put the
    whole protection with the socle's shape over it: every one of those
    settings replaced, and a read-back that passed because it compared with
    the body just sent. Beside a ruleset that requires none of the socle's
    checks it created a classic protection, under a plan that printed no
    `create` line and said --apply wrote the ruleset. And where GitHub did
    not name the default branch, it created one on `main`.
    """

    HELD = {"required_pull_request_reviews": {"required_approving_review_count": 2, "dismiss_stale_reviews": True,
                                              "require_code_owner_reviews": True,
                                              "require_last_push_approval": True},
            "required_linear_history": {"enabled": True}, "required_signatures": {"enabled": True},
            "enforce_admins": {"enabled": False}, "required_conversation_resolution": {"enabled": False},
            "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}}
    UNARMED = {"message": "the protection of main of o/r requires no check yet.",
               "hint": "This command changes the required checks of a protection that already requires some, and"
                       " creates a protection where none exists; it does not turn required status checks on in a"
                       " protection that exists, and never replaces one. Turning that setting on is the"
                       " repository's own to do; then protect --apply."}
    BESIDE = {"message": "main of o/r is protected by a ruleset that requires none of the socle's checks.",
              "hint": "This command does not create a classic protection beside a ruleset, and does not choose"
                      " which ruleset should require them. Requiring one of them in a ruleset is its owner's"
                      " setting to change; protect --apply then writes the checks of that ruleset and of no"
                      " other: it writes a ruleset of the repository, refuses one of the organisation, and"
                      " refuses where more than one ruleset requires some of them."}
    REFUSED = "protect: refused; the protection is not written\n"
    FLAGS = ((), ("--allow-narrow",), ("--allow-lock",), ("--allow-narrow", "--allow-lock"))
    WRITES_IT = " — --apply writes this ruleset, never a classic protection beside it\n"
    protect = NothingDiffersTest.protect

    def refused(self, state, refusal, *flags):
        """The plan and --apply refuse in the same words, exit 2, and nothing of the protection is written."""
        for options in (flags, (*flags, "--apply")):
            with self.subTest(options=options):
                host = state()
                before = copy.deepcopy((host.classic, host.ruleset))
                code, data = self.protect(host, *options)
                self.assertEqual((code, data["refused"]), (2, refusal), data)
                self.assertEqual((data["writes"], data["written"], data["applied"], data["nothingToChange"]),
                                 ([], [], False, False))
                self.assertEqual((host.writes, (host.classic, host.ruleset)), ([], before))
                self.assertNotIn("creates", data)
                code, text = self.protect(state(), *options, text=True)
                self.assertEqual(code, 2)
                self.assertIn(f"REFUSED  {refusal['message']} {refusal['hint']}\n", text)
                self.assertTrue(text.endswith(self.REFUSED), text)
                self.assertNotIn("create   ", text)
                # No command that changes a setting is printed.
                for words in ("gh api", "-X ", "--method", "PUT", "PATCH"):
                    self.assertNotIn(words, text)
        return text

    # -- a protection that exists and requires no check -------------------------------------------

    def test_a_protection_that_requires_no_check_is_refused_and_never_replaced(self):
        for why, held in (("no required checks at all", self.HELD),
                          ("required checks answered null", dict(self.HELD, required_status_checks=None)),
                          ("required checks answered as a list", dict(self.HELD, required_status_checks=[]))):
            for flags in self.FLAGS:
                with self.subTest(why=why, flags=flags):
                    text = self.refused(lambda: ProtectionHost(classic=held), self.UNARMED, *flags)
                    # What the plan says it keeps, this run keeps: nothing is written.
                    self.assertIn("keeps    every other setting as it is: ", text)
                    self.assertIn("required_linear_history=true", text)
                    self.assertIn('"required_approving_review_count": 2', text)
        # Whatever a ruleset beside it requires, short of a check of the socle.
        self.refused(lambda: ProtectionHost(classic=self.HELD, ruleset=ruleset_of(loose(["lint"]))), self.UNARMED)

    def test_without_legs_is_told_there_is_nothing_to_narrow(self):
        """Nothing requires the legs there, and --without-legs is not
        advised to turn a setting on: it is told to adopt, as where no
        protection exists at all."""
        adopt = "Nothing requires the legs, so the adoption will not lock anything: adopt first."
        for why, state, message in (
                ("a protection that requires no check", lambda: ProtectionHost(classic=self.HELD),
                 "the protection of main of o/r requires no check: there is nothing to narrow."),
                ("no protection", ProtectionHost, "main of o/r has no protection to narrow."),
                ("no protection, beside a ruleset that is not the socle's",
                 lambda: ProtectionHost(ruleset=ruleset_of(loose(["lint"]))), "main of o/r has no protection to narrow.")):
            with self.subTest(why=why):
                code, plan = self.protect(state(), "--without-legs")
                self.assertEqual((plan["writes"], plan["nothingToChange"]), ([], False))
                self.assertNotIn("refused", plan)
                self.assertTrue(self.protect(state(), "--without-legs", text=True)[1].endswith(NothingDiffersTest.APPLY))
                host = state()
                code, data = self.protect(host, "--without-legs", "--apply")
                self.assertEqual((code, host.writes, data["refused"]), (2, [], {"message": message, "hint": adopt}))

    def test_a_protection_that_requires_checks_still_has_them_changed_through_their_endpoint(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                classic = dict(self.HELD, required_status_checks={"strict": strict, "contexts": ALL_LEGS[:1]})
                body = {"strict": strict, "contexts": ALL_LEGS}
                code, plan = self.protect(ProtectionHost(classic=classic))
                self.assertEqual((code, plan["writes"]), (0, [{"method": "PATCH", "endpoint": CHECKS, "body": body}]))
                self.assertNotIn("refused", plan)
                host = ProtectionHost(classic=classic)
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"], host.writes), (0, True, [("PATCH", CHECKS, body)]))
                self.assertEqual(host.classic, dict(self.HELD, required_status_checks=body))

    def test_a_ruleset_carrying_the_socle_s_checks_is_written_whatever_the_classic_protection_lacks(self):
        """The classic protection is not what --apply writes there, so
        that it requires no check refuses nothing."""
        ruleset = fixtures.LegRenameTest.RULESET
        code, plan = self.protect(ProtectionHost(classic=self.HELD, ruleset=ruleset))
        self.assertNotIn("refused", plan)
        host = ProtectionHost(classic=self.HELD, ruleset=ruleset)
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]), (0, True, [("PUT", RULESET)]))
        self.assertEqual(host.classic, self.HELD)

    # -- no classic protection ---------------------------------------------------------------------

    def test_where_nothing_protects_the_branch_it_is_still_created_whole_with_its_create_lines(self):
        body = {**PACKAGE.protect.PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": ALL_LEGS}}
        code, plan = self.protect(ProtectionHost())
        self.assertEqual((code, plan["writes"]), (0, [{"method": "PUT", "endpoint": PROTECTION, "body": body}]))
        self.assertNotIn("refused", plan)
        self.assertEqual(plan["creates"]["required_linear_history"], False)
        _, text = self.protect(ProtectionHost(), text=True)
        for key, value in plan["creates"].items():
            self.assertIn(f"create   {key}={json.dumps(value)} (no protection exists; --apply creates it with this)\n",
                          text)
        self.assertTrue(text.endswith(NothingDiffersTest.APPLY), text)
        host = ProtectionHost()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], host.writes), (0, True, [("PUT", PROTECTION, body)]))
        self.assertEqual(host.events[-2:], [("write", PROTECTION), ("read", PROTECTION)])

    def test_no_classic_protection_is_created_beside_a_ruleset(self):
        """The plan said `--apply writes this ruleset, never a classic
        protection beside it`, printed no `create` line, and --apply put
        the whole protection beside the ruleset."""
        every = ruleset_of(loose(["lint"]))
        for why, ruleset, line in (
                ("it requires a check that is not the socle's", every,
                 "ruleset  requires lint — none of them the socle's: --apply does not write this ruleset\n"),
                ("it requires no check at all", dict(every, rules=every["rules"][:1]), None)):
            for flags in self.FLAGS:
                with self.subTest(why=why, flags=flags):
                    def state():
                        host = ProtectionHost(ruleset=ruleset)
                        host.fallback = protection_host(("absent", None), seen=[*ALL_LEGS, "lint"])
                        return host
                    text = self.refused(state, self.BESIDE, *flags)
                    self.assertIn("branch   main of o/r is protected by a ruleset\n", text)
                    self.assertNotIn(self.WRITES_IT, text)
                    if line:
                        self.assertIn(line, text)

    def test_once_a_ruleset_requires_a_check_of_the_socle_that_ruleset_is_the_one_written(self):
        """What the refusal says comes next, held to the code: a ruleset
        of the repository is written, one of the organisation refused, and
        of several the one that requires the check is the one, wherever
        GitHub lists it. The first that required any check was written:
        this ruleset of the repository, which requires `lint`, received
        the legs the organisation's ruleset carries."""
        one = ruleset_of(bound(ALL_LEGS[:1]))
        host = ProtectionHost(ruleset=one)
        _, text = self.protect(ProtectionHost(ruleset=one), text=True)
        self.assertIn(f"ruleset  requires {ALL_LEGS[0]}" + self.WRITES_IT, text)
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]), (0, True, [("PUT", RULESET)]))
        self.assertIsNone(host.classic)
        host = OrganisationHost(ruleset=one)
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, host.writes), (2, []))
        self.assertIn("come from a ruleset of the organisation", data["refused"]["message"])
        # The one that carries the socle's check, and never the first for coming first.
        other = rules_of(ruleset_of(bound(ALL_LEGS[:1])), identifier=9, source="Organization")
        theirs = ruleset_of(loose(["lint"]))
        host = ProtectionHost(ruleset=theirs, view=lambda host: host.applied_rules() + other)
        host.fallback = protection_host(("absent", None), seen=[*ALL_LEGS, "lint"])
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, host.writes, host.ruleset, host.classic), (2, [], theirs, None))
        self.assertEqual(data["refused"]["message"], "the socle's checks main of o/r requires come from a ruleset of"
                         " the organisation, which a repository cannot write.")
        self.assertNotIn(RULESET, host.reads)

    # -- a reading that is not one -----------------------------------------------------------------

    def test_an_answer_that_is_no_protection_is_not_read_as_one(self):
        """A 200 carrying nothing was read as a protection that requires
        no check: the whole protection was put over whatever was there."""
        for what, path, answers in (("the branch protection", PROTECTION, (None, [], {}, "x")),
                                    ("the rulesets", VIEW, (None, {}, "x"))):
            for body in answers:
                with self.subTest(unread=what, body=body):
                    state = lambda: ProtectionHost(answers={path: ("ok", body)})
                    code, plan = self.protect(state())
                    self.assertEqual((plan["unread"], plan["writes"], plan["nothingToChange"]), ([what], [], False))
                    self.assertNotIn("refused", plan)
                    if path == PROTECTION:
                        self.assertNotIn("creates", plan)
                        self.assertNotIn("keeps", plan)
                    _, text = self.protect(state(), text=True)
                    self.assertIn(f"UNREAD   GitHub did not answer for {what}", text)
                    host = state()
                    code, data = self.protect(host, "--apply")
                    self.assertEqual((code, host.writes), (2, []))
                    self.assertIn(f"GitHub did not answer for {what} of o/r", data["refused"]["message"])

    def test_a_default_branch_github_did_not_name_is_not_guessed_at(self):
        """The repository was not described, the branch fell back to
        `main`, every reading was of that guess, and --apply created a
        protection on it."""
        for why, answer in (("the repository could not be read", ("unreadable", None)),
                            ("it was not found", ("absent", None)),
                            ("its answer names no default branch", ("ok", {"name": "r"}))):
            with self.subTest(why=why):
                state = lambda: ProtectionHost(answers={"repos/o/r": answer})
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["writes"]), (["the default branch"], []))
                _, text = self.protect(state(), text=True)
                self.assertIn("UNREAD   GitHub did not answer for the default branch: this plan is not a reading,"
                              " and --apply refuses\n", text)
                self.assertTrue(text.endswith(NothingDiffersTest.APPLY), text)
                # Nothing is said of a branch that is a guess: neither that it is open, nor what it would be created with.
                self.assertIn("branch   main of o/r could not be read\n", text)
                self.assertNotIn("create   ", text)
                self.assertNotIn("creates", plan)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes, host.classic), (2, [], None))
                self.assertEqual(data["refused"]["message"], "GitHub did not answer for the default branch of o/r:"
                                 " nothing is written on a reading that is not there.")


class AbsenceTest(unittest.TestCase):
    """A 404 is an absence only where GitHub says so.

    The whole protection is put where nothing protects the branch, and
    that rested on two readings nothing had established. GitHub answers
    404 `Branch not protected` for a branch without protection, 404 `Not
    Found` to a token that may not read protections, and 404 `Branch not
    found` for a branch that does not exist; for the rules of a branch no
    rule applies to it answers `[]`, never a 404. Every one of those was
    read as an absence, and --apply put the socle's protection on it.
    """

    WORDLESS = ("absent", None)
    OTHERS = (("a token that may not read protections", answered_404("Not Found")),
              ("a branch that does not exist", answered_404("Branch not found")),
              ("words that are close", answered_404("Branch not protected yet")),
              ("another case", answered_404("branch not protected")),
              ("no word at all", answered_404("")),
              ("a host that carries no word", WORDLESS))
    REFUSAL = ("GitHub refused to say what protects main of o/r, which a private repository of a free plan does."
               " That is not the same fact as an open branch.")
    UNSAID = ("is NOT protected", "create   ", "no protection exists", "protected by a ruleset")
    protect = NothingDiffersTest.protect

    def test_a_404_that_says_branch_not_protected_and_an_empty_list_of_rules_still_create_the_protection_whole(self):
        """The other end: what establishes that nothing protects the
        branch is read as it was, plan and write."""
        body = {**PACKAGE.protect.PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": ALL_LEGS}}
        host = ProtectionHost()
        code, plan = self.protect(host)
        self.assertEqual((code, plan["unread"], plan["protected"]), (0, [], False))
        self.assertEqual(plan["writes"], [{"method": "PUT", "endpoint": PROTECTION, "body": body}])
        self.assertEqual(host.reads[-2:], [PROTECTION, VIEW])
        _, text = self.protect(ProtectionHost(), text=True)
        self.assertTrue(text.startswith("branch   main of o/r is NOT protected\n"), text)
        self.assertNotIn("UNREAD", text)
        created = [line for line in text.splitlines() if line.startswith("create   ")]
        self.assertEqual(created, [f"create   {key}={json.dumps(value)} (no protection exists; --apply creates it"
                                   " with this)" for key, value in plan["creates"].items()])
        self.assertEqual(len(created), 12)
        self.assertTrue(text.endswith(NothingDiffersTest.APPLY), text)
        host = ProtectionHost()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], data["written"]), (0, True, [f"PUT {PROTECTION}"]))
        self.assertEqual(host.writes, [("PUT", PROTECTION, body)])
        self.assertEqual([event for event in host.events if event[1] in (PROTECTION, VIEW)],
                         [("read", PROTECTION), ("read", VIEW), ("write", PROTECTION), ("read", PROTECTION)])
        self.assertTrue(self.protect(ProtectionHost(), "--apply", text=True)[1].endswith("protect: applied\n"))

    def test_any_other_404_for_the_protection_is_unread_and_the_plan_says_what_github_answered(self):
        """Unread as a 403 or a timeout is, and written on no more than
        they are. The note is its own: GitHub did answer, so it is not
        said to have refused to say, as a private repository of a free
        plan is told -- of `Branch not found` that was false."""
        refused = lambda: ProtectionHost(unreadable=(PROTECTION,))
        for why, answer in self.OTHERS:
            with self.subTest(why=why):
                state = lambda: ProtectionHost(answers={PROTECTION: answer})
                words = getattr(answer[0], "message", "")
                note = ("GitHub answered 404 with " + (f"the words '{words}'" if words else "no words") + " for the"
                        " protection of main of o/r, which is not what it says of a branch without protection:"
                        " nothing is concluded from it.")
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["writes"], plan["nothingToChange"], plan["protected"]),
                                 (["the branch protection"], [], False, False))
                self.assertEqual(plan["note"], note)
                for member in ("creates", "keeps", "refused"):
                    self.assertNotIn(member, plan)
                _, text = self.protect(state(), text=True)
                self.assertTrue(text.startswith("branch   main of o/r could not be read\n"), text)
                self.assertIn("UNREAD   GitHub did not answer for the branch protection: this plan is not a reading,"
                              " and --apply refuses\n", text)
                self.assertIn(f"note     {note}\n", text)
                for said in (*self.UNSAID, "refused to say", "free plan"):
                    self.assertNotIn(said, text)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes, data["writes"], data["written"], data["applied"]),
                                 (2, [], [], [], False))
                self.assertEqual(data["refused"]["message"], "GitHub did not answer for the branch protection of"
                                 " o/r: nothing is written on a reading that is not there.")
                # But for that note, nothing is said of it that is not said of a 403 or a timeout.
                for options in ((), ("--apply",)):
                    code, data = self.protect(state(), *options)
                    self.assertEqual((code, dict(data, note=self.REFUSAL)), self.protect(refused(), *options))
                    code, text = self.protect(state(), *options, text=True)
                    self.assertEqual((code, text.replace(note, self.REFUSAL)),
                                     self.protect(refused(), *options, text=True))
        # And an answer GitHub did not give keeps the note it had.
        for why, answer in (("a 403, a 502 or a timeout", ("unreadable", None)),):
            with self.subTest(why=why):
                code, plan = self.protect(ProtectionHost(answers={PROTECTION: answer}))
                self.assertEqual((plan["unread"], plan["note"]), (["the branch protection"], self.REFUSAL))
        self.assertNotIn("note", self.protect(ProtectionHost(answers={PROTECTION: ("ok", {})}))[1])

    def test_a_ruleset_is_not_written_either_beside_a_protection_that_404_does_not_establish(self):
        """The ruleset is the write, and the classic protection one of
        the readings nothing is written without."""
        state = lambda: ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET,
                                       answers={PROTECTION: answered_404("Not Found")})
        code, plan = self.protect(state())
        self.assertEqual((plan["unread"], plan["writes"], plan["protectedBy"]),
                         (["the branch protection"], [], ["a ruleset"]))
        host = state()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, host.writes), (2, []))
        self.assertIn("did not answer for the branch protection", data["refused"]["message"])
        # And said as GitHub says it, the same ruleset is written as it was.
        host = ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET)
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]), (0, True, [("PUT", RULESET)]))

    def test_a_404_for_the_rules_of_a_branch_is_not_a_reading_of_them(self):
        """Whatever it says: GitHub answers `[]` where no rule applies."""
        refused = lambda: ProtectionHost(unreadable=(VIEW,))
        for why, answer in (("the words of a branch without protection", UNPROTECTED), *self.OTHERS):
            with self.subTest(why=why):
                state = lambda: ProtectionHost(answers={VIEW: answer})
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["writes"], plan["nothingToChange"], plan["protected"]),
                                 (["the rulesets"], [], False, False))
                for member in ("creates", "refused"):
                    self.assertNotIn(member, plan)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes, data["writes"], data["written"], data["applied"]),
                                 (2, [], [], [], False))
                self.assertEqual(data["refused"]["message"], "GitHub did not answer for the rulesets of o/r:"
                                 " nothing is written on a reading that is not there.")
                for options in ((), ("--apply",)):
                    for text in (False, True):
                        self.assertEqual(self.protect(state(), *options, text=text),
                                         self.protect(refused(), *options, text=text))

    def test_with_its_rules_unread_a_branch_is_said_neither_open_nor_ruled_and_nothing_is_created(self):
        """`is NOT protected` and the `create` lines were printed over a
        timeout of the rules, and `is protected by a ruleset` from a body
        that was not a list of them: the plan says what was not read, and
        no more of that half."""
        legs = rules_of(ruleset_of(bound(ALL_LEGS)))
        unread = (("a 502, or a timeout", ("unreadable", None)), ("a 404", answered_404("Not Found")),
                  ("null", ("ok", None)), ("an empty object", ("ok", {})), ("zero", ("ok", 0)),
                  ("a string", ("ok", "x")), ("an error object", ("ok", {"message": "Not Found"})),
                  ("the rules inside an object", ("ok", {"rules": legs})))
        for why, answer in unread:
            with self.subTest(why=why, classic="none"):
                state = lambda: ProtectionHost(answers={VIEW: answer})
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["protected"], plan["protectedBy"], plan["ruleset"],
                                  plan["writes"]), (["the rulesets"], False, [], [], []))
                self.assertNotIn("creates", plan)
                _, text = self.protect(state(), text=True)
                self.assertTrue(text.startswith(
                    "branch   main of o/r could not be read\n"
                    "UNREAD   GitHub did not answer for the rulesets: this plan is not a reading, and --apply"
                    " refuses\n"), text)
                for words in (*self.UNSAID, "ruleset  "):
                    self.assertNotIn(words, text)
                self.assertTrue(text.endswith(NothingDiffersTest.APPLY), text)
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes), (2, []))
                self.assertIn("did not answer for the rulesets", data["refused"]["message"])
            with self.subTest(why=why, classic="holds the proposal"):
                # What was read is still said: the classic protection, and what it keeps.
                state = lambda: ProtectionHost(classic=FakeProtection(ALL_LEGS).body, answers={VIEW: answer})
                code, plan = self.protect(state())
                self.assertEqual((plan["unread"], plan["protectedBy"], plan["writes"], plan["nothingToChange"]),
                                 (["the rulesets"], ["branch protection"], [], False))
                self.assertIn("keeps", plan)
                _, text = self.protect(state(), text=True)
                self.assertTrue(text.startswith("branch   main of o/r is protected by branch protection\n"), text)
                for words in self.UNSAID:
                    self.assertNotIn(words, text)


class SeveralRulesets(FakeHost):
    """A branch several rulesets apply to, each read and written under its own identifier.

    The rulesets are (identifier, source, body), in the order `rules/branches`
    lists their rules. `drift` is what a write disturbs besides what it
    sent, given this host and the identifier written.
    """

    def __init__(self, *rulesets, classic=None, seen=ALL_LEGS, unreadable=(), drift=None):
        super().__init__()
        self.classic = copy.deepcopy(classic)
        self.held = {identifier: (source, copy.deepcopy(body)) for identifier, source, body in rulesets}
        self.unreadable, self.drift, self.events = unreadable, drift, []
        self.fallback = protection_host(UNPROTECTED, seen=seen)

    def read(self, path):
        self.reads.append(path)
        self.events.append(("read", path))
        if path in self.unreadable:
            return "unreadable", None
        if path == PROTECTION:
            return ("ok", copy.deepcopy(self.classic)) if self.classic else UNPROTECTED
        if path == VIEW:
            return "ok", [rule for identifier, (source, body) in self.held.items()
                          for rule in rules_of(body, identifier, source)]
        if path.startswith("repos/o/r/rulesets/"):
            identifier = int(path.rsplit("/", 1)[1])
            source, body = self.held[identifier]
            return "ok", dict(copy.deepcopy(body), id=identifier, source_type=source)
        return self.fallback.read(path)

    def write(self, method, endpoint, payload, cwd=None):
        body = json.loads(payload.read_text())
        self.writes.append((method, endpoint, body))
        self.events.append(("write", endpoint))
        if (method, endpoint) == ("PATCH", CHECKS):
            self.classic["required_status_checks"] = copy.deepcopy(body)
        elif method == "PUT" and endpoint.startswith("repos/o/r/rulesets/"):
            identifier = int(endpoint.rsplit("/", 1)[1])
            self.held[identifier][1].update(copy.deepcopy(body))
            if self.drift:
                self.drift(self, identifier)
        else:
            raise AssertionError(f"undeclared protection write: {method} {endpoint}")
        return subprocess.CompletedProcess([], 0, "", "")

    def requires(self, identifier):
        """The contexts each rule of a ruleset requires, rule after rule."""
        return [check["context"] for rule in self.held[identifier][1]["rules"]
                if rule["type"] == "required_status_checks" for check in rule["parameters"]["required_status_checks"]]


class SeveralRulesetsTest(unittest.TestCase):
    """With several rulesets, the one that carries the socle's checks is the one written.

    --apply wrote the first rule of the branch that required checks,
    whichever ruleset it came from. With `lint` required by one ruleset
    and the three legs by the next, it gave the legs to the first, read
    back the rules of both, found `lint` twice and the legs twice, and
    ended in an error -- on that run and on every one after it. And the
    refusal of 0.64.1 told an owner to require a check of the socle in a
    ruleset: one who did so in a second ruleset arrived there.

    The ruleset written is the one that already carries a check of the
    socle, wherever GitHub lists it, and its read-back judges the checks
    of that ruleset. One that carries none is never written. And of
    several that each carry some, this command writes none: which of them
    should hold them is not its choice.
    """

    LINT = [*ALL_LEGS, "lint"]
    NINE = "repos/o/r/rulesets/9"
    THEIRS = (7, "Repository", ruleset_of(loose(["lint"])))
    HINT = ("This command writes the one ruleset that carries the socle's checks, and does not choose between"
            " rulesets that each require some of them: nothing is written. Which of them should carry those checks"
            " is the repository's to settle, or the organisation's for a ruleset of its own; protect --apply then"
            " writes that one.")
    UNDECIDED = " — in more than one ruleset: --apply does not choose between them, and writes none\n"
    protect = NothingDiffersTest.protect

    def test_the_ruleset_that_carries_the_legs_is_written_wherever_it_is_listed_and_its_write_reads_back(self):
        """The state that ended in an error on every run: `lint` in one
        ruleset of the repository, the legs in another, and a pull request
        that ran them all. The ruleset that carries the legs is given the
        legs, and `lint` stays where it is required."""
        for why, held in (("the three legs", ALL_LEGS), ("one leg, as the refusal beside a ruleset asks", ALL_LEGS[:1])):
            for listed in ("second", "first"):
                with self.subTest(carries=why, listed=listed):
                    ours = (9, "Repository", ruleset_of(bound(held)))
                    order = (self.THEIRS, ours) if listed == "second" else (ours, self.THEIRS)
                    state = lambda: SeveralRulesets(*order, seen=self.LINT)
                    host = state()
                    code, plan = self.protect(host)
                    self.assertEqual((code, plan["writes"]), (0, [{"method": "PUT", "endpoint": self.NINE}]))
                    self.assertNotIn("refused", plan)
                    # The plan says `--apply writes this ruleset` of the one it writes, and lists what it requires.
                    self.assertEqual((plan["ruleset"], plan["before"]["rulesets"], plan["required"]),
                                     (held, sorted([*held, "lint"]), sorted([*held, "lint"])))
                    self.assertEqual((plan["proposed"], plan["dropping"]), (self.LINT, []))
                    self.assertEqual([path for path in host.reads if "/rulesets/" in path], [])
                    _, text = self.protect(state(), text=True)
                    self.assertIn("ruleset  requires " + ", ".join(held) + WholeProtectionTest.WRITES_IT, text)
                    self.assertTrue(text.endswith(NothingDiffersTest.APPLY), text)
                    # What a line of the plan says of a check is what --apply does with it.
                    self.assertEqual([line for line in text.splitlines() if line.startswith(("ok ", "add "))],
                                     [f"{'ok' if name in held else 'add':<8} {name}" for name in ALL_LEGS]
                                     + ["ok       lint"])
                    sends = held != ALL_LEGS
                    host = state()
                    for run in (1, 2, 3):
                        code, data = self.protect(host, "--apply")
                        self.assertEqual((code, data["applied"], data["nothingToChange"]),
                                         (0, sends and run == 1, not (sends and run == 1)), data)
                        self.assertEqual([write[:2] for write in host.writes],
                                         [("PUT", self.NINE)] if sends else [], run)
                        self.assertEqual(data["written"], [f"PUT {self.NINE}"] if sends and run == 1 else [])
                        if sends and run == 1:
                            self.assertEqual(host.events[-3:],
                                             [("read", self.NINE), ("write", self.NINE), ("read", VIEW)])
                    self.assertEqual((host.requires(9), host.requires(7)), (ALL_LEGS, ["lint"]))
                    # The ruleset that carries none of the socle's checks is neither read nor written.
                    self.assertEqual(host.held[7], (self.THEIRS[1], self.THEIRS[2]))
                    self.assertNotIn(RULESET, host.reads)
                    self.assertTrue(self.protect(host, "--apply", text=True)[1].endswith(NothingDiffersTest.CHANGE))

    def test_the_ruleset_written_is_given_no_check_that_is_another_mechanism_s(self):
        """Given the whole proposal, it received `lint` from the ruleset
        that requires it, bound to the app of its own checks: a `lint` that
        app never reports, awaited on every pull request, under a plan that
        printed `ok lint` and no `add`."""
        theirs = (3, "Repository", ruleset_of(bound(["lint"], 999)))
        legs = (7, "Repository", ruleset_of(bound(ALL_LEGS[:1])))
        classic = dict(FakeProtection(["unit"], strict=True).body)
        for why, rulesets, held, seen, kept in (
                ("a check another ruleset requires, bound to another app", (theirs, legs), None, self.LINT, []),
                ("a check the classic protection requires", (theirs, legs), classic, [*self.LINT, "unit"], []),
                ("a check of its own that pull requests produce",
                 (theirs, (7, "Repository", ruleset_of([*bound(ALL_LEGS[:1]), *bound(["unit"], 4242)]))), None,
                 [*self.LINT, "unit"], ["unit"])):
            with self.subTest(why=why):
                state = lambda: SeveralRulesets(*rulesets, classic=held, seen=seen)
                code, plan = self.protect(state())
                self.assertEqual((plan["dropping"], plan["writes"]), ([], [{"method": "PUT", "endpoint": RULESET}]))
                _, text = self.protect(state(), text=True)
                said = [line for line in text.splitlines() if line.startswith(("ok ", "add "))]
                self.assertEqual(sorted(said), sorted([f"ok       {ALL_LEGS[0]}",
                                                       *(f"add      {name}" for name in ALL_LEGS[1:]),
                                                       *(f"ok       {name}" for name in seen if name not in ALL_LEGS)]))
                host = state()
                before = copy.deepcopy((host.held[3], host.classic))
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]),
                                 (0, True, [("PUT", RULESET)]))
                # The legs with the app of the check it held, its own check with its own app, and no other.
                self.assertEqual(host.writes[0][2]["rules"][1]["parameters"]["required_status_checks"],
                                 [*bound(ALL_LEGS), *bound(kept, 4242)])
                self.assertEqual(((host.held[3], host.classic), host.requires(3)), (before, ["lint"]))
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes[1:], data["nothingToChange"]), (0, [], True))

    def test_a_check_pull_requests_produce_and_nothing_requires_is_added_to_no_ruleset_and_the_plan_says_so(self):
        """Which of several rulesets should require it is read nowhere:
        the plan printed `add build`, and --apply gave it to the ruleset
        that carries the legs."""
        seen = [*self.LINT, "build"]
        state = lambda: SeveralRulesets(self.THEIRS, (9, "Repository", ruleset_of(bound(ALL_LEGS[:1]))), seen=seen)
        code, plan = self.protect(state())
        self.assertEqual((plan["observed"], plan["proposed"]), (sorted(seen), self.LINT))
        self.assertEqual(plan["note"], "build is left out: pull requests produce it and nothing requires it, and of"
                         " several rulesets this command gives the one it writes the socle's checks and no other."
                         " Which ruleset should require it is its owner's to settle.")
        _, text = self.protect(state(), text=True)
        self.assertIn(f"note     {plan['note']}\n", text)
        self.assertNotIn("build\n", text)
        host = state()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, data["applied"], host.requires(9), host.requires(7)), (0, True, ALL_LEGS, ["lint"]))
        # The other end: one ruleset is the one mechanism, and is given the proposal whole, as it was.
        one = SeveralRulesets((9, "Repository", ruleset_of(bound(ALL_LEGS[:1]))), seen=seen)
        self.assertIn("add      build\n", self.protect(one, text=True)[1])
        code, data = self.protect(one, "--apply")
        self.assertEqual((code, data["applied"], host.requires(9) == ALL_LEGS, one.requires(9)),
                         (0, True, True, [*ALL_LEGS, "build", "lint"]))
        self.assertNotIn("note", data)

    STALE = "STALE    stale-x is required by a ruleset this command does not write, and no run on main produces it:" \
            " a pull request would wait for it forever\n"

    def test_a_check_only_a_ruleset_this_command_does_not_write_requires_is_not_said_to_be_dropped(self):
        """`stale-x` is required by the other ruleset and nothing produces
        it. The plan printed `DROP stale-x … --apply refuses without
        --allow-narrow` and the path that drops it, --apply refused a write
        that `would stop requiring stale-x` -- a write to the ruleset that
        carries the legs, which cannot touch it; adding the two legs that
        ruleset lacked was refused for a narrowing it would not perform.
        And with --allow-narrow --apply ended on exit 0 with `stale-x`
        still required, where its plan ended on 2."""
        stale = (3, "Repository", ruleset_of(loose(["stale-x"])))
        for held in (ALL_LEGS, ALL_LEGS[:1]):
            for flags in ((), ("--allow-narrow",)):
                with self.subTest(held=held, flags=flags):
                    state = lambda: SeveralRulesets(stale, (7, "Repository", ruleset_of(bound(held))))
                    host = state()
                    code, plan = self.protect(host, *flags)
                    self.assertEqual((code, plan["dropping"], plan["requiredButNeverRun"], host.writes),
                                     (2, [], ["stale-x"], []))
                    self.assertEqual(plan["writes"], [{"method": "PUT", "endpoint": RULESET}])
                    self.assertNotIn("refused", plan)
                    code, text = self.protect(state(), *flags, text=True)
                    self.assertEqual(code, 2)
                    self.assertIn(self.STALE, text)
                    for said in ("DROP ", "path ", "REFUSED", "--allow-narrow", "would stop requiring"):
                        self.assertNotIn(said, text)
                    # --apply writes what the ruleset it writes lacks, refuses nothing, and ends as the plan did.
                    host = state()
                    for run in (1, 2):
                        code, data = self.protect(host, *flags, "--apply")
                        sends = held != ALL_LEGS and run == 1
                        self.assertEqual((code, data["applied"], data["nothingToChange"], data["dropping"]),
                                         (2, sends, not sends, []), data)
                        self.assertNotIn("refused", data)
                        self.assertEqual([write[:2] for write in host.writes],
                                         [("PUT", RULESET)] if held != ALL_LEGS else [])
                    self.assertEqual((host.requires(7), host.requires(3)), (ALL_LEGS, ["stale-x"]))
                    code, text = self.protect(host, *flags, "--apply", text=True)
                    self.assertEqual(code, 2)
                    self.assertIn(self.STALE, text)
                    self.assertNotIn("DROP ", text)

    def test_what_the_classic_protection_requires_beside_several_rulesets_is_not_this_write_s_to_drop_either(self):
        classic = dict(FakeProtection(["stale-y"], strict=True).body)
        line = ("STALE    stale-y is required by the classic protection, which this command does not write here, and"
                " no run on main produces it: a pull request would wait for it forever\n")
        for flags in ((), ("--allow-narrow",)):
            with self.subTest(flags=flags):
                state = lambda: SeveralRulesets(self.THEIRS, (9, "Repository", ruleset_of(bound(ALL_LEGS[:1]))),
                                                classic=classic, seen=self.LINT)
                code, plan = self.protect(state(), *flags)
                self.assertEqual((code, plan["dropping"], plan["requiredButNeverRun"]), (2, [], ["stale-y"]))
                self.assertIn(line, self.protect(state(), *flags, text=True)[1])
                host = state()
                code, data = self.protect(host, *flags, "--apply")
                self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]),
                                 (2, True, [("PUT", self.NINE)]))
                self.assertNotIn("refused", data)
                self.assertEqual((host.requires(9), host.classic), (ALL_LEGS, classic))

    def test_where_the_classic_protection_is_what_is_written_a_check_only_a_ruleset_requires_is_not_its_to_drop(self):
        """Several rulesets and none of them the socle's: the classic
        protection is what --apply writes, and it cannot stop a ruleset
        requiring anything. Neither can a run that writes nothing."""
        rulesets = ((3, "Repository", ruleset_of(loose(["stale-x"]))), (9, "Repository", ruleset_of(loose(["lint"]))))
        body = {"strict": True, "contexts": self.LINT}
        for flags in ((), ("--allow-narrow",)):
            with self.subTest(written="the classic protection", flags=flags):
                state = lambda: SeveralRulesets(*rulesets, classic=FakeProtection(ALL_LEGS[:1], strict=True).body,
                                                seen=self.LINT)
                code, plan = self.protect(state(), *flags)
                self.assertEqual((code, plan["dropping"], plan["requiredButNeverRun"]), (2, [], ["stale-x"]))
                self.assertEqual(plan["writes"], [{"method": "PATCH", "endpoint": CHECKS, "body": body}])
                code, text = self.protect(state(), *flags, text=True)
                self.assertIn(self.STALE, text)
                for said in ("DROP ", "path ", "REFUSED", "would stop requiring"):
                    self.assertNotIn(said, text)
                host = state()
                code, data = self.protect(host, *flags, "--apply")
                self.assertEqual((code, data["applied"], host.writes), (2, True, [("PATCH", CHECKS, body)]))
                self.assertNotIn("refused", data)
                self.assertEqual((host.requires(3), host.requires(9)), (["stale-x"], ["lint"]))
            with self.subTest(written="nothing", flags=flags):
                for options in (flags, (*flags, "--apply")):
                    host = SeveralRulesets(*rulesets, seen=self.LINT)
                    code, data = self.protect(host, *options)
                    self.assertEqual((code, data["refused"], data["dropping"], host.writes),
                                     (2, WholeProtectionTest.BESIDE, [], []))
                    text = self.protect(SeveralRulesets(*rulesets, seen=self.LINT), *options, text=True)[1]
                    self.assertIn(self.STALE, text)
                    self.assertNotIn("DROP ", text)
        # A check of its own that nothing produces is the classic protection's to drop, as it was.
        state = lambda: SeveralRulesets(*rulesets, classic=FakeProtection([*ALL_LEGS, NOTHING]).body, seen=self.LINT)
        code, plan = self.protect(state())
        self.assertEqual(plan["dropping"], [NOTHING])
        host = state()
        code, data = self.protect(host, "--apply")
        self.assertEqual((code, host.writes, data["refused"]["message"]),
                         (2, [], f"this write would stop requiring {NOTHING} on main of o/r."))
        code, data = self.protect(host, "--apply", "--allow-narrow")
        self.assertEqual((code, data["applied"], host.classic["required_status_checks"]["contexts"]),
                         (2, True, self.LINT))
        # And of rulesets this command does not choose between, nothing is said to be dropped either.
        both = ((3, "Repository", ruleset_of(loose(["stale-x"]))), (7, "Repository", ruleset_of(bound(ALL_LEGS[:1]))),
                (9, "Repository", ruleset_of(bound([*ALL_LEGS[1:], NOTHING]))))
        for options in ((), ("--apply",), ("--apply", "--allow-narrow")):
            code, data = self.protect(SeveralRulesets(*both), *options)
            self.assertEqual((code, data["dropping"], data["requiredButNeverRun"]), (2, [], ["stale-x", NOTHING]))
            self.assertTrue(data["refused"]["message"].startswith("more than one ruleset requires checks of the socle"))
            self.assertNotIn("DROP ", self.protect(SeveralRulesets(*both), *options, text=True)[1])

    def test_a_check_nothing_produces_in_the_ruleset_written_is_still_this_write_s_to_drop(self):
        """Judged on the checks of the ruleset written: one it holds is
        refused without --allow-narrow and dropped with it, in the words
        there always were, and no check of another ruleset is named."""
        ours = (7, "Repository", ruleset_of(bound([*ALL_LEGS, NOTHING])))
        for why, other, settled in (("the other ruleset requires a check pull requests produce", "lint", 0),
                                    ("the other ruleset requires a check nothing produces too", "stale-x", 2)):
            with self.subTest(why=why):
                state = lambda: SeveralRulesets((3, "Repository", ruleset_of(loose([other]))), ours, seen=self.LINT)
                code, plan = self.protect(state())
                self.assertEqual((code, plan["dropping"], plan["writes"]), (2, [NOTHING], []))
                code, text = self.protect(state(), text=True)
                self.assertIn(f"DROP     {NOTHING} is required and this plan leaves it out: --apply refuses without"
                              " --allow-narrow\n", text)
                self.assertIn(f"STALE    {NOTHING} is required and no run on main produces it:", text)
                self.assertEqual(self.STALE in text, bool(settled))
                host = state()
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes), (2, []))
                self.assertEqual(data["refused"]["message"],
                                 f"this write would stop requiring {NOTHING} on main of o/r.")
                # Asked to narrow, the plan and --apply end alike: on what another ruleset still requires.
                self.assertEqual(self.protect(state(), "--allow-narrow")[0], 2)
                code, data = self.protect(host, "--apply", "--allow-narrow")
                self.assertEqual((code, data["applied"], host.requires(7), host.requires(3)),
                                 (settled, True, ALL_LEGS, [other]))
                self.assertNotIn("refused", data)
                self.assertEqual(self.protect(host, "--apply", "--allow-narrow")[0], settled)

    def test_the_narrowing_before_a_rename_takes_the_legs_from_the_ruleset_written_and_adds_nothing_to_it(self):
        """--without-legs gave the ruleset that carries the legs everything
        the branch required but them: `lint`, from the other ruleset."""
        held = [*ALL_LEGS, "check / sanitizers"]
        state = lambda: SeveralRulesets(self.THEIRS, (9, "Repository", ruleset_of(bound(held))), seen=self.LINT)
        code, plan = self.protect(state(), "--without-legs")
        self.assertEqual((plan["dropped"], plan["dropping"], plan["proposed"]),
                         (ALL_LEGS, ALL_LEGS, ["lint", "check / sanitizers"]))
        host = state()
        code, data = self.protect(host, "--without-legs", "--apply")
        self.assertEqual((code, data["applied"], [write[:2] for write in host.writes]), (0, True, [("PUT", self.NINE)]))
        self.assertEqual((host.requires(9), host.requires(7)), (["check / sanitizers"], ["lint"]))

    def test_a_ruleset_of_the_organisation_that_carries_the_legs_is_refused_and_no_other_is_written_for_it(self):
        """The ruleset of the repository came first and requires `lint`:
        it received the legs the organisation's ruleset carries."""
        ours = (9, "Organization", ruleset_of(bound(ALL_LEGS[:1])))
        for why, order, what in (("after a ruleset of the repository", (self.THEIRS, ours), "the socle's checks"),
                                 ("before it", (ours, self.THEIRS), "the socle's checks"),
                                 ("alone", (ours,), "the checks")):
            with self.subTest(listed=why):
                state = lambda: SeveralRulesets(*order, seen=self.LINT)
                code, plan = self.protect(state())
                self.assertEqual((plan["writes"], plan["nothingToChange"]), ([], False))
                host = state()
                before = copy.deepcopy(host.held)
                code, data = self.protect(host, "--apply")
                self.assertEqual((code, host.writes, host.held), (2, [], before))
                self.assertEqual(data["refused"]["message"], f"{what} main of o/r requires come from a ruleset of the"
                                 " organisation, which a repository cannot write.")
                self.assertEqual([path for path in host.reads if "/rulesets/" in path], [])

    def test_rulesets_that_each_carry_some_of_the_socle_s_checks_are_refused_and_none_is_written(self):
        every, one, rest = ruleset_of(bound(ALL_LEGS)), ruleset_of(bound(ALL_LEGS[:1])), ruleset_of(bound(ALL_LEGS[1:]))
        mixed = ruleset_of([*loose(["lint"]), *bound(ALL_LEGS[:1])])
        for why, rulesets, named in (
                ("the legs split over two", ((7, "Repository", one), (9, "Repository", rest)), "7 and 9"),
                ("one leg beside `lint`, and the three legs",
                 ((7, "Repository", mixed), (9, "Repository", every)), "7 and 9"),
                ("one of the organisation, one of the repository",
                 ((3, "Organization", one), (7, "Repository", every)), "3 and 7"),
                ("three of them", ((3, "Organization", one), (7, "Repository", every), (9, "Repository", rest)),
                 "3, 7 and 9"),
                ("two, after one that requires a check of its own",
                 ((3, "Repository", ruleset_of(loose(["lint"]))), (7, "Repository", one), (9, "Repository", every)),
                 "7 and 9")):
            refusal = {"message": "more than one ruleset requires checks of the socle on main of o/r: rulesets"
                                  f" {named}.", "hint": self.HINT}
            for flags in (*WholeProtectionTest.FLAGS, ("--without-legs",)):
                for options in (flags, (*flags, "--apply")):
                    with self.subTest(why=why, options=options):
                        host = SeveralRulesets(*rulesets, seen=self.LINT)
                        before = copy.deepcopy(host.held)
                        code, data = self.protect(host, *options)
                        self.assertEqual((code, data["refused"]), (2, refusal), data)
                        self.assertEqual((data["writes"], data["written"], data["applied"], data["nothingToChange"]),
                                         ([], [], False, False))
                        self.assertEqual((host.writes, host.held, host.classic), ([], before, None))
                        self.assertEqual([path for path in host.reads if "/rulesets/" in path], [])
                        self.assertNotIn("creates", data)
                        code, text = self.protect(SeveralRulesets(*rulesets, seen=self.LINT), *options, text=True)
                        self.assertEqual(code, 2)
                        self.assertIn(f"REFUSED  {refusal['message']} {refusal['hint']}\n", text)
                        self.assertTrue(text.endswith(WholeProtectionTest.REFUSED), text)
                        # The `ruleset` line says of them what --apply does, and of the rulesets concerned alone.
                        self.assertIn("ruleset  requires " + ", ".join(data["ruleset"]) + self.UNDECIDED, text)
                        self.assertEqual("lint" in data["ruleset"], rulesets[0][2] is mixed)
                        self.assertNotIn("--apply writes this ruleset", text)
                        for words in ("gh api", "-X ", "--method", "PUT", "PATCH", "create   "):
                            self.assertNotIn(words, text)

    def test_the_refusal_of_several_rulesets_is_said_beside_a_reading_that_is_not_there(self):
        """The rules of the branch were read, and they are what refuses:
        the plan names what was not read and refuses in the same run, and
        --apply in the same words. Said after the missing reading alone,
        the `ruleset` line of such a plan ended on `--apply writes this
        ruleset`."""
        rulesets = ((7, "Repository", ruleset_of(bound(ALL_LEGS[:1]))),
                    (9, "Repository", ruleset_of(bound(ALL_LEGS[1:]))))
        refusal = {"message": "more than one ruleset requires checks of the socle on main of o/r: rulesets 7 and 9.",
                   "hint": self.HINT}
        for what, path in (("the branch protection", PROTECTION), ("the check runs of recent pull requests", RUNS)):
            for options in ((), ("--apply",)):
                with self.subTest(unread=what, options=options):
                    host = SeveralRulesets(*rulesets, unreadable=(path,))
                    code, data = self.protect(host, *options)
                    self.assertEqual((code, data["unread"], data["refused"], data["writes"], host.writes),
                                     (2, [what], refusal, [], []))
                    _, text = self.protect(SeveralRulesets(*rulesets, unreadable=(path,)), *options, text=True)
                    self.assertIn(f"UNREAD   GitHub did not answer for {what}", text)
                    self.assertIn(self.UNDECIDED, text)
                    self.assertNotIn("--apply writes this ruleset", text)
                    self.assertTrue(text.endswith(WholeProtectionTest.REFUSED), text)
        # A default branch GitHub did not name is a guess, and its rules a reading of nothing: unread, and no more.
        state = lambda: SeveralRulesets(*rulesets)
        for options in ((), ("--apply",)):
            host = state()
            host.fallback = FakeHost(read=lambda path, read=host.fallback.read:
                                     ("unreadable", None) if path == "repos/o/r" else read(path))
            code, data = self.protect(host, *options)
            self.assertEqual((data["unread"], data["writes"], host.writes), (["the default branch"], [], []))
            self.assertEqual("refused" in data, bool(options))
            if options:
                self.assertIn("did not answer for the default branch", data["refused"]["message"])

    def test_rulesets_that_carry_none_of_the_socle_s_checks_are_never_written(self):
        """Whatever their number and their order: the classic protection
        is what --apply writes there, where one requires checks, and no
        classic protection is created beside them."""
        rulesets = (self.THEIRS, (9, "Repository", ruleset_of(loose(["build"]))))
        line = "ruleset  requires build, lint — none of them the socle's: --apply does not write this ruleset\n"
        seen = [*self.LINT, "build"]
        for flags in WholeProtectionTest.FLAGS:
            for options in (flags, (*flags, "--apply")):
                with self.subTest(classic="none", options=options):
                    host = SeveralRulesets(*rulesets, seen=seen)
                    before = copy.deepcopy(host.held)
                    code, data = self.protect(host, *options)
                    self.assertEqual((code, data["refused"], host.writes, host.held, host.classic),
                                     (2, WholeProtectionTest.BESIDE, [], before, None))
                    self.assertIn(line, self.protect(SeveralRulesets(*rulesets, seen=seen), *options, text=True)[1])
        with self.subTest(classic="lacks a leg"):
            state = lambda: SeveralRulesets(*rulesets, classic=FakeProtection(ALL_LEGS[:1], strict=True).body, seen=seen)
            body = {"strict": True, "contexts": [*ALL_LEGS, "build", "lint"]}
            code, plan = self.protect(state())
            self.assertEqual(plan["writes"], [{"method": "PATCH", "endpoint": CHECKS, "body": body}])
            self.assertIn(line, self.protect(state(), text=True)[1])
            host = state()
            before = copy.deepcopy(host.held)
            code, data = self.protect(host, "--apply")
            self.assertEqual((code, data["applied"], host.writes, host.held),
                             (0, True, [("PATCH", CHECKS, body)], before))

    def test_the_read_back_still_fails_on_anything_that_moved_beyond_the_checks_of_the_ruleset_written(self):
        """It judges the checks of the ruleset written, and every rule of
        every ruleset for the rest, as it did for one ruleset."""
        def its_policy(host, identifier):
            host.held[identifier][1]["rules"][1]["parameters"]["strict_required_status_checks_policy"] = True
        def its_rule(host, identifier):
            host.held[identifier][1]["rules"].pop(0)
        def its_checks(host, identifier):
            host.held[identifier][1]["rules"][1]["parameters"]["required_status_checks"].pop()
        def the_policy_of_the_other(host, identifier):
            host.held[7][1]["rules"][1]["parameters"]["strict_required_status_checks_policy"] = True
        def a_rule_of_the_other(host, identifier):
            host.held[7][1]["rules"].pop(0)
        for drift in (its_policy, its_rule, its_checks, the_policy_of_the_other, a_rule_of_the_other):
            with self.subTest(moved=drift.__name__):
                host = SeveralRulesets(self.THEIRS, (9, "Repository", ruleset_of(bound(ALL_LEGS[:1]))),
                                       seen=self.LINT, drift=drift)
                code, result = self.protect(host, "--apply")
                self.assertEqual((code, result["error"]["code"]), (1, "PROCESS_FAILED"), result)
                self.assertIn("ruleset 9 of o/r changed beyond its required checks, or they did not read back as"
                              " written.", result["error"]["message"])
                self.assertEqual(host.events[-2:], [("write", self.NINE), ("read", VIEW)])

    def test_one_ruleset_is_read_and_written_as_it_was(self):
        """The other end: the same calls in the same order, the same
        write and the same words, on a host that answers for several
        rulesets and holds one."""
        for why, ruleset in (("it lacks a leg and requires a job the call turns off", fixtures.LegRenameTest.RULESET),
                             ("it holds the proposal", ruleset_of(bound(ALL_LEGS))),
                             ("it requires a check of its own", ruleset_of(loose(["lint"])))):
            for options in ((), ("--apply",), ("--without-legs", "--apply")):
                with self.subTest(why=why, options=options):
                    one, several = ProtectionHost(ruleset=ruleset), SeveralRulesets((7, "Repository", ruleset))
                    for text in (False, True):
                        self.assertEqual(self.protect(several, *options, text=text),
                                         self.protect(one, *options, text=text))
                    self.assertEqual((several.events, several.writes), (one.events, one.writes))
                    self.assertEqual(several.held[7][1], one.ruleset)
