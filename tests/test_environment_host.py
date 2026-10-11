# SPDX-License-Identifier: MPL-2.0
"""`protect` arms the release environment through one stateful host, and erases nothing.

The first release of a product copied two `gh api` calls from the
conventions, ran the second twice, and had to read the list by hand to know
that the environment held one rule and not two; then did it all again when
its repository was created anew. `protect` writes them now. The host below
answers as GitHub does where its reference says or where it was measured,
and as badly as it could where neither says: a PUT replaces the whole
environment, so a write that omits a reviewer loses it; a POST never
refuses a duplicate; a list kept from an earlier custom policy is unlisted
(404, measured) until the policy is custom again. A test that passes here
passes on the pessimistic reading.
"""
from __future__ import annotations

import copy
import io
import json
import pathlib
import re
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import test_maelys_release as fixtures
from test_maelys_release import ALL_LEGS, MODULE, ROOT, FakeHost, FakeProtection, protection_host, using_host
from test_protect_host import ProtectionHost


ENVIRONMENT = "repos/o/r/environments/release"
ENVIRONMENTS = "repos/o/r/environments?per_page=100&page=1"
POLICIES = ENVIRONMENT + "/deployment-branch-policies"
LISTING = POLICIES + "?per_page=100&page=1"
CUSTOM = {"protected_branches": False, "custom_branch_policies": True}
PROTECTED = {"protected_branches": True, "custom_branch_policies": False}
TAG = {"name": "v*", "type": "tag"}
MAIN = {"id": 2, "type": "branch", "name": "main"}
CHECKS = "repos/o/r/branches/main/protection/required_status_checks"
PROTECTION = "repos/o/r/branches/main/protection"
REVIEWER = {"id": 7, "type": "required_reviewers", "prevent_self_review": True,
            "reviewers": [{"type": "User", "reviewer": {"login": "the-approver", "id": 4242}},
                          {"type": "Team", "reviewer": {"slug": "the-team", "id": 99}}]}
TIMER = {"id": 5, "type": "wait_timer", "wait_timer": 30}
KNOWN = {4242: {"login": "the-approver", "id": 4242}, 99: {"slug": "the-team", "id": 99}}


def environment(policy=CUSTOM, *rules, bypass=True):
    """An environment as `GET environments/release` answers it."""
    return {"name": "release", "can_admins_bypass": bypass, "deployment_branch_policy": copy.deepcopy(policy),
            "protection_rules": [copy.deepcopy(rule) for rule in rules]
            + ([{"id": 1, "type": "branch_policy"}] if policy else [])}


@contextmanager
def released_project(gate="none", released=True):
    """A product the socle releases: its ci.yml, its release.yml, its declared gate."""
    with tempfile.TemporaryDirectory(prefix="maelys-host-") as directory:
        project = pathlib.Path(directory) / "maelys-fixture"
        workflows = project / ".github" / "workflows"
        workflows.mkdir(parents=True)
        uses = "    uses: maelys-dev/maelys-release/.github/workflows/"
        (workflows / "ci.yml").write_text("jobs:\n  check:\n" + uses + "check-product.yml@" + "f" * 40
                                          + " # v9.9.9\n    with:\n      sanitizer_command: ''\n")
        if released:
            (workflows / "release.yml").write_text("jobs:\n  release:\n" + uses + "release.yml@" + "f" * 40
                                                   + " # v9.9.9\n    with:\n      product: maelys-fixture\n")
        if gate:
            (project / "maelys-release.conf").write_text(f"[gate]\n{gate}\n")
        yield project


class Branch:
    """A default branch under a classic protection that a write really changes."""

    def __init__(self, required=("check / check (linux)",), drift=None):
        self.protection = FakeProtection(list(required), drift=drift)
        self.fallback = protection_host(("absent", None), seen=ALL_LEGS)

    def read(self, path):
        return self.protection.read(path) if path == PROTECTION else self.fallback.read(path)

    def write(self, method, endpoint, payload, cwd=None):
        if (method, endpoint) != ("PATCH", CHECKS):
            raise AssertionError(f"undeclared protection write: {method} {endpoint}")
        return self.protection.write(method, endpoint, json.loads(payload.read_text()))


class OrganisationRuleset(ProtectionHost):
    """A branch whose checks come from a ruleset no repository can write."""

    def applied_rules(self):
        return [dict(rule, ruleset_source_type="Organization") for rule in super().applied_rules()]


class EnvironmentHost(FakeHost):
    """GitHub's environment endpoints, with state, beside a protected branch."""

    def __init__(self, held=None, policies=(), *, hidden=(), forgets=(), duplicates=False, unreadable=(),
                 branch=None, false_404=False, lists_hidden=False, reorders=False, ignores_policy=False, hook=None):
        super().__init__()
        self.environment = copy.deepcopy(held)
        self.policies = [dict(entry) for entry in policies]
        # Policies GitHub kept from an earlier custom policy: unlisted until
        # the environment has custom policies again.
        self.hidden = [dict(entry) for entry in hidden]
        self.forgets, self.duplicates, self.unreadable = forgets, duplicates, tuple(unreadable)
        self.false_404, self.lists_hidden, self.reorders, self.ignores_policy = \
            false_404, lists_hidden, reorders, ignores_policy
        # hook(host, kind, what, nth): before a read is answered, after a write is done.
        self.hook, self.seen = hook, {}
        self.events = []
        self.branch = branch or Branch()

    def custom(self):
        return bool(self.environment and (self.environment["deployment_branch_policy"] or {})
                    .get("custom_branch_policies"))

    def called(self, kind, what):
        self.seen[(kind, what)] = self.seen.get((kind, what), 0) + 1
        if self.hook:
            self.hook(self, kind, what, self.seen[(kind, what)])

    def read(self, path):
        self.reads.append(path)
        self.events.append(("read", path))
        self.called("read", path)
        if path in self.unreadable:
            return "unreadable", None
        if path == ENVIRONMENT:
            if self.false_404:
                return "absent", None
            return ("ok", copy.deepcopy(self.environment)) if self.environment else ("absent", None)
        if path == ENVIRONMENTS:
            names = [{"name": "release"}] if self.environment else []
            return "ok", {"total_count": 1 + len(names), "environments": [{"name": "staging"}, *names]}
        if path.startswith(POLICIES):
            if not self.environment:
                return "absent", None
            # The endpoint that says nothing: 404 without custom policies,
            # as measured on an environment of the fleet that has none.
            if not self.custom():
                if self.lists_hidden:
                    return "ok", {"total_count": len(self.hidden), "branch_policies": copy.deepcopy(self.hidden)}
                return "absent", None
            return "ok", {"total_count": len(self.policies), "branch_policies": copy.deepcopy(self.policies)}
        return self.branch.read(path)

    def write(self, method, endpoint, payload, cwd=None):
        body = json.loads(payload.read_text())
        self.writes.append((method, endpoint, body))
        self.events.append(("write", f"{method} {endpoint}"))
        done = subprocess.CompletedProcess([], 0, "", "")
        if (method, endpoint) == ("PUT", ENVIRONMENT):
            # The pessimistic reading of a reference that says nothing of an
            # omitted member: the PUT is the whole environment.
            rules = []
            if body.get("wait_timer") and "wait_timer" not in self.forgets:
                rules.append({"id": 5, "type": "wait_timer", "wait_timer": body["wait_timer"]})
            if body.get("reviewers") and "reviewers" not in self.forgets:
                who = body["reviewers"][::-1] if self.reorders else body["reviewers"]
                rules.append({"id": 7, "type": "required_reviewers",
                              "prevent_self_review": bool(body.get("prevent_self_review")),
                              "reviewers": [{"type": entry["type"], "reviewer": KNOWN[entry["id"]]}
                                            for entry in who]})
            was_custom = self.custom()
            policy = (self.environment or {}).get("deployment_branch_policy") if self.ignores_policy \
                else body.get("deployment_branch_policy")
            self.environment = environment(policy, *rules, bypass=body.get("can_admins_bypass", True))
            if self.custom() and not was_custom:
                self.policies, self.hidden = self.policies + self.hidden, []
            elif was_custom and not self.custom():
                self.hidden, self.policies = self.policies, []
        elif (method, endpoint) == ("POST", POLICIES):
            if not self.custom():
                return subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
            # No 303 for a pattern already there: a second call makes a
            # second rule, which is what a read before the call prevents.
            for _ in range(2 if self.duplicates else 1):
                self.policies.append({"id": 100 + len(self.policies), **body})
        else:
            done = self.branch.write(method, endpoint, payload, cwd)
        self.called("write", f"{method} {endpoint}")
        return done

    def environment_writes(self):
        return [(method, endpoint, body) for method, endpoint, body in self.writes if "/environments/" in endpoint]

    def protection_writes(self):
        return [(method, endpoint) for method, endpoint, _ in self.writes if "/environments/" not in endpoint]


class ProtectEnvironmentTest(unittest.TestCase):
    def run_protect(self, host, *options, gate="none", released=True, text=False):
        out, err = io.StringIO(), io.StringIO()
        with released_project(gate, released) as project, using_host(host), \
                redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["protect", str(project), *options,
                                    *(["--format", "text"] if text else ["--format", "json", "--compact"])])
        if text:
            return code, out.getvalue() + err.getvalue()
        result = json.loads(err.getvalue() if code == 1 else out.getvalue())
        self.assertEqual(result["exitCode"], code)
        return code, result

    # -- absent: created, with one rule -------------------------------------------------

    def test_the_plan_of_a_missing_environment_names_both_writes_and_sends_none(self) -> None:
        host = EnvironmentHost()
        code, result = self.run_protect(host)
        self.assertEqual(code, 0, result)
        planned = result["data"]["environment"]
        self.assertIs(planned["exists"], False)
        self.assertEqual(planned["writes"], [
            {"method": "PUT", "endpoint": ENVIRONMENT, "body": {"deployment_branch_policy": CUSTOM}},
            {"method": "POST", "endpoint": POLICIES, "body": TAG}])
        self.assertFalse(planned["applied"])
        self.assertEqual(host.writes, [])
        self.assertIsNone(host.environment)
        code, text = self.run_protect(EnvironmentHost(), text=True)
        # One rule for the exit, and the words follow it: what --apply
        # settles is said as what is and what it becomes, never as a FAIL
        # under an exit 0.
        self.assertEqual(code, 0)
        self.assertNotIn("FAIL", text)
        self.assertIn("now      environment release does not exist in o/r", text)
        self.assertIn("then     created, limited to tags v* and nothing else", text)
        self.assertIn(f"write    PUT {ENVIRONMENT} " + json.dumps({"deployment_branch_policy": CUSTOM}), text)
        self.assertIn(f"write    POST {POLICIES} " + json.dumps(TAG), text)
        self.assertIn("environment: nothing written; add --apply", text)

    def test_apply_creates_it_with_the_tag_rule_alone_and_reads_each_write_back(self) -> None:
        host = EnvironmentHost()
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 0, result)
        armed = result["data"]["environment"]
        self.assertTrue(armed["applied"])
        self.assertEqual(armed["written"], [f"PUT {ENVIRONMENT}", f"POST {POLICIES}"])
        # Exactly the two bodies the conventions gave to copy, and nothing
        # else: no reviewer, no timer, no member that could set anything.
        self.assertEqual(host.environment_writes(), [
            ("PUT", ENVIRONMENT, {"deployment_branch_policy": CUSTOM}), ("POST", POLICIES, TAG)])
        self.assertEqual(host.environment["deployment_branch_policy"], CUSTOM)
        self.assertEqual([(entry["name"], entry["type"]) for entry in host.policies], [("v*", "tag")])
        self.assertEqual(armed["rules"], [{"id": 100, "type": "tag", "name": "v*"}])
        self.assertEqual(armed["before"]["exists"], False)
        self.assertEqual(armed["state"], "environment release of o/r limits deployments to tags v*")
        # Every write is followed by the read that confirms it, and the rule
        # is posted on a list read immediately before it.
        for index, event in enumerate(host.events):
            if event[0] == "write":
                self.assertEqual(host.events[index + 1][0], "read", event)
        put = host.events.index(("write", f"PUT {ENVIRONMENT}"))
        post = host.events.index(("write", f"POST {POLICIES}"))
        self.assertEqual(host.events[put + 1], ("read", ENVIRONMENT))
        self.assertEqual(host.events[post - 2:post], [("read", ENVIRONMENT), ("read", LISTING)])
        self.assertEqual(host.events[post + 1:], [("read", ENVIRONMENT), ("read", LISTING)])

    # -- present and right: nothing written, and said --------------------------------------

    def test_an_environment_that_is_right_is_not_written_and_the_run_says_so(self) -> None:
        for carried in ((), (REVIEWER,)):
            with self.subTest(reviewer=bool(carried)):
                host = EnvironmentHost(environment(CUSTOM, *carried), [{"id": 1, **TAG}])
                before = copy.deepcopy(host.environment)
                code, result = self.run_protect(host, "--apply")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["data"]["environment"]["writes"], [])
                self.assertFalse(result["data"]["environment"]["applied"])
                self.assertEqual(host.environment_writes(), [])
                self.assertEqual(host.environment, before)
                self.assertEqual(host.policies, [{"id": 1, **TAG}])
        for options in ((), ("--apply",)):
            _, text = self.run_protect(EnvironmentHost(environment(), [{"id": 1, **TAG}]), *options, text=True)
            self.assertIn("now      environment release of o/r limits deployments to tags v*", text)
            self.assertIn("environment: nothing to change\n", text)

    def test_a_second_run_changes_nothing(self) -> None:
        host = EnvironmentHost()
        self.assertEqual(self.run_protect(host, "--apply")[0], 0)
        first = host.environment_writes()
        held, policies = copy.deepcopy(host.environment), copy.deepcopy(host.policies)
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(host.environment_writes(), first, "the second run wrote to the environment")
        self.assertEqual((host.environment, host.policies), (held, policies))
        self.assertEqual(len(host.policies), 1)
        self.assertEqual(result["data"]["environment"]["written"], [])

    # -- a policy beside the tag rule: refused, nothing written to the environment ----------

    def test_a_policy_beside_the_tag_rule_is_refused_in_the_words_of_preflight(self) -> None:
        for beside in (MAIN, {"id": 3, "type": "tag", "name": "nightly-*"}):
            for held in ([{"id": 1, **TAG}, beside], [beside]):
                with self.subTest(beside=beside["name"], tag_rule=len(held) == 2):
                    host = EnvironmentHost(environment(), held)
                    with using_host(EnvironmentHost(environment(), held)):
                        said = [message for status, message in
                                MODULE.policy_verdicts(MODULE.read_release_environment("o/r"), ".")
                                if status == "fail" and "also admits" in message]
                    self.assertEqual(len(said), 1)
                    self.assertIn(f"-X DELETE {POLICIES}/{beside['id']}", said[0])
                    code, plan = self.run_protect(host)
                    self.assertEqual(code, 2, "a plan that --apply will not settle is not a clean plan")
                    self.assertEqual(plan["data"]["environment"]["refused"], said)
                    self.assertEqual(plan["data"]["environment"]["writes"], [])
                    code, refused = self.run_protect(host, "--apply")
                    self.assertEqual(code, 2)
                    self.assertEqual(refused["data"]["environment"]["refused"], said)
                    self.assertFalse(refused["data"]["environment"]["applied"])
                    self.assertEqual(host.environment_writes(), [])
                    self.assertEqual(host.policies, held)

    def test_a_reading_that_is_not_there_writes_nothing(self) -> None:
        for path, held, what in (
                (ENVIRONMENT, None, "the release environment"),
                (LISTING, environment(), "the deployment policies of the release environment"),
                (LISTING, environment(None), "the deployment policies of the release environment"),
                (ENVIRONMENTS, None, "the list of the repository's environments")):
            with self.subTest(unreadable=path, held=held):
                host = EnvironmentHost(held, unreadable=(path,))
                code, plan = self.run_protect(host)
                self.assertEqual(code, 0, plan)
                self.assertEqual(plan["data"]["environment"]["writes"], [])
                self.assertIn(what, plan["data"]["environment"]["unread"][0])
                code, refused = self.run_protect(host, "--apply")
                self.assertEqual(code, 2)
                self.assertIn(f"GitHub did not answer for {what}", refused["data"]["environment"]["refused"][0])
                self.assertEqual(host.environment_writes(), [])
        _, text = self.run_protect(EnvironmentHost(unreadable=(ENVIRONMENT,)), text=True)
        self.assertIn("UNREAD   GitHub did not answer for the release environment", text)

    def test_an_answer_that_is_not_an_object_is_not_an_absence(self) -> None:
        """`ok` with something else than an environment used to read as
        "missing", the one reading a creating PUT is planned on."""
        class Odd(EnvironmentHost):
            def read(self, path):
                return ("ok", []) if path == ENVIRONMENT else super().read(path)
        host = Odd(environment(None, REVIEWER))
        with using_host(host):
            reading = MODULE.read_release_environment("o/r")
            verdicts = MODULE.policy_verdicts(reading, ".")
        self.assertEqual((reading.missing, reading.unreadable), (False, True))
        self.assertEqual([status for status, _ in verdicts], ["note"])
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(host.environment_writes(), [])
        self.assertIn("did not answer for the release environment", result["data"]["environment"]["refused"][0])

    # -- an environment is created on two witnesses ---------------------------------------------

    def test_one_404_is_not_enough_to_create_over_what_may_exist(self) -> None:
        """One 404 answered in error over an environment that holds two
        reviewers: a policy-only PUT would lose both on this host, with
        nothing to compare afterwards. The list of environments names it."""
        host = EnvironmentHost(environment(None, REVIEWER, TIMER), false_404=True)
        before = copy.deepcopy(host.environment)
        code, plan = self.run_protect(host, gate="reviewer")
        self.assertEqual(code, 2)
        self.assertEqual(plan["data"]["environment"]["writes"], [])
        self.assertIn("names it in the list", plan["data"]["environment"]["refused"][0])
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        self.assertEqual(code, 2)
        self.assertFalse(result["data"]["environment"]["applied"])
        self.assertEqual(host.environment_writes(), [])
        self.assertEqual(host.environment, before)
        self.assertIn(ENVIRONMENTS, host.reads)

    def test_the_absence_is_asked_again_at_the_moment_of_creating(self) -> None:
        def appears(host, kind, what, nth):
            # Somebody creates it, armed, between the plan and the write.
            if (kind, what, nth) == ("read", ENVIRONMENT, 2):
                host.environment = environment(None, REVIEWER)
        host = EnvironmentHost(hook=appears)
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("no longer confirmed absent", result["data"]["environment"]["refused"][0])
        self.assertEqual(host.environment_writes(), [])
        self.assertEqual(host.environment, environment(None, REVIEWER))

    # -- what the environment carries survives -----------------------------------------------

    def test_a_reviewer_survives_a_policy_that_had_to_be_written(self) -> None:
        """The case a PUT can ruin: an environment somebody armed with a
        reviewer and never limited. The host drops whatever a PUT omits."""
        for policy in (None, PROTECTED):
            with self.subTest(policy=policy):
                host = EnvironmentHost(environment(policy, REVIEWER, TIMER, bypass=False))
                code, plan = self.run_protect(host, gate="reviewer")
                self.assertEqual(code, 0, plan)
                sent = {"deployment_branch_policy": CUSTOM, "wait_timer": 30, "prevent_self_review": True,
                        "reviewers": [{"type": "User", "id": 4242}, {"type": "Team", "id": 99}],
                        "can_admins_bypass": False}
                self.assertEqual(plan["data"]["environment"]["writes"][0],
                                 {"method": "PUT", "endpoint": ENVIRONMENT, "body": sent})
                self.assertEqual(plan["data"]["environment"]["policy"],
                                 "protected-branches" if policy else "all")
                code, result = self.run_protect(host, "--apply", gate="reviewer")
                self.assertEqual(code, 0, result)
                self.assertEqual(host.environment_writes(), [("PUT", ENVIRONMENT, sent), ("POST", POLICIES, TAG)])
                held = {rule["type"]: rule for rule in host.environment["protection_rules"]}
                self.assertEqual([entry["reviewer"]["id"] for entry in held["required_reviewers"]["reviewers"]],
                                 [4242, 99])
                self.assertIs(held["required_reviewers"]["prevent_self_review"], True)
                self.assertEqual(held["wait_timer"]["wait_timer"], 30)
                self.assertIs(host.environment["can_admins_bypass"], False)
                self.assertEqual(result["data"]["environment"]["before"]["holds"]["reviewers"][0]["name"],
                                 "the-approver")

    def test_a_limit_to_protected_branches_is_said_for_what_it_is_and_for_what_replaces_it(self) -> None:
        _, text = self.run_protect(EnvironmentHost(environment(PROTECTED)), text=True)
        self.assertIn("now      environment release of o/r admits protected branches only, and no tag", text)
        self.assertIn("then     limited to tags v* and nothing else: the PUT replaces the limit to protected"
                      " branches, it does not add to it", text)
        self.assertNotIn("any branch", text)
        self.assertNotIn("no deployment policy", text)
        _, open_text = self.run_protect(EnvironmentHost(environment(None)), text=True)
        self.assertIn("has no deployment policy: every branch and every tag is admitted", open_text)
        self.assertIn("the PUT replaces the absence of a limit", open_text)

    def test_the_same_reviewers_in_another_order_are_the_same_reviewers(self) -> None:
        host = EnvironmentHost(environment(None, REVIEWER), reorders=True)
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        self.assertEqual(code, 0, result)
        held = {rule["type"]: rule for rule in host.environment["protection_rules"]}
        self.assertEqual([entry["reviewer"]["id"] for entry in held["required_reviewers"]["reviewers"]], [99, 4242])
        self.assertTrue(result["data"]["environment"]["applied"])

    def test_an_environment_whose_policy_is_right_is_never_put(self) -> None:
        """A reviewer, custom policies, and no tag rule yet: the rule is the
        only write, and the environment itself is not sent at all."""
        host = EnvironmentHost(environment(CUSTOM, REVIEWER))
        before = copy.deepcopy(host.environment)
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        self.assertEqual(code, 0, result)
        self.assertEqual(host.environment_writes(), [("POST", POLICIES, TAG)])
        self.assertEqual(host.environment, before)

    def test_what_cannot_be_sent_back_is_not_put_over(self) -> None:
        nameless = {"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"login": "ghost"}}]}
        unknown = {"id": 9, "type": "a_rule_of_tomorrow"}
        # `prevent_self_review` with nobody to review: no member of the PUT
        # is known to carry a rule that names no one, and on this host the
        # PUT that left it out lost it.
        nobody = {"id": 7, "type": "required_reviewers", "prevent_self_review": True, "reviewers": []}
        for rule, named in ((nameless, "the reviewer ghost"), (unknown, "a_rule_of_tomorrow"),
                            (nobody, "a required_reviewers rule that names no reviewer")):
            with self.subTest(rule=named):
                host = EnvironmentHost(environment(None, rule))
                before = copy.deepcopy(host.environment)
                code, plan = self.run_protect(host)
                self.assertEqual(code, 2)
                self.assertIn(named, plan["data"]["environment"]["refused"][0])
                self.assertEqual(plan["data"]["environment"]["writes"], [])
                code, refused = self.run_protect(host, "--apply")
                self.assertEqual(code, 2)
                self.assertEqual(host.environment_writes(), [])
                self.assertEqual(host.environment, before)

    # -- the read-back, branch by branch --------------------------------------------------------------

    def failed(self, host, *named, gate="reviewer"):
        code, result = self.run_protect(host, "--apply", gate=gate)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "PROCESS_FAILED")
        for words in named:
            self.assertIn(words, result["error"]["message"])
        return result["error"]

    def test_a_protection_that_moved_under_the_write_stops_the_command(self) -> None:
        """What the re-read is for: a GitHub that drops what was sent back."""
        for forgotten, named in (("reviewers", "reviewers: "), ("wait_timer", "waitTimer: 30 -> 0")):
            with self.subTest(forgotten=forgotten):
                host = EnvironmentHost(environment(None, REVIEWER, TIMER), forgets=(forgotten,))
                error = self.failed(host, "changed beyond its deployment policy", named)
                # An error replaces the report, so it carries what each half
                # wrote and what each replaced: `before` is promised to the
                # terminal of every run that wrote.
                self.assertIn(f"The protection half had written: PATCH {CHECKS}. Before, it was "
                              '{"classic": {', error["hint"])
                self.assertIn('"required": ["check / check (linux)"]', error["hint"])
                self.assertIn(f"The environment half had written: PUT {ENVIRONMENT}. Before, it was ", error["hint"])
                self.assertIn('"name": "the-approver"', error["hint"])
                self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT"])

    def test_a_re_read_github_does_not_answer_confirms_nothing(self) -> None:
        def silent(host, kind, what, nth):
            if (kind, what) == ("write", f"PUT {ENVIRONMENT}"):
                host.unreadable = (ENVIRONMENT,)
        host = EnvironmentHost(environment(None, REVIEWER), hook=silent)
        self.failed(host, "GitHub did not answer its re-read")
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT"])

    def test_a_policy_that_does_not_read_back_as_sent_is_not_taken_for_written(self) -> None:
        host = EnvironmentHost(environment(None, REVIEWER), ignores_policy=True)
        self.failed(host, "does not read back with the deployment policy that was sent: null")
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT"])

    def test_a_list_github_does_not_give_back_confirms_nothing(self) -> None:
        def silent(host, kind, what, nth):
            if (kind, what) == ("write", f"PUT {ENVIRONMENT}"):
                host.unreadable = (LISTING,)
        host = EnvironmentHost(environment(None, REVIEWER), hook=silent)
        self.failed(host, "did not give the whole list of its policies")
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT"])

    def test_the_rule_is_read_back_like_any_other_write(self) -> None:
        def loses(host, kind, what, nth):
            if (kind, what) == ("write", f"POST {POLICIES}"):
                host.environment["protection_rules"] = [rule for rule in host.environment["protection_rules"]
                                                        if rule["type"] != "required_reviewers"]
        host = EnvironmentHost(environment(CUSTOM, REVIEWER), hook=loses)
        self.failed(host, "changed beyond its deployment policy", "reviewers: ")
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["POST"])

    def test_two_rules_read_back_are_a_failure_and_not_a_success(self) -> None:
        self.failed(EnvironmentHost(duplicates=True), "it holds 2 policies", gate="none")

    # -- the list, read immediately before the rule ---------------------------------------------

    def test_a_list_that_moved_since_the_plan_is_not_added_to(self) -> None:
        """The plan read an empty list; by the time of the POST somebody has
        put a branch in it. The rule is posted on a list read at that
        moment, on every path -- this one has no PUT before it."""
        def meddles(host, kind, what, nth):
            if (kind, what, nth) == ("read", LISTING, 2):
                host.policies.append(dict(MAIN))
        host = EnvironmentHost(environment(CUSTOM, REVIEWER), hook=meddles)
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        self.assertEqual(code, 2, result)
        armed = result["data"]["environment"]
        self.assertIn("also admits the branch main", armed["refused"][0])
        self.assertIn("not the one the plan read", armed["refused"][0])
        self.assertEqual(host.environment_writes(), [])
        self.assertEqual(host.policies, [MAIN])
        self.assertFalse(armed["applied"])

    def test_a_list_that_cannot_be_read_again_is_not_posted_on(self) -> None:
        def silent(host, kind, what, nth):
            if (kind, what, nth) == ("read", LISTING, 2):
                host.unreadable = (LISTING,)
        host = EnvironmentHost(environment(CUSTOM), hook=silent)
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2, result)
        self.assertIn("could not be read whole immediately before the tag rule was added",
                      result["data"]["environment"]["refused"][0])
        self.assertEqual(host.environment_writes(), [])

    def test_a_rule_somebody_added_meanwhile_is_not_posted_again(self) -> None:
        def helps(host, kind, what, nth):
            if (kind, what, nth) == ("read", LISTING, 2):
                host.policies.append({"id": 1, **TAG})
        host = EnvironmentHost(environment(CUSTOM), hook=helps)
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(host.environment_writes(), [])
        self.assertEqual(host.policies, [{"id": 1, **TAG}])

    def test_the_tag_rule_is_posted_on_the_list_as_it_reads_then(self) -> None:
        """A list GitHub kept from an earlier custom policy shows only once
        the PUT is done. The rule already in it is not posted again: this
        host would answer a second call with a second rule."""
        host = EnvironmentHost(environment(None), hidden=[{"id": 1, **TAG}])
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT"])
        self.assertEqual(host.policies, [{"id": 1, **TAG}])
        self.assertEqual(result["data"]["environment"]["written"], [f"PUT {ENVIRONMENT}"])

    def test_a_kept_list_github_gives_is_refused_before_any_write(self) -> None:
        """Where GitHub does list the policies of an environment that has no
        custom ones, a branch in that list is seen before the PUT that would
        make it the only rule -- tags could no longer deploy."""
        for policy in (None, PROTECTED):
            with self.subTest(policy=policy):
                host = EnvironmentHost(environment(policy, REVIEWER), hidden=[MAIN], lists_hidden=True)
                before = copy.deepcopy(host.environment)
                code, plan = self.run_protect(host, gate="reviewer")
                self.assertEqual(code, 2)
                self.assertIn("holds the branch main", plan["data"]["environment"]["refused"][0])
                self.assertIn(f"-X DELETE {POLICIES}/2", plan["data"]["environment"]["refused"][0])
                code, result = self.run_protect(host, "--apply", gate="reviewer")
                self.assertEqual(code, 2)
                self.assertEqual(host.environment_writes(), [])
                self.assertEqual((host.environment, host.hidden), (before, [MAIN]))

    def test_a_kept_list_github_hides_is_found_after_the_put_and_the_policy_put_back(self) -> None:
        """Measured: the listing answers 404 while the policy is not custom,
        so a kept list cannot be known before the PUT. Left custom, the
        branch in it would be the only rule. The policy goes back as read,
        with everything the environment carried, and the half is refused."""
        for policy in (None, PROTECTED):
            with self.subTest(policy=policy):
                host = EnvironmentHost(environment(policy, TIMER, REVIEWER, bypass=False), hidden=[MAIN])
                before = copy.deepcopy(host.environment)
                code, result = self.run_protect(host, "--apply", gate="reviewer")
                self.assertEqual(code, 2, result)
                armed = result["data"]["environment"]
                self.assertIn("also admits the branch main", armed["refused"][0])
                self.assertIn("put back as it was read", armed["refused"][0])
                self.assertFalse(armed["applied"])
                self.assertEqual([(method, body["deployment_branch_policy"])
                                  for method, _, body in host.environment_writes()],
                                 [("PUT", CUSTOM), ("PUT", policy)])
                self.assertEqual(host.environment_writes()[1][2]["reviewers"],
                                 [{"type": "User", "id": 4242}, {"type": "Team", "id": 99}])
                self.assertEqual(host.environment, before)
                self.assertEqual((host.policies, host.hidden), ([], [MAIN]))
                self.assertEqual(len(armed["written"]), 2)
                self.assertIn("put back as it was read", armed["written"][1])

    # -- the reviewer is not the socle's to choose ----------------------------------------------

    def test_a_declared_reviewer_is_named_as_still_to_be_set_and_never_picked(self) -> None:
        host = EnvironmentHost()
        code, plan = self.run_protect(host, gate="reviewer")
        self.assertEqual(code, 2, "a promise nothing keeps yet")
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        self.assertEqual(code, 2, result)
        armed = result["data"]["environment"]
        self.assertTrue(armed["applied"])
        self.assertEqual([line["status"] for line in armed["gate"]], ["fail"])
        for report in (plan["data"]["environment"], armed):
            self.assertIn("requires no reviewer", report["gate"][0]["message"])
            self.assertIn("this repository's to choose", report["gate"][0]["message"])
        for _, _, body in host.environment_writes():
            self.assertNotIn("reviewers", body)
        self.assertEqual(host.environment["protection_rules"], [{"id": 1, "type": "branch_policy"}])

    def remedy(self, message):
        """The body of the PUT a gate line prints, with ID as the line says to replace it."""
        self.assertIn("replace ID by the reviewer's numeric id", message)
        self.assertIn("gh api users/LOGIN --jq .id", message)
        printed = re.search(r"gh api -X PUT (\S+) --input - <<<'([^']+)'", message)
        self.assertEqual(printed.group(1), ENVIRONMENT)
        return json.loads(printed.group(2).replace('"id":ID', '"id":4242'))

    def test_the_line_that_names_a_reviewer_erases_nothing_the_environment_holds(self) -> None:
        """The line `preflight`, `cut` and `protect` print was a PUT of the
        reviewer, then of the reviewer and the policy: on this host the
        second still lost a wait timer and reset the bypass. It carries
        everything that was read, or it is not printed."""
        host = EnvironmentHost(environment(CUSTOM, TIMER, bypass=False), [{"id": 1, **TAG}])
        code, plan = self.run_protect(host, gate="reviewer")
        self.assertEqual(code, 2)
        body = self.remedy(plan["data"]["environment"]["gate"][0]["message"])
        with tempfile.TemporaryDirectory() as directory:
            payload = pathlib.Path(directory) / "line.json"
            payload.write_text(json.dumps(body))
            host.write("PUT", ENVIRONMENT, payload)
        held = {rule["type"]: rule for rule in host.environment["protection_rules"]}
        self.assertEqual(held["wait_timer"]["wait_timer"], 30)
        self.assertIs(host.environment["can_admins_bypass"], False)
        self.assertEqual(host.environment["deployment_branch_policy"], CUSTOM)
        self.assertEqual(host.policies, [{"id": 1, **TAG}])
        self.assertEqual([entry["reviewer"]["id"] for entry in held["required_reviewers"]["reviewers"]], [4242])
        # And the same reading through the verdict `preflight` and `cut` print.
        with using_host(host):
            host.environment = environment(PROTECTED, TIMER)
            said = MODULE.environment_gate("o/r", host.environment, "reviewer")[0][1]
        self.assertEqual(self.remedy(said), {"reviewers": [{"type": "User", "id": 4242}],
                                             "deployment_branch_policy": PROTECTED, "wait_timer": 30})

    def test_the_line_after_a_creation_carries_the_policy_just_written(self) -> None:
        host = EnvironmentHost()
        code, result = self.run_protect(host, "--apply", gate="reviewer")
        body = self.remedy(result["data"]["environment"]["gate"][0]["message"])
        self.assertEqual(body, {"reviewers": [{"type": "User", "id": 4242}], "deployment_branch_policy": CUSTOM})
        _, plan = self.run_protect(EnvironmentHost(), gate="reviewer")
        self.assertEqual(self.remedy(plan["data"]["environment"]["gate"][0]["message"]), body)

    def test_no_line_is_printed_for_what_no_line_can_carry(self) -> None:
        unknown = {"id": 9, "type": "a_rule_of_tomorrow"}
        said = MODULE.environment_gate("o/r", environment(CUSTOM, unknown), "reviewer")[0][1]
        self.assertNotIn("gh api -X PUT", said)
        self.assertIn("https://github.com/o/r/settings/environments", said)
        self.assertIn("a protection rule of type a_rule_of_tomorrow", said)

    def test_an_undeclared_gate_is_a_note_and_a_declared_none_is_silent_before_it_exists(self) -> None:
        code, undeclared = self.run_protect(EnvironmentHost(), gate="")
        self.assertEqual(code, 0)
        self.assertEqual([line["status"] for line in undeclared["data"]["environment"]["gate"]], ["note"])
        code, none = self.run_protect(EnvironmentHost(), gate="none")
        self.assertEqual(none["data"]["environment"]["gate"], [])

    def test_the_gate_is_read_by_the_parser_preflight_reads_it_with(self) -> None:
        for gate in ("reviewer", "none", ""):
            with self.subTest(gate=gate), released_project(gate) as project:
                self.assertEqual(MODULE.protect.declared_gate(project), gate)
                self.assertEqual(MODULE.protect.declared_gate(project),
                                 MODULE.read_declarations(project, "maelys-fixture", "maelys-release").gate)
        with released_project("none") as project:
            # A section the grammar refuses is not a gate declared in passing:
            # the scan this replaced answered `none` here.
            (project / "maelys-release.conf").write_text("[gate]\nnone\nreviewer\n")
            self.assertEqual(MODULE.protect.declared_gate(project), "")
        source = (ROOT / "bin" / "maelys_socle" / "protect.py").read_text(encoding="utf-8")
        self.assertNotIn("declared_word", source)

    # -- two halves, and neither waits for the other -----------------------------------------------

    def test_a_refused_protection_does_not_stop_the_environment(self) -> None:
        """Coupled, a repository whose protection this command will not
        write could never arm its environment with it."""
        host = EnvironmentHost(branch=Branch(["check / check (linux)", "a check nothing produces"]))
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2, result)
        data = result["data"]
        self.assertIn("would stop requiring a check nothing produces", data["refused"]["message"])
        self.assertIn("--allow-narrow", data["refused"]["hint"])
        self.assertFalse(data["applied"])
        self.assertEqual(host.protection_writes(), [])
        self.assertTrue(data["environment"]["applied"])
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT", "POST"])
        self.assertEqual(host.policies, [{"id": 100, **TAG}])
        _, text = self.run_protect(EnvironmentHost(branch=Branch(["check / check (linux)", "a check nothing produces"])),
                                   "--apply", text=True)
        self.assertIn("REFUSED  this write would stop requiring a check nothing produces", text)
        self.assertIn("environment: applied", text)
        self.assertIn("protect: refused; the protection is not written", text)

    def test_a_refused_environment_does_not_stop_the_protection(self) -> None:
        host = EnvironmentHost(environment(), [{"id": 1, **TAG}, MAIN])
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2, result)
        self.assertTrue(result["data"]["applied"])
        self.assertNotIn("refused", result["data"])
        self.assertEqual(host.protection_writes(), [("PATCH", CHECKS)])
        self.assertEqual(host.branch.protection.body["required_status_checks"]["contexts"], ALL_LEGS)
        self.assertEqual(host.environment_writes(), [])
        self.assertTrue(result["data"]["environment"]["refused"])

    def test_a_branch_protected_by_a_ruleset_gets_its_environment(self) -> None:
        host = EnvironmentHost(branch=ProtectionHost(ruleset=fixtures.LegRenameTest.RULESET))
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["applied"])
        self.assertEqual(host.protection_writes(), [("PUT", "repos/o/r/rulesets/7")])
        self.assertTrue(result["data"]["environment"]["applied"])
        self.assertEqual([method for method, _, _ in host.environment_writes()], ["PUT", "POST"])

    def test_a_ruleset_of_the_organisation_is_refused_and_the_environment_is_armed(self) -> None:
        """The dead end the coupling made: no repository can write that
        ruleset, so the protection half always refuses there."""
        host = EnvironmentHost(branch=OrganisationRuleset(ruleset=fixtures.LegRenameTest.RULESET))
        code, result = self.run_protect(host, "--apply")
        self.assertEqual(code, 2, result)
        self.assertIn("ruleset of the", result["data"]["refused"]["message"])
        self.assertEqual(host.protection_writes(), [])
        self.assertTrue(result["data"]["environment"]["applied"])
        self.assertEqual(host.policies, [{"id": 100, **TAG}])

    def test_a_protection_that_failed_says_what_the_environment_wrote(self) -> None:
        """The protection is written first and read back; when that fails,
        the environment half still runs, and the error carries both."""
        host = EnvironmentHost(branch=Branch(drift={"required_linear_history": {"enabled": False}}))
        error = self.failed(host, "changed beyond what the plan said", gate="none")
        self.assertIn(f"The protection half had written: PATCH {CHECKS}. Before, it was " '{"classic": {',
                      error["hint"])
        self.assertIn(f"The environment half had written: PUT {ENVIRONMENT}; POST {POLICIES}. Before, it was ",
                      error["hint"])
        self.assertEqual(host.policies, [{"id": 100, **TAG}])

    def test_without_legs_is_the_narrowing_of_a_rename_and_nothing_else(self) -> None:
        """It used to read the environment, create and arm one on its way,
        and be refused by a policy beside the tag rule."""
        for held, policies in ((None, []), (environment(), [{"id": 1, **TAG}, MAIN])):
            for options in (("--without-legs",), ("--without-legs", "--apply")):
                with self.subTest(exists=bool(held), options=options):
                    host = EnvironmentHost(held, policies, branch=Branch([*ALL_LEGS, "mine"]))
                    code, result = self.run_protect(host, *options)
                    self.assertEqual(code, 0, result)
                    self.assertNotIn("environment", result["data"])
                    self.assertEqual([path for path in host.reads if "/environments" in path], [])
                    self.assertEqual(host.environment_writes(), [])
                    self.assertEqual(host.environment, held)
                    if "--apply" in options:
                        self.assertEqual(host.branch.protection.body["required_status_checks"]["contexts"], ["mine"])

    # -- only a product the socle releases --------------------------------------------------------

    def test_a_product_the_socle_does_not_release_is_not_read_nor_written(self) -> None:
        host = EnvironmentHost()
        code, result = self.run_protect(host, "--apply", released=False)
        self.assertEqual(code, 0, result)
        self.assertNotIn("environment", result["data"])
        self.assertEqual([path for path in host.reads if "/environments" in path], [])
        self.assertEqual(host.environment_writes(), [])
        self.assertIsNone(host.environment)

    # -- one reader --------------------------------------------------------------------------------

    def test_one_reader_serves_the_command_that_verifies_and_the_one_that_writes(self) -> None:
        package = ROOT / "bin" / "maelys_socle"
        spelled = [path.name for path in sorted(package.glob("*.py"))
                   if "/environments" in path.read_text(encoding="utf-8")]
        self.assertEqual(spelled, ["github.py"], "the environment's endpoints are spelled where they are read")
        for name in ("environment.py", "release_checks.py"):
            source = (package / name).read_text(encoding="utf-8")
            self.assertIn("read_release_environment(", source)
            self.assertNotIn("github_read(", source, f"{name} reads the environment through the one reader")
        self.assertNotIn("read_release_environment(", (package / "protect.py").read_text(encoding="utf-8"))
        # And one loop over the list, for an environment with custom
        # policies and for one without.
        self.assertEqual((package / "github.py").read_text(encoding="utf-8").count('.get("branch_policies")'), 1)

    def test_the_remedy_of_a_missing_environment_is_the_command_on_the_directory_given(self) -> None:
        with using_host(FakeHost({})):
            for held, state in ((None, "absent"), ({"deployment_branch_policy": None}, "ok"),
                                ({"deployment_branch_policy": PROTECTED}, "ok")):
                reading = MODULE.github.release_environment("o/r", held, state)
                found = MODULE.policy_verdicts(reading, "/where/the/product/is")
                self.assertEqual([status for status, _ in found], ["fail"])
                self.assertIn("'maelys-release protect /where/the/product/is --apply'", found[0][1])
                self.assertNotIn("gh api", found[0][1])
                self.assertNotIn("protect . ", found[0][1])
            # A limit to protected branches is not "no deployment policy".
            self.assertIn("limits deployments to protected branches, not to tags v*", found[0][1])
            self.assertNotIn("any branch", found[0][1])


if __name__ == "__main__":
    unittest.main()
