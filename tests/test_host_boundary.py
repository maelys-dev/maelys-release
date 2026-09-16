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
    def test_an_accidental_or_invalid_self_test_environment_fails_at_startup(self):
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("_MAELYS_RELEASE_SELF_TEST", "_MAELYS_RELEASE_SELF_TEST_FILE", "_MAELYS_RELEASE_TEST_GH")}
        with tempfile.TemporaryDirectory() as directory:
            witness = pathlib.Path(directory) / "token"
            witness.write_text("a" * 64, encoding="utf-8")
            for variables in (
                    {"_MAELYS_RELEASE_SELF_TEST": "1"},
                    {"_MAELYS_RELEASE_SELF_TEST": ""},
                    {"_MAELYS_RELEASE_SELF_TEST_FILE": str(witness)},
                    {"_MAELYS_RELEASE_TEST_GH": "/accidental/gh"},
                    {"_MAELYS_RELEASE_SELF_TEST": "a" * 64},
                    {"_MAELYS_RELEASE_SELF_TEST": "b" * 64, "_MAELYS_RELEASE_SELF_TEST_FILE": str(witness)},
                    {"_MAELYS_RELEASE_SELF_TEST": "a" * 64, "_MAELYS_RELEASE_SELF_TEST_FILE": str(witness / "missing")},
            ):
                with self.subTest(variables=variables):
                    result = subprocess.run([sys.executable, str(CLI), "describe", "--format", "json"],
                                            env={**clean, **variables}, text=True, capture_output=True, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("reserved for self-test", result.stderr)
                    self.assertIn("Unset _MAELYS_RELEASE_SELF_TEST", result.stderr)
                    body = json.loads(result.stderr)
                    self.assertEqual(body["contract"], "agent-cli/v2")
                    self.assertEqual(body["schemaVersion"], 2)
                    self.assertEqual(body["command"], "describe")
                    self.assertFalse(body["ok"])
                    self.assertEqual(body["exitCode"], result.returncode)
                    self.assertEqual(body["error"]["code"], "PRECONDITION_FAILED")

    def test_token_refusal_uses_the_resolved_command_and_output_format(self):
        env = {**os.environ, "_MAELYS_RELEASE_SELF_TEST": "invalid", "MAELYS_CLI_FORMAT": "json"}
        for argv, command, machine in (
                (["version"], "version", True),
                (["--version", "--json"], "version", True),
                (["describe", "--format=json", "--compact"], "describe", True),
                (["version", "--format", "jsonl", "--field", "version"], "version", True),
                (["help", "--format", "text"], "help", False),
        ):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, str(CLI), *argv], env=env,
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                if machine:
                    body = json.loads(result.stderr)
                    self.assertEqual(body["command"], command)
                    self.assertEqual(body["error"]["code"], "PRECONDITION_FAILED")
                    self.assertEqual(body["exitCode"], 1)
                    self.assertFalse(body["ok"])
                else:
                    self.assertIn("maelys-release: [PRECONDITION_FAILED]", result.stderr)
                for name in ("_MAELYS_RELEASE_SELF_TEST", "_MAELYS_RELEASE_SELF_TEST_FILE", "_MAELYS_RELEASE_TEST_GH"):
                    self.assertIn(name, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_token_refusal_precedes_any_command_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "_MAELYS_RELEASE_SELF_TEST": "invalid"}
            result = subprocess.run([sys.executable, str(CLI), "adopt", directory, "--apply", "--format", "json"],
                                    env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            body = json.loads(result.stderr)
            self.assertEqual(body["command"], "adopt")
            self.assertEqual(body["error"]["code"], "PRECONDITION_FAILED")
            self.assertIn("reserved for self-test", body["error"]["message"])
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_imported_host_refuses_an_invalid_environment_before_any_use(self):
        code = '''import importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader("candidate", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
for operation in (lambda: module.HOST.which("python3"),
                  lambda: module.HOST.run(["/unreachable"]),
                  lambda: module.HOST.stream(["/unreachable"], sys.stdout),
                  lambda: module.HOST.exec("/unreachable", ["/unreachable"], dict(os.environ))):
    try:
        operation()
    except module.Failure as failure:
        assert failure.code == "PRECONDITION_FAILED"
    else:
        raise AssertionError("the invalid environment reached a host operation")
'''
        result = subprocess.run([sys.executable, "-c", code, str(CLI)],
                                env={**os.environ, "_MAELYS_RELEASE_SELF_TEST": "invalid"},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_self_test_token_is_unique_and_expires_with_its_context(self):
        with MODULE.Host.self_test_environment() as first, MODULE.Host.self_test_environment() as second:
            self.assertNotEqual(first["_MAELYS_RELEASE_SELF_TEST"], second["_MAELYS_RELEASE_SELF_TEST"])
            witness = pathlib.Path(first["_MAELYS_RELEASE_SELF_TEST_FILE"])
            self.assertEqual(witness.read_text(), first["_MAELYS_RELEASE_SELF_TEST"])
            result = subprocess.run([sys.executable, str(CLI), "describe", "--format", "json"],
                                    env=first, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(witness.exists())
        expired = subprocess.run([sys.executable, str(CLI), "describe", "--format", "json"],
                                 env=first, text=True, capture_output=True, check=False)
        self.assertNotEqual(expired.returncode, 0)
        self.assertIn("reserved for self-test", expired.stderr)
        self.assertEqual(json.loads(expired.stderr)["error"]["code"], "PRECONDITION_FAILED")

    def test_api_writes_and_process_calls_stay_inside_host(self):
        for path in [CLI, *sorted((ROOT / "bin" / "maelys_socle").rglob("*.py"))]:
            with self.subTest(path=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                host = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                             and node.name == "Host" and path == CLI), None)
                inside_host = set(ast.walk(host)) if host else set()
                # Search literal flags regardless of quoting, not the longer help
                # strings that hand an API command to the operator without running it.
                escaped = [node.lineno for node in ast.walk(tree)
                           if isinstance(node, ast.Constant) and node.value == "-X" and node not in inside_host]
                self.assertEqual(escaped, [], "GitHub write flags outside Host")
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        if node.func.value.id == "subprocess" or (node.func.value.id, node.func.attr) in (
                                ("os", "execve"), ("shutil", "which")):
                            self.assertIn(node, inside_host, ast.unparse(node))

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
