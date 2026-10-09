# SPDX-License-Identifier: MPL-2.0
"""The release runs the verification it tells a user to run, before it publishes.

A reader outside the fleet tried `gh release verify` on maelys-system
v0.12.2 and was told "no attestations for tag". The attestations were there
-- one per file -- and the command that reads them was written in every
product's RELEASING.md; nobody could say it answered, because nothing had
ever run it from one end to the other.
"""
from __future__ import annotations

import re
import unittest

from test_maelys_release import MODULE, ROOT
from maelys_socle import workflows as rules

RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def steps(job: str) -> list:
    """(name, text) of each step of `job`, in the order they run."""
    body = rules.workflow_jobs(RELEASE)[job]
    found = re.split(r"^      - (?=name: |uses: )", body, flags=re.M)[1:]
    return [(re.match(r"(?:name: )?(.*)", text).group(1).strip(), text) for text in found]


class ReleaseVerifiesProvenanceTest(unittest.TestCase):
    def step(self) -> str:
        return dict(steps("publish"))["Verify the provenance with the command a user is given"]

    def test_it_runs_before_the_release_is_published(self) -> None:
        names = [name for name, _ in steps("publish")]
        self.assertLess(names.index("Assemble and verify"),
                        names.index("Verify the provenance with the command a user is given"))
        self.assertLess(names.index("Verify the provenance with the command a user is given"),
                        names.index("Publish GitHub release"))

    def test_it_runs_wherever_the_build_attested_and_nowhere_else(self) -> None:
        """The same condition as the step that writes the attestation, less
        the matrix: a private repository has none to verify."""
        attested = re.search(r"- name: Provenance attestation\n        id: provenance\n        if: (.*)\n", RELEASE)
        verified = re.search(r"^        if: (.*)$", self.step(), re.M)
        self.assertTrue(attested and verified)
        self.assertEqual("matrix.package && (" + verified.group(1) + ")", attested.group(1))

    def test_it_is_the_command_releasing_md_gives(self) -> None:
        template = (ROOT / "share" / "templates" / "RELEASING.md").read_text(encoding="utf-8")
        given = re.search(r"`(gh attestation verify ASSET --repo\s+OWNER/REPO --signer-repo (\S+))`", template)
        self.assertTrue(given, "RELEASING.md no longer gives the command this step runs")
        self.assertEqual(given.group(2), MODULE.SOCLE_REPOSITORY)
        self.assertIn('gh attestation verify "dist/$name" --repo "${GITHUB_REPOSITORY}" --signer-repo "$SOCLE"',
                      self.step())
        # The signer is read from the job, never spelled: a fork of the
        # socle signs as itself.
        self.assertIn("SOCLE: ${{ job.workflow_repository }}", self.step())

    def test_every_file_of_the_manifest_is_verified_and_none_is_not_an_answer(self) -> None:
        self.assertIn("done <dist/SHA256SUMS", self.step())
        self.assertIn('test "$verified" -gt 0', self.step())

    def test_the_job_may_read_attestations_and_asks_its_caller_for_nothing_new(self) -> None:
        body = rules.workflow_jobs(RELEASE)["publish"]
        self.assertEqual(rules.permission_block(body),
                         {"contents": "write", "attestations": "read"})
        needs, _ = rules.called_workflow_needs(RELEASE)
        self.assertEqual(needs["attestations"], "write")     # the build job's, which every caller already grants
