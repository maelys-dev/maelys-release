# SPDX-License-Identifier: MPL-2.0
"""Tests of the assemble step of release.yml against artifacts on disk.

`build` runs once per target and hands each result to `publish` as its own
artifact. What reaches the release is whatever those artifacts hold under
the manifest's globs, so two targets that produce the same name produce one
release file and a SHA256SUMS that vouches for it: the manifest agrees with
itself and describes bytes one of the targets never built. The step's run
block is extracted from the workflow text and run against directories that
stand in for the downloaded artifacts. It needs bash and sha256sum.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
BASH = shutil.which("bash") or "bash"


def assemble_script() -> str:
    """The run block of the 'Assemble and verify' step, dedented."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Assemble and verify")
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


@unittest.skipUnless(shutil.which("sha256sum"), "sha256sum is not installed")
class AssembleStepTest(unittest.TestCase):
    """What the step does with the artifacts of more than one target.

    The guard is for a development machine, not for the fleet's runners:
    macOS carries sha256sum in /sbin, and these tests run on macos-15 as
    they do on Linux.
    """

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-assemble-test."))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.script = self.work / "assemble.sh"
        self.script.write_text(assemble_script(), encoding="utf-8")

    def target(self, name: str, packages: dict, extras: dict = {}) -> None:
        """An artifact of target NAME.

        `package_command` writes a .sha256 beside each package it publishes
        and nothing beside the rest: a digest for a file the manifest does
        not name would already fail the step's `sha256sum -c`, so PACKAGES
        get one and EXTRAS do not.
        """
        directory = self.work / "incoming" / f"dist-{name}"
        directory.mkdir(parents=True, exist_ok=True)
        for file_name, content in extras.items():
            (directory / file_name).write_text(content, encoding="utf-8")
        for file_name, content in packages.items():
            (directory / file_name).write_text(content, encoding="utf-8")
            digest = subprocess.run(["sha256sum", file_name], cwd=directory,
                                    capture_output=True, text=True, check=True).stdout
            (directory / f"{file_name}.sha256").write_text(digest, encoding="utf-8")

    def assemble(self, patterns: str = "*.tar.gz") -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script)], cwd=self.work, capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "MANIFEST_PATTERNS": patterns,
                                   "GH_TOKEN": "unused", "TAG": "v1.0.0", "PRODUCT": "maelys-fixture"})

    def manifest(self) -> list[str]:
        return (self.work / "dist" / "SHA256SUMS").read_text(encoding="utf-8").split()[1::2]

    def test_step_is_extracted(self) -> None:
        self.assertIn("MANIFEST_PATTERNS", assemble_script())
        self.assertIn("sha256sum", assemble_script())

    def test_refuses_the_same_name_with_different_bytes(self) -> None:
        """The defect this step exists to catch: one release file, two targets."""
        self.target("linux-x86_64", {"maelys-fixture-1.0.0.tar.gz": "x86 bytes"})
        self.target("linux-arm64", {"maelys-fixture-1.0.0.tar.gz": "arm bytes"})
        done = self.assemble()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("maelys-fixture-1.0.0.tar.gz is built by more than one target", done.stderr)
        # Naming the file is not enough to act on it: the message names the
        # artifact of each target, which is where the operator looks.
        self.assertIn("dist-linux-x86_64", done.stderr)
        self.assertIn("dist-linux-arm64", done.stderr)
        self.assertFalse((self.work / "dist" / "SHA256SUMS").exists())

    def test_accepts_the_same_name_with_the_same_bytes(self) -> None:
        """A package that does not depend on the architecture is not a collision."""
        self.target("linux-x86_64", {"maelys-fixture-1.0.0.tar.gz": "one archive"})
        self.target("linux-arm64", {"maelys-fixture-1.0.0.tar.gz": "one archive"})
        done = self.assemble()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.manifest(), ["maelys-fixture-1.0.0.tar.gz"])

    def test_keeps_every_target_when_the_names_differ(self) -> None:
        self.target("linux-x86_64", {"maelys-fixture-1.0.0-linux-x86_64.tar.gz": "x86"})
        self.target("macos-arm64", {"maelys-fixture-1.0.0-macos-arm64.tar.gz": "mac"})
        done = self.assemble()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.manifest(), ["maelys-fixture-1.0.0-linux-x86_64.tar.gz",
                                           "maelys-fixture-1.0.0-macos-arm64.tar.gz"])

    def test_ignores_a_collision_the_manifest_does_not_publish(self) -> None:
        """Only what reaches the release has to name one file."""
        self.target("linux-x86_64", {"maelys-fixture-1.0.0-linux-x86_64.tar.gz": "x86"},
                    extras={"build.log": "the x86 build"})
        self.target("macos-arm64", {"maelys-fixture-1.0.0-macos-arm64.tar.gz": "mac"},
                    extras={"build.log": "the mac build"})
        done = self.assemble()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.manifest(), ["maelys-fixture-1.0.0-linux-x86_64.tar.gz",
                                           "maelys-fixture-1.0.0-macos-arm64.tar.gz"])


if __name__ == "__main__":
    unittest.main()
