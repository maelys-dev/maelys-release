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
