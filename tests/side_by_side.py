#!/usr/bin/env python3
"""Run the suite one test class per process, several at a time.

The suite is a thousand tests that each build a repository and run the
command in it: nine minutes one after the other, and nothing in them waits
on another. A class is the unit that is run apart, since a class may share
what its `setUpClass` builds and nothing is shared above it: no module has a
fixture of its own.

A class that fails is printed whole, as `unittest` printed it, and names the
line that runs it alone. The order of the report is the order the classes
were found in, whatever order they finished in.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import pathlib
import re
import subprocess
import sys
import time
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
SKIPPED = re.compile(r"\bskipped=(\d+)")


def classes() -> list[tuple[str, int]]:
    """Every test class of the suite with the number of its tests, in the order `discover` finds them.

    Counted by the tests' own names: a class one module imports from another
    is found in both, and is one class.
    """
    found: dict[str, set] = {}

    def walk(suite: unittest.TestSuite) -> None:
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            elif type(test).__module__ == "unittest.loader":
                # A module that does not import is a test that fails, and is
                # run where it stands: by its own name, in a process of its own.
                found.setdefault(test.id().rpartition(".")[2], set()).add(test.id())
            else:
                found.setdefault(f"{type(test).__module__}.{type(test).__qualname__}", set()).add(test.id())

    walk(unittest.defaultTestLoader.discover(str(TESTS)))
    return [(name, len(tests)) for name, tests in found.items()]


def run(name: str, verbose: bool) -> tuple[int, str, float]:
    command = [sys.executable, "-W", "error", "-m", "unittest", *(["-v"] if verbose else []), name]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(TESTS), *filter(None, [os.environ.get("PYTHONPATH")])])}
    started = time.monotonic()
    done = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)
    return done.returncode, done.stdout.decode("utf-8", "backslashreplace"), time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()
    if arguments.jobs < 1:
        parser.error("--jobs takes a number of processes, 1 or more")
    started = time.monotonic()
    found = classes()
    # The longest first, as far as a count of tests says which they are: the
    # last process to start should not be the one everything then waits for.
    queued = sorted(found, key=lambda entry: -entry[1])
    results: dict[str, tuple[int, str, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=arguments.jobs) as pool:
        pending = {pool.submit(run, name, arguments.verbose): name for name, _ in queued}
        for finished in concurrent.futures.as_completed(pending):
            results[pending[finished]] = finished.result()
            sys.stdout.write("." if results[pending[finished]][0] == 0 else "F")
            sys.stdout.flush()
    sys.stdout.write("\n")
    ran = skipped = 0
    failed = []
    for name, expected in found:
        code, output, _ = results[name]
        counted = RAN.search(output)
        ran += int(counted.group(1)) if counted else 0
        skipped += sum(int(number) for number in SKIPPED.findall(output))
        # A class whose process ran another number of tests than were found
        # in it did not run what was asked, whatever its exit says.
        if code != 0 or not counted or int(counted.group(1)) != expected:
            failed.append(name)
            print("=" * 70)
            print(f"FAILED: {name} (exit {code}) -- alone: python3 -W error -m unittest {name}, from tests/")
            print(output.rstrip())
        elif arguments.verbose:
            print(output.rstrip())
    print("-" * 70)
    print(f"Ran {ran} tests of {len(found)} classes in {time.monotonic() - started:.3f}s,"
          f" {arguments.jobs} at a time")
    print()
    if arguments.verbose:
        for name, _ in sorted(found, key=lambda entry: -results[entry[0]][2])[:10]:
            print(f"{results[name][2]:8.1f}s  {name}")
        print()
    if failed:
        print(f"FAILED (classes={len(failed)}" + (f", skipped={skipped}" if skipped else "") + ")")
        return 1
    print("OK" + (f" (skipped={skipped})" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
