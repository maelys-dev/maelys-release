# SPDX-License-Identifier: MPL-2.0
"""The socle obeys, on its own workflows, the rule it writes on a product's.

A called workflow never receives more than its caller grants, and `secrets:
inherit` forwards only the secrets whose names already match what it
declares. The socle writes both on every product that calls its reusable
workflows. On 2026-09-25 neither held for its own `formula.yml`: the run of
v0.62.0 failed before a single job started, and the two runs after it
rendered a formula with no credentials to push it with. Three defects of
that class in one day, none of them visible to any test, because the
socle's own workflows are not written by the code that writes a product's.

This reads the requirement from the called workflow and applies it to
whoever calls it -- the socle's own files and what `adopt` writes, by the
same measure.
"""
from __future__ import annotations

import pathlib
import re
import unittest

from test_maelys_release import MODULE, ROOT, Product
# The entry script puts its own package on the path; the rule lives there.
from maelys_socle import workflows as rules

WORKFLOWS = ROOT / ".github" / "workflows"
LOCAL_CALL = re.compile(r"^\s*uses:\s*\./\.github/workflows/([A-Za-z0-9_-]+\.yml)\s*$", re.M)
SOCLE_CALL = re.compile(r"^\s*uses:\s*" + re.escape(MODULE.SOCLE_REPOSITORY)
                        + r"/\.github/workflows/([A-Za-z0-9_-]+\.yml)@", re.M)


def callers(text: str, pattern: re.Pattern) -> list:
    """(job, called workflow) of every job of `text` that calls one of ours."""
    found = []
    for job, body in rules.workflow_jobs(text).items():
        called = pattern.search(body)
        if called:
            found.append((job, called.group(1)))
    return found


def faults_of(caller: str, job: str, called: str) -> list:
    needs, secrets = rules.called_workflow_needs((WORKFLOWS / called).read_text(encoding="utf-8"))
    return rules.caller_faults(caller, job, needs, secrets)


class CallerRequirementTest(unittest.TestCase):
    def test_the_requirement_is_read_from_the_called_workflow(self) -> None:
        needs, secrets = rules.called_workflow_needs((WORKFLOWS / "tap.yml").read_text(encoding="utf-8"))
        # Its bottle job attests; its other two jobs keep the workflow's read.
        self.assertEqual(needs, {"contents": "write", "id-token": "write", "attestations": "write"})
        self.assertEqual(secrets, ["tap_token", "tap_signing_key"])

    def test_every_workflow_of_the_socle_that_calls_another_grants_what_it_asks(self) -> None:
        seen = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for job, called in callers(text, LOCAL_CALL):
                seen.append(f"{path.name}:{job} -> {called}")
                self.assertEqual(faults_of(text, job, called), [], f"{path.name}:{job}")
        # The socle calls its own tap.yml to publish its formula; a rule that
        # measured nothing would pass this file in silence.
        self.assertIn("formula.yml:formula -> tap.yml", seen)

    def test_the_shape_v0_62_0_shipped_is_refused(self) -> None:
        """Without this the rule proves nothing: it is the two defects of
        2026-09-25, put back in the file they were in."""
        text = (WORKFLOWS / "formula.yml").read_text(encoding="utf-8")
        broken = text.replace("    permissions:\n      contents: write\n"
                              "      id-token: write\n      attestations: write\n",
                              "    permissions:\n      contents: read\n")
        broken = re.sub(r"    secrets:\n      tap_token:.*\n      tap_signing_key:.*\n",
                        "    secrets: inherit\n", broken)
        self.assertNotEqual(broken, text)
        faults = faults_of(broken, "formula", "tap.yml")
        self.assertEqual(len(faults), 5, faults)
        for wanted in ("contents: write", "id-token: write", "attestations: write",
                       "secrets: inherit", "tap_signing_key"):
            self.assertTrue(any(wanted in fault for fault in faults), (wanted, faults))

    def test_what_adopt_writes_for_a_product_is_held_to_the_same_rule(self) -> None:
        """The other end of the measure: the caller the socle generates."""
        product = Product()
        self.addCleanup(product.close)
        product.write("maelys-release.conf", "[channels]\nnpm github-packages\n\n[dependencies]\napart\n")
        product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        product.run("adopt", str(product.dir), "--apply")
        text = product.read(".github/workflows/release.yml")
        jobs = callers(text, SOCLE_CALL)
        called = sorted({name for _, name in jobs})
        self.assertEqual(called, ["channel.yml", "release.yml", "tap.yml"], jobs)
        for job, name in jobs:
            self.assertEqual(faults_of(text, job, name), [], f"{job} -> {name}")


class HeldToWhatItWritesTest(unittest.TestCase):
    """Three more rules the socle keeps for a product, read on its own files.

    None of these is broken today; that is the point. The three defects of
    2026-09-25 were all rules the socle held for nine repositories and not
    for itself, and each was found by a release failing rather than by a
    test. What can be read from the files can be held before the release.
    """

    def workflows(self) -> dict:
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))}

    def test_every_action_is_pinned_by_commit(self) -> None:
        """What the socle writes into every product's workflows: a version
        tag moves under a repository, a commit does not."""
        read = 0
        for name, text in self.workflows().items():
            for number, line in enumerate(text.splitlines(), 1):
                # `- uses:` on a step, `uses:` on a job that calls a
                # workflow: a pattern that read only the second inspected
                # nothing at all and passed on an `actions/checkout@v7`
                # put there to catch it.
                used = re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)", line)
                if not used or used.group(1).startswith("./"):
                    continue
                read += 1
                self.assertRegex(used.group(1), r"@[0-9a-f]{40}$", f"{name}:{number}")
        self.assertGreater(read, 10, "this rule must read the uses lines, not miss them")

    def test_an_action_is_pinned_at_one_commit_across_the_workflows(self) -> None:
        """Two workflows written a week apart pinned the same two actions
        four major versions apart, for a month: `release.yml` and
        `channel.yml` at v4 of the artifact actions, the six other files at
        v7 and v8. Nothing chose that; the later file was written from an
        older memory of the pins. One action, one commit."""
        commits: dict = {}
        for name, text in self.workflows().items():
            for used in re.findall(r"^\s*(?:-\s+)?uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})", text, re.M):
                commits.setdefault(used[0], {}).setdefault(used[1], []).append(name)
        self.assertGreater(len(commits), 3, "this rule must read the actions, not miss them")
        split = {action: {commit[:7]: sorted(set(files)) for commit, files in by.items()}
                 for action, by in commits.items() if len(by) > 1}
        self.assertEqual(split, {})

    def test_every_workflow_declares_its_permissions(self) -> None:
        """A workflow without one takes the repository's default, which the
        socle neither sets nor sees."""
        for name, text in self.workflows().items():
            self.assertTrue(rules.workflow_permissions(text), f"{name} declares no top-level permissions")

    def test_a_workflow_that_runs_on_a_tag_can_be_replayed_on_that_tag(self) -> None:
        """A failed publication is replayed on the tag it already has, never
        by moving one. The socle writes that on every product's release.yml;
        its own formula.yml had to have it added by hand after v0.62.0
        published nothing.
        """
        replayable = []
        for name, text in self.workflows().items():
            push = rules.sub_block(rules.top_block(text, "on"), "push")
            if not rules.block_list(push, "tags"):
                continue
            replayable.append(name)
            dispatch = rules.sub_block(rules.top_block(text, "on"), "workflow_dispatch")
            self.assertTrue(re.search(r"^\s+tag:\s*$", rules.sub_block(dispatch, "inputs"), re.M),
                            f"{name} runs on a tag and takes no tag to replay")
        self.assertEqual(replayable, ["formula.yml"], "the tag-driven workflows of this repository")

    def test_the_product_workflow_the_socle_writes_is_replayable_too(self) -> None:
        """The other end: what a product receives carries the same input."""
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        text = product.read(".github/workflows/release.yml")
        push = rules.sub_block(rules.top_block(text, "on"), "push")
        self.assertEqual(rules.block_list(push, "tags"), ["v*"])
        dispatch = rules.sub_block(rules.top_block(text, "on"), "workflow_dispatch")
        self.assertTrue(re.search(r"^\s+tag:\s*$", rules.sub_block(dispatch, "inputs"), re.M), text)


class NoJobHangsForSixHoursTest(unittest.TestCase):
    """Every job of the socle is bounded, because nobody else can bound it.

    GitHub refuses `timeout-minutes` on a job that calls a reusable workflow,
    so a product cannot cut a job of the socle that hangs: it has the default,
    six hours. maelys-egress watched three of its own jobs sit in
    `apt-get update` on a hosted runner, one for an hour until it was
    cancelled by hand, and found that not one job of these eight workflows
    carried a bound -- nor could its own `uses:` lines be given one.
    """

    def workflows(self) -> dict:
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))}

    def test_every_job_that_runs_on_a_runner_is_bounded(self) -> None:
        read = 0
        for name, text in self.workflows().items():
            for job, body in rules.workflow_jobs(text).items():
                bound = re.search(r"^    timeout-minutes: (\d+)\s*$", body, re.M)
                if re.search(r"^    uses:", body, re.M):
                    # The one place a bound cannot be written, which is the
                    # whole reason the called workflow must carry it.
                    self.assertIsNone(bound, f"{name}:{job} calls a workflow; GitHub refuses a bound there")
                    continue
                read += 1
                self.assertIsNotNone(bound, f"{name}:{job} has no timeout-minutes")
                # Far under the six hours it replaces, and not so tight that
                # the longest green build of the fleet (8 minutes) meets it.
                self.assertTrue(10 <= int(bound.group(1)) <= 60, f"{name}:{job} {bound.group(1)}")
        self.assertEqual(read, 16, "the jobs of this repository that run on a runner")

    def test_apt_get_update_is_cut_when_it_never_returns(self) -> None:
        """`update || warning` covers an update that fails. The one egress
        saw did not fail: it never returned, so the `||` was never reached."""
        read = 0
        for name, text in self.workflows().items():
            for number, line in enumerate(text.splitlines(), 1):
                if "apt-get update" not in line or line.lstrip().startswith("#"):
                    continue
                read += 1
                self.assertRegex(line, r"sudo timeout -k \d+ \d+ apt-get update \|\| ", f"{name}:{number}")
        self.assertEqual(read, 6, "the apt-get update steps of this repository")
