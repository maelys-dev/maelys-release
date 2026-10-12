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
import signal
import subprocess
import tempfile

from maelys_cli import Failure


HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class Absent(str):
    """The state `absent`, with the words GitHub answered its 404 in.

    One status for several facts: `Branch not protected` for a branch that
    has no protection, `Not Found` for a token that may not read one,
    `Branch not found` for a branch that does not exist. It compares as the
    state it always was, so every reader of an absence receives what it
    did; the one that must tell those facts apart reads `message`, and a
    state that carries none has established none of them.
    """

    def __new__(cls, message: str = "") -> "Absent":
        state = super().__new__(cls, "absent")
        state.message = message
        return state


# A runner says that it is one, and with these words: a variable set to
# `false` or `0` on an author's machine is not a runner. Nothing in the
# checkout tells either: a job started by a push checks its branch out by
# name, has no identity and has nothing to push, exactly like a clone made a
# minute ago. One definition, read through Host.is_runner by everything that
# is said to an author and not to a job, or looked for on an author's
# machine and not on a runner's.
RUNNER_VARIABLES = ("CI", "GITHUB_ACTIONS")
RUNNER_VALUES = ("true", "1")


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
        if name in ("hostname", "scutil") and testing:
            # What names the machine is isolated as the author's list is: a
            # self-test asks the tool a test wrote under the temporary
            # directory, and never the developer's own. A suite whose verdict
            # depends on what the machine it runs on is called is not one.
            found = shutil.which(name)
            return found if found and self._temporary(found) else None
        return shutil.which(name)

    @staticmethod
    def _temporary(path: str) -> bool:
        roots = [pathlib.Path(tempfile.gettempdir()).resolve(), pathlib.Path("/tmp").resolve()]
        return any(root in pathlib.Path(path).resolve().parents for root in roots)

    def is_runner(self) -> bool:
        """Whether this is a CI runner, by what every one of them says of itself.

        `CI` or `GITHUB_ACTIONS` set to `true` or `1`, and to nothing else.
        A runner has a name and no author: what is local by nature -- the
        machine's name, the author's own terms -- is not looked for there,
        and what is said to whoever is about to push is not said there.
        The one place the environment is read for it, so that a recorded
        reference, whose host answers for itself, is the same on a runner
        and on a laptop.
        """
        return any(os.environ.get(name, "").strip().lower() in RUNNER_VALUES for name in RUNNER_VARIABLES)

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

    def feed(self, command: list[str], data: bytes, cwd: pathlib.Path | None = None,
             env: dict[str, str] | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
        """A process given bytes and answering bytes: `git cat-file --batch` over a history.

        `run` decodes what it reads, and a history holds blobs that are not
        text, as a repository holds names that are not. The same guard as
        `run` stands in front of it.

        With `timeout`, in seconds, the process answers in that time or is
        given up on, whatever it is waiting for. It runs in a session of its
        own: it has no terminal to ask anything on, and it is stopped with
        everything it started -- git talks to a repository through a helper,
        and stopping git alone leaves the helper holding the pipes this call
        reads. What it had said comes back with the code of a process that
        was stopped, which is never 0.
        """
        command = self._command(command, cwd, env)
        if timeout is None:
            return subprocess.run(command, cwd=cwd, env=env, input=data, check=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        try:
            said, complained = child.communicate(data, timeout=timeout)
        except BaseException as stopped:
            # The clock, or the operator's interrupt, which a session of its
            # own no longer receives: the session goes either way.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not isinstance(stopped, subprocess.TimeoutExpired):
                child.wait()
                raise
            try:
                said, complained = child.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                # What left the session still holds the pipes, and is not
                # waited for: the bound is the caller's, not that process's.
                child.stdout.close()
                child.stderr.close()
                child.wait()
                said = complained = b""
        return subprocess.CompletedProcess(command, child.returncode, said, complained)

    def private_terms(self) -> pathlib.Path | None:
        """Where the author keeps the terms no repository may carry: outside every repository.

        ${XDG_CONFIG_HOME:-~/.config}/maelys-release/private-terms, one term
        a line. A committed list of what must not be published would be the
        publication, so the list is the machine's and `check` reads it the
        way it reads the machine's name. A self-test reads the list a test
        wrote under the temporary directory and never the developer's own:
        their terms are not a fixture, and a suite whose verdict depends on
        who runs it is not one.
        """
        base = os.environ.get("XDG_CONFIG_HOME")
        if self._self_test() and not (base and self._temporary(str(pathlib.Path(base) / "x"))):
            return None
        return pathlib.Path(base or pathlib.Path.home() / ".config") / "maelys-release" / "private-terms"

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
        if not status or status.group(1) != "404":
            return "unreadable", None
        # gh prints GitHub's message before the status, on the same line.
        said = completed.stderr[:status.start()].rsplit("\n", 1)[-1]
        return Absent(said.removeprefix("gh:").strip()), None

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
