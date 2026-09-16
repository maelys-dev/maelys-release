# SPDX-License-Identifier: MPL-2.0
"""A behavioral reference cannot be refreshed by an ordinary replay."""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "tests" / "golden" / "capture.py"


class GoldenRecordingTest(unittest.TestCase):
    def refused(self, directory, *arguments):
        output = pathlib.Path(directory) / "capture"
        completed = subprocess.run([sys.executable, str(CAPTURE), str(output), *arguments],
                                   text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertFalse(output.exists())
        return completed.stderr

    def test_candidate_needs_an_explicit_record_request(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn("--candidate requires --record", self.refused(directory, "--candidate", "0" * 40))

    def test_candidate_must_name_the_exact_committed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            for invalid in ("", "HEAD", "a" * 39):
                with self.subTest(candidate=invalid):
                    self.assertIn("full 40-character commit", self.refused(
                        directory, "--record", "--candidate", invalid))
            self.assertIn("exact clean HEAD commit", self.refused(directory, "--record", "--candidate", "0" * 40))

    def test_a_correct_commit_does_not_authorize_dirty_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source"
            source.mkdir()
            def git(*arguments):
                return subprocess.run(["git", "-C", str(source.resolve()), "-c", "user.name=test",
                                       "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                                       *arguments], text=True, capture_output=True, check=True).stdout.strip()
            git("init", "-q")
            inputs = [source / name for name in ("bin/maelys-release", ".github/workflows/check-product.yml",
                                                "dependencies/maelys-cli.pin")]
            for path in inputs:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("committed\n")
            git("add", ".")
            git("commit", "-qm", "behavior fix")
            commit = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", commit)
            for path in inputs:
                with self.subTest(dirty=path.relative_to(source)):
                    path.write_text("uncommitted\n")
                    self.assertIn("exact clean HEAD commit", self.refused(
                        directory, "--source", str(source), "--record", "--candidate", commit))
                    self.assertIn("unchanged origin/main source", self.refused(
                        directory, "--source", str(source), "--record"))
                    path.write_text("committed\n")
