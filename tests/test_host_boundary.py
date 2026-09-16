# SPDX-License-Identifier: MPL-2.0
"""The host is the sole process boundary, including inside self-test children."""
from __future__ import annotations

import ast
import base64
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from test_maelys_release import (CLI, MODULE, ROOT, ALL_LEGS, FakeHost, FakeProtection,
                                protection_host, using_host, workflow_project)


class HostBoundaryTest(unittest.TestCase):
    def test_api_writes_and_process_calls_stay_inside_host(self):
        source = CLI.read_text(encoding="utf-8")
        tree = ast.parse(source)
        host = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Host")
        outside = source.splitlines()[:host.lineno - 1] + source.splitlines()[host.end_lineno:]
        self.assertNotIn('"-X"', "\n".join(outside))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "subprocess" or (node.func.value.id, node.func.attr) in (
                        ("os", "execve"), ("shutil", "which")):
                    self.assertTrue(host.lineno <= node.lineno <= host.end_lineno, ast.unparse(node))

    def test_tests_replace_the_host_not_module_functions(self):
        tree = ast.parse((ROOT / "tests" / "test_maelys_release.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                expression = ast.unparse(node)
                if expression.startswith("MODULE."):
                    self.assertEqual(expression, "MODULE.HOST")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                self.assertNotEqual(ast.unparse(node.args[0]), "MODULE")

    def test_default_host_refuses_every_non_read_gh_command(self):
        self.assertTrue(os.environ.get("_MAELYS_RELEASE_SELF_TEST"))
        self.assertNotIn("_MAELYS_RELEASE_TEST_GH", os.environ)
        host = MODULE.Host()
        for command in (["gh", "pr", "create"], ["gh", "release", "create"], ["gh", "repo", "edit"],
                        ["gh", "api", "repos/o/r", "--field", "private=true"],
                        ["gh", "api", "--method", "DELETE", "repos/o/r"],
                        ["gh", "api", "graphql", "--raw-field", "query=mutation{}"]):
            with self.subTest(command=command), self.assertRaisesRegex(AssertionError, "GitHub write"):
                host.run(command)
            with self.assertRaisesRegex(AssertionError, "GitHub write"):
                host.stream(command, io.StringIO())
        for method in ("PUT", "PATCH", "POST", "DELETE"):
            with self.subTest(method=method), self.assertRaisesRegex(AssertionError, "GitHub write"):
                host.write(method, "repos/o/r", pathlib.Path("never-opened.json"))

    def test_default_host_guard_reaches_a_cli_subprocess(self):
        # Import exactly as a child CLI does, without the parent's installed
        # host. No gh executable need exist: refusal happens before execution.
        code = '''import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader("candidate", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
module.HOST.run(["gh", "pr", "create"])
'''
        completed = subprocess.run([sys.executable, "-c", code, str(CLI)],
                                   text=True, capture_output=True, check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("AssertionError: self-test refused a real GitHub write", completed.stderr)

    def test_a_missing_declared_fake_cannot_fall_back_to_an_installed_gh(self):
        with tempfile.TemporaryDirectory() as directory:
            # PATH has no executable. Even a read must fail before looking
            # elsewhere once a test explicitly selected its fake executable.
            env = {**os.environ, "PATH": directory, "_MAELYS_RELEASE_TEST_GH": str(pathlib.Path(directory) / "gh")}
            with self.assertRaisesRegex(AssertionError, "explicitly declared fake"):
                MODULE.Host().run(["gh", "api", "repos/o/r"], env=env)

    def test_network_pushes_are_refused_even_when_the_remote_is_named(self):
        with tempfile.TemporaryDirectory(prefix="maelys-host-") as directory:
            project = pathlib.Path(directory)
            host = MODULE.Host()
            self.assertEqual(host.run(["git", "init", "-q"], cwd=project).returncode, 0)
            host.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=project)
            for remote in ("origin", "https://github.com/o/r.git", "git@github.com:o/r.git"):
                with self.subTest(remote=remote), self.assertRaisesRegex(AssertionError, "non-local git push"):
                    host.run(["git", "push", "-q", remote, "HEAD:main"], cwd=project)

    def test_a_local_bare_push_is_still_permitted(self):
        with tempfile.TemporaryDirectory(prefix="maelys-host-") as directory:
            work = pathlib.Path(directory)
            origin, project = work / "origin.git", work / "product"
            origin.mkdir()
            project.mkdir()
            host = MODULE.Host()
            for command, cwd in ((["git", "init", "-q", "--bare"], origin),
                                 (["git", "init", "-q"], project),
                                 (["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                                   "-c", "commit.gpgsign=false", "commit", "-qm", "fixture", "--allow-empty"], project),
                                 (["git", "remote", "add", "origin", str(origin)], project),
                                 (["git", "push", "-q", "origin", "HEAD:main"], project)):
                completed = host.run(command, cwd=cwd)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(host.run(["git", "rev-parse", "refs/heads/main"], cwd=origin).stdout.strip())

    def test_public_wrappers_share_the_installed_host_and_restore_it(self):
        original = MODULE.HOST
        host = FakeHost({"object": ("ok", {"a": 1}), "array": ("ok", [1, 2])})
        with self.assertRaisesRegex(RuntimeError, "leave"):
            with using_host(host):
                self.assertEqual(MODULE.github_read("object"), ("ok", {"a": 1}))
                self.assertEqual(MODULE.github_api("object"), {"a": 1})
                self.assertEqual(MODULE.github_list("array"), [1, 2])
                self.assertEqual(MODULE.git("remote", "get-url", "origin"), "https://github.com/o/r.git")
                raise RuntimeError("leave")
        self.assertIs(MODULE.HOST, original)
        self.assertEqual(host.reads, ["object", "object", "array"])
        self.assertEqual(len(host.commands), 1)

    def test_api_write_keeps_method_payload_and_cwd(self):
        class RecordingHost(MODULE.Host):
            def run(self, command, cwd=None, env=None):
                self.command, self.cwd = command, cwd
                self.body = json.loads(pathlib.Path(command[-1]).read_text())
                return subprocess.CompletedProcess(command, 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            payload = project / "checks.json"
            body = {"strict": True, "contexts": ["check / check (linux)"]}
            payload.write_text(json.dumps(body))
            host = RecordingHost()
            for method in ("PATCH", "PUT"):
                host.write(method, "repos/o/r/branches/main/protection", payload, cwd=project)
                self.assertEqual(host.command, ["gh", "api", "-X", method,
                                               "repos/o/r/branches/main/protection", "--input", str(payload)])
                self.assertEqual(host.body, body)
                self.assertEqual(host.cwd, project)

    def test_protect_really_reads_withdrawn_before_writing(self):
        for answer in (("unreadable", None),
                       ("ok", {"encoding": "base64", "content": base64.b64encode(
                           f"{MODULE.socle_version()} protect unsafe\n".encode()).decode()})):
            fake = FakeProtection([])
            host = protection_host(fake.read(""), seen=ALL_LEGS, write=fake.write)
            original_read = host.reader
            host.reader = lambda path: answer if path.endswith("/WITHDRAWN") else original_read(path)
            with workflow_project() as project, using_host(host):
                invocation = type("I", (), {"operands": [str(project)], "format": "json",
                    "flag": lambda self, name: name == "--apply",
                    "option": lambda self, name, default="": default})()
                with self.assertRaises(MODULE.Failure):
                    MODULE.handle_protect(invocation)
            self.assertEqual(host.writes, [])
            self.assertIn(f"repos/{MODULE.SOCLE_REPOSITORY}/contents/WITHDRAWN", host.reads)

    def test_undeclared_fake_writes_fail_instead_of_running_gh(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = pathlib.Path(directory) / "body.json"
            payload.write_text("{}")
            with self.assertRaisesRegex(AssertionError, "undeclared GitHub write"):
                FakeHost().write("PUT", "repos/o/r", payload)
