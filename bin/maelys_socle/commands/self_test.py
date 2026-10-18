# SPDX-License-Identifier: MPL-2.0
"""`self-test`: the socle's own tests, one class per process.
"""
from __future__ import annotations

import os
import sys

from maelys_cli import EXIT_OK, Failure, Invocation
from .. import host
from ..context import Context


def handle_self_test(invocation: Invocation, context: Context) -> tuple[dict, int]:
    tests = context.socle_root() / "tests"
    if not tests.is_dir():
        raise Failure("NOT_FOUND", f"{tests} is not part of this installation.", "Run from a maelys-release checkout.")
    # One class per process, several at a time: the suite builds a
    # repository for almost every test and took nine minutes one after the
    # other, on a machine with processors to spare.
    jobs = str(invocation.option("--jobs", "")) or str(os.cpu_count() or 1)
    if not jobs.isdigit() or int(jobs) < 1:
        raise Failure("VALIDATION_FAILED", f"--jobs takes a number of processes, 1 or more, not {jobs!r}.",
                      "Leave it out to run as many classes at a time as this machine has processors.")
    command = [sys.executable, "-W", "error", str(tests / "side_by_side.py"), "--jobs", jobs]
    # --verbose is also a global option, which the framework reads first:
    # asked of the command alone, it was never passed on and no run
    # reported its tests.
    if invocation.flag("--verbose") or invocation.verbose:
        command.append("--verbose")
    log = sys.stderr if invocation.format != "text" else sys.stdout
    log.flush()
    with host.HOST.self_test_environment() as environment:
        completed = host.HOST.stream(command, log, env=environment)
    if completed.returncode != 0:
        raise Failure("PROCESS_FAILED", f"The self-test failed with exit {completed.returncode}.", "Read the report above.")
    return {"tests": str(tests), "passed": True}, EXIT_OK
