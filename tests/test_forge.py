# SPDX-License-Identifier: MPL-2.0
"""The forge is one object, reached through the host, and GitHub is the one the socle has.

What names the forge -- its hosts, how a repository is read from a remote,
where the fleet is cloned from, the commands of its CLI that are not a GET
of its API -- stands in one module, and nothing else in the socle names
github.com. Every answer of the forge is a call on the host, so that a fake,
a recording and a replay see what they saw before the object existed.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_maelys_release as fixtures

MODULE = fixtures.MODULE
# The script imports the function `forge`; the module is the package's.
FORGE = sys.modules["maelys_socle.forge"]
CHECKOUTS = sys.modules["maelys_socle.checkouts"]
ROOT = fixtures.ROOT
SOCLE = ROOT / "bin" / "maelys_socle"


class RecordingHost(MODULE.Host):
    """Answers every command with an empty success, and keeps what was asked."""

    def __init__(self, gh: bool = True) -> None:
        self.gh, self.commands, self.lookups = gh, [], []

    def which(self, name):
        self.lookups.append(name)
        return "/fake/gh" if name == "gh" and self.gh else None

    def run(self, command, cwd=None, env=None):
        self.commands.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "", "")


class GitHubForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.forge = FORGE.forge()
        self.project = pathlib.Path("/work/product")

    def test_the_forge_of_the_fleet_is_github(self) -> None:
        self.assertIsInstance(self.forge, FORGE.GitHub)
        self.assertEqual((self.forge.name, self.forge.hosts), ("GitHub", ("github.com", "www.github.com")))

    def test_a_repository_is_read_from_a_remote_as_it_was(self) -> None:
        """The reading `github_repository` made with its own pattern, now the forge's: the same answers."""
        for remote, expected in (
                ("https://github.com/maelys-dev/maelys-release.git", "maelys-dev/maelys-release"),
                ("https://github.com/maelys-dev/maelys-release", "maelys-dev/maelys-release"),
                ("git@github.com:maelys-dev/maelys-release.git", "maelys-dev/maelys-release"),
                ("ssh://git@github.com/o/r.git", "o/r"),
                ("https://www.github.com/o/r", "o/r"),
                ("https://github.com/o/r.git\n", "o/r"),
                ("https://gitlab.example/o/r.git", ""),
                ("file:///tmp/remotes/o/r.git", ""),
                ("", "")):
            with self.subTest(remote=remote):
                self.assertEqual(self.forge.repository(remote), expected)

    def test_the_forge_is_available_where_its_cli_is(self) -> None:
        for installed in (True, False):
            with fixtures.using_host(RecordingHost(gh=installed)) as host:
                self.assertEqual(self.forge.available(), installed)
                self.assertEqual(host.lookups, ["gh"])

    def test_the_clone_base_is_the_variable_the_managed_script_reads(self) -> None:
        with patch.dict(os.environ, {"MAELYS_GIT_BASE": "file:///tmp/remotes"}):
            self.assertEqual(self.forge.clone_base(), "file:///tmp/remotes")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAELYS_GIT_BASE", None)
            self.assertEqual(self.forge.clone_base(), "https://github.com/maelys-dev")
        # One accessor for every reader of it.
        with patch.dict(os.environ, {"MAELYS_GIT_BASE": "file:///tmp/elsewhere"}):
            self.assertEqual(CHECKOUTS.git_base(), "file:///tmp/elsewhere")

    def test_a_clone_url_carries_a_token_only_where_a_push_needs_one(self) -> None:
        self.assertEqual(self.forge.clone_url("maelys-dev/homebrew-tap"), "https://github.com/maelys-dev/homebrew-tap.git")
        self.assertEqual(self.forge.clone_url("maelys-dev/homebrew-tap", "tok"),
                         "https://x-access-token:tok@github.com/maelys-dev/homebrew-tap.git")

    def test_the_commands_of_the_cli_are_the_ones_the_host_ran_before(self) -> None:
        """Argument for argument: a recorded reference and a scripted host read the same calls."""
        body = pathlib.Path("/tmp/body.md")
        with fixtures.using_host(RecordingHost()) as host:
            self.forge.pull_requests(self.project, "o/r", "release/v1.2.3")
            self.forge.open_pull_request(self.project, "o/r", "main", "release/v1.2.3", "product 1.2.3", body)
            self.forge.view_pull_request(self.project, "o/r", "release/v1.2.3")
            self.forge.download_release(self.project, "o/r", "v1.2.3", pathlib.Path("/tmp/dist"))
        self.assertEqual(host.commands, [
            (["gh", "pr", "list", "--repo", "o/r", "--head", "release/v1.2.3", "--state", "all",
              "--limit", "10", "--json", "number,url,state,mergeCommit,headRefOid"], self.project),
            (["gh", "pr", "create", "--repo", "o/r", "--base", "main", "--head", "release/v1.2.3",
              "--title", "product 1.2.3", "--body-file", "/tmp/body.md"], self.project),
            (["gh", "pr", "view", "release/v1.2.3", "--repo", "o/r", "--json", "number,url,state"], self.project),
            (["gh", "release", "download", "v1.2.3", "--repo", "o/r", "--dir", "/tmp/dist"], self.project),
        ])


class OneForgeTest(unittest.TestCase):
    """Nothing but the forge and GitHub's own module names the forge."""

    SOURCES = [ROOT / "bin" / "maelys-release", *sorted(SOCLE.rglob("*.py"))]

    def test_nothing_else_names_github_com(self) -> None:
        # What is left names no host a repository is reached at: an address
        # git writes for an account, and a comment. A new one is a reading
        # or a URL that belongs in the forge.
        allowed = {"forge.py": None, "github.py": None,
                   "authorship.py": 1, "checkouts.py": 1, "privacy.py": 1}
        found = {}
        for path in self.SOURCES:
            count = path.read_text(encoding="utf-8").count("github.com")
            if count:
                found[path.name] = count
        for name, count in found.items():
            with self.subTest(file=name):
                self.assertIn(name, allowed)
                if allowed[name] is not None:
                    self.assertEqual(count, allowed[name])

    def test_the_clone_base_is_read_in_one_place(self) -> None:
        for path in self.SOURCES:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                if path.name == "forge.py":
                    self.assertIn('os.environ.get("MAELYS_GIT_BASE"', text)
                else:
                    self.assertNotIn('"MAELYS_GIT_BASE"', text)

    def test_only_the_host_and_the_forge_run_the_cli_of_the_forge(self) -> None:
        for path in self.SOURCES:
            if path.name in ("host.py", "forge.py"):
                continue
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant):
                        self.assertNotEqual(node.elts[0].value, "gh", ast.unparse(node))

    def test_the_forge_starts_no_process_of_its_own(self) -> None:
        tree = ast.parse((SOCLE / "forge.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(ast.unparse(node.func.value), ("subprocess", "shutil", "os"), ast.unparse(node))


if __name__ == "__main__":
    unittest.main()
