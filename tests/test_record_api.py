# SPDX-License-Identifier: MPL-2.0
"""The recorder of the API reference refuses what it was not told it may do.

It is the one tool of the reference that can read GitHub and run git, so
what it accepts is the reference's own boundary: a reference whose inputs
were not verified, a read nobody asked to be live, a process outside its
list, and a manifest that names a commit beside a version that is not that
commit's. None of this needs a recording to be measured.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
SCRIPT = GOLDEN / "record_api.py"
INPUTS = GOLDEN / "api" / "inputs"


def load_recorder():
    sys.path.insert(0, str(GOLDEN))
    try:
        spec = importlib.util.spec_from_file_location("api_golden_recorder", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(GOLDEN))


RECORDER = load_recorder()
ENDPOINT = "repos/" + RECORDER.REPOSITORIES[0]


class Original:
    """What stands behind the recorder: every call it receives is kept."""

    calls: list = []

    def which(self, name):
        Original.calls.append(("which", name))
        return "/original/" + name

    def read(self, path):
        Original.calls.append(("read", path))
        return "ok", {"read": "live"}

    def run(self, command, cwd=None, env=None):
        Original.calls.append(("run", command))
        return subprocess.CompletedProcess(command, 0, "original\n", "")


class RecorderCommandTest(unittest.TestCase):
    """The command line, on a source that is a commit and nothing else."""

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-recorder.")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.source = self.work / "source"
        self.source.mkdir()
        self.git("init", "-q")
        (self.source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "a candidate")
        self.commit = self.git("rev-parse", "HEAD")
        # The suite runs inside a self-test environment, which the recorder
        # refuses first; these tests are about what it refuses next.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("_MAELYS_RELEASE_")}

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.source), "-c", "user.name=test",
                               "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *arguments],
                              text=True, capture_output=True, check=True).stdout.strip()

    def record(self, *options: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.work / "output"), "--source", str(self.source),
                               "--commit", self.commit, *options], env=self.env, text=True, capture_output=True,
                              check=False)

    def refused(self, *options: str) -> str:
        completed = self.record(*options)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertFalse((self.work / "output").exists())
        return completed.stderr

    def test_a_reference_is_verified_before_a_line_of_it_is_used(self) -> None:
        cache = str(self.work / "cache.json")
        # A directory that only looks like one: the two files the recording reads, and nothing else.
        bare = self.work / "bare"
        (bare / "socle").mkdir(parents=True)
        for name in ("VERSION", "CHANGELOG.md"):
            shutil.copy2(INPUTS / "socle" / name, bare / "socle" / name)
        self.assertIn("--reference is not a verified set of inputs",
                      self.refused("--responses-cache", cache, "--reference", str(bare)))
        # The real inputs with one frozen file edited: the version a whole recording would run under.
        edited = self.work / "edited"
        shutil.copytree(INPUTS, edited)
        (edited / "socle" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
        said = self.refused("--responses-cache", cache, "--reference", str(edited))
        self.assertIn("--reference is not a verified set of inputs", said)
        self.assertIn("digest differs: socle/VERSION", said)
        self.assertIn("--reference is not a verified set of inputs",
                      self.refused("--responses-cache", cache, "--reference", str(self.work / "absent")))

    def test_verified_inputs_pass_that_refusal(self) -> None:
        """Counted at the other end: the committed inputs are not what the refusal above refuses."""
        completed = self.record("--responses-cache", str(INPUTS / "responses.json"), "--offline",
                                "--reference", str(INPUTS))
        self.assertNotIn("--reference is not a verified set of inputs", completed.stderr)
        self.assertNotEqual(completed.returncode, 2, "it stops later, on a source that is no socle")
        self.assertFalse((self.work / "output").exists())

    def test_offline_needs_a_cache_to_answer_for_it(self) -> None:
        self.assertIn("--offline needs the responses cache to exist",
                      self.refused("--responses-cache", str(self.work / "absent.json"), "--offline"))

    def test_the_first_refusals_stand(self) -> None:
        self.assertIn("--commit must name the full HEAD", subprocess.run(
            [sys.executable, str(SCRIPT), str(self.work / "output"), "--source", str(self.source), "--commit",
             "0" * 40, "--responses-cache", str(self.work / "cache.json")], env=self.env, text=True,
            capture_output=True, check=False).stderr)
        inside = subprocess.run([sys.executable, str(SCRIPT), str(self.work / "output"), "--source", str(self.source),
                                 "--commit", self.commit, "--responses-cache", str(self.work / "cache.json")],
                                env={**self.env, "_MAELYS_RELEASE_SELF_TEST": "1"}, text=True, capture_output=True,
                                check=False)
        self.assertIn("record outside a self-test environment", inside.stderr)


class ResponsesTest(unittest.TestCase):
    """A read is the cache's or GitHub's, and the recording says which it allows."""

    def setUp(self) -> None:
        Original.calls = []
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-recorder.")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cache = self.work / "cache.json"
        self.cache.write_text(json.dumps({ENDPOINT: {"state": "ok", "body": {"read": "recorded"}, "provenance": {
            "kind": "live", "method": "GET", "endpoint": ENDPOINT, "recordedAt": "2026-09-16T00:00:00+00:00"}}}),
            encoding="utf-8")

    def test_offline_serves_the_cache_and_refuses_everything_else(self) -> None:
        before = self.cache.read_bytes()
        responses = RECORDER.Responses(Original(), self.cache, offline=True)
        key, answer = responses.read(ENDPOINT, "live")
        self.assertEqual((key, answer["body"]), (ENDPOINT, {"read": "recorded"}))
        with self.assertRaisesRegex(RuntimeError, r"live read refused \(--offline\): " + ENDPOINT + "/branches"):
            responses.read(ENDPOINT + "/branches", "live")
        self.assertEqual(Original.calls, [], "nothing was asked of GitHub")
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(list(responses.used), [ENDPOINT])

    def test_without_it_a_miss_is_read_live_and_recorded_as_such(self) -> None:
        """The default, said so that nobody takes a cache for a promise."""
        responses = RECORDER.Responses(Original(), self.cache)
        _, answer = responses.read(ENDPOINT + "/branches", "live")
        self.assertEqual(Original.calls, [("read", ENDPOINT + "/branches")])
        self.assertEqual((answer["body"], answer["provenance"]["kind"]), ({"read": "live"}, "live"))
        self.assertIn(ENDPOINT + "/branches", json.loads(self.cache.read_text(encoding="utf-8")))


class WithheldTest(unittest.TestCase):
    """What GitHub says of a real repository's security settings is in no stored answer."""

    SETTINGS = {"secret_scanning": {"status": "disabled"}, "secret_scanning_push_protection": {"status": "disabled"}}

    def setUp(self) -> None:
        Original.calls = []
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-recorder.")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cache = self.work / "cache.json"
        # As GitHub answers an administrator, and as a cache written before
        # this rule holds it: the member at the top of a repository document,
        # and once more in a document that embeds one.
        self.body = {"visibility": "public", "private": False, "default_branch": "main",
                     "security_and_analysis": self.SETTINGS,
                     "parent": {"name": "embedded", "security_and_analysis": self.SETTINGS},
                     "topics": [{"security_and_analysis": self.SETTINGS, "kept": True}]}

    def cached(self) -> None:
        self.cache.write_text(json.dumps({ENDPOINT: {"state": "ok", "body": self.body, "provenance": {
            "kind": "live", "method": "GET", "endpoint": ENDPOINT, "recordedAt": "2026-09-16T00:00:00+00:00"}}}),
            encoding="utf-8")

    def assert_withheld(self, value) -> None:
        self.assertNotIn(RECORDER.WITHHELD[0], json.dumps(value))

    def test_an_answer_taken_from_the_cache_loses_the_member_and_nothing_else(self) -> None:
        self.cached()
        before = self.cache.read_bytes()
        responses = RECORDER.Responses(Original(), self.cache, offline=True)
        _, answer = responses.read(ENDPOINT, "live")
        self.assertEqual(answer["body"], {"visibility": "public", "private": False, "default_branch": "main",
                                          "parent": {"name": "embedded"}, "topics": [{"kept": True}]})
        # The answer derived from it for a constructed branch has none either.
        key, derived = responses.read(ENDPOINT, "private")
        self.assertEqual((key, derived["body"]["visibility"]), ("private:" + ENDPOINT, "private"))
        self.assert_withheld(responses.used)
        self.assertEqual(sorted(responses.used), sorted([ENDPOINT, "private:" + ENDPOINT]))
        # Nothing was read live, so the cache is the file it was.
        self.assertEqual((self.cache.read_bytes(), Original.calls), (before, []))

    def test_an_answer_read_live_is_stored_and_cached_without_it(self) -> None:
        body = self.body

        class Administrator(Original):
            def read(self, path):
                Original.calls.append(("read", path))
                return "ok", json.loads(json.dumps(body))

        responses = RECORDER.Responses(Administrator(), self.cache)
        _, answer = responses.read(ENDPOINT, "live")
        self.assertEqual(Original.calls, [("read", ENDPOINT)])
        self.assertEqual(answer["body"]["default_branch"], "main")
        for stored in (answer, responses.used, json.loads(self.cache.read_text(encoding="utf-8"))):
            self.assert_withheld(stored)

    def test_what_is_stored_makes_preflight_say_nothing_of_the_settings(self) -> None:
        """Both ends: the answer as GitHub gave it makes the note, the answer as stored makes none."""
        self.cached()
        _, stored = RECORDER.Responses(Original(), self.cache, offline=True).read(ENDPOINT, "live")
        github = RECORDER.replay.load_source(ROOT).github
        repository = ENDPOINT[len("repos/"):]
        told = github.secret_scanning(repository, github.Repository("ok", self.body))
        self.assertEqual([status for status, _ in told], ["note"])
        self.assertIn("secret scanning and push protection are off", told[0][1])
        self.assertEqual(github.secret_scanning(repository, github.Repository(stored["state"], stored["body"])), [])
        # And the stored answer is still a public repository's, read as one.
        self.assertEqual(github.Repository(stored["state"], stored["body"]).visibility, "public")

    def test_only_what_is_named_is_withheld(self) -> None:
        self.assertEqual(RECORDER.WITHHELD, ("security_and_analysis",))
        for value in (None, [], "text", 3, {"security": 1, "analysis": {"security_and_analysis_": 2}}):
            self.assertEqual(RECORDER.withheld(value), value)


class RecordingHostTest(unittest.TestCase):
    """What a recorded program may ask the machine, and what is answered in its place."""

    def setUp(self) -> None:
        Original.calls = []
        self.project = pathlib.Path("/fixture/product")
        self.host = RECORDER.recording_host(types.SimpleNamespace(Host=Original), self.project,
                                            pathlib.Path("/fixture/socle"), None, "live")

    def test_what_git_would_compose_is_answered_and_never_asked(self) -> None:
        self.assertEqual(self.host.which("env"), RECORDER.MACHINE["env"])
        answer = self.host.run(RECORDER.COMPOSED)
        self.assertEqual((answer.returncode, answer.stdout), (0, RECORDER.MACHINE["stdout"]))
        self.assertEqual(Original.calls, [], "the recording machine was asked nothing")
        self.assertEqual(self.host.events, [
            {"call": {"kind": "which", "name": "env"}, "result": RECORDER.MACHINE["env"]},
            {"call": {"kind": "run", "argv": RECORDER.COMPOSED, "cwd": None},
             "result": {"returncode": 0, "stdout": RECORDER.MACHINE["stdout"], "stderr": ""}}])
        # A machine that does not exist, under a name nothing resolves.
        self.assertIn("@golden-machine.invalid>", RECORDER.MACHINE["stdout"])

    def test_env_is_one_question_and_no_other_process(self) -> None:
        for command, cwd in ((["env"], None), (["env", "git", "push"], None), ([*RECORDER.COMPOSED, "extra"], None),
                             (RECORDER.COMPOSED[:-1], None), (RECORDER.COMPOSED, self.project), (["hostname"], None)):
            with self.subTest(command=command[-2:], cwd=cwd), self.assertRaises(AssertionError):
                self.host.run(command, cwd=cwd)
        with self.assertRaises(AssertionError):
            self.host.run(RECORDER.COMPOSED, env={"PATH": ""})
        for name in ("brew", "docker"):
            with self.subTest(name=name), self.assertRaisesRegex(AssertionError, "unexpected tool lookup"):
                self.host.which(name)
        self.assertEqual(Original.calls, [])

    def test_the_machine_has_no_name_to_give_and_is_never_asked_for_one(self) -> None:
        """The privacy guard looks its two tools up; the recorder answers that neither exists.

        Refusing the lookup, as every lookup but `gh` and `env` is refused,
        would refuse every recorded `check`: the guard asks on each. Running
        either tool stays what it was, a process outside the list.
        """
        for name in ("hostname", "scutil"):
            with self.subTest(name=name):
                self.assertIsNone(self.host.which(name))
        self.assertEqual(self.host.events, [{"call": {"kind": "which", "name": name}, "result": None}
                                            for name in ("hostname", "scutil")])
        for command in (["hostname"], ["hostname", "-s"], ["scutil", "--get", "ComputerName"]):
            with self.subTest(command=command), self.assertRaises(AssertionError):
                self.host.run(command)
        self.assertEqual(Original.calls, [], "the recording machine was asked nothing")

    def test_the_git_it_runs_is_a_list_of_reads_in_the_product(self) -> None:
        reads = (["git", "config", "-z", "--get-regexp", "^user"], ["git", "config", "--get", "core.hooksPath"],
                 ["git", "symbolic-ref", "-q", "--short", "HEAD"], ["git", "rev-parse", "-q", "--verify", "main@{u}"],
                 ["git", "log", "--boundary", "--format=%h", "HEAD"], ["git", "for-each-ref", "refs/tags"],
                 ["git", "remote", "get-url", "origin"], ["git", "ls-files", "-z"])
        for command in reads:
            with self.subTest(command=command[:3]):
                self.assertEqual(self.host.run(command, cwd=self.project).stdout, "original\n")
        self.assertEqual([call[1] for call in Original.calls], list(reads))
        refused = (["git", "config", "user.email", "x"], ["git", "config", "--unset", "user.email"],
                   ["git", "symbolic-ref", "HEAD", "refs/heads/other"], ["git", "log", "-1"], ["git", "commit"],
                   ["git", "push"], ["git", "update-ref", "HEAD", "0" * 40], ["git", "tag", "v1"], ["rm", "-rf", "x"])
        for command in refused:
            with self.subTest(command=command), self.assertRaisesRegex(AssertionError, "unapproved recording process"):
                self.host.run(command, cwd=self.project)
        # A read anywhere else than the product is not one of them either.
        with self.assertRaisesRegex(AssertionError, "unapproved recording process"):
            self.host.run(reads[0], cwd=pathlib.Path("/elsewhere"))
        self.assertEqual(len(Original.calls), len(reads))

    def test_writes_streams_and_exec_stay_refused(self) -> None:
        for call in (lambda: self.host.write("PUT", "repos/o/r", pathlib.Path("payload")),
                     lambda: self.host.stream(["git", "status"], sys.stdout),
                     lambda: self.host.exec("/bin/sh", ["sh"], {})):
            with self.assertRaises(AssertionError):
                call()


class ProvenanceTest(unittest.TestCase):
    def test_the_manifest_names_the_commit_its_version_and_the_frozen_one(self) -> None:
        commit = "c" * 40
        self.assertEqual(RECORDER.provenance(commit, "0.63.3", "0.60.0"),
                         {"schemaVersion": 1, "sourceCommit": commit, "sourceVersion": "0.63.3",
                          "frozenVersion": "0.60.0"})
        # A plain recording freezes what it carries: the two say the same, and both are said.
        plain = RECORDER.provenance(commit, "0.63.3", "0.63.3")
        self.assertEqual(plain["sourceVersion"], plain["frozenVersion"])

    def test_the_question_asked_of_git_takes_every_identity_away(self) -> None:
        """The argv the recorder answers is one that could not have read the operator's own."""
        argv = RECORDER.COMPOSED
        self.assertEqual(argv[0], "env")
        removed = {argv[index + 1] for index, word in enumerate(argv) if word == "-u"}
        self.assertLessEqual({"EMAIL", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_NAME",
                              "GIT_COMMITTER_NAME", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}, removed)
        for assignment in ("GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null", "GIT_CONFIG_NOSYSTEM=1"):
            self.assertIn(assignment, argv)
        self.assertEqual(argv[-5:], ["git", "-C", "/", "var", "GIT_COMMITTER_IDENT"])


if __name__ == "__main__":
    unittest.main()
