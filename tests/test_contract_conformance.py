# SPDX-License-Identifier: MPL-2.0
"""Conformance of bin/maelys-release to agent-cli/v2.

The contract lives in maelys-dev/agent-cli-spec; adapter/AGENT_CLI_SPEC_PIN
names the tag and commit this program is held to. The kit of that
repository drives the program from the outside; every one of its checks
must pass. AGENT_CLI_SPEC_DIR names a checkout to use; otherwise
../agent-cli-spec when it is at the pinned commit, otherwise a clone at
the pin.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "maelys-release"
PIN = ROOT / "adapter" / "AGENT_CLI_SPEC_PIN"


def spec_checkout() -> pathlib.Path:
    tag, commit = PIN.read_text(encoding="utf-8").split()[:2]
    named = os.environ.get("AGENT_CLI_SPEC_DIR")
    if named:
        return pathlib.Path(named)
    sibling = ROOT.parent / "agent-cli-spec"
    if (sibling / "conformance" / "run.py").is_file():
        head = subprocess.run(["git", "-C", str(sibling), "rev-parse", "HEAD"], check=False, text=True,
                              stdout=subprocess.PIPE).stdout.strip()
        if head == commit:
            return sibling
    clone = pathlib.Path(tempfile.gettempdir()) / f"agent-cli-spec-{commit[:12]}"
    if not (clone / "conformance" / "run.py").is_file():
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "fetch", "-q", "--depth", "1",
                        "https://github.com/maelys-dev/agent-cli-spec.git", commit], check=True)
        subprocess.run(["git", "-C", str(clone), "checkout", "-q", "FETCH_HEAD"], check=True)
    del tag
    return clone


class ConformanceTest(unittest.TestCase):
    def test_every_check_of_the_kit_passes(self) -> None:
        spec = spec_checkout()
        env = {**os.environ}
        env.pop("MAELYS_CLI_FORMAT", None)
        completed = subprocess.run([sys.executable, str(spec / "conformance" / "run.py"), str(CLI), "--json"],
                                   env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        report = json.loads(completed.stdout)
        failed = [f"{check['name']} ({check['detail']})" for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, [], "\n".join(failed))
        self.assertGreater(report["counts"]["passed"], 60)


if __name__ == "__main__":
    unittest.main()
