# SPDX-License-Identifier: MPL-2.0
"""The current shell and GitHub host; every caller reads this module's HOST."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile

from maelys_cli import Failure


HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class Host:
    """The current process's shell and GitHub boundary.

    Tests replace this object, never the functions that read it. The private
    self-test environment follows CLI subprocesses as well as in-process calls;
    only an explicitly named fake gh may receive writes in that environment.
    """

    @staticmethod
    def _self_test() -> bool:
        keys = ("_MAELYS_RELEASE_SELF_TEST", "_MAELYS_RELEASE_SELF_TEST_FILE", "_MAELYS_RELEASE_TEST_GH",
                "_MAELYS_RELEASE_TEST_SIGNERS")
        if not any(key in os.environ for key in keys):
            return False
        token = os.environ.get(keys[0], "")
        path = os.environ.get(keys[1], "")
        if re.fullmatch(r"[0-9a-f]{64}", token) and path:
            try:
                with open(path, encoding="utf-8") as witness:
                    if secrets.compare_digest(witness.read(65), token):
                        return True
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
        raise Failure("PRECONDITION_FAILED",
                      "_MAELYS_RELEASE_SELF_TEST and its companion variables are reserved "
                      "for self-test; the temporary token is missing, invalid or expired.",
                      "Unset _MAELYS_RELEASE_SELF_TEST, _MAELYS_RELEASE_SELF_TEST_FILE,"
                      " _MAELYS_RELEASE_TEST_GH and _MAELYS_RELEASE_TEST_SIGNERS.")

    @staticmethod
    @contextmanager
    def self_test_environment():
        """A child and its descendants may use the guard only while this file lives."""
        token = secrets.token_hex(32)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="maelys-self-test-") as witness:
            witness.write(token)
            witness.flush()
            yield {**os.environ, "_MAELYS_RELEASE_SELF_TEST": token,
                   "_MAELYS_RELEASE_SELF_TEST_FILE": witness.name}

    def which(self, name: str) -> str | None:
        testing = self._self_test()
        if name == "gh" and testing:
            return os.environ.get("_MAELYS_RELEASE_TEST_GH") or None
        return shutil.which(name)

    def test_signers(self) -> str | None:
        """The allowed signers a self-test names, and nothing outside one.

        The fleet's file names the keys that sign its releases; a fixture
        signs with a key generated for the test, which that file will never
        hold. The same guard as the fake gh stands in front of this: outside
        a live self-test environment, with its token and its witness file,
        asking for it is a refusal.
        """
        return (os.environ.get("_MAELYS_RELEASE_TEST_SIGNERS") or None) if self._self_test() else None

    def _command(self, command: list[str], cwd: pathlib.Path | None, env: dict[str, str] | None) -> list[str]:
        testing = self._self_test()
        environment = os.environ if env is None else env
        if pathlib.Path(command[0]).name == "gh" and testing:
            fake = environment.get("_MAELYS_RELEASE_TEST_GH")
            executable = shutil.which(command[0], path=environment.get("PATH"))
            if fake:
                if not executable or pathlib.Path(executable).resolve() != pathlib.Path(fake).resolve():
                    raise AssertionError("self-test gh is not the explicitly declared fake executable")
                command = [fake, *command[1:]]
            elif not (len(command) == 3 and command[1] == "api" and not command[2].startswith("-")
                      or command[1:3] in (["pr", "list"], ["pr", "view"], ["release", "download"])):
                raise AssertionError(f"self-test refused a real GitHub write: {command!r}")
        if command[0] == "git":
            if testing and "push" in command[1:]:
                self._local_push(command, cwd, env)
            command = ["git", "-C", str((cwd or pathlib.Path.cwd()).resolve()), *command[1:]]
        return command

    def _local_push(self, command: list[str], cwd: pathlib.Path | None, env: dict[str, str] | None) -> None:
        # A test may push to its throwaway bare origin, never a network
        # transport. Resolve push URLs (including insteadOf) before checking.
        arguments = command[2:] if command[1] == "push" else []
        while arguments and (arguments[0] in ("-q", "--quiet", "--force-with-lease")
                             or arguments[0].startswith("--force-with-lease=")):
            arguments = arguments[1:]
        remote = arguments[0] if arguments and not arguments[0].startswith("-") else ""
        if not remote:
            raise AssertionError(f"self-test refused an undeclared git push: {command!r}")
        resolved = self.run(["git", "remote", "get-url", "--push", "--all", remote], cwd=cwd, env=env)
        urls = resolved.stdout.splitlines() if resolved.returncode == 0 else [remote]
        roots = [pathlib.Path(tempfile.gettempdir()).resolve(), pathlib.Path("/tmp").resolve()]
        for url in urls:
            local = url.removeprefix("file://")
            path = ((cwd or pathlib.Path.cwd()) / local).resolve()
            if ":" in local or not path.exists() or not any(root in path.parents for root in roots):
                raise AssertionError(f"self-test refused a non-local git push: {url}")

    def run(self, command: list[str], cwd: pathlib.Path | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self._command(command, cwd, env), cwd=cwd, env=env, check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stream(self, command: list[str], log, cwd: pathlib.Path | None = None,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self._command(command, cwd, env), cwd=cwd, env=env, check=False,
                              stdout=log, stderr=subprocess.STDOUT)

    def exec(self, executable: str, command: list[str], env: dict[str, str]) -> None:
        self._command([executable, *command[1:]], None, env)
        os.execve(executable, command, env)

    def read(self, path: str) -> tuple[str, object]:
        if not self.which("gh"):
            return "no-gh", None
        completed = self.run(["gh", "api", path])
        if completed.returncode == 0:
            try:
                return "ok", json.loads(completed.stdout)
            except json.JSONDecodeError:
                return "unreadable", None
        status = HTTP_STATUS.search(completed.stderr)
        return ("absent" if status and status.group(1) == "404" else "unreadable"), None

    def write(self, method: str, endpoint: str, payload: pathlib.Path,
              cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        return self.run(["gh", "api", "-X", method, endpoint, "--input", str(payload)], cwd=cwd)


HOST = Host()


def run(command: list[str], cwd: pathlib.Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return HOST.run(command, cwd=cwd, env=env)


def git(*arguments: str, cwd: pathlib.Path | None = None, check: bool = True) -> str:
    completed = run(["git", *arguments], cwd=cwd)
    if check and completed.returncode != 0:
        raise Failure("PROCESS_FAILED", f"git {' '.join(arguments)} failed: {completed.stderr.strip()}",
                      "Fix the repository state and retry.")
    return completed.stdout.strip() if completed.returncode == 0 else ""
