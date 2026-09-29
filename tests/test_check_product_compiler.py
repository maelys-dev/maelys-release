# SPDX-License-Identifier: MPL-2.0
"""The compiler reaches the command, not its argument list.

`check-product.yml` used to append `CC=clang CXX=clang++` to the fuzz and
sanitizer commands. For `make` that is a command-line assignment, which is
what a Makefile assigning `CC` itself needs to be overridden. For anything
else it is two positional arguments, and a product's fuzzer was handed
`CC=clang` as an argv entry.

`MAKEFLAGS` says the same thing to make without writing a word on the
command line. These measure that: the workflow's own values, applied to a
real Makefile and to a real script.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import test_maelys_release  # noqa: F401  -- puts the socle's package on the path
from maelys_socle import workflows as rules

WORKFLOW = (pathlib.Path(__file__).resolve().parent.parent
            / ".github" / "workflows" / "check-product.yml")


def job_environment(body: str) -> dict:
    """The `env:` mapping of one job, as the file writes it."""
    found = {}
    for line in rules.sub_block(body, "env").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.+?)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


class CompilerOverrideTest(unittest.TestCase):
    JOBS = ("fuzz", "sanitizers")

    def setUp(self) -> None:
        self.jobs = rules.workflow_jobs(WORKFLOW.read_text(encoding="utf-8"))

    def test_the_line_is_run_as_written(self) -> None:
        """Nothing is appended to what the product declared."""
        for job, gate in zip(self.JOBS, ("fuzz_command", "sanitizer_command")):
            with self.subTest(job=job):
                run = re.findall(r"^\s*run: (\$\{\{ inputs\.\w+ \}\}.*)$", self.jobs[job], re.M)
                self.assertIn("${{ inputs." + gate + " }}", run)
                for line in run:
                    self.assertEqual(line.strip(), "${{ inputs." + gate + " }}", line)

    def test_both_jobs_carry_the_override_in_their_environment(self) -> None:
        for job in self.JOBS:
            with self.subTest(job=job):
                environment = job_environment(self.jobs[job])
                self.assertEqual(environment.get("CC"), "clang")
                self.assertEqual(environment.get("CXX"), "clang++")
                self.assertEqual(environment.get("MAKEFLAGS"), "CC=clang CXX=clang++")

    def environment(self, job: str) -> dict:
        return {**os.environ, **{key: value for key, value in job_environment(self.jobs[job]).items()}}

    @unittest.skipUnless(subprocess.run(["make", "-v"], capture_output=True, check=False).returncode == 0,
                         "make is required to measure what it honours")
    def test_a_makefile_that_assigns_cc_is_overridden(self) -> None:
        """The thing the appended words used to buy, kept.

        Measured, because it is the whole reason they were there: with the
        environment alone, a Makefile that assigns `CC` keeps its own value.
        """
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            (work / "Makefile").write_text("CC = gcc\nall:\n\t@echo \"$(CC) $(CXX)\"\n", encoding="utf-8")
            said = subprocess.run(["make"], cwd=work, env=self.environment("fuzz"),
                                  capture_output=True, text=True, check=True)
            self.assertEqual(said.stdout.strip(), "clang clang++")
            # And without MAKEFLAGS the Makefile wins, which is the defect
            # the appended words were hiding.
            bare = {key: value for key, value in self.environment("fuzz").items() if key != "MAKEFLAGS"}
            kept = subprocess.run(["make"], cwd=work, env=bare, capture_output=True, text=True, check=True)
            self.assertEqual(kept.stdout.strip(), "gcc clang++")

    def test_a_command_that_is_not_make_receives_no_arguments(self) -> None:
        """A fuzz_command naming a script was handed `CC=clang` as argv."""
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            script = work / "fuzz.sh"
            script.write_text("#!/bin/sh\necho \"args=$# CC=$CC\"\n", encoding="utf-8")
            script.chmod(0o755)
            # What the step runs is the input, verbatim; the test above holds
            # the workflow to that, and this is what the product then sees.
            said = subprocess.run([str(script)], cwd=work, env=self.environment("fuzz"),
                                  capture_output=True, text=True, check=True)
            self.assertEqual(said.stdout.strip(), "args=0 CC=clang")
