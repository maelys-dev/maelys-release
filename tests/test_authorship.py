# SPDX-License-Identifier: MPL-2.0
"""An address git made up on this machine is noted before the push, and refused by the hook.

A clone with no identity pushed twenty commits whose address carried the
machine's name. `cut` reads back one commit at publication; what is measured
here is read where the commits are made: the identity git would write, the
commits a branch has ahead of its upstream, and the hook a repository may
enable.

Every rule is measured at both ends -- on a clone that has the defect and on
a correct one -- and each test that expects silence first counts what was
read, so that a reading that found nothing to read cannot pass for one that
found nothing wrong. A line that rewrites history is run as printed wherever
one is printed, and its absence is asserted everywhere else.

No line of this file holds an address: each is assembled when the test
runs, so that a reader of tracked files finds none of the shapes the rules
name.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_allowed_signers import lines_to_run
from test_maelys_release import CLI, MODULE, using_host

AUTHORSHIP = MODULE.authorship
SOCLE = ("--socle-sha", "f" * 40, "--socle-tag", "v9.9.9")
ZEROS = "0" * 40


def address(login: str, *labels: str) -> str:
    """LOGIN at the domain LABELS spell, assembled here and written nowhere."""
    return login + "@" + ".".join(labels)


LOGIN, MACHINE = "alice", "studio"
ACCOUNT = address("1234+" + LOGIN, "users", "noreply", "github", "com")
OTHER = address("5678+bob", "users", "noreply", "github", "com")
# What git composes on the fixture's machine, and the same machine on another network.
OWN = address(LOGIN, MACHINE, "home")
OWN_ELSEWHERE = address(LOGIN, MACHINE, "lan")
# A mailbox at a private network: somebody's, perhaps.
MAILBOX = address("bob", "corp", "local")
BARE = LOGIN
REWRITE = "git rebase"


def real_composed() -> str:
    """The address git composes on the machine running the suite; '' where it refuses to."""
    completed = subprocess.run(AUTHORSHIP.COMPOSED, text=True, capture_output=True, check=False)
    return completed.stdout.partition("<")[2].partition(">")[0] if completed.returncode == 0 else ""


class CountingHost(MODULE.Host):
    """The real host, with every call kept; what git composes with nothing set is the test's."""

    def __init__(self, composed: str = OWN, name: str = "Alice Account", refuses: bool = False,
                 env: bool = True) -> None:
        self.composed, self.name, self.refuses, self.env = composed, name, refuses, env
        self.commands, self.lookups = [], []

    def which(self, name):
        self.lookups.append(name)
        if name == "env":
            return "/fixture/env" if self.env else None
        return super().which(name)

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if command == AUTHORSHIP.COMPOSED:
            if not self.env:
                raise AssertionError("env was run where it is not installed")
            if self.refuses:
                return subprocess.CompletedProcess(
                    command, 128, "", f"fatal: unable to auto-detect email address (got '{self.composed}')\n")
            return subprocess.CompletedProcess(command, 0, f"{self.name} <{self.composed}> 1700000000 +0000\n", "")
        return super().run(command, cwd=cwd, env=env)

    def ran(self, *prefix: str) -> int:
        return len([command for command in self.commands if command[:len(prefix)] == list(prefix)])


class Clone:
    """A bare origin with one pushed commit and a clone of it, with no identity anywhere."""

    def __init__(self, test: unittest.TestCase, remote: bool = True, name: str = "maelys-fixture") -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-authorship.")).resolve()
        test.addCleanup(shutil.rmtree, self.work, True)
        dropped = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
                   *AUTHORSHIP.RUNNER_VARIABLES)
        self.env = {key: value for key, value in os.environ.items() if key not in dropped}
        # A global configuration the test owns: empty until a test writes an
        # identity into it, as an operator does once for a machine.
        self.env.update(GIT_CONFIG_GLOBAL=str(self.work / "gitconfig"), GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", HOME=str(self.work), GIT_TERMINAL_PROMPT="0")
        (self.work / "gitconfig").write_text("", encoding="utf-8")
        patcher = patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        test.addCleanup(patcher.stop)
        self.origin = self.work / "origin.git"
        self.dir = self.work / name
        if remote:
            self.git(self.work, "init", "-q", "--bare", "-b", "main", str(self.origin))
            self.git(self.work, "clone", "-q", str(self.origin), str(self.dir))
            self.commit("pushed", email=ACCOUNT)
            self.git(self.dir, "push", "-q", "-u", "origin", "HEAD:refs/heads/main")
        else:
            self.git(self.work, "init", "-q", "-b", "main", str(self.dir))

    def git(self, cwd: pathlib.Path, *arguments: str, env: "dict | None" = None, check: bool = True):
        completed = subprocess.run(["git", "-C", str(cwd), *arguments], env=env or self.env, check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and completed.returncode != 0:
            raise AssertionError(f"git {arguments}: {completed.stderr}")
        return completed

    def commit(self, message: str, *, email: "str | None" = None, name: "str | None" = LOGIN,
               committer: "tuple[str, str] | None" = None, path: str = "file", empty: bool = False) -> str:
        """One commit. EMAIL and NAME stand for what git would write here when
        they are given, and the configuration is left to answer when they are
        None: a made-up address is written the way git writes one, without
        depending on what this machine happens to be called."""
        if not empty:
            with (self.dir / path).open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
            self.git(self.dir, "add", path)
        identity = [word for key, value in (("user.name", name), ("user.email", email)) if value is not None
                    for word in ("-c", f"{key}={value}")]
        env = dict(self.env)
        if committer:
            env.update(GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1])
        self.git(self.dir, *identity, "commit", "-q", *(["--allow-empty"] if empty else []), "-m", message, env=env)
        return self.short()

    def short(self, revision: str = "HEAD") -> str:
        return self.git(self.dir, "rev-parse", "--short", revision).stdout.strip()

    def full(self, revision: str = "HEAD") -> str:
        return self.git(self.dir, "rev-parse", revision).stdout.strip()

    def authors(self, revision: str = "HEAD", count: int = 20) -> list:
        return self.git(self.dir, "log", f"-{count}", "--format=%ae %ce", revision).stdout.split("\n")[:-1]

    def configure(self, email: "str | None" = ACCOUNT, name: "str | None" = LOGIN) -> None:
        for key, value in (("user.email", email), ("user.name", name)):
            if value is None:
                self.git(self.dir, "config", "--unset", key, check=False)
            else:
                self.git(self.dir, "config", key, value)

    def somebody_else(self, branch: str = "other", count: int = 3) -> None:
        """COUNT commits of another author, pushed to BRANCH of the origin and fetched."""
        start = self.git(self.dir, "symbolic-ref", "--short", "HEAD").stdout.strip()
        self.git(self.dir, "switch", "-q", "-c", branch, "origin/main")
        for index in range(count):
            self.commit(f"theirs {index}", email=OTHER, name="bob", path="theirs")
        self.git(self.dir, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
        self.git(self.dir, "switch", "-q", start)
        self.git(self.dir, "branch", "-q", "-D", branch)

    def cli(self, *arguments: str, env: "dict | None" = None, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *arguments], env=env or self.env, input=stdin, check=False,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def json(self, *arguments: str, expect: int = 0, env: "dict | None" = None, stdin: str = "") -> dict:
        completed = self.cli(*arguments, "--format", "json", env=env, stdin=stdin)
        if completed.returncode != expect:
            raise AssertionError(f"{arguments}: exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}")
        return json.loads(completed.stderr if expect == 1 else completed.stdout)


def about_identity(notes: list) -> list:
    return [note for note in notes if " is set here" in note or "the address set here" in note]


def about_commits(notes: list) -> list:
    return [note for note in notes if "an address made up on this machine, as author or committer" in note]


def about_private(notes: list) -> list:
    return [note for note in notes if "a name private networks and machines use" in note and " commit" in note]


def about_names(notes: list) -> list:
    return [note for note in notes if "another name than its own" in note]


def line_of(note: str) -> str:
    return note.rpartition(": ")[2] if REWRITE in note else ""


class RuleTest(unittest.TestCase):
    """What an address is: nobody's mailbox, a private name, or nothing the rules name."""

    @staticmethod
    def composed() -> tuple:
        return "Alice Account", OWN

    def kind(self, written: str, composed=None) -> tuple:
        return AUTHORSHIP.kind_of(written, composed or self.composed)

    def test_an_address_with_no_domain_is_nobodys(self) -> None:
        for written in ("", LOGIN, LOGIN + "@"):
            with self.subTest(written=written):
                self.assertEqual(self.kind(written), ("bare", "it has no domain"))

    def test_what_git_composes_here_is_recognised_on_any_network_and_in_any_case(self) -> None:
        composed = ("composed", "it is the address git composes on this machine")
        for written in (OWN, OWN.upper(), OWN + ".", OWN_ELSEWHERE, address(LOGIN, MACHINE),
                        address(LOGIN, MACHINE, "(none)"), address(LOGIN, MACHINE, "home", "arpa")):
            with self.subTest(written=written.count(".")):
                self.assertEqual(self.kind(written), composed)
        # Git's own answer is not lowercase everywhere, and the commit need not be.
        self.assertEqual(self.kind(OWN, lambda: ("", OWN.upper())), composed)
        # A machine with a public name: the same login at the same domain, whole.
        public = address(LOGIN, MACHINE, "example", "org")
        self.assertEqual(self.kind(public, lambda: ("", public)), composed)
        self.assertEqual(self.kind(address(LOGIN, MACHINE, "example", "com"), lambda: ("", public)), ("", ""))

    def test_the_rule_broken_on_purpose(self) -> None:
        """Another login, another machine, a public suffix: each is somebody else's, or nobody's rule."""
        self.assertEqual(self.kind(address("bob", MACHINE, "home"))[0], "local")
        self.assertEqual(self.kind(address(LOGIN, "atelier", "home"))[0], "local")
        self.assertEqual(self.kind(address(LOGIN, MACHINE, "example", "org")), ("", ""))
        # With nothing known of what git composes, nothing is this machine's.
        self.assertEqual(self.kind(OWN, lambda: ("", ""))[0], "local")

    def test_each_private_suffix_names_itself_and_only_itself(self) -> None:
        for suffix in AUTHORSHIP.LOCAL_SUFFIXES:
            labels = suffix.strip(".").split(".")
            written = address("bob", "host", *labels)
            with self.subTest(suffix=suffix):
                self.assertEqual(self.kind(written), ("local", f"its domain ends in {suffix}"))
                # The suffix alone is a domain too.
                if len(labels) > 1:
                    self.assertEqual(self.kind(address("bob", *labels))[0], "local")
                with patch.object(AUTHORSHIP, "LOCAL_SUFFIXES",
                                  tuple(known for known in AUTHORSHIP.LOCAL_SUFFIXES if known != suffix)):
                    removed = AUTHORSHIP.kind_of(written, self.composed)
                    self.assertNotEqual(removed, ("local", f"its domain ends in {suffix}"))
        self.assertEqual(len(AUTHORSHIP.LOCAL_SUFFIXES), 12)
        self.assertEqual(self.kind(address("bob", "host")), ("local", "its domain has no dot"))

    def test_an_address_nothing_names(self) -> None:
        """Counted: each reaches the last rule, which asks what git composes, and passes."""
        correct = (ACCOUNT, address(LOGIN, "example", "org"), address("noreply", "github", "com"),
                   address(LOGIN, "home", "example"), address(LOGIN, "local", "example"),
                   address(LOGIN, "homes", "example"), address(LOGIN, "thelan", "example"),
                   # Reserved for tests and documentation: no machine is called that.
                   address("test", "example", "invalid"), address("test", "example", "test"),
                   # A mailbox provider that shares a machine's first label is still a provider.
                   address(LOGIN, MACHINE, "example"))
        for written in correct:
            asked = []
            with self.subTest(written=written.rpartition("@")[2]):
                self.assertEqual(AUTHORSHIP.kind_of(written, lambda: asked.append(1) or ("", OWN)), ("", ""))
                self.assertEqual(len(asked), 1)

    def test_what_git_composes_is_asked_once_and_read_from_either_stream(self) -> None:
        host = CountingHost()
        with using_host(host):
            composed = AUTHORSHIP.once(AUTHORSHIP.composed_identity)
            self.assertEqual([composed(), composed(), composed()], [("Alice Account", OWN)] * 3)
        self.assertEqual((host.lookups, host.commands), (["env"], [AUTHORSHIP.COMPOSED]))
        # Where git refuses to compose, it still says what it tried.
        with using_host(CountingHost(refuses=True)):
            self.assertEqual(AUTHORSHIP.composed_identity(), ("", OWN))
        absent = CountingHost(env=False)
        with using_host(absent):
            self.assertEqual(AUTHORSHIP.composed_identity(), ("", ""))
        self.assertEqual(absent.commands, [])

    def test_the_question_takes_every_configured_identity_away(self) -> None:
        """Asked for real, with an identity in every place git reads one: none of them answers."""
        if not real_composed():
            self.skipTest("git refuses to compose an address on this machine")
        chosen = address("chosen", "example", "org")
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config"
            config.write_text(f"[user]\n\temail = {chosen}\n\tname = Chosen\n", encoding="utf-8")
            env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "EMAIL": chosen, "GIT_AUTHOR_EMAIL": chosen,
                   "GIT_COMMITTER_EMAIL": chosen, "GIT_COMMITTER_NAME": "Chosen", "GIT_DIR": "/nonexistent",
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email", "GIT_CONFIG_VALUE_0": chosen}
            answer = subprocess.run(AUTHORSHIP.COMPOSED, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertNotIn("chosen", answer.stdout.lower())

    def test_a_runner_says_it_is_one_in_a_runners_words(self) -> None:
        clean = {key: value for key, value in os.environ.items() if key not in AUTHORSHIP.RUNNER_VARIABLES}
        for name in AUTHORSHIP.RUNNER_VARIABLES:
            for value, runner in (("true", True), ("1", True), ("TRUE", True), ("false", False), ("0", False),
                                  ("", False), ("no", False)):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {**clean, name: value}, clear=True):
                    self.assertIs(AUTHORSHIP.on_a_runner(), runner)
        with patch.dict(os.environ, clean, clear=True):
            self.assertFalse(AUTHORSHIP.on_a_runner())


class IdentityTest(unittest.TestCase):
    """The name and the address git would write here, in git's own order."""

    def setUp(self) -> None:
        self.clone = Clone(self)
        self.host = CountingHost()

    def notes(self) -> list:
        self.host.commands.clear()
        with using_host(self.host):
            return AUTHORSHIP.identity_notes(self.clone.dir)

    def test_nothing_set_is_one_note_for_both(self) -> None:
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("neither a name nor an address is set here"), notes[0])
        self.assertEqual(self.host.ran("git", "config", "-z", "--get-regexp"), 1)
        # Nothing to compare: git is not asked what it would compose.
        self.assertEqual(self.host.ran("env"), 0)

    def test_each_half_is_said_alone(self) -> None:
        self.clone.configure(name=None)
        self.assertTrue(self.notes()[0].startswith("no name is set here: git writes into each commit this"
                                                   " account's name"), self.notes())
        self.assertEqual(len(self.notes()), 1)
        self.clone.configure(email=None, name=LOGIN)
        self.assertTrue(self.notes()[0].startswith("no address is set here"), self.notes())
        self.assertEqual(len(self.notes()), 1)

    def test_an_identity_that_is_set_is_asked_about_and_passes(self) -> None:
        self.clone.configure()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.host.ran("env"), 1, "the address was compared with what git composes")

    def test_the_keys_of_one_role_are_an_identity(self) -> None:
        """`author.email` and `committer.email` with no `user.email`: nothing is missing."""
        for key, value in (("author.email", ACCOUNT), ("committer.email", ACCOUNT), ("user.name", LOGIN)):
            self.clone.git(self.clone.dir, "config", key, value)
        self.assertEqual(self.notes(), [])
        self.clone.git(self.clone.dir, "config", "--unset", "committer.email")
        self.assertEqual(len(about_identity(self.notes())), 1, "the committer has none")

    def test_the_key_of_one_role_wins_over_user_email(self) -> None:
        """`user.email` right and a made-up `author.email`: git writes the made-up one."""
        self.clone.configure()
        self.assertEqual(self.notes(), [])
        self.clone.git(self.clone.dir, "config", "author.email", OWN)
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("the address set here is one made up on this machine"), notes[0])
        self.assertIn("it is the address git composes on this machine", notes[0])
        self.assertNotIn(MACHINE, notes[0])

    def test_git_told_to_guess_nothing_composes_nothing(self) -> None:
        self.clone.git(self.clone.dir, "config", "user.useConfigOnly", "true")
        self.assertEqual(self.notes(), [], "git refuses to commit here, and says so itself")
        self.clone.git(self.clone.dir, "config", "user.useConfigOnly", "false")
        self.assertEqual(len(self.notes()), 1)

    def test_a_private_name_is_said_as_what_it_is(self) -> None:
        self.clone.configure(email=MAILBOX)
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("the address set here is under a name private networks"), notes[0])
        self.assertIn("its domain ends in .local", notes[0])
        self.assertIn("a mailbox there is nothing to change", notes[0])

    def test_no_note_gives_a_line_that_sets_an_identity(self) -> None:
        """Each of the five names the keys and whose they are to set, word for word. They printed
        `git config --global user.name NAME` and its twin for the address: lines an agent can fill
        in with a name and an address of its own making, for the whole machine."""
        def said(name: str, email: str, only: bool = False) -> list:
            role = {"name": name, "address": email}
            return AUTHORSHIP.identity_messages({"roles": {"author": role, "committer": role}, "configOnly": only},
                                                lambda: ("", ""))

        own = " to set, in the author's own git configuration, before the next commit: "
        noreply = ("user.email -- on GitHub, the account's noreply address ("
                   + address("ID+LOGIN", "users", "noreply", "github", "com") + ") names no machine")
        notes = said("", "") + said("", ACCOUNT) + said(LOGIN, "") + said(LOGIN, BARE) + said(LOGIN, MAILBOX)
        for note in notes:
            with self.subTest(note=note[:32]):
                self.assertEqual(lines_to_run(note), [], note)
                self.assertIn("author", note)
        self.assertEqual(notes, [
            "neither a name nor an address is set here: git writes into each commit this account's name as the"
            " system holds it, and an address it composes from the login and this machine's name where it can,"
            " and a push publishes both. They are their author's" + own + "user.name and " + noreply,
            "no name is set here: git writes into each commit this account's name as the system holds it, and a"
            " push publishes it. It is the author's" + own + "user.name",
            "no address is set here: git composes one from the login and this machine's name where it can, writes"
            " it into each commit, and a push publishes it. It is the author's" + own + noreply,
            "the address set here is one made up on this machine, not an account's (it has no domain): each"
            " commit carries it, and a push publishes it. It is the author's" + own + noreply,
            "the address set here is under a name private networks and machines use (its domain ends in"
            " .local): a mailbox there is nothing to change; a machine's name is published by each push. Where"
            " it is a machine's, their author sets another address in their own git configuration, before the"
            " next commit: " + noreply])
        self.assertTrue(lines_to_run("Set one before the next commit: 'git config --global user.name NAME'"))
        self.assertEqual(said("", "", only=True), [])

    def test_the_environment_is_an_identity_too(self) -> None:
        """git reads it before its configuration, one variable for each role."""
        both = {"GIT_AUTHOR_EMAIL": ACCOUNT, "GIT_COMMITTER_EMAIL": ACCOUNT, "GIT_AUTHOR_NAME": LOGIN,
                "GIT_COMMITTER_NAME": LOGIN}
        with patch.dict(os.environ, both):
            self.assertEqual(self.notes(), [])
        with patch.dict(os.environ, {key: value for key, value in both.items() if "COMMITTER" not in key}):
            self.assertEqual(len(self.notes()), 1, "the committer still has neither")
        self.clone.configure(email=None)
        with patch.dict(os.environ, {"EMAIL": ACCOUNT}):
            self.assertEqual(self.notes(), [])
        with patch.dict(os.environ, {"EMAIL": OWN}):
            self.assertTrue(self.notes()[0].startswith("the address set here is one made up"))
        self.assertEqual(len(self.notes()), 1, "and nothing of it outlives the variable")

    def test_a_runner_is_asked_nothing(self) -> None:
        self.assertEqual(len(self.notes()), 1, "the clone must have the defect for the silence to mean anything")
        for name in AUTHORSHIP.RUNNER_VARIABLES:
            with self.subTest(variable=name), patch.dict(os.environ, {name: "true"}):
                self.assertEqual(self.notes(), [])
                self.assertEqual(self.host.commands, [])
                with using_host(self.host):
                    self.assertEqual(AUTHORSHIP.notes(self.clone.dir), [])
                    self.assertEqual(AUTHORSHIP.hook_proposal(self.clone.dir), "")
                self.assertEqual(self.host.commands, [])
            # A variable that says no is an author's machine.
            with self.subTest(variable=name, value="false"), patch.dict(os.environ, {name: "false"}):
                self.assertEqual(len(self.notes()), 1)


class RangeTest(unittest.TestCase):
    """The commits a branch has ahead of its upstream, and the one line that may write them again."""

    def setUp(self) -> None:
        self.clone = Clone(self)
        self.host = CountingHost()

    def notes(self, directory: "pathlib.Path | None" = None) -> list:
        self.host.commands.clear()
        self.host.lookups.clear()
        with using_host(self.host):
            return AUTHORSHIP.notes(directory or self.clone.dir)

    def assert_no_line(self, notes: list, reason: str) -> None:
        self.assertEqual([note for note in notes if REWRITE in note], [], "no rewriting command")
        self.assertEqual(len(about_commits(notes)), 1, notes)
        self.assertIn(f"No line is given to write them again: {reason}", about_commits(notes)[0])

    def run_line(self, line: str) -> subprocess.CompletedProcess:
        return subprocess.run(line, shell=True, cwd=self.clone.dir, env=self.clone.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_a_clone_with_no_identity_gets_both_notes_and_no_line_yet(self) -> None:
        for message in ("one", "two"):
            self.clone.commit(message, email=OWN)
        notes = self.notes()
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(notes[0].startswith("neither a name nor an address is set here"), notes[0])
        self.assertIn("2 of the 2 commits main has not pushed to its upstream carry", notes[1])
        self.assertIn("it is the address git composes on this machine", notes[1])
        self.assert_no_line(notes, "the identity here is not set yet")

    def test_the_same_clone_with_an_account_gets_none(self) -> None:
        self.clone.configure()
        for message in ("one", "two"):
            self.clone.commit(message, name=None)
        self.assertEqual(self.notes(), [])
        # Counted: the two commits were read, with the tags, the identity and what git composes.
        with using_host(self.host):
            self.assertEqual(len(AUTHORSHIP.read_head(self.clone.dir)["commits"]), 2)
        self.notes()
        for prefix, count in ((("git", "config", "-z", "--get-regexp"), 1), (("git", "symbolic-ref"), 1),
                              (("git", "rev-parse", "-q", "--verify", "main@{upstream}"), 1),
                              (("git", "log", "--boundary"), 1), (("git", "for-each-ref"), 1), (("env",), 1)):
            self.assertEqual(self.host.ran(*prefix), count, prefix)
        self.assertEqual(len(self.host.commands), 6, self.host.commands)

    def test_the_line_names_the_branch_and_the_commit_and_is_the_fix(self) -> None:
        """Run as printed, from another branch: only the named branch moves, and nothing is left to note."""
        made = [self.clone.commit(message, email=OWN) for message in ("one", "two", "three")]
        self.clone.configure()
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        line = line_of(notes[0])
        base = self.clone.short("origin/main")
        self.assertEqual(line, f"git rebase {base} main {AUTHORSHIP.AMEND}")
        for effect in ("checks out main", "all of them this machine's own", "author and committer become the identity"
                       " set here", "author dates become now", "signatures are dropped unless commit signing is"
                       " configured", "stops on an empty commit or a failing pre-commit hook",
                       "'git rebase --abort' undoes it"):
            self.assertIn(effect, notes[0])
        self.clone.git(self.clone.dir, "switch", "-q", "--detach", "origin/main")
        done = self.run_line(line)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.clone.git(self.clone.dir, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        self.assertEqual(self.clone.authors("main", 3), [f"{ACCOUNT} {ACCOUNT}"] * 3)
        self.assertEqual(self.clone.short("main~3"), base, "on the commit they started from")
        self.assertNotIn(self.clone.short("main"), made)
        self.assertEqual(self.notes(), [])

    def test_what_the_line_says_it_stops_on_stops_it(self) -> None:
        self.clone.commit("one", email=OWN)
        self.clone.commit("nothing", email=OWN, empty=True)
        self.clone.configure()
        line = line_of(self.notes()[0])
        self.assertTrue(line.startswith(REWRITE), line)
        stopped = self.run_line(line)
        self.assertNotEqual(stopped.returncode, 0, "an empty commit stops it")
        self.assertEqual(self.run_line("git rebase --abort").returncode, 0)
        self.assertEqual(self.clone.authors("main", 2), [f"{OWN} {OWN}"] * 2, "and the abort undid it")
        self.clone.git(self.clone.dir, "reset", "-q", "--hard", "HEAD~1")
        hook = self.clone.dir / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        self.assertNotEqual(self.run_line(line_of(self.notes()[0])).returncode, 0, "a failing pre-commit hook too")
        self.assertEqual(self.run_line("git rebase --abort").returncode, 0)
        self.assertEqual(self.clone.authors("main", 1), [f"{OWN} {OWN}"])

    def test_a_merge_in_the_range_gets_no_line(self) -> None:
        """One made-up commit, then the merge of three pushed commits of somebody else."""
        self.clone.somebody_else()
        self.clone.commit("mine", email=OWN)
        self.clone.git(self.clone.dir, "-c", f"user.email={OWN}", "-c", f"user.name={LOGIN}", "merge", "-q",
                       "--no-edit", "origin/other")
        self.clone.configure()
        notes = self.notes()
        self.assert_no_line(notes, "the range holds a merge")
        self.assertIn("2 of the 5 commits main has not pushed to its upstream carry", about_commits(notes)[0])
        self.assertEqual(self.clone.authors("origin/other", 3), [f"{OTHER} {OTHER}"] * 3)

    def test_a_pull_that_merged_gets_no_line(self) -> None:
        """The upstream moved, and `git pull` merged it: two commits bound the range."""
        self.clone.commit("mine", email=OWN)
        self.clone.somebody_else("main-of-theirs", count=2)
        self.clone.git(self.clone.dir, "push", "-q", "origin", "origin/main-of-theirs:refs/heads/main")
        self.clone.git(self.clone.dir, "-c", f"user.email={OWN}", "-c", f"user.name={LOGIN}", "pull", "-q",
                       "--no-rebase", "--no-edit")
        self.clone.configure()
        self.assert_no_line(self.notes(), "the range holds a merge")

    def test_one_commit_of_somebody_else_gets_no_line(self) -> None:
        self.clone.commit("mine", email=OWN)
        self.clone.commit("theirs", email=OTHER, name="bob")
        self.clone.configure()
        notes = self.notes()
        self.assert_no_line(notes, "1 of them is not this machine's own as author and committer")
        self.assertIn("1 of the 2 commits", about_commits(notes)[0])

    def test_an_author_who_is_somebody_else_gets_no_line(self) -> None:
        """Committed here with no identity, authored elsewhere: the committer alone is this machine's."""
        self.clone.commit("applied", email=OTHER, name="bob", committer=(LOGIN, OWN))
        self.clone.configure()
        notes = self.notes()
        self.assert_no_line(notes, "1 of them is not this machine's own as author and committer")
        self.assertIn("1 of the 1 commit main has not pushed to its upstream carries", notes[0])

    def test_an_address_with_no_domain_is_nobodys_and_not_proved_this_machines(self) -> None:
        """Refused at push, and still no line: nothing says a bare word was written here."""
        self.clone.commit("one", email=BARE)
        self.clone.commit("two", email=BARE)
        self.clone.configure()
        notes = self.notes()
        self.assert_no_line(notes, "2 of them are not this machine's own as author and committer")
        self.assertIn("2 of the 2 commits main has not pushed to its upstream carry", notes[0])

    def test_a_range_that_starts_from_no_commit_of_the_upstream_gets_no_line(self) -> None:
        """An unrelated history given an upstream: linear, all this machine's, and nothing to rebase on."""
        self.clone.configure()
        self.clone.git(self.clone.dir, "switch", "-q", "--orphan", "fix/unrelated")
        self.clone.commit("root", email=OWN)
        self.clone.git(self.clone.dir, "branch", "-q", "--set-upstream-to=origin/main")
        with using_host(self.host):
            read = AUTHORSHIP.read_head(self.clone.dir)
        self.assertEqual((len(read["commits"]), read["base"], bool(read["upstream"])), (1, [], True))
        self.assert_no_line(self.notes(), "the range does not start from one commit of the upstream")

    def test_a_branch_with_no_upstream_is_not_said_unpushed_and_gets_no_line(self) -> None:
        self.clone.configure()
        self.clone.git(self.clone.dir, "switch", "-q", "-c", "fix/a-real-bug")
        self.clone.commit("one", email=OWN)
        notes = self.notes()
        self.assert_no_line(notes, "fix/a-real-bug has no upstream this clone holds")
        self.assertIn("1 of the 1 commit that no remote-tracking ref of this clone reaches -- pushed or not,"
                      " nothing here says -- carries", notes[0])
        self.assertNotIn("has not pushed", notes[0])

    def test_pushed_by_url_with_no_remote_is_not_said_unpushed(self) -> None:
        """Two published commits: nothing in the clone says so, and nothing is claimed."""
        clone = Clone(self, remote=False)
        clone.commit("one", email=OWN)
        clone.commit("two", email=BARE)
        clone.git(clone.work, "init", "-q", "--bare", "-b", "main", str(clone.origin))
        clone.git(clone.dir, "push", "-q", str(clone.origin), "main")
        clone.configure()
        notes = self.notes(clone.dir)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("2 of the 2 commits that no remote-tracking ref of this clone reaches", notes[0])
        self.assertNotIn(REWRITE, notes[0])
        self.assertIn("main has no upstream this clone holds", notes[0])
        # And the command that refuses does not refuse what it cannot place: the bare address is
        # nobody's on any machine, and it is said, with what is known of where it is.
        data = clone.json("outgoing", str(clone.dir))["data"]
        self.assertEqual((data["valid"], data["scope"], data["commits"], data["violations"]),
                         (True, "untracked", 2, []))
        self.assertEqual([entry["rules"] for entry in data["fabricated"]], [["it has no domain"]])
        self.assertEqual(len(about_commits(data["notes"])), 1, data["notes"])
        self.assertIn("which nothing here says were not pushed already", clone.cli("outgoing", str(clone.dir)).stdout)

    def test_an_upstream_that_is_gone_is_no_upstream(self) -> None:
        self.clone.configure()
        self.clone.git(self.clone.dir, "switch", "-q", "-c", "fix/gone")
        self.clone.commit("one", email=OWN)
        self.clone.git(self.clone.dir, "push", "-q", "-u", "origin", "fix/gone")
        self.assertEqual(self.notes(), [], "pushed, and its upstream says so")
        self.clone.git(self.clone.dir, "push", "-q", "origin", ":refs/heads/fix/gone")
        self.clone.git(self.clone.dir, "fetch", "-q", "--prune", "origin")
        notes = self.notes()
        self.assert_no_line(notes, "fix/gone has no upstream this clone holds")
        self.assertNotIn("has not pushed", notes[0])

    def test_a_shallow_single_branch_clone_is_not_said_unpushed_after_its_push(self) -> None:
        shallow = self.clone.work / "shallow"
        self.clone.git(self.clone.work, "clone", "-q", "--depth", "1", f"file://{self.clone.origin}", str(shallow))
        self.clone.git(shallow, "switch", "-q", "-c", "fix/shallow")
        (shallow / "more").write_text("more\n", encoding="utf-8")
        self.clone.git(shallow, "add", "more")
        self.clone.git(shallow, "-c", f"user.name={LOGIN}", "-c", f"user.email={OWN}", "commit", "-q", "-m", "more")
        self.clone.git(shallow, "push", "-q", "-u", "origin", "fix/shallow")
        for key, value in (("user.email", ACCOUNT), ("user.name", LOGIN)):
            self.clone.git(shallow, "config", key, value)
        for note in self.notes(shallow):
            self.assertNotIn("has not pushed", note)
            self.assertNotIn(REWRITE, note)

    def test_a_contributors_commit_fetched_from_a_fork_is_a_note_and_never_a_line(self) -> None:
        self.clone.configure()
        fork = self.clone.work / "fork.git"
        self.clone.git(self.clone.work, "clone", "-q", "--bare", str(self.clone.origin), str(fork))
        contributor = self.clone.work / "contributor"
        self.clone.git(self.clone.work, "clone", "-q", str(fork), str(contributor))
        (contributor / "theirs").write_text("theirs\n", encoding="utf-8")
        self.clone.git(contributor, "add", "theirs")
        self.clone.git(contributor, "-c", "user.name=bob", "-c", f"user.email={MAILBOX}", "commit", "-q", "-m", "pr")
        self.clone.git(contributor, "push", "-q", "origin", "HEAD:refs/heads/their-change")
        self.clone.git(self.clone.dir, "fetch", "-q", str(fork), "their-change:pr/12")
        self.clone.git(self.clone.dir, "switch", "-q", "pr/12")
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(len(about_private(notes)), 1)
        self.assertIn("1 of the 1 commit that no remote-tracking ref of this clone reaches", notes[0])
        self.assertIn("Nothing here rewrites them: they may be somebody else's", notes[0])
        self.assertNotIn(REWRITE, notes[0])
        self.assertEqual(about_commits(notes), [])

    def test_a_detached_head_gets_no_line(self) -> None:
        self.clone.configure()
        self.clone.commit("one", email=OWN)
        self.clone.git(self.clone.dir, "switch", "-q", "--detach", "HEAD")
        self.assert_no_line(self.notes(), "HEAD is detached")

    def test_a_pushed_commit_is_not_the_subject(self) -> None:
        self.clone.commit("left", email=OWN)
        self.clone.git(self.clone.dir, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.clone.configure()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.host.ran("git", "log", "--boundary"), 1)
        with using_host(self.host):
            self.assertEqual(AUTHORSHIP.read_head(self.clone.dir)["commits"], [])

    def test_a_repository_with_no_commit_and_a_directory_that_is_none(self) -> None:
        clone = Clone(self, remote=False)
        plain = clone.work / "plain"
        plain.mkdir()
        for directory in (clone.dir, plain):
            with self.subTest(directory=directory.name):
                notes = self.notes(directory)
                self.assertEqual(len(notes), 1, notes)
                self.assertTrue(notes[0].startswith("neither a name nor an address"))
        # Outside a repository nothing is asked of one.
        self.assertEqual((self.host.ran("git", "log"), self.host.ran("git", "for-each-ref")), (0, 0))

    def test_another_name_under_this_checkouts_address_is_a_note_with_no_line(self) -> None:
        self.clone.configure(name="Alice Example")
        first = self.clone.commit("one", name="alice-m", email=ACCOUNT)
        second = self.clone.commit("two", name="alice-m", email=ACCOUNT.upper())
        self.clone.commit("three", name=None)
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(len(about_names(notes)), 1)
        self.assertTrue(notes[0].startswith("2 commits main has not pushed to its upstream are authored under"),
                        notes[0])
        self.assertIn(f"({second}, {first})", notes[0])
        self.assertIn("Nothing here rewrites them", notes[0])
        for said in (REWRITE, "alice-m", "Alice Example"):
            self.assertNotIn(said, notes[0])

    def test_what_the_name_rule_does_not_say(self) -> None:
        """Counted first: three commits are read, and none is this shape."""
        self.clone.configure(name="Alice Example")
        self.clone.commit("somebody else's commit", name="Bob Example", email=OTHER)
        self.clone.commit("a merge made on GitHub", name=None, committer=("GitHub", address("noreply", "github", "com")))
        self.clone.commit("this checkout's own", name=None)
        with using_host(self.host):
            self.assertEqual(len(AUTHORSHIP.read_head(self.clone.dir)["commits"]), 3)
        self.assertEqual(self.notes(), [])

    def test_the_name_rule_broken_on_purpose(self) -> None:
        """The same commit, under each condition the rule holds on."""
        self.clone.configure(name="Alice Example")
        self.clone.commit("one", name="alice-m", email=ACCOUNT)
        self.assertEqual(len(about_names(self.notes())), 1)
        # Another address in the configuration: the commit is no longer this checkout's.
        self.clone.configure(email=address("1234+" + LOGIN, "example", "org"), name="Alice Example")
        self.assertEqual(self.notes(), [])
        # The name of the commit in the configuration: nothing differs.
        self.clone.configure(name="alice-m")
        self.assertEqual(about_names(self.notes()), [])
        # No name configured at all: there is nothing to differ from.
        self.clone.configure(name=None)
        self.assertEqual(about_names(self.notes()), [])

    def test_an_annotated_tag_carries_its_taggers_address(self) -> None:
        self.clone.configure()
        self.clone.git(self.clone.dir, "tag", "light")
        self.clone.git(self.clone.dir, "tag", "-a", "-m", "correct", "v0.9.0")
        self.assertEqual(self.notes(), [], "a tagger with an account, and a tag with no tagger")
        self.assertEqual(self.host.ran("git", "for-each-ref"), 1)
        self.clone.git(self.clone.dir, "-c", f"user.email={OWN}", "tag", "-a", "-m", "made up", "v1.0.0")
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("1 annotated tag carries an address made up on this machine as tagger"
                                            " (v1.0.0)"), notes[0])
        self.assertNotIn(MACHINE, notes[0])

    def test_no_message_carries_an_address_a_login_or_a_machine(self) -> None:
        self.clone.configure(email=OWN_ELSEWHERE, name="Alice Account")
        self.clone.commit("one", name=None)
        self.clone.commit("two", email=MAILBOX, name="bob")
        self.clone.commit("three", email=BARE)
        self.clone.git(self.clone.dir, "-c", f"user.email={OWN}", "tag", "-a", "-m", "made up", "v1.0.0")
        notes = self.notes()
        self.assertEqual(len(notes), 4, notes)
        self.assertIn("it has no domain; it is the address git composes on this machine", notes[1])
        for note in notes:
            for said in (MACHINE, "@", "corp", "Alice Account", "bob"):
                self.assertNotIn(said, note.replace(address("ID+LOGIN", "users", "noreply", "github", "com"), ""))

    def test_where_git_cannot_be_asked_only_what_needs_no_answer_is_nobodys(self) -> None:
        self.clone.configure()
        self.clone.commit("one", email=OWN)
        self.clone.commit("two", email=BARE)
        self.host = CountingHost(env=False)
        notes = self.notes()
        self.assertEqual(self.host.lookups.count("env"), 1)
        self.assertEqual(self.host.ran("env"), 0)
        self.assertEqual((len(about_commits(notes)), len(about_private(notes))), (1, 1), notes)
        self.assertIn("1 of the 2 commits", about_commits(notes)[0])
        self.assertIn("it has no domain", about_commits(notes)[0])
        self.assertEqual([note for note in notes if REWRITE in note], [])

    def test_a_runner_reads_nothing_and_says_nothing(self) -> None:
        for message in ("one", "two"):
            self.clone.commit(message, email=OWN)
        self.assertEqual(len(self.notes()), 2, "the clone must have the defect for the silence to mean anything")
        for name in AUTHORSHIP.RUNNER_VARIABLES:
            with self.subTest(variable=name), patch.dict(os.environ, {name: "true"}):
                self.assertEqual(self.notes(), [])
                self.assertEqual((self.host.commands, self.host.lookups), ([], []))
            with self.subTest(variable=name, value="0"), patch.dict(os.environ, {name: "0"}):
                self.assertEqual(len(self.notes()), 2)


class PushedRefsTest(unittest.TestCase):
    """What a pre-push hook is handed, read line by line."""

    def test_the_three_shapes_of_a_line(self) -> None:
        local, remote = "a" * 40, "b" * 40
        self.assertEqual(AUTHORSHIP.pushed_refs(f"refs/heads/main {local} refs/heads/main {remote}\n"),
                         [{"ref": "refs/heads/main", "object": local, "remoteRef": "refs/heads/main", "held": remote}])
        # A new branch has nothing on the other side to start from.
        self.assertEqual(AUTHORSHIP.pushed_refs(f"refs/heads/fix/x {local} refs/heads/fix/x {ZEROS}\n")[0]["held"], "")
        # A deletion sends no commit.
        self.assertEqual(AUTHORSHIP.pushed_refs(f"(delete) {ZEROS} refs/heads/fix/x {remote}\n"), [])

    def test_several_refs_one_push_and_nothing_at_all(self) -> None:
        local, other, remote = "a" * 40, "c" * 40, "b" * 40
        read = AUTHORSHIP.pushed_refs(f"refs/heads/main {local} refs/heads/main {remote}\n"
                                      f"refs/tags/v1 {local} refs/tags/v1 {ZEROS}\n"
                                      f"refs/heads/fix/x {other} refs/heads/y {remote}\n"
                                      "not a line of the hook\n")
        self.assertEqual([(entry["ref"], entry["remoteRef"]) for entry in read],
                         [("refs/heads/main", "refs/heads/main"), ("refs/tags/v1", "refs/tags/v1"),
                          ("refs/heads/fix/x", "refs/heads/y")])
        self.assertEqual(AUTHORSHIP.pushed_refs(""), [])


class CommandTest(unittest.TestCase):
    """`check`, `new`, `adopt`, `outgoing` and `hook`, as an operator runs them."""

    def setUp(self) -> None:
        self.clone = Clone(self)
        self.dir = str(self.clone.dir)

    def check_notes(self, env: "dict | None" = None) -> list:
        data = self.clone.json("check", self.dir, "--mechanism", "custom", *SOCLE, expect=2, env=env)["data"]
        about = [check for check in data["checks"]
                 if " is set here" in check["message"] or "made up on this machine" in check["message"]]
        self.assertEqual({check["status"] for check in about} - {"note"}, set(), "never more than a note")
        self.assertEqual({check["scope"] for check in about} - {"conventions"}, set())
        for check in about:
            self.assertNotIn(check["message"], data["violations"])
        return [check["message"] for check in about]

    def pre_push(self, remote: str, *lines: str, expect: int = 0) -> dict:
        return self.clone.json("outgoing", self.dir, "--pre-push", remote, expect=expect,
                               stdin="".join(line + "\n" for line in lines))["data"]

    def test_check_notes_and_never_refuses(self) -> None:
        self.clone.commit("one", email=BARE)
        notes = self.check_notes()
        self.assertEqual(len(notes), 2, notes)
        text = self.clone.cli("check", self.dir, "--mechanism", "custom", *SOCLE).stdout
        self.assertIn("note     neither a name nor an address is set here", text)
        self.assertIn("note     1 of the 1 commit main has not pushed to its upstream carries", text)
        self.assertNotIn(REWRITE, text)

    def test_check_on_a_correct_clone_says_nothing(self) -> None:
        self.clone.configure()
        self.clone.commit("one", name=None)
        self.assertEqual(self.check_notes(), [])

    def test_check_is_silent_on_a_runner_and_only_on_one(self) -> None:
        self.clone.commit("one", email=BARE)
        self.assertEqual(len(self.check_notes()), 2)
        for name in AUTHORSHIP.RUNNER_VARIABLES:
            for value, count in (("true", 0), ("1", 0), ("false", 2), ("0", 2)):
                with self.subTest(variable=name, value=value):
                    self.assertEqual(len(self.check_notes(env={**self.clone.env, name: value})), count)

    def test_new_says_it_before_the_first_commit(self) -> None:
        target = str(self.clone.work / "maelys-new")
        for mode in ((), ("--apply",)):
            with self.subTest(mode=mode):
                data = self.clone.json("new", target, *mode, *SOCLE)["data"]
                self.assertEqual(len(data["notes"]), 1, data["notes"])
                self.assertTrue(data["notes"][0].startswith("neither a name nor an address is set here"))
                shutil.rmtree(target, ignore_errors=True)
        self.assertIn("note     neither a name nor an address", self.clone.cli("new", target, *SOCLE).stdout)
        self.assertEqual(self.clone.json("new", target, *SOCLE, env={**self.clone.env, "CI": "true"})["data"]["notes"],
                         [])
        # The machine's own identity, set once: nothing to say, in a directory that does not exist yet.
        self.clone.git(self.clone.work, "config", "--global", "user.email", ACCOUNT)
        self.clone.git(self.clone.work, "config", "--global", "user.name", LOGIN)
        self.assertEqual(self.clone.json("new", target, *SOCLE)["data"]["notes"], [])
        self.assertNotIn("note ", self.clone.cli("new", target, *SOCLE).stdout)

    def adoptable(self, directory: pathlib.Path) -> None:
        for name, content in (("VERSION", "1.2.3\n"), ("CHANGELOG.md", "# Changelog\n\n## 1.2.3 — 2026-09-03\n\n- One.\n")):
            (directory / name).write_text(content, encoding="utf-8")

    def test_adopt_says_it_and_proposes_the_hook_only_where_the_line_would_write_it(self) -> None:
        self.clone.commit("one", email=BARE)
        self.adoptable(self.clone.dir)
        hook = self.clone.dir / ".git" / "hooks" / "pre-push"
        for mode in ((), ("--apply",)):
            with self.subTest(mode=mode):
                data = self.clone.json("adopt", self.dir, "--mechanism", "custom", *mode, *SOCLE)["data"]
                messages = [check["message"] for check in data["checks"] if check["status"] == "note"]
                self.assertEqual((len(about_identity(messages)), len(about_commits(messages))), (1, 1), messages)
                self.assertEqual(data["hook"], f"maelys-release hook {self.clone.dir} --apply")
                self.assertFalse(hook.exists(), "adopt proposes the hook and never writes it")
        text = self.clone.cli("adopt", self.dir, "--mechanism", "custom", *SOCLE).stdout
        self.assertIn(f"hook     optional, and not a managed file: maelys-release hook {self.clone.dir} --apply", text)

        def proposed(env: "dict | None" = None) -> bool:
            return "hook" in self.clone.json("adopt", self.dir, "--mechanism", "custom", *SOCLE, env=env)["data"]

        self.assertFalse(proposed({**self.clone.env, "CI": "true"}), "not on a runner")
        # Not where the hooks live elsewhere: the line would write nothing.
        self.clone.git(self.clone.dir, "config", "core.hooksPath", "hooks")
        self.assertFalse(proposed())
        self.clone.git(self.clone.dir, "config", "--unset", "core.hooksPath")
        self.assertTrue(proposed())
        # And not twice.
        self.assertTrue(self.clone.json("hook", self.dir, "--apply")["data"]["wrote"])
        self.assertFalse(proposed())

    def test_the_proposed_line_is_one_a_shell_can_run(self) -> None:
        clone = Clone(self, name="maelys fixture")
        self.adoptable(clone.dir)
        proposal = clone.json("adopt", str(clone.dir), "--product", "maelys-fixture", "--mechanism", "custom",
                              *SOCLE)["data"]["hook"]
        words = shlex.split(proposal)
        self.assertEqual(words, ["maelys-release", "hook", str(clone.dir), "--apply"])
        self.assertTrue(clone.json(*words[1:])["data"]["wrote"])

    def test_adopt_proposes_no_hook_outside_a_repository(self) -> None:
        plain = self.clone.work / "maelys-plain"
        plain.mkdir()
        self.adoptable(plain)
        data = self.clone.json("adopt", str(plain), "--mechanism", "custom", *SOCLE)["data"]
        self.assertNotIn("hook", data)
        self.assertEqual(len(about_identity([check["message"] for check in data["checks"]])), 1)

    def test_outgoing_refuses_what_a_branch_has_ahead_of_its_upstream(self) -> None:
        self.clone.configure()
        first = self.clone.commit("one", email=BARE)
        self.clone.commit("two", name=None)
        data = self.clone.json("outgoing", self.dir, expect=2)["data"]
        self.assertEqual((data["valid"], data["source"], data["scope"], data["commits"]),
                         (False, "head", "upstream", 2))
        self.assertEqual(data["fabricated"], [{"commit": first, "rules": ["it has no domain"]}])
        self.assertEqual((len(data["violations"]), data["fix"]), (1, ""))
        text = self.clone.cli("outgoing", self.dir)
        self.assertEqual(text.returncode, 2)
        self.assertIn("REFUSED  1 of the 2 commits main has not pushed to its upstream carries", text.stdout)
        self.assertIn("outgoing: 2 commits ahead of the upstream, 1 carrying", text.stdout)
        # A runner that pushes publishes like anyone: the silence is check's.
        self.clone.json("outgoing", self.dir, expect=2, env={**self.clone.env, "CI": "true"})

    def test_outgoing_passes_a_correct_range_it_has_counted(self) -> None:
        self.clone.configure(name="Alice Example")
        self.clone.commit("one", name=None)
        renamed = self.clone.commit("two", name="alice-m", email=ACCOUNT)
        private = self.clone.commit("three", name="bob", email=MAILBOX)
        data = self.clone.json("outgoing", self.dir)["data"]
        self.assertEqual((data["valid"], data["commits"], data["fabricated"], data["violations"]), (True, 3, [], []))
        # A private name and one address under two names stay notes, here as in check.
        self.assertEqual(data["renamed"], [renamed])
        self.assertEqual(data["local"], [{"commit": private, "rules": ["its domain ends in .local"]}])
        self.assertEqual(len(data["notes"]), 2)
        self.assertIn("outgoing: 3 commits ahead of the upstream, none carrying",
                      self.clone.cli("outgoing", self.dir).stdout)

    def test_a_reading_that_did_not_happen_is_a_failure_and_never_a_count(self) -> None:
        plain = self.clone.work / "plain"
        plain.mkdir()
        empty = Clone(self, remote=False)
        broken = Clone(self)
        (broken.dir / ".git" / "HEAD").write_text("ref: refs/heads/nothing-here\n", encoding="utf-8")
        for directory in (plain, empty.dir, broken.dir):
            with self.subTest(directory=directory.name):
                error = self.clone.json("outgoing", str(directory), expect=1)["error"]
                self.assertEqual(error["code"], "PRECONDITION_FAILED")
                self.assertTrue(error["message"].startswith("nothing was read"), error["message"])
                said = self.clone.cli("outgoing", str(directory))
                self.assertEqual((said.returncode, said.stdout), (1, ""))
                self.assertNotIn("0 commits", said.stderr)
        # At push time too, and without sending to a flag the hook has already made moot.
        error = self.clone.json("outgoing", self.dir, "--pre-push", "origin", expect=1,
                                stdin=f"refs/heads/main {'1' * 40} refs/heads/main {ZEROS}\n")["error"]
        self.assertEqual(error["code"], "PROCESS_FAILED")
        self.assertTrue(error["message"].startswith("nothing was read"), error["message"])
        self.assertIn("refs/heads/main", error["message"])
        self.assertNotIn("--no-verify", error["message"] + error.get("hint", ""))

    def test_neither_command_asks_for_a_product_name(self) -> None:
        for name in ("wt.a2", "Maelys-Fixture"):
            clone = Clone(self, name=name)
            clone.configure()
            clone.commit("one", email=BARE)
            with self.subTest(name=name):
                self.assertEqual(clone.json("outgoing", str(clone.dir), expect=2)["data"]["commits"], 1)
                pushed = f"refs/heads/main {clone.full()} refs/heads/main {clone.full('origin/main')}\n"
                self.assertFalse(clone.json("outgoing", str(clone.dir), "--pre-push", "origin", expect=2,
                                            stdin=pushed)["data"]["valid"])
                self.assertTrue(clone.json("hook", str(clone.dir), "--apply")["data"]["wrote"])
        gone = self.clone.json("hook", str(self.clone.work / "absent"), expect=1)["error"]
        self.assertEqual(gone["code"], "NOT_FOUND")
        self.assertNotIn("--product", gone["message"] + gone.get("hint", ""))

    def test_a_push_is_judged_ref_by_ref_and_names_the_ref(self) -> None:
        self.clone.configure()
        pushed = self.clone.full("origin/main")
        self.clone.git(self.clone.dir, "switch", "-q", "-c", "fix/made-up")
        self.clone.commit("made up", email=BARE)
        made_up = self.clone.full()
        self.clone.git(self.clone.dir, "switch", "-q", "main")
        self.clone.commit("correct", name=None)
        correct = self.clone.full()
        main, fix = f"refs/heads/main {correct} refs/heads/main {pushed}", \
            f"refs/heads/fix/made-up {made_up} refs/heads/fix/made-up {ZEROS}"
        # HEAD is correct; the branch the push sends is not, and it is the one judged.
        passed = self.pre_push("origin", main)
        self.assertEqual((passed["scope"], passed["remote"], passed["commits"], passed["valid"]),
                         ("push", "origin", 1, True))
        refused = self.pre_push("origin", fix, expect=2)
        self.assertEqual((refused["commits"], len(refused["fabricated"])), (1, 1))
        self.assertIn("1 of the 1 commit refs/heads/fix/made-up sends to refs/heads/fix/made-up of origin carries",
                      refused["violations"][0])
        # No line: HEAD is another branch, and a line here would rewrite it.
        self.assertEqual(refused["fix"], "")
        self.assertNotIn(REWRITE, refused["violations"][0])
        self.assertIn("'maelys-release check' on the branch", refused["violations"][0])
        both = self.pre_push("origin", main, fix, expect=2)
        self.assertEqual((both["commits"], len(both["fabricated"]), len(both["violations"])), (2, 1, 1))
        # A deletion sends nothing; what the other side already holds is not sent again,
        # and an object it holds that this clone never fetched is not a refusal.
        self.assertEqual(self.pre_push("origin", f"(delete) {ZEROS} refs/heads/fix/made-up {made_up}")["commits"], 0)
        self.assertEqual(self.pre_push("origin", f"refs/heads/x {made_up} refs/heads/x {made_up}")["commits"], 0)
        self.assertEqual(self.pre_push("origin", f"refs/heads/main {correct} refs/heads/main {'1' * 40}")["commits"], 1)
        self.assertEqual(self.pre_push("origin")["commits"], 0)

    def test_a_commit_another_remote_holds_is_still_published_by_this_push(self) -> None:
        self.clone.configure()
        backup = self.clone.work / "backup.git"
        self.clone.git(self.clone.work, "init", "-q", "--bare", "-b", "main", str(backup))
        self.clone.git(self.clone.dir, "remote", "add", "backup", str(backup))
        self.clone.commit("made up", email=BARE)
        head = self.clone.full()
        self.clone.git(self.clone.dir, "push", "-q", "backup", "main")
        self.assertTrue(self.clone.git(self.clone.dir, "rev-parse", "backup/main").stdout.startswith(head))
        line = f"refs/heads/main {head} refs/heads/main {self.clone.full('origin/main')}"
        refused = self.pre_push("origin", line, expect=2)
        self.assertEqual((refused["commits"], len(refused["fabricated"])), (1, 1))
        # To the backup, which holds it, the same push sends nothing.
        self.assertEqual(self.pre_push("backup", f"refs/heads/main {head} refs/heads/main {head}")["commits"], 0)
        # A push by URL names no remote of this clone: only the object git gives is taken off.
        url = self.pre_push(str(self.clone.origin), line, expect=2)
        self.assertEqual((url["commits"], url["remote"]), (1, str(self.clone.origin)))

    def test_a_private_name_is_never_a_refusal_at_push_time(self) -> None:
        self.clone.configure()
        self.clone.commit("a contributor's", name="bob", email=MAILBOX)
        data = self.pre_push("origin", f"refs/heads/main {self.clone.full()} refs/heads/main {ZEROS}")
        self.assertEqual((data["valid"], len(data["local"]), len(data["notes"]), data["violations"]),
                         (True, 1, 1, []))
        self.assertIn("refs/heads/main sends to refs/heads/main of origin", data["notes"][0])

    def test_a_tag_made_with_no_identity_is_refused_as_a_commit_is(self) -> None:
        self.clone.configure()
        self.clone.commit("correct", name=None)
        self.clone.git(self.clone.dir, "push", "-q", "origin", "main")
        self.clone.git(self.clone.dir, "tag", "-a", "-m", "correct", "v0.9.0")
        self.clone.git(self.clone.dir, "-c", f"user.email={BARE}", "tag", "-a", "-m", "made up", "v1.0.0")
        self.clone.git(self.clone.dir, "tag", "light")

        def tag(name: str) -> str:
            return f"refs/tags/{name} {self.clone.full(name)} refs/tags/{name} {ZEROS}"

        passed = self.pre_push("origin", tag("v0.9.0"), tag("light"))
        self.assertEqual((passed["valid"], passed["tags"], passed["commits"]), (True, [], 0))
        refused = self.pre_push("origin", tag("v0.9.0"), tag("v1.0.0"), expect=2)
        self.assertEqual((refused["tags"], refused["fabricated"], len(refused["violations"])), (["v1.0.0"], [], 1))
        self.assertIn("1 annotated tag carries an address made up on this machine as tagger (v1.0.0)",
                      refused["violations"][0])
        self.assertIn("REFUSED  1 annotated tag", self.clone.cli(
            "outgoing", self.dir, "--pre-push", "origin", stdin=tag("v1.0.0") + "\n").stdout)

    def test_hook_plans_writes_and_never_overwrites(self) -> None:
        hook = self.clone.dir / ".git" / "hooks" / "pre-push"
        plan = self.clone.json("hook", self.dir)["data"]
        self.assertEqual((plan["mode"], plan["action"], plan["path"], plan["wrote"]),
                         ("plan", "create", str(hook), False))
        self.assertFalse(hook.exists())
        self.assertIn("hook: nothing written, plan only", self.clone.cli("hook", self.dir).stdout)
        written = self.clone.json("hook", self.dir, "--apply")["data"]
        self.assertEqual((written["action"], written["wrote"]), ("create", True))
        self.assertEqual(hook.read_text(encoding="utf-8"), AUTHORSHIP.HOOK_TEXT)
        self.assertTrue(hook.stat().st_mode & stat.S_IXUSR)
        again = self.clone.json("hook", self.dir, "--apply")["data"]
        self.assertEqual((again["action"], again["wrote"]), ("same", False))
        self.assertIn("hook: nothing written: it is already there", self.clone.cli("hook", self.dir, "--apply").stdout)
        # This repository's own hook is kept, byte for byte, and the answer says nothing was written.
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        kept = self.clone.json("hook", self.dir, "--apply")["data"]
        self.assertEqual((kept["action"], kept["wrote"], kept["mode"]), ("kept", False, "apply"))
        self.assertIn("does not overwrite", kept["reason"])
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")
        hook.unlink()
        # A hooks directory shared or tracked is not the socle's to write into.
        self.clone.git(self.clone.dir, "config", "core.hooksPath", "hooks")
        kept = self.clone.json("hook", self.dir, "--apply")["data"]
        self.assertEqual((kept["action"], kept["wrote"]), ("kept", False))
        self.assertIn("core.hooksPath", kept["reason"])
        self.assertFalse((self.clone.dir / "hooks").exists())
        said = self.clone.cli("hook", self.dir, "--apply").stdout
        self.assertIn("kept    ", said)
        self.assertIn("hook: nothing written, and nothing refuses a push here", said)

    def test_hook_refuses_a_directory_that_is_no_repository(self) -> None:
        plain = self.clone.work / "plain"
        plain.mkdir()
        error = self.clone.json("hook", str(plain), "--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertEqual(list(plain.iterdir()), [])


class PushTest(unittest.TestCase):
    """The hook, run by git on a real push to a bare origin of the fixture."""

    NAME = "maelys-fixture"

    def setUp(self) -> None:
        self.clone = Clone(self, name=self.NAME)
        self.clone.configure()
        self.assertTrue(self.clone.json("hook", str(self.clone.dir), "--apply")["data"]["wrote"])
        # The command as an installation gives it: one word on PATH.
        self.bin = self.clone.work / "bin"
        self.bin.mkdir()
        self.install(f'exec {shlex.quote(sys.executable)} {shlex.quote(str(CLI))} "$@"')
        self.installed = {**self.clone.env, "PATH": f"{self.bin}:{self.clone.env.get('PATH', '')}"}

    def install(self, body: str) -> None:
        command = self.bin / "maelys-release"
        command.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        command.chmod(0o755)

    def push(self, *arguments: str, env: "dict | None" = None) -> subprocess.CompletedProcess:
        return self.clone.git(self.clone.dir, "push", *(arguments or ("origin", "HEAD:refs/heads/main")),
                              env=env or self.installed, check=False)

    def remote(self, ref: str = "refs/heads/main", repository: "pathlib.Path | None" = None) -> str:
        return self.clone.git(repository or self.clone.origin, "rev-parse", "-q", "--verify", ref,
                              check=False).stdout.strip()

    def test_a_push_sending_a_made_up_address_is_refused_and_no_verify_passes_it(self) -> None:
        before = self.remote()
        self.clone.commit("one", email=BARE)
        refused = self.push()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.remote(), before, "nothing left")
        said = refused.stdout + refused.stderr
        self.assertIn("REFUSED  1 of the 1 commit HEAD sends to refs/heads/main of origin carries", said)
        self.assertNotIn(REWRITE, said)
        passed = self.push("--no-verify", "origin", "HEAD:refs/heads/main")
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(self.remote(), self.clone.full())

    def test_what_git_composes_on_this_very_machine_is_refused(self) -> None:
        """Not the fixture's address: the one this machine would write, asked of git for real."""
        composed = real_composed()
        if not composed:
            self.skipTest("git refuses to compose an address on this machine")
        before = self.remote()
        self.clone.commit("one", email=composed)
        refused = self.push()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.remote(), before)
        said = refused.stdout + refused.stderr
        self.assertIn("it is the address git composes on this machine", said)
        self.assertNotIn(composed, said)
        self.assertNotIn(composed.rpartition("@")[2], said)
        # And the line check gives for it, on this branch, is the fix: the push then passes.
        note = [check["message"] for check in self.clone.json(
            "check", str(self.clone.dir), "--product", "maelys-fixture", "--mechanism", "custom", *SOCLE,
            expect=2)["data"]["checks"] if REWRITE in check["message"]]
        self.assertEqual(len(note), 1)
        done = subprocess.run(line_of(note[0]), shell=True, cwd=self.clone.dir, env=self.clone.env,
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        passed = self.push()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.remote(), self.clone.full())

    def test_a_correct_push_passes_in_silence(self) -> None:
        self.clone.commit("one", name=None)
        passed = self.push()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.remote(), self.clone.full())
        # Counted by the command the hook runs, and unsaid by the hook.
        self.assertNotIn("outgoing", passed.stdout + passed.stderr)
        self.assertNotIn("commit", passed.stdout + passed.stderr)

    def test_the_hook_counts_the_push_it_lets_through(self) -> None:
        """What the hook runs, on the input git gives it: read, counted, and passed."""
        pushed = self.remote()
        self.clone.commit("one", name=None)
        line = f"refs/heads/main {self.clone.full()} refs/heads/main {pushed}\n"
        data = self.clone.json("outgoing", str(self.clone.dir), "--pre-push", "origin", stdin=line)["data"]
        self.assertEqual((data["source"], data["commits"], data["valid"]), ("pre-push", 1, True))
        said = self.clone.cli("outgoing", str(self.clone.dir), "--pre-push", "origin", stdin=line)
        self.assertEqual((said.returncode, said.stdout, said.stderr), (0, "", ""))

    def test_a_ref_that_is_not_head_is_the_one_refused_and_head_is_left_alone(self) -> None:
        self.clone.git(self.clone.dir, "switch", "-q", "-c", "fix/made-up")
        self.clone.commit("made up", email=BARE)
        self.clone.git(self.clone.dir, "switch", "-q", "main")
        self.clone.commit("correct", name=None)
        main = self.clone.full("main")
        refused = self.push("origin", "fix/made-up")
        self.assertNotEqual(refused.returncode, 0)
        said = refused.stdout + refused.stderr
        self.assertIn("refs/heads/fix/made-up sends to refs/heads/fix/made-up of origin", said)
        self.assertNotIn(REWRITE, said)
        self.assertEqual(self.remote("refs/heads/fix/made-up"), "")
        self.assertEqual(self.clone.full("main"), main)
        # main itself is correct, and goes.
        self.assertEqual(self.push("origin", "main").returncode, 0)

    def test_a_commit_a_backup_holds_is_refused_on_its_way_to_the_origin(self) -> None:
        backup = self.clone.work / "backup.git"
        self.clone.git(self.clone.work, "init", "-q", "--bare", "-b", "main", str(backup))
        self.clone.git(self.clone.dir, "remote", "add", "backup", str(backup))
        self.clone.commit("made up", email=BARE)
        before = self.remote()
        self.assertEqual(self.push("--no-verify", "backup", "main").returncode, 0)
        self.assertEqual(self.remote(repository=backup), self.clone.full())
        refused = self.push("origin", "main")
        self.assertNotEqual(refused.returncode, 0, "the backup holding it publishes nothing on the origin")
        self.assertEqual(self.remote(), before)

    def test_the_remote_git_names_is_the_one_judged(self) -> None:
        """What the origin holds is still new to the backup: the hook is told which, and uses it."""
        backup = self.clone.work / "backup.git"
        self.clone.git(self.clone.work, "init", "-q", "--bare", "-b", "main", str(backup))
        self.clone.git(self.clone.dir, "remote", "add", "backup", str(backup))
        self.clone.commit("made up", email=BARE)
        self.assertEqual(self.push("--no-verify", "origin", "main").returncode, 0)
        refused = self.push("backup", "main")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refs/heads/main sends to refs/heads/main of backup", refused.stdout + refused.stderr)
        self.assertEqual(self.remote(repository=backup), "")

    def test_a_tag_made_with_no_identity_is_refused(self) -> None:
        self.clone.git(self.clone.dir, "-c", f"user.email={BARE}", "tag", "-a", "-m", "made up", "v1.0.0")
        refused = self.push("origin", "v1.0.0")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("1 annotated tag carries an address made up on this machine as tagger (v1.0.0)",
                      refused.stdout + refused.stderr)
        self.assertEqual(self.remote("refs/tags/v1.0.0"), "")

    def test_the_hook_does_nothing_where_the_command_is_not_installed(self) -> None:
        """Silently, and with the very push the installed command refuses."""
        self.clone.commit("one", email=BARE)
        self.assertNotEqual(self.push().returncode, 0, "the push must be one the hook refuses when it can")
        (self.bin / "maelys-release").unlink()
        if shutil.which("maelys-release", path=self.installed["PATH"]):
            self.skipTest("this machine has maelys-release installed on PATH")
        passed = self.push()
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(self.remote(), self.clone.full())
        self.assertNotIn("maelys-release", passed.stdout + passed.stderr)
        self.assertNotIn("not found", passed.stdout + passed.stderr)

    def test_a_check_that_did_not_run_says_so_in_one_line_and_does_not_hold_the_push(self) -> None:
        self.clone.commit("one", email=BARE)
        self.install("echo 'maelys-release: broken' >&2\necho 'a second line' >&2\nexit 1")
        passed = self.push()
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(self.remote(), self.clone.full())
        said = [line for line in passed.stderr.splitlines() if "pre-push" in line or "broken" in line
                or "second line" in line]
        self.assertEqual(said, ["pre-push: maelys-release did not read this push (exit 1), which goes through unread:"
                                " maelys-release: broken"])

    def test_the_real_command_failing_at_push_time_says_what_was_not_read(self) -> None:
        """A HEAD git cannot read: the real command fails, the hook says so, the push is git's to judge."""
        self.install(f'exec {shlex.quote(sys.executable)} {shlex.quote(str(CLI))} outgoing /nonexistent')
        self.clone.commit("one", name=None)
        passed = self.push()
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("pre-push: maelys-release did not read this push (exit 1), which goes through unread:",
                      passed.stderr)


class PushFromAnOddlyNamedCloneTest(PushTest):
    """The same pushes from a directory no product could be named after."""

    NAME = "Wt.a2 copy"


if __name__ == "__main__":
    unittest.main()
