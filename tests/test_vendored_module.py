# SPDX-License-Identifier: MPL-2.0
"""bin/maelys_cli.py is python/maelys_cli.py of maelys-cli at the pinned commit, byte for byte.

Two levels: offline, the digest recorded on line 3 of adapter/MAELYS_CLI_PIN
matches the copy; online, the file at the pinned commit matches the copy
(../maelys-cli when it holds that commit, otherwise a depth-1 fetch).
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COPY = ROOT / "bin" / "maelys_cli.py"
PIN = ROOT / "adapter" / "MAELYS_CLI_PIN"


def pinned() -> tuple[str, str, str]:
    lines = PIN.read_text(encoding="utf-8").split()
    return lines[0], lines[1], lines[2].partition(":")[2]


def file_at_pin(commit: str) -> bytes:
    sibling = pathlib.Path(os.environ.get("MAELYS_CLI_DIR") or ROOT.parent / "maelys-cli")
    if (sibling / ".git").exists():
        shown = subprocess.run(["git", "-C", str(sibling), "show", f"{commit}:python/maelys_cli.py"],
                               check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if shown.returncode == 0:
            return shown.stdout
    with tempfile.TemporaryDirectory(prefix="maelys-cli-pin.") as temp:
        base = os.environ.get("MAELYS_GIT_BASE", "https://github.com/maelys-dev")
        subprocess.run(["git", "init", "-q", temp], check=True)
        subprocess.run(["git", "-C", temp, "fetch", "-q", "--depth", "1", f"{base}/maelys-cli.git", commit], check=True)
        return subprocess.run(["git", "-C", temp, "show", "FETCH_HEAD:python/maelys_cli.py"], check=True,
                              stdout=subprocess.PIPE).stdout


class VendoredModuleTest(unittest.TestCase):
    def test_pin_shape(self) -> None:
        tag, commit, digest = pinned()
        self.assertRegex(tag, r"^v[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_copy_matches_recorded_digest(self) -> None:
        _, _, digest = pinned()
        self.assertEqual(hashlib.sha256(COPY.read_bytes()).hexdigest(), digest,
                         "bin/maelys_cli.py differs from adapter/MAELYS_CLI_PIN; run maelys-release vendor")

    def test_copy_matches_the_pinned_commit(self) -> None:
        _, commit, _ = pinned()
        self.assertEqual(COPY.read_bytes(), file_at_pin(commit),
                         "bin/maelys_cli.py is not python/maelys_cli.py of maelys-cli at the pinned commit")

    def test_header_is_the_frameworks(self) -> None:
        head = COPY.read_text(encoding="utf-8").splitlines()[:2]
        self.assertEqual(head[0], "# SPDX-License-Identifier: MPL-2.0")
        self.assertTrue(head[1].startswith('"""maelys_cli:'))


if __name__ == "__main__":
    unittest.main()
