# SPDX-License-Identifier: MPL-2.0
"""Tests of bin/maelys-release on a throwaway product.

Black-box through the executable for the contract (envelopes, exit codes,
text rendering), plus a few unit tests of the pure functions loaded from
the same file. They need git and python3; ssh-keygen for the signed-tag
case; nothing else.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import ast
import atexit
import io
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "maelys-release"
# A conformant product with pins says where its build reads them. Appended
# and never prepended: the refusals of this file are reported with their
# line number, and three lines at the top would move every one of them.
APART = "\n[dependencies]\napart\n"
PINNED_TAG = "v0.0.1"


def load_module():
    loader = importlib.machinery.SourceFileLoader("maelys_release", str(CLI))
    spec = importlib.util.spec_from_loader("maelys_release", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


MODULE = load_module()
# Also guard direct unittest runs and every CLI subprocess they start. Import
# first so an invalid inherited self-test environment fails instead of being
# silently replaced by a fresh token. A self-test child reuses its live token.
if not MODULE.host.HOST._self_test():
    _test_environment = MODULE.host.HOST.self_test_environment()
    os.environ.update(_test_environment.__enter__())
    atexit.register(_test_environment.__exit__, None, None, None)


@contextmanager
def using_host(host):
    previous = MODULE.host.HOST
    MODULE.host.HOST = host
    try:
        yield host
    finally:
        MODULE.host.HOST = previous


class FakeHost(MODULE.Host):
    """No shell or network fallback. Each write needs an explicit handler."""

    def __init__(self, answers=None, *, read=None, write=None, run=None, gh=True):
        self.answers = answers or {}
        self.reader, self.writer, self.runner, self.gh = read, write, run, gh
        self.reads, self.writes, self.commands = [], [], []

    def which(self, name):
        return "/fake/gh" if name == "gh" and self.gh else None

    def read(self, path):
        self.reads.append(path)
        if self.reader:
            return self.reader(path)
        if path not in self.answers:
            raise AssertionError(f"undeclared GitHub read: {path}")
        state, body = self.answers[path]
        return state, json.loads(json.dumps(body))

    def write(self, method, endpoint, payload, cwd=None):
        body = json.loads(payload.read_text())
        self.writes.append((method, endpoint, body))
        if self.writer is None:
            raise AssertionError(f"undeclared GitHub write: {method} {endpoint}")
        return self.writer(method, endpoint, body)

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if self.runner:
            return self.runner(command, cwd=cwd, env=env)
        if command == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/o/r.git\n", "")
        raise AssertionError(f"undeclared command: {command!r}")

    def stream(self, command, log, cwd=None, env=None):
        raise AssertionError(f"undeclared streaming command: {command!r}")

    def exec(self, executable, command, env):
        raise AssertionError(f"undeclared exec: {executable}")


@contextmanager
def workflow_project(caller="check", fuzz=False):
    """Real workflow input for socle_check_contexts, including all three legs."""
    with tempfile.TemporaryDirectory(prefix="maelys-host-") as directory:
        project = pathlib.Path(directory) / "maelys-fixture"
        ci = project / ".github" / "workflows" / "ci.yml"
        ci.parent.mkdir(parents=True)
        ci.write_text(f"jobs:\n  {caller}:\n"
                      "    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@" + "f" * 40
                      + " # v9.9.9\n    with:\n      sanitizer_command: ''\n"
                      + ("      fuzz_command: make fuzz\n" if fuzz else ""))
        yield project


def protection_host(classic, *, rules=("ok", []), seen=(), open_pulls=(), write=None, read=None):
    """API inputs for real repository, observation and withdrawal readers."""
    answers = {
        "repos/o/r": ("ok", {"default_branch": "main"}),
        "repos/o/r/branches/main/protection": classic,
        "repos/o/r/rules/branches/main": rules,
        "repos/o/r/pulls?state=closed&per_page=30":
            ("ok", [{"merged_at": "x", "head": {"sha": "abc"}}]),
        "repos/o/r/commits/abc/check-runs?per_page=100":
            ("ok", {"check_runs": [{"name": name, "conclusion": "success"} for name in seen]}),
        "repos/o/r/pulls?state=open&base=main&per_page=50":
            ("ok", [{"number": number} for number in open_pulls]),
        f"repos/{MODULE.SOCLE_REPOSITORY}/contents/WITHDRAWN":
            ("ok", {"encoding": "base64", "content": ""}),
    }

    def answer(path):
        # Protection/rules callbacks own the same state the writer updates.
        if read and path not in ("repos/o/r", f"repos/{MODULE.SOCLE_REPOSITORY}/contents/WITHDRAWN"):
            return read(path)
        if path not in answers:
            raise AssertionError(f"undeclared GitHub read: {path}")
        return json.loads(json.dumps(answers[path]))
    return FakeHost(read=answer, write=write)


ALL_LEGS = ["check / check (linux)", "check / check (linux-arm64)", "check / check (macos)"]


class Product:
    """A product fixture with one pinned dependency served from a bare repository."""

    def __init__(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-test."))
        # The host's git configuration must not make the preflight pass or fail.
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1", "MAELYS_CLI_FORMAT": ""}
        self.env.pop("MAELYS_CLI_FORMAT")
        source = self.work / "src" / "maelys-system"
        source.mkdir(parents=True)
        self.git(source, "init", "-q")
        (source / "file").write_text("one\n")
        self.git(source, "add", "file")
        self.git(source, "commit", "-q", "-m", "one")
        self.git(source, "tag", PINNED_TAG)
        (source / "file").write_text("two\n")
        self.git(source, "commit", "-q", "-am", "two")
        self.tagged = self.git(source, "rev-list", "-n", "1", PINNED_TAG)
        self.pinned = self.git(source, "rev-parse", "HEAD")
        remotes = self.work / "remotes"
        remotes.mkdir()
        self.git(self.work, "clone", "-q", "--bare", str(source), str(remotes / "maelys-system.git"))
        self.git(remotes / "maelys-system.git", "config", "uploadpack.allowFilter", "true")
        self.env["MAELYS_GIT_BASE"] = f"file://{remotes}"
        self.dir = self.work / "maelys-fixture"
        (self.dir / "scripts").mkdir(parents=True)
        (self.dir / "packaging" / "homebrew").mkdir(parents=True)
        (self.dir / "dependencies").mkdir()
        self.write("VERSION", "1.2.3\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.write("scripts/package-release.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.write("dependencies/maelys-system.pin", f"{PINNED_TAG}-1-g{self.pinned[:7]}\n{self.pinned}\n")
        self.write("dependencies/packages", "# build inputs\n[linux]\npkg-config\nlibjansson-dev\n\n[macos]\njansson\n")
        self.write("maelys-release.conf", APART.lstrip("\n"))
        # A conformant product with pins says where its build reads them.
        self.write("packaging/homebrew/maelys-fixture.rb.in", "class MaelysFixture < Formula\nend\n")
        self.write("packaging/homebrew/libmaelys-fixture.rb.in", "class LibmaelysFixture < Formula\nend\n")
        self.write("AGENTS.md", "# Agent instructions\n\nKeep me.\n")

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def git(self, cwd: pathlib.Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd.resolve()), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main",
             *arguments], cwd=cwd, env=self.env, check=True, text=True, stdout=subprocess.PIPE)
        return completed.stdout.strip()

    def write(self, relative: str, content: str, executable: bool = False) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)

    def read(self, relative: str) -> str:
        return (self.dir / relative).read_text(encoding="utf-8")

    SOCLE = ("--socle-sha", "f" * 40, "--socle-tag", "v9.9.9")

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        # The checkout under test may carry uncommitted changes, which adopt
        # refuses; the tests name the socle commit explicitly instead.
        if arguments and arguments[0] in ("adopt", "check", "preflight", "rehearse") and "--socle-sha" not in arguments:
            arguments = (*arguments, *self.SOCLE)
        # A repository with no release.yml declares its mechanism once, as an
        # operator does at a first adoption; afterwards the file says it.
        if arguments and arguments[0] in ("adopt", "check") \
                and "--mechanism" not in arguments \
                and not (self.dir / ".github" / "workflows" / "release.yml").is_file():
            arguments = (*arguments, "--mechanism", "maelys-release")
        return subprocess.run([str(CLI), *arguments], cwd=self.work, env=self.env, check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run(self, *arguments: str, expect: int = 0) -> subprocess.CompletedProcess:
        completed = self.cli(*arguments)
        if completed.returncode != expect:
            raise AssertionError(f"{arguments}: exit {completed.returncode}, expected {expect}\n"
                                 f"stdout: {completed.stdout}\nstderr: {completed.stderr}")
        return completed

    def json(self, *arguments: str, expect: int = 0) -> dict:
        completed = self.run(*arguments, "--format", "json", "--compact", expect=expect)
        stream = completed.stdout if expect != 1 else completed.stderr
        other = completed.stderr if expect != 1 else completed.stdout
        assert other == "", f"the other stream is not empty: {other!r}"
        body = json.loads(stream)
        assert body["contract"] == "agent-cli/v2" and body["schemaVersion"] == 2, body
        assert body["exitCode"] == expect and body["ok"] == (expect != 1), body
        return body


class ContractTest(unittest.TestCase):
    """The agent-cli/v2 surface: catalog, envelopes, errors, completion."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.product = Product()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.product.close()

    def test_version_and_describe(self) -> None:
        version = self.product.json("version")["data"]
        self.assertEqual(version["program"], "maelys-release")
        self.assertEqual(version["version"], (ROOT / "VERSION").read_text().strip())
        catalog = self.product.json("describe")["data"]
        self.assertEqual(catalog["kind"], "catalog")
        identifiers = [command["id"] for command in catalog["commands"]]
        for expected in ("help", "version", "describe", "completion", "adopt", "check", "preflight",
                         "rehearse", "render", "tap", "self-test"):
            self.assertIn(expected, identifiers)
        for command in catalog["commands"]:
            self.assertEqual(command["usage"], command["input"]["synopsis"])
            self.assertIn("outputSchema", command)
            self.assertEqual(command["exitCodes"]["2"], "valid report with violations")
        summary = self.product.json("describe", "--summary")["data"]
        self.assertEqual(summary["kind"], "summary")
        self.assertNotIn("outputSchema", summary["commands"][0])
        one = self.product.json("describe", "adopt")["data"]
        self.assertEqual(one["kind"], "command")
        self.assertEqual(one["commands"][0]["effect"], {"plan": "preview", "apply": "apply"})
        self.assertNotIn("globalOptions", one)

    def test_help(self) -> None:
        text = self.product.run("help").stdout
        self.assertIn("adopt DIR [--product NAME] [--mechanism MECHANISM] [--allow-untagged] [--allow-lock] [--apply]", text)
        self.assertNotIn("--socle-sha", text)                       # hidden: parsed, described, never shown
        self.assertEqual(self.product.run("--help").stdout, text)
        self.assertIn("OPTIONS", self.product.run("help", "adopt").stdout)
        self.assertEqual(self.product.run("adopt", "--help").stdout, self.product.run("help", "adopt").stdout)
        body = self.product.json("help", "check")
        self.assertEqual(body["data"]["commands"], ["check"])

    def test_failures(self) -> None:
        unknown = self.product.run("frobnicate", expect=1)
        self.assertEqual(unknown.stdout, "")
        self.assertIn("maelys-release: [INVALID_COMMAND]", unknown.stderr)
        self.assertIn("Hint:", unknown.stderr)
        error = self.product.json("adopt", str(self.product.dir), "--bogus", expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("--bogus", error["message"])
        error = self.product.json("adopt", str(self.product.dir), "--dry-run", expect=1)["error"]
        self.assertIn("plans by default", error["message"])
        error = self.product.json("adopt", expect=1)["error"]
        self.assertIn("Operands do not match", error["message"])
        error = self.product.json("rehearse", str(self.product.dir), "macos-arm64", expect=1)["error"]
        self.assertIn("linux-x86_64, linux-arm64", error["message"])
        error = self.product.json("adopt", str(self.product.dir), "--apply", "--apply", expect=1)["error"]
        self.assertIn("twice", error["message"])
        self.assertEqual(self.product.run("version", "--format", "jsonl", expect=1).stdout, "")
        env_json = subprocess.run([str(CLI), "version"], env={**self.product.env, "MAELYS_CLI_FORMAT": "json"},
                                  check=True, text=True, stdout=subprocess.PIPE)
        self.assertEqual(json.loads(env_json.stdout)["command"], "version")

    def test_completion(self) -> None:
        self.assertEqual(self.product.run("__complete", "--", "ad").stdout, "adopt\n")
        self.assertIn("--apply", self.product.run("__complete", "--", "adopt", "x", "--").stdout.split())
        self.assertEqual(self.product.run("__complete", "--", "rehearse", "x", "li").stdout, "linux-arm64\nlinux-x86_64\n")
        completed = self.product.run("__complete", "--format", "json", "--compact", "--", "de")
        self.assertEqual(json.loads(completed.stdout)["data"]["records"],
                         [{"word": "declarations"}, {"word": "dependencies"}, {"word": "describe"}])
        jsonl = self.product.run("__complete", "--format", "jsonl", "--", "ver").stdout
        self.assertEqual(json.loads(jsonl), {"word": "version"})
        for shell in ("bash", "zsh", "fish"):
            self.assertIn("__complete", self.product.run("completion", shell).stdout)
        self.assertEqual(self.product.run("completion", "tcsh", expect=1).stdout, "")


class AdoptTest(unittest.TestCase):
    """adopt, check and the generated files on the fixture."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)

    def tearDown(self) -> None:
        self.product.close()

    def test_the_shared_context_observes_a_replaced_host_before_adoption_writes(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        before = {str(path.relative_to(self.product.dir)): path.read_bytes()
                  for path in self.product.dir.rglob("*") if path.is_file()}
        fake = protection_host(("ok", {"required_status_checks": {"contexts": ["check / check (gone)"]}}))
        def local_read(command, cwd=None, env=None):
            if command == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(command, 0, "https://github.com/o/r.git\n", "")
            self.assertEqual(cwd, self.product.dir.resolve())
            self.assertIn(command[:2], (["git", "grep"], ["git", "rev-parse"]))
            return MODULE.Host.run(fake, command, cwd=cwd, env=self.product.env)
        fake.runner = local_read
        invocation, _ = MODULE.APP.parse(["adopt", self.dir, *Product.SOCLE, "--apply"])
        # MODULE and its context already exist when the host is replaced.
        # The real declaration reader and lock guard must still reach it.
        with using_host(fake), self.assertRaises(MODULE.Failure) as refused:
            MODULE.handle_adopt(invocation)
        self.assertEqual(refused.exception.code, "PRECONDITION_FAILED")
        self.assertIn("check / check (gone)", refused.exception.message)
        self.assertIn("--without-legs --apply", refused.exception.hint)
        self.assertIn("then", refused.exception.hint)
        self.assertIn("repos/o/r/branches/main/protection", fake.reads)
        self.assertEqual(fake.writes, [])
        after = {str(path.relative_to(self.product.dir)): path.read_bytes()
                 for path in self.product.dir.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_contract_refusals(self) -> None:
        product = self.product
        cases = [
            ("VERSION", None, "VERSION file"),
            ("CHANGELOG.md", "# Changelog\n\n## Unreleased\n", "dated entry"),
            ("scripts/checkout-system.sh", "#!/bin/sh\nexit 0\n", "delete these"),
            ("dependencies/maelys-system.pin", "v0.0.1\nnot-a-commit\n", "line 2 must be the pinned commit"),
            ("dependencies/packages", "pkg-config\n[linux]\n", "outside a [linux] or [macos] section"),
            ("dependencies/packages", "[windows]\nfoo\n", "unknown section"),
            ("dependencies/packages", "[linux]\nfoo bar\n", "one package per line"),
            ("adapter/MAELYS_SYSTEM_PIN", "v0\n" + "a" * 40 + "\n", "adapter/ is the pre-0.14 layout"),
            ("dependencies/Bad_Name.pin", "v0\n" + "a" * 40 + "\n", "named after the repository"),
        ]
        for relative, content, expected in cases:
            with self.subTest(relative=relative, expected=expected):
                path = product.dir / relative
                backup = path.read_text() if path.is_file() else None
                if content is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                error = product.json("adopt", self.dir, expect=1)["error"]
                self.assertEqual(error["code"], "PRECONDITION_FAILED")
                self.assertIn(expected, error["message"])
                check = product.json("check", self.dir, expect=2)["data"]
                self.assertFalse(check["valid"])
                self.assertTrue(any(expected in violation for violation in check["violations"]))
                if backup is None:
                    path.unlink()
                    if path.parent.name == "adapter":
                        path.parent.rmdir()
                else:
                    path.write_text(backup)

    def test_plan_apply_and_generated_files(self) -> None:
        product = self.product
        planned = product.json("adopt", self.dir)["data"]
        self.assertEqual(planned["mode"], "plan")
        self.assertTrue(planned["changed"])
        actions = {entry["path"]: entry["action"] for entry in planned["files"]}
        self.assertEqual(actions["AGENTS.md"], "update")            # the fixture's own file gains the block
        self.assertEqual({action for path, action in actions.items() if path != "AGENTS.md"}, {"create"})
        self.assertFalse((product.dir / ".github" / "workflows" / "release.yml").exists())
        self.assertEqual(product.json("check", self.dir, expect=2)["data"]["valid"], False)
        text = product.run("adopt", self.dir).stdout
        self.assertIn("create   .github/workflows/release.yml", text)
        self.assertIn("adopt: plan only", text)
        applied = product.json("adopt", self.dir, "--apply")["data"]
        self.assertEqual(applied["mode"], "apply")
        workflow = product.read(".github/workflows/release.yml")
        for expected in (
            "product: maelys-fixture",
            "\n  id-token: write",
            "  workflow_dispatch:",
            "      tag: ${{ inputs.tag || github.ref_name }}",
            '        sh scripts/checkout-dependencies.sh "$RUNNER_TEMP/dependencies" >>"$GITHUB_ENV"\n',
            "      linux_packages: build-essential dpkg-dev file rpm pkg-config libjansson-dev\n",
            "      macos_packages: jansson\n",
            "  tap-maelys-fixture:",
            "  tap-libmaelys-fixture:",
            "      product: libmaelys-fixture",
        ):
            self.assertIn(expected, workflow)
        self.assertNotIn("if: github.event_name == 'push'", workflow)
        self.assertGreaterEqual(workflow.count("      id-token: write"), 2)
        agents = product.read("AGENTS.md")
        for name in ("AGENTS.md", "CLAUDE.md", ".claude/skills/maelys-release/SKILL.md"):
            text = product.read(name)
            self.assertIn("SPDX-License-Identifier: CC-BY-4.0", text)
            self.assertIn("Copyright 2026 David Bromberg", text)
            self.assertIn("https://creativecommons.org/licenses/by/4.0/", text)
            self.assertIn("Source: https://github.com/maelys-dev/maelys-release/", text)
            self.assertNotIn("CC0", text)
        self.assertIn("Keep me.", agents)
        self.assertEqual(agents.count("maelys-release:begin"), 1)
        self.assertIn("packaging/homebrew/libmaelys-fixture.rb.in, packaging/homebrew/maelys-fixture.rb.in", agents)
        self.assertNotIn("@FORMULAS@", agents)
        skill = product.read(".claude/skills/maelys-release/SKILL.md")
        self.assertIn("packaging/homebrew/libmaelys-fixture.rb.in", skill)
        self.assertNotIn("@PRODUCT@", skill)
        self.assertTrue((product.dir / "CLAUDE.md").is_file())
        self.assertTrue((product.dir / "RELEASING.md").is_file())
        self.assertTrue(os.access(product.dir / "scripts" / "checkout-dependency.sh", os.X_OK))
        ci = product.read(".github/workflows/ci.yml")
        self.assertIn("check-product.yml@", ci)
        self.assertIn("      product: maelys-fixture\n", ci)
        self.assertNotIn("dependency_checkout", ci)          # the job reads dependencies/ itself
        # idempotence, then the product's own edits of the created-once files
        self.assertTrue(product.json("check", self.dir)["data"]["valid"])
        self.assertFalse(product.json("adopt", self.dir)["data"]["changed"])
        with (product.dir / ".github" / "workflows" / "ci.yml").open("a") as ci_file:
            ci_file.write("  mine:\n    runs-on: ubuntu-26.04\n")
        product.run("adopt", self.dir, "--apply")
        self.assertIn("  mine:", product.read(".github/workflows/ci.yml"))
        self.assertEqual(product.read("AGENTS.md").count("maelys-release:begin"), 1)
        # the socle line of a product-owned ci.yml is managed; its absence is a warning
        ci_path = product.dir / ".github" / "workflows" / "ci.yml"
        stale = re.sub(r"check-product\.yml@[0-9a-f]{40} # \S+", "check-product.yml@" + "0" * 40 + " # v0.0.0", ci_path.read_text())
        ci_path.write_text(stale)
        drift = product.json("check", self.dir, expect=2)["data"]
        self.assertIn(".github/workflows/ci.yml: update", drift["violations"])
        product.run("adopt", self.dir, "--apply")
        self.assertNotIn("0" * 40, ci_path.read_text())
        self.assertIn("  mine:", ci_path.read_text())
        ci_path.write_text("name: ci\non: [push]\njobs:\n  mine:\n    runs-on: ubuntu-26.04\n")
        data = product.json("check", self.dir, expect=2)["data"]
        self.assertTrue(any("does not call the socle's check-product.yml" in violation for violation in data["violations"]))
        self.assertEqual(product.json("adopt", self.dir)["data"]["mode"], "plan")   # a warning does not block adopt
        self.assertIn("WARN", product.run("adopt", self.dir).stdout)
        declarations = product.json("declarations", self.dir)["data"]
        self.assertEqual(declarations["dependencies"], ["maelys-system"])
        self.assertEqual(declarations["linuxPackages"], "pkg-config libjansson-dev")
        rendered = product.run("declarations", self.dir).stdout.splitlines()
        self.assertIn("dependencies  maelys-system", rendered)

    def test_drift(self) -> None:
        product = self.product
        product.run("adopt", self.dir, "--apply")
        with (product.dir / ".github" / "workflows" / "release.yml").open("a") as workflow_file:
            workflow_file.write("\n# edited\n")
        drift = product.json("check", self.dir, expect=2)["data"]
        self.assertIn(".github/workflows/release.yml: update", drift["violations"])
        self.assertIn("-# edited", product.run("adopt", self.dir).stdout)
        product.run("adopt", self.dir, "--apply")
        checkout = product.dir / "scripts" / "checkout-dependency.sh"
        checkout.chmod(0o644)
        self.assertIn("not executable", product.run("check", self.dir, expect=2).stdout)
        product.run("adopt", self.dir, "--apply")
        self.assertTrue(os.access(checkout, os.X_OK))
        # a product pinned at another socle is reported, not silently regenerated
        workflow = product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("release.yml@", "release.yml@0000000000000000000000000000000000000000 # v0.0.0\n#", 1))
        drift = product.json("check", self.dir, expect=2)["data"]
        self.assertTrue(any("pins maelys-release v0.0.0" in violation for violation in drift["violations"]))

    def test_product_name_comes_from_release_yml(self) -> None:
        product = self.product
        product.run("adopt", self.dir, "--apply")
        elsewhere = product.work / "worktree-7f3a"
        shutil.copytree(product.dir, elsewhere)
        data = product.json("check", str(elsewhere))["data"]
        self.assertEqual(data["product"], "maelys-fixture")            # not "worktree-7f3a"
        self.assertTrue(data["valid"])
        self.assertEqual(product.json("declarations", str(elsewhere))["data"]["product"], "maelys-fixture")
        (elsewhere / ".github" / "workflows" / "release.yml").unlink()
        self.assertEqual(product.json("adopt", str(elsewhere))["data"]["product"], "worktree-7f3a")

    def test_without_pins_or_formulas(self) -> None:
        product = self.product
        for path in (product.dir / "packaging" / "homebrew").glob("*.rb.in"):
            path.unlink()
        (product.dir / "dependencies" / "maelys-system.pin").unlink()
        product.run("adopt", self.dir, "--apply")
        workflow = product.read(".github/workflows/release.yml")
        self.assertNotIn("tap.yml@", workflow)
        self.assertNotIn("dependency_checkout", workflow)
        self.assertFalse((product.dir / "scripts" / "checkout-dependency.sh").exists())
        self.assertIn("packaging/homebrew/<name>.rb.in", product.read("AGENTS.md"))

    def test_dirty_socle_checkout_is_refused(self) -> None:
        product = self.product
        copy = product.work / "socle-copy"
        product.git(product.work, "clone", "-q", str(ROOT), str(copy))
        # the copy runs the program under test, committed there so it is clean
        shutil.copy2(ROOT / "VERSION", copy / "VERSION")
        for directory in ("bin", "share", "dependencies"):
            shutil.rmtree(copy / directory, ignore_errors=True)
            shutil.copytree(ROOT / directory, copy / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        product.git(copy, "add", "-A")
        product.git(copy, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "under test")
        with (copy / "share" / "agents" / "instructions-block.md").open("a") as block:
            block.write("\nedited\n")
        dirty = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--allow-untagged", "--format", "json"],
                               env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(dirty.returncode, 1)
        error = json.loads(dirty.stderr)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("uncommitted changes", error["message"])
        product.git(copy, "checkout", "-q", "--", "share")
        # clean but without a tag: refused unless the caller says it is a trial.
        # The pin is what a tag guarantees, so the refusal is the release
        # mechanism's; conventions alone install from any commit.
        untagged = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir,
                                   "--mechanism", "maelys-release", "--format", "json"],
                                  env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(untagged.returncode, 1)
        self.assertIn("not a release", json.loads(untagged.stderr)["error"]["message"])
        conventions = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--format", "json"],
                                     env=product.env, check=False, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(conventions.returncode, 0, conventions.stderr)
        self.assertEqual(json.loads(conventions.stdout)["data"]["mechanism"], "custom")
        clean = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--mechanism", "maelys-release",
                                "--allow-untagged", "--format", "json"],
                               env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["data"]["socle"]["sha"], product.git(copy, "rev-parse", "HEAD"))
        # a socle that knows no tag (a depth-1 fetch in CI) takes the label the product pins
        subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--apply", "--allow-untagged",
                        "--mechanism", "maelys-release"], env=product.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for name in (".github/workflows/release.yml", ".github/workflows/ci.yml", "AGENTS.md", "CLAUDE.md",
                     ".claude/skills/maelys-release/SKILL.md", "scripts/checkout-dependency.sh"):
            path = product.dir / name
            path.write_text(path.read_text().replace("untagged", "v7.7.7"))
        labelled = subprocess.run([str(copy / "bin" / "maelys-release"), "check", self.dir, "--format", "json"],
                                  env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(labelled.returncode, 0, labelled.stdout + labelled.stderr)
        self.assertEqual(json.loads(labelled.stdout)["data"]["socle"]["tag"], "v7.7.7")
        # another socle re-executes check from a cached checkout of the pinned one
        bare = product.work / "remotes" / "maelys-release.git"
        product.git(product.work, "clone", "-q", "--bare", str(copy), str(bare))
        env = {**product.env, "XDG_CACHE_HOME": str(product.work / "cache"), "MAELYS_RELEASE_NO_RELOCATE": ""}
        env.pop("MAELYS_RELEASE_NO_RELOCATE")
        relocated = subprocess.run([str(CLI), "check", self.dir, "--format", "json"], env=env, check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(relocated.returncode, 0, relocated.stdout + relocated.stderr)
        self.assertEqual(relocated.stderr, "")                     # an envelope owns stdout, nothing on stderr
        pinned_sha = product.git(copy, "rev-parse", "HEAD")
        self.assertEqual(json.loads(relocated.stdout)["data"]["socle"]["sha"], pinned_sha)
        self.assertTrue((product.work / "cache" / "maelys-release" / pinned_sha / "bin" / "maelys-release").is_file())
        for path in (ROOT / "bin" / "maelys_socle").glob("*.py"):
            self.assertEqual((product.work / "cache" / "maelys-release" / pinned_sha / "bin" / "maelys_socle" / path.name).read_bytes(),
                             path.read_bytes())
        text = subprocess.run([str(CLI), "check", self.dir], env=env, check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn("running the pinned socle", text.stderr)
        kept = subprocess.run([str(CLI), "check", self.dir, "--format", "json"], env={**env, "MAELYS_RELEASE_NO_RELOCATE": "1"},
                              check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # without relocation this checkout answers for itself: refused when it
        # is dirty (a developer's tree), a pin mismatch when it is clean (CI)
        if kept.returncode == 1:
            self.assertIn("uncommitted changes", json.loads(kept.stderr)["error"]["message"])
        else:
            self.assertEqual(kept.returncode, 2, kept.stdout + kept.stderr)
            self.assertTrue(any("pins maelys-release" in item for item in json.loads(kept.stdout)["data"]["violations"]))
        # a product pinned before the command existed gets the upgrade path, not a missing file
        product.git(copy, "rm", "-q", "bin/maelys-release")
        product.git(copy, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "before the command")
        old = product.git(copy, "rev-parse", "HEAD")
        product.git(copy, "push", "-q", str(bare), "HEAD:refs/heads/main")
        workflow = product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(re.sub(r"release\.yml@[0-9a-f]{40} # \S+", f"release.yml@{old} # v0.2.8", workflow.read_text()))
        ancient = subprocess.run([str(CLI), "check", self.dir, "--format", "json"], env=env, check=False, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(ancient.returncode, 1, ancient.stdout + ancient.stderr)
        error = json.loads(ancient.stderr)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("predates the maelys-release command", error["message"])
        self.assertIn("adopt", error["hint"])
        # preflight names the cause when the check part fails
        cause = subprocess.run([str(CLI), "preflight", self.dir, "--socle-sha", "f" * 40, "--socle-tag", "v9.9.9"],
                               env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(cause.returncode, 2)
        self.assertIn("drift    maelys-fixture pins maelys-release", cause.stdout)
        self.assertIn("preflight: maelys-fixture is not ready to tag", cause.stdout)

    def test_checkout_dependency(self) -> None:
        product = self.product
        product.run("adopt", self.dir, "--apply")
        script = product.dir / "scripts" / "checkout-dependency.sh"
        destination = product.work / "maelys-system"
        subprocess.run([str(script), "maelys-system", str(destination)], env=product.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(product.git(destination, "rev-parse", "HEAD"), product.pinned)
        again = subprocess.run([str(script), "maelys-system", str(destination)], env=product.env, check=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(again.returncode, 1)
        unknown = subprocess.run([str(script), "maelys-json", str(product.work / "maelys-json")], env=product.env,
                                 check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(unknown.returncode, 66)
        self.assertEqual(subprocess.run([str(script), "Bad Name"], check=False, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE).returncode, 64)


PERMISSION_LEVELS = {"none": 0, "read": 1, "write": 2}


def workflow_permissions(text: str) -> dict[str, dict[str, str]]:
    """job -> {scope: level}, the job's own block or the workflow's default."""
    default: dict[str, str] = {}
    head = text.split("\njobs:\n", 1)[0]
    if "\npermissions:\n" in head:
        for line in head.split("\npermissions:\n", 1)[1].splitlines():
            entry = re.fullmatch(r"  ([a-z-]+): ([a-z]+)", line)
            if not entry:
                break
            default[entry.group(1)] = entry.group(2)
    jobs: dict[str, dict[str, str]] = {}
    name, inside = "", False
    for line in text.split("\njobs:\n", 1)[-1].splitlines():
        job = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if job:
            name, inside = job.group(1), False
            jobs[name] = dict(default)
            continue
        if line == "    permissions:":
            jobs[name], inside = {}, True
            continue
        entry = re.fullmatch(r"      ([a-z-]+): ([a-z]+)", line)
        if inside and entry:
            jobs[name][entry.group(1)] = entry.group(2)
            continue
        if inside and re.fullmatch(r"\s*#.*", line):
            continue                              # a comment does not end the block
        inside = False
    return jobs


def workflow_calls(text: str) -> dict[str, str]:
    """job -> the socle workflow file it calls, for a generated caller."""
    calls: dict[str, str] = {}
    name = ""
    for line in text.split("\njobs:\n", 1)[-1].splitlines():
        job = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if job:
            name = job.group(1)
            continue
        called = re.match(r"    uses: \S+/\.github/workflows/([a-z-]+\.yml)@", line)
        if called:
            calls[name] = called.group(1)
    return calls


class ProductNeedsTest(unittest.TestCase):
    """What a product asked the socle for: a third-party pin, a verification
    at the tag, a stronger commit check, a fuzz job."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)

    def tearDown(self) -> None:
        self.product.close()

    def test_a_dependency_outside_maelys_dev_declares_where_it_lives(self) -> None:
        self.product.write("dependencies/mbedtls.pin",
                           "v3.6.7\n" + "b" * 40 + "\nrepository https://github.com/Mbed-TLS/mbedtls.git\n")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertTrue(data["valid"], data["checks"])
        self.assertIn("mbedtls", data["dependencies"])
        self.assertTrue(any("cloned from https://github.com/Mbed-TLS/mbedtls.git" in check["message"]
                            for check in data["checks"]), data["checks"])

    def test_a_repository_line_that_is_not_an_https_url_is_refused(self) -> None:
        self.product.write("dependencies/mbedtls.pin",
                           "v3.6.7\n" + "b" * 40 + "\nrepository git@github.com:Mbed-TLS/mbedtls.git\n")
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(any("must be an https URL" in check["message"] for check in data["checks"]))

    def test_the_managed_script_clones_the_declared_repository(self) -> None:
        self.product.write("dependencies/mbedtls.pin",
                           "v3.6.7\n" + "b" * 40 + "\nrepository https://example.invalid/mbedtls.git\n")
        self.product.run("adopt", self.dir, "--apply")
        script = self.product.dir / "scripts" / "checkout-dependency.sh"
        traced = subprocess.run(["sh", "-x", str(script), "mbedtls", str(self.product.work / "gone")],
                                cwd=self.product.dir, env=self.product.env, check=False, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
        self.assertIn("git clone --quiet --filter=blob:none --no-checkout https://example.invalid/mbedtls.git", traced)
        # A Maelys dependency keeps the organisation's base.
        traced = subprocess.run(["sh", "-x", str(script), "maelys-system", str(self.product.work / "gone2")],
                                cwd=self.product.dir, env=self.product.env, check=False, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
        self.assertIn("/maelys-system.git", traced)
        self.assertNotIn("example.invalid", traced)

    def test_a_dependency_declares_the_submodules_its_build_needs(self) -> None:
        self.product.write("dependencies/mbedtls.pin",
                           "v3.6.7\n" + "b" * 40 + "\nrepository https://example.invalid/mbedtls.git\nsubmodules\n")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertTrue(data["valid"], data["checks"])
        self.assertTrue(any("initialises its submodules, from the URLs its .gitmodules names" in check["message"]
                            for check in data["checks"]), data["checks"])
        self.product.write("dependencies/mbedtls.pin",
                           "v3.6.7\n" + "b" * 40 + "\nsubmodules recursive\n")
        self.assertTrue(any("initialises its submodules recursively" in check["message"]
                            for check in self.product.json("declarations", self.dir)["data"]["checks"]))

    def test_an_unknown_submodules_mode_is_refused(self) -> None:
        self.product.write("dependencies/mbedtls.pin", "v3.6.7\n" + "b" * 40 + "\nsubmodules shallow\n")
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(any("takes nothing, or recursive" in check["message"] for check in data["checks"]))

    def test_the_managed_script_initialises_only_what_is_declared(self) -> None:
        # A superproject pins its submodule's commit, but its URL comes from
        # the .gitmodules of a repository this pin does not name: the socle
        # fetches it only when the product asked.
        inner = self.product.work / "src" / "inner"
        inner.mkdir(parents=True)
        (inner / "file").write_text("inner\n")
        self.product.git(inner, "init", "-q")
        self.product.git(inner, "add", "-A")
        self.product.git(inner, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "inner")
        outer = self.product.work / "src" / "outer"
        outer.mkdir(parents=True)
        self.product.git(outer, "init", "-q")
        self.product.git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "framework")
        self.product.git(outer, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "outer")
        commit = self.product.git(outer, "rev-parse", "HEAD")
        remotes = self.product.work / "remotes"
        self.product.git(self.product.work, "clone", "-q", "--bare", str(outer), str(remotes / "outer.git"))
        self.product.write("dependencies/outer.pin", f"v1.0.0\n{commit}\n")
        self.product.run("adopt", self.dir, "--apply")
        script = self.product.dir / "scripts" / "checkout-dependency.sh"
        environment = {**self.product.env, "GIT_ALLOW_PROTOCOL": "file"}

        def clone(destination: str) -> pathlib.Path:
            subprocess.run(["sh", str(script), "outer", str(self.product.work / destination)],
                           cwd=self.product.dir, env=environment, check=True, text=True, stdout=subprocess.PIPE)
            return self.product.work / destination

        # Declared nothing: the submodule stays an empty directory.
        self.assertEqual(list((clone("without") / "framework").iterdir()), [])
        self.product.write("dependencies/outer.pin", f"v1.0.0\n{commit}\nsubmodules\n")
        self.assertTrue((clone("with") / "framework" / "file").is_file())

    def test_a_verify_script_is_run_before_packaging_at_the_tag(self) -> None:
        """With bash, like the package_command beside it. `sh` is dash on
        Ubuntu and bash in POSIX mode on macOS, so a bashism would pass on the
        operator's machine and fail on the runner at the tag."""
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn("verify_command: bash scripts/verify-release.sh TARGET", workflow)

    def test_without_that_script_the_release_workflow_asks_for_no_verification(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.assertNotIn("verify_command:", self.product.read(".github/workflows/release.yml"))

    def test_a_product_declares_the_targets_it_builds(self) -> None:
        self.product.write("maelys-release.conf",
                           "[targets]\nlinux-x86_64\nlinux-arm64\nmacos-arm64\nwasm32 ubuntu-26.04\n" + APART)
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn('targets: \'[{"target": "linux-x86_64"}, {"target": "linux-arm64"},'
                      ' {"target": "macos-arm64"}, {"target": "wasm32", "runner": "ubuntu-26.04"}]\'', workflow)
        # A target that names no label keeps the runner release.yml holds for
        # it, so changing a default runner still reaches this product.
        self.assertNotIn('"linux-x86_64", "runner"', workflow)

    def test_a_runner_is_a_label_or_a_label_set(self) -> None:
        self.product.write("maelys-release.conf", "[targets]\nmacos-arm64 self-hosted macOS ARM64\n" + APART)
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn('{"target": "macos-arm64", "runner": ["self-hosted", "macOS", "ARM64"]}',
                      self.product.read(".github/workflows/release.yml"))

    def test_a_product_adds_an_archive_kind_to_the_manifest(self) -> None:
        self.product.write("maelys-release.conf", "[manifest]\n*.wasm\nreceipt.json\n" + APART)
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("manifest_patterns: '*.tar.gz *.deb *.rpm *.wasm receipt.json'",
                      self.product.read(".github/workflows/release.yml"))

    def test_without_that_file_the_release_workflow_asks_for_neither(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertNotIn("targets:", workflow)
        self.assertNotIn("manifest_patterns:", workflow)
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["targets"], ["linux-x86_64", "linux-arm64", "macos-arm64"])
        self.assertEqual(data["manifestPatterns"], "*.tar.gz *.deb *.rpm")

    def test_a_release_declaration_the_socle_cannot_honour_is_a_violation(self) -> None:
        # The scope is the release mechanism, so the product must be on it
        # before the declaration means anything.
        self.product.run("adopt", self.dir, "--apply")
        self.product.write("maelys-release.conf", "[targets]\nwasm32\n" + APART)
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(any("names no runner" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_a_product_declares_a_publication_channel(self) -> None:
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn("  channel-npm:", workflow)
        # After the release, never before: a registry publication cannot be
        # withdrawn, so nothing the socle runs may fail after it.
        self.assertIn("    needs: release\n    if: needs.release.result == 'success'", workflow)
        self.assertIn("      packages: write", workflow)
        self.assertIn("      publish_command: bash scripts/publish-channel.sh TAG CHANNEL", workflow)

    def test_the_channel_marker_is_written_by_a_job_that_runs_no_product_code(self) -> None:
        """A publication that happened is recorded by a job that could not
        have said otherwise: `needs` makes the marker an observation, and one
        asset per channel keeps two channels from clobbering each other."""
        channel = (ROOT / ".github" / "workflows" / "channel.yml").read_text()
        jobs = workflow_permissions(channel)
        self.assertEqual(jobs["publish"].get("contents"), "read")
        self.assertEqual(jobs["record"].get("contents"), "write")
        record = channel.split("\n  record:\n", 1)[1]
        self.assertIn("needs: publish", record)
        # no checkout, no dist/, nothing of the product runs in the writing job
        self.assertNotIn("actions/checkout", record)
        self.assertIn('gh release upload "$TAG"', record)
        self.assertIn('"channel-$CHANNEL.json"', record)
        # the caller must grant what that job narrows to
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        caller = self.product.read(".github/workflows/release.yml")
        granted = workflow_permissions(caller)["channel-npm"]
        self.assertEqual(granted.get("contents"), "write")
        self.assertEqual(granted.get("packages"), "write")

    def test_a_channel_without_its_script_is_a_violation(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(any("publish-channel.sh" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_a_script_without_its_declaration_publishes_nothing(self) -> None:
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        self.assertNotIn("channel-", self.product.read(".github/workflows/release.yml"))
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["channels"], [])
        self.assertTrue(any("nothing calls it" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_the_socle_serves_no_registry_it_cannot_reach(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.product.write("maelys-release.conf", "[channels]\nsdk pypi\n" + APART)
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertTrue(any("serves no pypi channel" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_the_generated_caller_grants_every_scope_its_workflows_declare(self) -> None:
        """A caller that under-grants fails the whole run at startup, the
        release job included, so this is checked here and not on a runner."""
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.write("packaging/homebrew/maelys-fixture.rb.in", "class F < Formula\nend\n")
        self.product.run("adopt", self.dir, "--apply")
        caller = self.product.read(".github/workflows/release.yml")
        granted = workflow_permissions(caller)
        calls = workflow_calls(caller)
        self.assertEqual(set(calls.values()), {"release.yml", "channel.yml", "tap.yml"}, calls)
        for job, filename in calls.items():
            called = (ROOT / ".github" / "workflows" / filename).read_text()
            for name, wanted in workflow_permissions(called).items():
                for scope, level in wanted.items():
                    have = granted[job].get(scope, "none")
                    self.assertGreaterEqual(
                        PERMISSION_LEVELS[have], PERMISSION_LEVELS[level],
                        f"{job} grants {scope}: {have} but {filename}:{name} declares {level}")

    def test_harnesses_the_socle_job_replays_are_reported(self) -> None:
        self.product.write("tests/fuzz/fuzz_target.c", "int main(void) { return 0; }\n")
        self.product.run("adopt", self.dir, "--apply")
        ci = self.product.dir / ".github" / "workflows" / "ci.yml"
        ci.write_text(ci.read_text() + "      fuzz_command: make fuzz-smoke\n")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["fuzz"], {"harnesses": "tests/fuzz", "runs": "socle"})

    def test_harnesses_a_job_of_the_product_runs_are_reported(self) -> None:
        self.product.write("fuzz/fuzz_target.c", "int main(void) { return 0; }\n")
        self.product.run("adopt", self.dir, "--apply")
        ci = self.product.dir / ".github" / "workflows" / "ci.yml"
        ci.write_text(ci.read_text() + "  mine:\n    steps:\n      - run: make fuzz-smoke\n")
        data = self.product.json("declarations", self.dir)["data"]
        # fuzz/ is the layout of four repositories; the socle reads it and
        # says so rather than refusing it.
        self.assertEqual(data["fuzz"], {"harnesses": "fuzz", "runs": "own"})

    def test_harnesses_no_job_runs_are_a_note_and_never_a_violation(self) -> None:
        self.product.write("tests/fuzz/fuzz_target.c", "int main(void) { return 0; }\n")
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["fuzz"], {"harnesses": "tests/fuzz", "runs": "none"})
        self.assertTrue(data["valid"], data["checks"])
        note = [c for c in data["checks"] if "tests/fuzz/" in c["message"]]
        self.assertEqual([c["status"] for c in note], ["note"], note)
        # check exits 2 on anything but ok and note, so this must stay green.
        self.assertTrue(self.product.json("check", self.dir)["data"]["valid"])

    def test_a_repository_without_harnesses_is_told_nothing(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["fuzz"], {"harnesses": "", "runs": "none"})
        self.assertEqual([c for c in data["checks"] if "fuzz" in c["message"]], [])

    def test_the_contract_separates_what_is_declared_from_what_is_default(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("declarations", self.dir)["data"]
        # Effective and declared are not the same answer: folding them makes a
        # fleet read "everyone targets these three" where nobody declared one.
        self.assertEqual(data["targets"], ["linux-x86_64", "linux-arm64", "macos-arm64"])
        self.assertEqual(data["declared"], {"targets": [], "manifestPatterns": [], "sbomPattern": "", "macosRunner": [], "dependenciesApart": True, "commitVerification": "", "legs": []})
        self.product.write("maelys-release.conf", "[targets]\nlinux-arm64\n\n[manifest]\n*.wasm\n" + APART)
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["declared"], {"targets": ["linux-arm64"],
                                            "manifestPatterns": ["*.wasm"], "sbomPattern": "", "macosRunner": [], "dependenciesApart": True, "commitVerification": "", "legs": []})
        self.assertEqual(data["manifestPatterns"], "*.tar.gz *.deb *.rpm *.wasm")

    def test_the_contract_carries_the_pin_its_file_and_the_stamp(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["pinned"]["file"], ".github/workflows/release.yml")
        self.assertTrue(data["managedBy"], data)

    def test_runners_are_read_through_a_matrix_and_never_guessed(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.product.write(".github/workflows/own.yml", """name: own
on: [push]
jobs:
  plain:
    runs-on: ubuntu-24.04
  through-a-matrix:
    strategy:
      matrix:
        os: [ubuntu-24.04, self-hosted]
    runs-on: ${{ matrix.os }}
  through-an-input:
    runs-on: ${{ fromJSON(inputs.runner) }}
""")
        runners = self.product.json("declarations", self.dir)["data"]["runners"]
        # A self-hosted runner named only inside a matrix is found: searching
        # the text of runs-on for the label would have read this as clean.
        self.assertIn("self-hosted", runners["labels"])
        self.assertIn("ubuntu-24.04", runners["labels"])
        # And what cannot be named is reported rather than omitted, so an
        # observer blocks on ignorance instead of passing.
        self.assertEqual(runners["unresolved"], ["own.yml:12: ${{ fromJSON(inputs.runner) }}"])
        # A repository whose jobs only call the socle names no runner: an
        # empty list means it chooses none, not that it is safe.
        self.assertTrue(runners["delegated"])

    def test_the_block_says_where_the_prose_lives_and_names_the_product(self) -> None:
        """maelys-platform's documentation policy says this block carries it,
        and it did not. Since 0.58.0 only a repository that declares [docs]
        named carries the name: three public products had it in two public
        files each, under a bullet forbidding it."""
        existing = self.product.dir / "maelys-release.conf"
        conf = existing.read_text() if existing.is_file() else ""
        self.product.write("maelys-release.conf", conf + "\n[docs]\nnamed\n")
        self.product.run("adopt", self.dir, "--apply")
        for name in ("AGENTS.md", "CLAUDE.md"):
            text = self.product.read(name)
            self.assertIn("`maelys-dev/maelys-docs`", text)
            self.assertIn("../maelys-docs", text)
            self.assertIn("never name it from a public README", text)
            # the directory is the product's, not a placeholder left behind
            self.assertIn("`maelys-fixture/`", text)
            self.assertNotIn("@PRODUCT@", text)

    def test_only_the_publishing_job_may_write_and_it_runs_no_product_code(self) -> None:
        """The job that runs the product's package_command held contents:
        write, which on GitHub is repository-wide: it could rewrite the assets
        of releases already published, push to any branch, and create tags."""
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        jobs = workflow_permissions(release)
        self.assertEqual(jobs["build"].get("contents"), "read")
        self.assertEqual(jobs["publish"].get("contents"), "write")
        writing = [name for name, scopes in jobs.items() if scopes.get("contents") == "write"]
        self.assertEqual(writing, ["publish"], jobs)
        # and the human gate stands in front of that one job
        publish = release.split("\n  publish:\n", 1)[1]
        self.assertIn("environment: ${{ inputs.release_environment }}", publish.split("\n    steps:")[0])
        # the bytes cross the run as artifacts, not through a draft a writing
        # job could have edited between build and publish
        self.assertIn("actions/upload-artifact@", release)
        self.assertIn("retention-days: 1", release)
        self.assertNotIn("gh release upload", release.split("\n  publish:\n")[0])

    def test_the_reusable_workflows_carry_the_new_inputs(self) -> None:
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("verify_command:", release)
        self.assertIn("commit_verification:", release)
        self.assertIn("signed-on-default-branch", release)
        # The attestation covers everything package_command leaves in dist/.
        self.assertIn("every file\n          package_command leaves there is attested, an SBOM included", release)
        self.assertIn("targets:", release)
        self.assertIn("manifest_patterns:", release)
        # The matrix is resolved once, in verify, so a product can name a
        # target the socle does not know.
        self.assertIn("matrix: ${{ fromJSON(needs.verify.outputs.matrix) }}", release)
        check_product = (ROOT / ".github" / "workflows" / "check-product.yml").read_text()
        self.assertIn("fuzz_command:", check_product)
        self.assertIn("make fuzz-smoke", check_product)
        # The fleet was surveyed in 0.26.0: nine repositories fuzz, and the
        # socle schedules none of them.
        self.assertNotIn("the three products that fuzz", check_product)
        self.assertIn("The socle schedules\n          nothing.", check_product)


class MechanismTest(unittest.TestCase):
    """A product the socle does not release: conventions installed, workflows untouched."""

    OWN_WORKFLOW = "name: release\n\non:\n  push:\n    tags: [\"v*\"]\n\njobs:\n  release:\n    runs-on: ubuntu-26.04\n    steps:\n      - run: echo mine\n"

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.write(".github/workflows/release.yml", self.OWN_WORKFLOW)

    def tearDown(self) -> None:
        self.product.close()

    def test_a_release_workflow_of_its_own_declares_a_custom_mechanism(self) -> None:
        data = self.product.json("adopt", self.dir)["data"]
        self.assertEqual(data["mechanism"], "custom")
        written = {entry["path"] for entry in data["files"]}
        # The conventions, the shared CI, which is not the mechanism, and --
        # since 0.57.2 -- the checkout scripts that CI calls for a product that
        # pins, whatever publishes it: maelys-warden copied them by hand.
        self.assertEqual(written, {"AGENTS.md", "CLAUDE.md", "RELEASING.md", "LICENSING.md", "SECURITY.md",
                                   ".github/workflows/ci.yml", "scripts/checkout-dependency.sh",
                                   "scripts/checkout-dependencies.sh"})
        self.assertNotIn(".github/workflows/release.yml", written)

    def test_adopt_leaves_the_products_own_workflow_alone(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        for name in ("AGENTS.md", "CLAUDE.md"):
            text = self.product.read(name)
            self.assertIn("SPDX-License-Identifier: CC-BY-4.0", text)
            self.assertIn("Copyright 2026 David Bromberg", text)
            self.assertIn("https://creativecommons.org/licenses/by/4.0/", text)
            self.assertNotIn("CC0", text)
        self.assertEqual(self.product.read(".github/workflows/release.yml"), self.OWN_WORKFLOW)
        # The scripts the shared CI calls, yes; the release skill, no.
        self.assertTrue(os.access(self.product.dir / "scripts" / "checkout-dependency.sh", os.X_OK))
        self.assertFalse((self.product.dir / ".claude").exists())

    def test_the_shared_ci_is_installed_whatever_publishes_the_product(self) -> None:
        # The CI is not the release mechanism: a product that publishes by
        # itself can still run check-product.yml.
        data = self.product.json("adopt", self.dir, "--apply")["data"]
        written = {entry["path"] for entry in data["files"]}
        self.assertIn(".github/workflows/ci.yml", written)
        self.assertNotIn(".github/workflows/release.yml", written)
        ci = self.product.read(".github/workflows/ci.yml")
        self.assertIn("check-product.yml@" + "f" * 40, ci)
        self.assertEqual(self.product.read(".github/workflows/release.yml"), self.OWN_WORKFLOW)

    def test_the_socle_commit_is_readable_from_the_ci_that_calls_it(self) -> None:
        # check-product.yml reads the socle commit from release.yml when the
        # socle wrote it, and from the ci.yml line that calls it otherwise.
        self.product.run("adopt", self.dir, "--apply")
        workflows = self.product.dir / ".github" / "workflows"
        script = ('sha="$(sed -n \'s|.*maelys-release/.github/workflows/release.yml@\\([0-9a-f]\\{40\\}\\).*|\\1|p\''
                  ' .github/workflows/release.yml 2>/dev/null || true)"\n'
                  'if [ -z "$sha" ]; then\n'
                  '  sha="$(sed -n \'s|.*maelys-release/.github/workflows/check-product\\.yml@\\([0-9a-f]\\{40\\}\\).*|\\1|p\''
                  ' .github/workflows/*.yml | head -n 1)"\nfi\nprintf %s "$sha"\n')
        found = subprocess.run(["sh", "-c", script], cwd=self.product.dir, check=True, text=True,
                               stdout=subprocess.PIPE).stdout
        self.assertEqual(found, "f" * 40, list(workflows.iterdir()))

    def test_the_pin_is_read_from_the_ci_when_the_release_is_the_products_own(self) -> None:
        # maelys-http, in its CI: the socle is fetched by commit, so it knows
        # no tag, and the label comes from the product's pin. Read from
        # release.yml alone, that product had no pin at all and its own
        # ci.yml line drifted against a socle labelled "untagged".
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir, "--socle-sha", "f" * 40)["data"]
        # The file is part of the answer: a product that keeps its own
        # release names the socle in ci.yml and nowhere else.
        self.assertEqual(data["pinned"], {"sha": "f" * 40, "tag": "v9.9.9",
                                          "file": ".github/workflows/ci.yml"})
        self.assertEqual(data["socle"]["tag"], "v9.9.9")
        self.assertTrue(data["valid"], data["violations"])
        self.assertEqual([entry["action"] for entry in data["files"]
                          if entry["path"] == ".github/workflows/ci.yml"], ["same"])

    def test_the_exit_code_never_contradicts_the_verdict_shown(self) -> None:
        # A verdict that does not apply cannot carry a violation: the shared
        # CI belongs to the conventions, whatever publishes the product.
        self.product.run("adopt", self.dir, "--apply")
        ci = self.product.dir / ".github" / "workflows" / "ci.yml"
        ci.write_text(ci.read_text().replace("f" * 40, "0" * 40))
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertFalse(data["release"]["applicable"])
        self.assertEqual(data["release"]["violations"], [])
        self.assertIn(".github/workflows/ci.yml: update", data["conventions"]["violations"])

    def test_check_passes_without_a_word_about_the_workflows(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        completed = self.product.run("check", self.dir)
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"])
        self.assertTrue(data["conventions"]["valid"])
        self.assertFalse(data["release"]["applicable"])
        self.assertEqual(data["release"]["violations"], [])
        self.assertIn("release mechanism: not applicable (custom mechanism)", completed.stdout)
        self.assertNotIn("package-release.sh", completed.stdout)
        # Not one release-scope line: the workflows this product owns are
        # never reported. What it may still hear about is the shared CI it
        # calls, which is a convention and not the release mechanism -- so
        # an announcement about check-product.yml reaches it, and should.
        self.assertFalse([check for check in data["checks"] if check["scope"] == "release"],
                         data["checks"])

    def test_the_block_states_the_conventions_not_the_socle_release(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        agents = self.product.read("AGENTS.md")
        self.assertIn("Maelys repository conventions", agents)
        self.assertIn("its release mechanism is its own", agents)
        self.assertNotIn("publishes through the shared maelys-release workflows", agents)

    def test_a_mechanism_contradicting_the_carried_workflow_is_refused(self) -> None:
        error = self.product.json("adopt", self.dir, "--mechanism", "maelys-release", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("carries a custom release.yml", error["message"])

    def test_preflight_and_rehearse_refuse_a_product_they_do_not_release(self) -> None:
        for command in ("preflight", "rehearse"):
            arguments = (command, self.dir) + (("linux-x86_64",) if command == "rehearse" else ())
            error = self.product.json(*arguments, expect=1)["error"]
            self.assertEqual(error["code"], "PRECONDITION_FAILED")
            self.assertIn("custom mechanism", error["message"])

    def test_no_release_workflow_means_the_socle_does_not_release_it(self) -> None:
        # maelys-warden publishes through its own qualify and publish
        # workflows and carries no release.yml: the socle must not claim it.
        (self.product.dir / ".github" / "workflows" / "release.yml").unlink()
        # Straight to the program: the fixture would declare the mechanism.
        completed = subprocess.run([str(CLI), "adopt", self.dir, "--product", "maelys-fixture", *Product.SOCLE,
                                    "--format", "json", "--compact"],
                                   cwd=self.product.work, env=self.product.env, check=True, text=True,
                                   stdout=subprocess.PIPE)
        data = json.loads(completed.stdout)["data"]
        self.assertEqual(data["mechanism"], "custom")
        self.assertNotIn(".github/workflows/release.yml", {entry["path"] for entry in data["files"]})
        chosen = self.product.json("adopt", self.dir, "--mechanism", "maelys-release")["data"]
        self.assertEqual(chosen["mechanism"], "maelys-release")
        self.assertIn(".github/workflows/release.yml", {entry["path"] for entry in chosen["files"]})
        self.assertEqual(self.product.json("adopt", self.dir, "--mechanism", "none")["data"]["mechanism"], "none")

    def test_a_seeded_file_is_a_note_never_a_violation(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        for name in ("RELEASING.md", "LICENSING.md", "SECURITY.md"):
            (self.product.dir / name).unlink()
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"], data["violations"])
        notes = [check["message"] for check in data["checks"] if check["status"] == "note"
                 and "writes it once" in check["message"]]
        self.assertEqual(len(notes), 3, notes)
        self.assertTrue(all("adopt --apply' writes it once" in note for note in notes), notes)


class NewTest(unittest.TestCase):
    """new creates a repository the socle already accepts."""

    def setUp(self) -> None:
        self.product = Product()          # for its local remote and its git configuration
        self.target = self.product.work / "maelys-widget"

    def tearDown(self) -> None:
        self.product.close()

    def run_new(self, *arguments: str, expect: int = 0) -> dict:
        return self.product.json("new", str(self.target), "--product", "maelys-widget", *arguments,
                                 *Product.SOCLE, expect=expect)

    def test_plan_lists_the_base_files_and_writes_nothing(self) -> None:
        data = self.run_new()["data"]
        self.assertEqual(data["mode"], "plan")
        self.assertEqual([entry["path"] for entry in data["files"]],
                         ["LICENSE", "VERSION", "CHANGELOG.md", "README.md", "scripts/package-release.sh"])
        self.assertEqual(data["adopted"], [])
        self.assertFalse(self.target.exists())

    def test_apply_creates_a_repository_check_accepts(self) -> None:
        data = self.run_new("--apply")["data"]
        # What was published last, and nothing was: the first release is
        # cut as 0.1.0, which a scaffold saying 0.1.0 made cut refuse.
        self.assertEqual(data["version"], "0.0.0")
        self.assertEqual(data["mechanism"], "maelys-release")
        written = {entry["path"] for entry in data["adopted"]}
        self.assertIn(".github/workflows/release.yml", written)
        self.assertIn("AGENTS.md", written)
        self.assertEqual((self.target / "VERSION").read_text(), "0.0.0\n")
        self.assertIn("Mozilla Public License", (self.target / "LICENSE").read_text().split("\n", 1)[0])
        self.assertRegex((self.target / "CHANGELOG.md").read_text(), r"## 0\.0\.0 — \d{4}-\d{2}-\d{2}")
        self.assertIn("cut as 0.1.0", (self.target / "CHANGELOG.md").read_text())
        self.assertTrue(os.access(self.target / "scripts" / "package-release.sh", os.X_OK))
        verdicts = self.product.json("check", str(self.target), "--product", "maelys-widget")["data"]
        self.assertTrue(verdicts["valid"], verdicts["violations"])

    def test_the_packaging_stub_refuses_to_publish_nothing(self) -> None:
        self.run_new("--apply")
        completed = subprocess.run(["sh", "scripts/package-release.sh", "linux-x86_64"], cwd=self.target,
                                   check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("not implemented yet", completed.stderr)

    def test_a_dependency_is_pinned_at_the_commit_its_tag_names(self) -> None:
        data = self.run_new("--depends", f"maelys-system@{PINNED_TAG}", "--apply")["data"]
        self.assertEqual(data["dependencies"], [{"name": "maelys-system", "tag": PINNED_TAG,
                                                 "commit": self.product.tagged}])
        pin = (self.target / "dependencies" / "maelys-system.pin").read_text().splitlines()
        self.assertEqual(pin, [PINNED_TAG, self.product.tagged])
        self.assertTrue((self.target / "scripts" / "checkout-dependency.sh").is_file())

    def test_a_malformed_or_absent_dependency_is_refused(self) -> None:
        for spec, code in ((f"maelys-system{PINNED_TAG}", "VALIDATION_FAILED"),
                           ("maelys-system@v9.9.9", "NOT_FOUND")):
            error = self.run_new("--depends", spec, expect=1)["error"]
            self.assertEqual(error["code"], code, spec)

    def test_a_directory_that_holds_anything_is_refused(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "README.md").write_text("mine\n")
        error = self.run_new(expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("is not empty", error["message"])

    def test_a_product_without_the_socle_mechanism_gets_the_conventions_only(self) -> None:
        data = self.run_new("--mechanism", "none", "--apply")["data"]
        written = {entry["path"] for entry in data["adopted"]}
        self.assertNotIn(".github/workflows/release.yml", written)
        self.assertIn("AGENTS.md", written)
        self.assertFalse((self.target / ".github").exists())


@unittest.skipUnless(shutil.which("git-filter-repo"), "git-filter-repo is required to rewrite history")
class MigrateTest(unittest.TestCase):
    """The prose leaves the product and arrives in maelys-docs, with its history."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.write("docs/architecture.md", "# Architecture\n\nFirst.\n")
        self.product.write("docs/guide.md", "# Guide\n\nProse.\n")
        self.product.write("docs/schema.json", '{"a": 1}\n')
        self.product.write("README.md", "# Fixture\n\n## Documentation\n\n"
                                        "- [architecture](docs/architecture.md): how.\n"
                                        "- [guide](docs/guide.md): usage.\n\n## Licence\n\nMPL-2.0.\n")
        self.product.git(self.product.dir, "init", "-q")
        self.product.git(self.product.dir, "add", "-A")
        self.product.git(self.product.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")
        self.product.git(self.product.dir, "tag", "v1.2.3")     # maelys-docs pins the tag it describes
        # maelys-docs stands in the fixture's remotes, as the socle clones it.
        self.documents = self.product.work / "remotes" / "maelys-docs.git"
        seed = self.product.work / "documents-seed"
        seed.mkdir()
        (seed / "README.md").write_text("# Maelys documentation\n", encoding="utf-8")
        self.product.git(seed, "init", "-q")
        self.product.git(seed, "add", "-A")
        self.product.git(seed, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed")
        self.product.git(self.product.work, "clone", "-q", "--bare", str(seed), str(self.documents))

    def tearDown(self) -> None:
        self.product.close()

    def commit(self, message: str = "more") -> None:
        """Track what the test just wrote: filter-repo only keeps history."""
        self.product.git(self.product.dir, "add", "-A")
        self.product.git(self.product.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)

    def records(self, *names: str) -> str:
        path = self.product.work / "prose.jsonl"
        path.write_text("".join(json.dumps({"repository": "maelys-fixture", "path": f"docs/{name}",
                                            "kind": "prose",
                                            "destination": f"maelys-docs/maelys-fixture/{name}"}) + "\n"
                                for name in names), encoding="utf-8")
        return str(path)

    def migrate(self, *arguments: str, names: tuple = ("architecture.md", "guide.md"), expect: int = 0) -> dict:
        return self.product.json("migrate", self.dir, "--product", "maelys-fixture",
                                 "--documents", self.records(*names),
                                 "--documents-repository", "maelys-dev/maelys-docs", *arguments, expect=expect)

    def test_the_plan_says_what_moves_what_stays_and_what_still_names_it(self) -> None:
        data = self.migrate()["data"]
        self.assertEqual(data["mode"], "plan")
        self.assertEqual([entry["path"] for entry in data["moving"]], ["docs/architecture.md", "docs/guide.md"])
        self.assertEqual(data["moving"][0]["referencedBy"], ["README.md"])
        staying = {entry["path"]: entry["reason"] for entry in data["staying"]}
        self.assertEqual(staying, {"docs/schema.json": "data, not prose"})
        self.assertTrue((self.product.dir / "docs" / "architecture.md").is_file())   # nothing written

    def test_a_list_of_another_product_or_a_wrong_destination_is_refused(self) -> None:
        path = self.product.work / "foreign.jsonl"
        path.write_text(json.dumps({"repository": "maelys-other", "path": "docs/a.md",
                                    "destination": "maelys-docs/maelys-other/a.md"}) + "\n", encoding="utf-8")
        error = self.product.json("migrate", self.dir, "--product", "maelys-fixture", "--documents", str(path),
                                  expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("maelys-other", error["message"])

    def test_a_document_that_is_not_in_the_product_is_refused(self) -> None:
        error = self.migrate(names=("absent.md",), expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("is not a file", error["message"])

    def test_apply_moves_the_history_and_leaves_the_product_clean(self) -> None:
        data = self.migrate("--apply")["data"]
        self.assertEqual(data["mode"], "apply")
        self.assertFalse(data["pushed"])
        kept = pathlib.Path(data["kept"])
        documents, product = kept / "documents", kept / "product"
        # The prose arrived on its new path, with the commit that wrote it.
        self.assertTrue((documents / "maelys-fixture" / "architecture.md").is_file())
        log = self.product.git(documents, "log", "--oneline", "--", "maelys-fixture/architecture.md")
        self.assertIn("fixture", log)
        self.assertEqual((documents / "maelys-fixture" / "VERSION").read_text(), "1.2.3\n")
        # The pin the fleet uses since 0.14.0, not the adapter/ the socle refuses.
        self.assertFalse((documents / "adapter").exists())
        self.assertEqual((documents / "dependencies" / "maelys-fixture.pin").read_text().splitlines(),
                         ["v1.2.3", self.product.git(self.product.dir, "rev-parse", "HEAD")])
        # And left the product, whose README says the prose went, without
        # naming a destination a reader of a public repository cannot open:
        # maelys-docs is private, and the socle used to plant that reference.
        self.assertFalse((product / "docs" / "architecture.md").exists())
        self.assertTrue((product / "docs" / "schema.json").is_file())
        readme = (product / "README.md").read_text()
        self.assertNotIn("docs/architecture.md", readme)
        self.assertNotIn("maelys-dev/maelys-docs", readme)
        self.assertIn("no longer kept in this repository", readme)
        self.assertIn("## Licence", readme)      # the rest of the README is untouched
        # The operator's checkout is not written to at all.
        self.assertTrue((self.product.dir / "docs" / "architecture.md").is_file())

    def test_an_untagged_product_describes_no_release(self) -> None:
        self.product.git(self.product.dir, "tag", "-d", "v1.2.3")
        error = self.migrate("--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("no tag", error["message"])

    def test_every_markdown_of_the_product_is_pointed_away_when_the_destination_is_named(self) -> None:
        # The first shape rewrote README.md alone, and a product had to
        # repoint examples/README.md by hand: reporting a problem and fixing
        # half of it is worse than either extreme.
        self.product.write("maelys-release.conf", "[docs]\nnamed\n" + APART)
        self.product.write("examples/README.md", "See [the guide](docs/guide.md) and `docs/guide.md`.\n")
        self.product.write("docs/other.md", "Also [guide](guide.md), which travels with it.\n")
        self.commit()
        data = self.migrate("--apply", names=("guide.md", "other.md"))["data"]
        kept = pathlib.Path(data["kept"]) / "product"
        self.assertIn("examples/README.md", data["rewritten"])
        example = (kept / "examples" / "README.md").read_text()
        self.assertNotIn("docs/guide.md", example)
        self.assertIn("the guide", example)                                   # the text stays
        self.assertIn("maelys-docs/maelys-fixture/guide.md", example)         # the path is repointed
        self.assertEqual(data["remaining"], [])

    def test_the_markdown_names_no_destination_unless_declared(self) -> None:
        # The rewrite above wrote `maelys-docs/...` into the AGENTS.md of a
        # public product, beside the managed block that had just stopped
        # naming it (maelys-cli#83). Without [docs] named the link goes, the
        # path stays, and the file is reported for a hand.
        self.product.write("examples/README.md", "See [the guide](docs/guide.md) and `docs/guide.md`.\n")
        self.commit()
        data = self.migrate("--apply", names=("guide.md",))["data"]
        kept = pathlib.Path(data["kept"]) / "product"
        self.assertIn("examples/README.md", data["rewritten"])
        example = (kept / "examples" / "README.md").read_text()
        self.assertEqual(example, "See the guide and `docs/guide.md`.\n")
        self.assertNotIn("maelys-docs", example)
        self.assertEqual([(entry["path"], entry["line"]) for entry in data["remaining"]],
                         [("examples/README.md", 1)])
        self.assertIn("BY HAND  examples/README.md:1 still names docs/guide.md", MODULE.text_migrate(data))

    def test_a_document_that_moves_keeps_its_own_relative_links(self) -> None:
        self.product.write("docs/other.md", "Also [guide](guide.md), which travels with it.\n")
        self.commit()
        data = self.migrate("--apply", names=("guide.md", "other.md"))["data"]
        moved = pathlib.Path(data["kept"]) / "documents" / "maelys-fixture" / "other.md"
        self.assertIn("[guide](guide.md)", moved.read_text())

    def test_what_the_socle_will_not_rewrite_is_named_with_its_line(self) -> None:
        self.product.write("include/fixture.h", "/* See docs/guide.md for the format. */\n")
        self.product.write("Makefile", "install:\n\tinstall -m 0644 docs/*.md $(DESTDIR)/share\n")
        self.commit()
        data = self.migrate("--apply", names=("guide.md",))["data"]
        self.assertEqual([(entry["path"], entry["line"]) for entry in data["remaining"]],
                         [("include/fixture.h", 1)])
        self.assertEqual([(entry["path"], entry["line"]) for entry in data["globs"]], [("Makefile", 2)])
        # And the commit that asks for the pull request carries the list.
        message = self.product.git(pathlib.Path(data["kept"]) / "product", "log", "-1", "--format=%B")
        self.assertIn("include/fixture.h:1 names docs/guide.md", message)
        self.assertIn("Makefile:2", message)

    def test_a_prose_already_migrated_is_refused_instead_of_failing_opaquely(self) -> None:
        # Pushing needs both sides to have a remote, as a real product does.
        remotes = self.product.work / "remotes"
        self.product.git(self.product.work, "clone", "-q", "--bare", self.dir, str(remotes / "maelys-fixture.git"))
        self.product.git(self.product.dir, "remote", "add", "origin", str(remotes / "maelys-fixture.git"))
        self.migrate("--apply", "--push")
        # The pull request of that push is merged: maelys-docs carries the
        # prose on its default branch from now on.
        self.product.git(self.documents, "update-ref", "refs/heads/main", "refs/heads/migrate/maelys-fixture")
        # The same migration again: maelys-docs has it, so there is nothing to add.
        error = self.migrate("--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("already carries this prose", error["message"])

    def test_nothing_leaves_the_machine_without_push(self) -> None:
        self.migrate("--apply")
        remote = self.product.git(self.documents, "for-each-ref", "--format=%(refname)")
        self.assertNotIn("migrate/maelys-fixture", remote)


class DocsContractTest(unittest.TestCase):
    """docs/ holds what a machine writes and what LICENSING.md engages; prose moves."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)

    def tearDown(self) -> None:
        self.product.close()

    def notes(self) -> list[str]:
        return [check["message"] for check in self.product.json("check", self.dir)["data"]["checks"]
                if check["status"] == "note"]

    def test_prose_is_noted_with_its_destination_and_nothing_else_is(self) -> None:
        self.product.write("docs/architecture.md", "# Architecture\n\nProse.\n")
        self.product.write("docs/cli.md", "<!-- generated from describe; do not edit -->\n\n# CLI\n")
        self.product.write("docs/schema.json", '{"a": 1}\n')
        self.product.write("docs/other-generated.md", "<!-- GENERATED; do not edit -->\n\n# Other\n")
        self.product.run("adopt", self.dir, "--apply")
        notes = [note for note in self.notes() if "maelys-docs" in note]
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("docs/architecture.md: prose belongs in"
                                            " maelys-docs/maelys-fixture/architecture.md"), notes[0])
        self.assertIn("migrate", notes[0])      # the note names the remedy

    def test_the_generated_mark_is_read_in_the_head_not_on_one_line(self) -> None:
        # The three spellings in the fleet, one of them under the title.
        self.product.write("docs/a.md", "<!-- GENERATED by maelys-cli-reference; do not edit manually. -->\n# A\n")
        self.product.write("docs/b.md", "# Configuration key reference\n\n"
                                        "<!-- Generated by tools/generate_config_reference.py; do not edit. -->\n")
        self.product.write("docs/c.md", "<!-- GENERATED by maelys-cli 0.5.19 (catalog); do not edit by hand. -->\n")
        # The word alone is not the mark: prose says "generated" all the time.
        self.product.write("docs/d.md", "# Design\n\nThe identifiers are generated at build time.\n")
        self.product.run("adopt", self.dir, "--apply")
        prose = [note.split(":")[0] for note in self.notes() if "maelys-docs" in note]
        self.assertEqual(prose, ["docs/d.md"])

    def test_a_document_licensing_engages_stays(self) -> None:
        self.product.write("docs/open-core.md", "# Open core\n\nProse, but engaged.\n")
        self.product.write("LICENSING.md", "# Licensing\n\nSee [open core](docs/open-core.md).\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertEqual([note for note in self.notes() if "maelys-docs" in note], [])

    def test_a_verified_reference_stays(self) -> None:
        """maelys-platform's sixth category, on its own condition: a reference
        a check of the repository reads against the code, which a private
        maelys-docs would take out of reach of that check."""
        self.product.write("docs/api-reference.md", "<!-- VERIFIED by make api-doc-check against the public API;"
                                                    " edit with the code. -->\n# API\n")
        # Naming a check without asking to be edited with the code is not the mark.
        self.product.write("docs/testing.md", "# Testing\n\nVerified by make check against every target.\n")
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        self.assertEqual([note.split(":")[0] for note in self.notes() if "maelys-docs" in note], ["docs/testing.md"])
        self.assertIn("docs/api-reference.md is a reference make api-doc-check verifies against the code: it stays",
                      [check["message"] for check in data["checks"]])

    def test_prose_a_readme_links_is_named_and_never_refused(self) -> None:
        """check is offline: it sees the link, not the visibility nor a site,
        so the note says the rule and the strict contract asserts nothing."""
        for name in ("linked", "under", "mentioned", "absolute", "free"):
            self.product.write(f"docs/{'guides/' if name == 'under' else ''}{name}.md", f"# {name}\n")
        self.product.write("README.md", "# Fixture\n\n[Linked](./docs/linked.md#top) and [guides](docs/guides/).\n"
                           "Also `docs/mentioned.md`, in a sentence.\n\n"
                           "[abs]: https://github.com/maelys-dev/maelys-fixture/blob/main/docs/absolute.md\n")
        self.product.run("adopt", self.dir, "--apply")
        self.product.git(self.product.dir, "init", "-q")
        self.product.git(self.product.dir, "remote", "add", "origin", "https://github.com/maelys-dev/maelys-fixture.git")
        held = sorted(note.split(":")[0] for note in self.notes() if "README.md links it" in note)
        self.assertEqual(held, ["docs/absolute.md", "docs/guides/under.md", "docs/linked.md"])
        strict = self.product.json("check", self.dir, "--docs-contract", expect=2)["data"]["conventions"]["violations"]
        self.assertEqual(sorted(violation.split(":")[0] for violation in strict if "maelys-docs" in violation),
                         ["docs/free.md", "docs/mentioned.md"])

    def test_the_conditions_are_maelys_platforms(self) -> None:
        # Named, never guessed next door: a neighbour is whatever its
        # developer left there, which is the rule this socle holds itself to.
        named = os.environ.get("MAELYS_PLATFORM_DIR")
        platform = pathlib.Path(named) / "bin" / "maelys-platform" if named else None
        if not platform or not platform.is_file():
            self.skipTest("MAELYS_PLATFORM_DIR names no maelys-platform checkout")
        text = platform.read_text(encoding="utf-8")
        self.assertIn(MODULE.VERIFIED_MARK.pattern, text)
        self.assertIn(MODULE.README_LINK.pattern, text)

    def test_prose_is_a_note_by_default_and_a_violation_on_demand(self) -> None:
        self.product.write("docs/architecture.md", "# Architecture\n\nProse.\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertTrue(self.product.json("check", self.dir)["data"]["conventions"]["valid"])
        strict = self.product.json("check", self.dir, "--docs-contract", expect=2)["data"]
        self.assertFalse(strict["conventions"]["valid"])
        self.assertTrue(any(violation.startswith("docs/architecture.md: prose belongs in"
                                                  " maelys-docs/maelys-fixture/architecture.md")
                            for violation in strict["conventions"]["violations"]),
                        strict["conventions"]["violations"])

    def test_the_earlier_reference_paths_are_named_with_their_git_mv(self) -> None:
        self.product.write("docs/cli-reference.md", "<!-- GENERATED; do not edit -->\n\n# CLI\n")
        self.product.run("adopt", self.dir, "--apply")
        note = [note for note in self.notes() if "docs/cli-reference.md" in note]
        self.assertEqual(len(note), 1, self.notes())
        self.assertIn("git mv docs/cli-reference.md docs/cli.md", note[0])
        strict = self.product.json("check", self.dir, "--docs-contract", expect=2)["data"]
        self.assertTrue(any("docs/cli-reference.md" in violation for violation in strict["conventions"]["violations"]))

    RECORDING_GENERATOR = (
        "import pathlib, sys\n"
        "arguments = sys.argv[1:]\n"
        "markdown = pathlib.Path(arguments[arguments.index('--markdown') + 1])\n"
        "contract = pathlib.Path(arguments[arguments.index('--json') + 1])\n"
        "build = arguments[arguments.index('--build') + 1]\n"
        "rest = arguments[arguments.index('--json') + 2:]\n"
        "markdown.write_text('<!-- generated; do not edit -->\\n\\n# CLI\\n\\nbuild=%s rest=%s\\n'\n"
        "                    % (pathlib.Path(build).name, ' '.join(rest)))\n"
        "contract.write_text('{}\\n')\n")

    def with_a_command_line(self, generator: str | None = None) -> None:
        """A product whose command line is built on the framework pins maelys-cli.

        The generator is maelys-cli's; a checkout beside the product at the
        pinned commit stands in for it, as checkout-dependency.sh leaves one.
        """
        self.product.write("docs/cli.md", "<!-- generated; do not edit -->\n\n# CLI\n")
        commit = "c" * 40
        if generator is not None:
            checkout = self.product.work / "maelys-cli"
            (checkout / "tools").mkdir(parents=True, exist_ok=True)
            (checkout / "tools" / "generate_cli_reference.py").write_text(generator, encoding="utf-8")
            self.product.git(checkout, "init", "-q")
            self.product.git(checkout, "add", "-A")
            self.product.git(checkout, "commit", "-q", "-m", "generator")
            commit = self.product.git(checkout, "rev-parse", "HEAD")
        self.product.write("dependencies/maelys-cli.pin", f"v0.5.19\n{commit}\n")

    def test_the_socle_generates_the_reference_itself(self) -> None:
        self.with_a_command_line(self.RECORDING_GENERATOR)
        data = self.product.json("adopt", self.dir, "--apply")["data"]
        written = {entry["path"] for entry in data["files"]}
        self.assertIn("docs/cli.md", written)
        self.assertIn("docs/cli-contract.json", written)
        # No rule, no path and no drift check of its own: nothing to call.
        self.assertNotIn("scripts/render-cli-reference.sh", written)
        self.assertFalse((self.product.dir / "scripts" / "render-cli-reference.sh").exists())
        # The programs default to the product's commands, its libraries aside.
        self.assertIn("build=bin rest=maelys-fixture", self.product.read("docs/cli.md"))
        self.assertTrue(self.product.json("check", self.dir)["data"]["valid"])

    def test_declared_programs_and_flags_replace_a_makefile_variable(self) -> None:
        self.with_a_command_line(self.RECORDING_GENERATOR)
        self.product.write("docs/cli.reference",
                           "# what this product's reference needs\n"
                           "[programs]\nmaelys\nmaelys-hello\n\n"
                           "[flags]\n--neutral-availability unpack-rootfs\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("rest=--neutral-availability unpack-rootfs maelys maelys-hello",
                      self.product.read("docs/cli.md"))

    def test_the_framework_itself_carries_the_generator_instead_of_pinning_it(self) -> None:
        # maelys-cli holds tools/generate_cli_reference.py; it is held to the
        # same rule as the products it serves, with no pin to itself.
        self.product.write("docs/cli.md", "<!-- generated; do not edit -->\n\n# CLI\n")
        self.product.write("tools/generate_cli_reference.py", self.RECORDING_GENERATOR)
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("build=bin rest=maelys-fixture", self.product.read("docs/cli.md"))
        self.assertTrue(self.product.json("check", self.dir)["data"]["valid"])

    def test_the_build_directory_is_declared_not_guessed(self) -> None:
        self.with_a_command_line(self.RECORDING_GENERATOR)
        self.product.write("docs/cli.reference", "[build]\nbuild/release/bin\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("build=bin", self.product.read("docs/cli.md"))
        data = self.product.json("adopt", self.dir)["data"]
        self.assertTrue(any(entry["path"] == "docs/cli.md" for entry in data["files"]))
        for bad in ("[build]\n/etc\n", "[build]\n../elsewhere\n", "[build]\nbuild/a\nbuild/a\n"):
            self.product.write("docs/cli.reference", bad)
            refused = self.product.json("check", self.dir, expect=2)["data"]
            self.assertTrue(any("docs/cli.reference" in violation
                                for violation in refused["conventions"]["violations"]), bad)

    def test_a_build_tree_per_platform_names_every_one_of_them(self) -> None:
        """One directory made the note certain on every machine but one.

        maelys-oci declares build/linux-x86_64/release/bin; on a Mac the
        socle printed "the generator did not run here" at every adopt and
        every check, which is a note that stops being read. The first
        directory holding the programs is used -- not the first that
        exists, since a machine may carry a cross-compiled tree it cannot
        run.
        """
        self.with_a_command_line(self.RECORDING_GENERATOR)
        self.product.write("docs/cli.reference",
                           "[build]\nbuild/linux-x86_64/release/bin\nbuild/macos-arm64/release/bin\n")
        here = self.product.dir / "build" / "macos-arm64" / "release" / "bin"
        here.mkdir(parents=True)
        (here / "maelys-fixture").write_text("#!/bin/sh\nexit 0\n")
        (here / "maelys-fixture").chmod(0o755)
        # The cross-compiled tree exists and holds nothing runnable here.
        (self.product.dir / "build" / "linux-x86_64" / "release" / "bin").mkdir(parents=True)
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("build=bin", self.product.read("docs/cli.md"))
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])

    def test_the_note_says_which_directories_were_tried(self) -> None:
        # A generator that refuses, which is what an unbuilt product's does.
        self.with_a_command_line("import sys\nsys.exit(1)\n")
        self.product.write("docs/cli.reference", "[build]\nbuild/a/bin\nbuild/b/bin\n")
        self.product.run("adopt", self.dir, "--apply")
        notes = [check["message"] for check in self.product.json("check", self.dir)["data"]["checks"]
                 if check["status"] == "note" and "did not run here" in check["message"]]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("[build] named build/a/bin, build/b/bin", notes[0])

    def test_a_malformed_declaration_is_refused(self) -> None:
        self.with_a_command_line(self.RECORDING_GENERATOR)
        self.product.write("docs/cli.reference", "[targets]\nmaelys\n")
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertTrue(any("docs/cli.reference" in violation for violation in data["conventions"]["violations"]))

    def test_a_stale_reference_is_a_drift(self) -> None:
        self.with_a_command_line(self.RECORDING_GENERATOR)
        self.product.run("adopt", self.dir, "--apply")
        self.product.write("docs/cli.md", "<!-- generated; do not edit -->\n\n# CLI stale\n")
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertIn("docs/cli.md: update", data["conventions"]["violations"])

    def test_an_unavailable_generator_leaves_the_reference_alone(self) -> None:
        self.with_a_command_line()      # no maelys-cli at the pinned commit
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"], data["violations"])
        self.assertTrue(any("maelys-cli is not available" in check["message"] for check in data["checks"]))
        self.assertEqual(self.product.read("docs/cli.md"), "<!-- generated; do not edit -->\n\n# CLI\n")

    def test_a_library_is_told_nothing_about_a_reference_it_cannot_have(self) -> None:
        # Everything this fixture publishes becomes a library: no command, so
        # no reference, and no note asking for one.
        for name in ("maelys-fixture", "libmaelys-fixture"):
            (self.product.dir / "packaging" / "homebrew" / f"{name}.rb.in").unlink()
        self.product.write("packaging/homebrew/libmaelys-fixture.rb.in", "class LibmaelysFixture < Formula\nend\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertEqual([note for note in self.notes() if "cli.md" in note], [])

    def test_a_product_without_a_framework_command_line_generates_nothing(self) -> None:
        self.product.write("docs/cli.md", "<!-- generated; do not edit -->\n\n# CLI\n")
        data = self.product.json("adopt", self.dir)["data"]
        self.assertNotIn("docs/cli-contract.json", {entry["path"] for entry in data["files"]})


COMING_NOTE = ("coming in maelys-release " + MODULE.COMING[0][0] + ": " + MODULE.COMING[0][2]) \
    if MODULE.COMING else ""


class GoldenTest(unittest.TestCase):
    """The text a human reads, in full: check conformant, check drifting, preflight not ready."""

    CONTRACT = textwrap.dedent("""\
        ok       VERSION file
        ok       scripts/package-release.sh TARGET writing dist/
        ok       CHANGELOG.md
        ok       CHANGELOG.md has a dated ## 1.2.3 entry
        ok       Homebrew formula templates: libmaelys-fixture maelys-fixture
        ok       pinned dependencies: maelys-system
        ok       .github/workflows/ci.yml calls check-product.yml of the socle
        ok       dependencies/packages: linux [pkg-config libjansson-dev] macos [jansson]
        ok       maelys-release.conf [dependencies] apart: the pins are materialised under $MAELYS_DEPENDENCIES_DIR, never beside the product
        """) + ("note     " + COMING_NOTE + "\n" if COMING_NOTE else "")
    FILES = textwrap.dedent("""\
        same     .github/workflows/release.yml
        same     scripts/checkout-dependency.sh
        same     scripts/checkout-dependencies.sh
        same     AGENTS.md
        same     CLAUDE.md
        same     .claude/skills/maelys-release/SKILL.md
        same     .github/workflows/ci.yml
        """)
    VERDICTS = "verdict  conventions: ok\nverdict  release mechanism: ok\n"

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.run("adopt", self.dir, "--apply")

    def tearDown(self) -> None:
        self.product.close()

    def test_check_conformant(self) -> None:
        completed = self.product.run("check", self.dir)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout, self.CONTRACT + self.FILES + self.VERDICTS
                         + "check: maelys-fixture is on maelys-release v9.9.9\n")

    def test_check_drifting(self) -> None:
        """The whole answer, not the word "update".

        `check` computes the diff either way and the JSON has always carried
        it; the text printed the action and threw the rest, so a product
        that declared something without adopting again read a FAIL naming a
        file and nothing about the lines in it. maelys-egress read that.
        """
        with (self.product.dir / ".github" / "workflows" / "release.yml").open("a") as workflow:
            workflow.write("\n# edited\n")
        completed = self.product.run("check", self.dir, expect=2)
        self.assertEqual(completed.stderr, "")
        drifted = textwrap.dedent("""\
            update   .github/workflows/release.yml [prose]
                     --- .github/workflows/release.yml
                     +++ .github/workflows/release.yml
                     @@ -63,5 +63,3 @@
                          secrets:
                            tap_token: ${{ secrets.HOMEBREW_TAP_TOKEN }}
                            tap_signing_key: ${{ secrets.HOMEBREW_TAP_SIGNING_KEY }}
                     -
                     -# edited
            """)
        self.assertEqual(completed.stdout, self.CONTRACT
                         + self.FILES.replace("same     .github/workflows/release.yml\n", drifted)
                         # Only a comment moved, and the line above the verdict says so.
                         + "touches  1 prose: 'mechanism' changes what runs, 'pin' only the socle commit a"
                           " workflow names, 'prose' only text and comments\n"
                         + self.VERDICTS.replace("release mechanism: ok", "release mechanism: FAIL")
                         + f"write    'maelys-release adopt {self.product.dir.resolve()} --apply'"
                           " writes .github/workflows/release.yml\n"
                         + "check: maelys-fixture drifts from maelys-release v9.9.9\n")

    def test_the_half_adoption_says_which_lines_and_which_command(self) -> None:
        """Declare, then check: the order maelys-egress followed, and ours.

        adopt, read what the socle advises, declare it -- and the
        declaration changes what the generated files must hold, so the
        natural order ends on a FAIL that named a file and no more. The
        product had no way to see that its three sibling checkouts were
        the thing to replace.
        """
        # The state the order leaves: release.yml was generated before the
        # declaration, so it still clones a sibling per pin while the
        # declaration says the build reads a root.
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace(
            '        sh scripts/checkout-dependencies.sh "$RUNNER_TEMP/dependencies" >>"$GITHUB_ENV"',
            "        sh scripts/checkout-dependency.sh maelys-system"), encoding="utf-8")
        completed = self.product.run("check", self.dir, expect=2)
        self.assertIn("-        sh scripts/checkout-dependency.sh maelys-system", completed.stdout)
        self.assertIn('+        sh scripts/checkout-dependencies.sh "$RUNNER_TEMP/dependencies"', completed.stdout)
        self.assertIn(f"'maelys-release adopt {self.product.dir.resolve()} --apply'", completed.stdout)

    def test_check_pinned_elsewhere(self) -> None:
        """A product behind its socle is told which file moved, and how.

        This branch used to answer one sentence and no file: an editorial
        pass on a managed block and a fix to the release mechanism read
        identically, and the product had to adopt to find out which it was.
        The plan is not drift -- the product is conformant to the socle it
        pins -- so it is shown beside the verdict and counted as nothing.
        """
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("release.yml@" + "f" * 40 + " # v9.9.9",
                                                         "release.yml@" + "0" * 40 + " # v0.0.0"))
        completed = self.product.run("check", self.dir, expect=2)
        self.assertIn("not a drift against the socle this product pins", completed.stdout)
        # Only the commit it names moved: a pin, which a reader knows
        # without reading the diff below.
        self.assertIn("update   .github/workflows/release.yml [pin]", completed.stdout)
        self.assertIn("touches  1 pin:", completed.stdout)
        self.assertIn("-    uses: maelys-dev/maelys-release/.github/workflows/release.yml@" + "0" * 40,
                      completed.stdout)
        self.assertIn("+    uses: maelys-dev/maelys-release/.github/workflows/release.yml@" + "f" * 40,
                      completed.stdout)
        self.assertIn("says what each asks of a product", completed.stdout)
        self.assertIn("drift    maelys-fixture pins maelys-release v0.0.0 (0000000) but this is v9.9.9 (fffffff)",
                      completed.stdout)
        self.assertTrue(completed.stdout.endswith("check: maelys-fixture drifts from maelys-release v9.9.9\n"))
        # The files of a socle ahead are not violations: the product is
        # conformant to what it pins.
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertEqual(data["violations"],
                         [violation for violation in data["violations"] if "pins maelys-release" in violation])

    def test_preflight_not_ready(self) -> None:
        product = self.product
        product.git(product.dir, "init", "-q")
        product.git(product.dir, "add", "-A")
        product.git(product.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")
        completed = product.run("preflight", self.dir, expect=2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout, self.CONTRACT + self.FILES + self.VERDICTS + textwrap.dedent("""\
            check: maelys-fixture is on maelys-release v9.9.9
            FAIL     tag.gpgsign is not true: git config tag.gpgsign true
            FAIL     user.signingkey is not set (gpg.format = openpgp); the key must be registered on GitHub
            note     no v* tag yet
            ok       tag v1.2.3 is free
            note     origin is not on GitHub: release environment not checked
            preflight: maelys-fixture is not ready to tag
            """))

    def test_texts_carry_no_socle_version(self) -> None:
        for name in ("AGENTS.md", "CLAUDE.md", ".claude/skills/maelys-release/SKILL.md", "scripts/checkout-dependency.sh"):
            text = self.product.read(name)
            self.assertNotIn("v9.9.9", text, name)
            self.assertNotIn((ROOT / "VERSION").read_text().strip(), text, name)


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.run("adopt", self.dir, "--apply")
        self.product.git(self.product.dir, "init", "-q")
        self.product.git(self.product.dir, "add", "-A")
        self.product.git(self.product.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")

    def tearDown(self) -> None:
        self.product.close()

    def preflight(self, expect: int) -> dict:
        return self.product.json("preflight", self.dir, expect=expect)["data"]

    def test_signing_configuration_and_tags(self) -> None:
        product = self.product
        data = self.preflight(2)
        self.assertFalse(data["ready"])
        self.assertTrue(any("tag.gpgsign" in item["message"] for item in data["preflight"] if item["status"] == "fail"))
        product.git(product.dir, "config", "tag.gpgsign", "true")
        product.git(product.dir, "config", "gpg.format", "ssh")
        key = product.work / "signing-key"
        product.git(product.dir, "config", "user.signingkey", str(key))
        data = self.preflight(0)
        self.assertTrue(data["ready"])
        self.assertIn("no v* tag yet", [item["message"] for item in data["preflight"]])
        self.assertIn("preflight: maelys-fixture is ready to tag", product.run("preflight", self.dir).stdout)
        product.git(product.dir, "-c", "tag.gpgsign=false", "tag", "v1.0.0")
        self.assertTrue(any("lightweight" in item["message"] for item in self.preflight(2)["preflight"]))
        product.git(product.dir, "tag", "-d", "v1.0.0")
        product.git(product.dir, "-c", "tag.gpgsign=false", "tag", "-a", "v1.0.0", "-m", "unsigned")
        self.assertTrue(any("not signed" in item["message"] for item in self.preflight(2)["preflight"]))
        product.git(product.dir, "tag", "-d", "v1.0.0")
        if shutil.which("ssh-keygen"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            product.git(product.dir, "tag", "-s", "v1.0.0", "-m", "signed")
            self.assertTrue(self.preflight(0)["ready"])
            product.git(product.dir, "-c", "tag.gpgsign=false", "tag", "v1.2.3")
            self.assertTrue(any("already exists" in item["message"] for item in self.preflight(2)["preflight"]))

    def test_drift_stops_preflight(self) -> None:
        (self.product.dir / "VERSION").write_text("9.9.9\n")
        data = self.preflight(2)
        self.assertFalse(data["valid"])
        self.assertEqual(data["preflight"], [])


class CutTest(unittest.TestCase):
    """The gate `cut` holds before it writes anything.

    The command's middle is GitHub's — a pull request, its checks and its
    merge — so what is tested here is everything that happens before the
    first push and the reading of a changelog, which is where a release
    goes wrong on a laptop.
    """

    ISOLATE = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")

    class Stub:
        """An invocation of the framework, reduced to what cut reads of it."""

        def __init__(self, **options) -> None:
            self.options = options
            self.format = "json"

        def flag(self, name: str) -> bool:
            return bool(self.options.get(name, False))

        def option(self, name: str, default=None):
            return self.options.get(name, default)

    def setUp(self) -> None:
        self.product = Product()
        self.dir = self.product.dir
        self.product.run("adopt", str(self.dir), "--apply")
        self.product.git(self.dir, "init", "-q")
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")
        # An origin, so the plan path reaches the gate: cut fetches the base
        # branch and refuses a HEAD that is not level with it.
        self.origin = self.product.work / "origin.git"
        self.product.git(self.product.work, "init", "-q", "--bare", str(self.origin))
        self.product.git(self.dir, "config", "user.name", "test")
        self.product.git(self.dir, "config", "user.email", "test@example.invalid")
        self.product.git(self.dir, "remote", "add", "origin", str(self.origin))
        self.product.git(self.dir, "push", "-q", "origin", "main")
        # These tests call the module in process: the host's git
        # configuration must not decide whether the gate passes.
        self.saved = {name: os.environ.get(name) for name in self.ISOLATE}
        os.environ.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                           "GIT_CONFIG_NOSYSTEM": "1"})
        self.log = open(self.product.work / "cut.log", "w", encoding="utf-8")

    def tearDown(self) -> None:
        self.log.close()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.product.close()

    def declarations(self, mechanism: str = "custom"):
        return MODULE.read_declarations(self.dir, "maelys-fixture", mechanism)

    def cut(self, version: str = "1.3.0", **options):
        data = {"mode": "apply" if options.get("--apply") else "plan", "stage": "open",
                "product": "maelys-fixture", "project": str(self.dir),
                "repository": "maelys-dev/maelys-fixture", "version": version, "tag": f"v{version}",
                "branch": f"release/v{version}", "base": "main", "gate": [], "checks": [], "ready": False}
        # A real stream: cut hands the verify command's output straight to
        # this file descriptor, so an operator watching a long `make check`
        # sees it as it runs rather than at the end.
        return MODULE.cut_open(self.Stub(**options), self.declarations(), data, self.log, 1, 1)

    def refusal(self, version: str = "1.3.0", **options) -> MODULE.Failure:
        with self.assertRaises(MODULE.Failure) as raised:
            self.cut(version, **options)
        return raised.exception

    def test_a_published_version_is_never_cut_twice(self) -> None:
        # No tag names 1.2.3 yet: the hint says VERSION was never published
        # and what to set -- a scaffold said 0.1.0 and its first cut of
        # 0.1.0 was refused on the pilot, with the hint of a published one.
        for version in ("1.2.3", "1.2.2"):
            failure = self.refusal(version)
            self.assertEqual(failure.code, "VALIDATION_FAILED")
            self.assertIn("1.2.3", failure.message)
            self.assertIn("No tag v1.2.3 exists", failure.hint)
        self.product.git(self.dir, "tag", "v1.2.3")
        failure = self.refusal("1.2.3")
        self.assertIn("never cut twice", failure.hint)
        self.assertNotIn("No tag", failure.hint)

    def test_only_the_bump_may_be_uncommitted(self) -> None:
        self.product.write("AGENTS.md", "# Agent instructions\n\nEdited.\n")
        failure = self.refusal()
        self.assertEqual(failure.code, "PRECONDITION_FAILED")
        self.assertIn("AGENTS.md", failure.message)
        # The bump itself is exactly what cut expects to find in progress.
        self.product.git(self.dir, "checkout", "--", "AGENTS.md")
        self.product.write("CHANGELOG.md", self.product.read("CHANGELOG.md") + "\n")
        self.assertIn("no dated entry", self.refusal().message)

    def test_the_changelog_entry_is_required_and_never_future_dated(self) -> None:
        self.assertIn("no dated entry", self.refusal().message)
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2999-01-01\n\n- Later.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        failure = self.refusal()
        self.assertEqual(failure.code, "VALIDATION_FAILED")
        self.assertIn("2999-01-01", failure.message)

    def test_a_release_is_not_cut_from_a_branch(self) -> None:
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.git(self.dir, "switch", "-q", "-c", "work")
        failure = self.refusal()
        self.assertEqual(failure.code, "PRECONDITION_FAILED")
        self.assertIn("HEAD is on work", failure.message)

    def test_the_gate_reads_the_signing_configuration_of_this_checkout(self) -> None:
        gate = MODULE.cut_gate(self.declarations(), "1.3.0")
        self.assertTrue(any(status == "fail" and "tag.gpgsign" in message for status, message in gate))
        self.product.git(self.dir, "config", "tag.gpgsign", "true")
        self.product.git(self.dir, "config", "user.signingkey", str(self.product.work / "signing-key"))
        gate = MODULE.cut_gate(self.declarations(), "1.3.0")
        self.assertEqual([status for status, _ in gate if status == "fail"], [])
        self.assertIn("tag v1.3.0 is free", [message for _, message in gate])
        # A repository the socle does not release has no release environment
        # to read, and the gate must not invent one for it.
        self.assertEqual([message for _, message in gate if "environment" in message], [])

    def test_the_changelog_entry_is_read_in_the_worktree_and_at_a_commit(self) -> None:
        self.assertEqual(MODULE.changelog_entry(self.dir, "1.2.3"), ("2026-09-03", "- Something."))
        self.assertEqual(MODULE.changelog_entry(self.dir, "9.9.9"), ("", ""))
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 1.2.3 — 2026-09-04\n\n- Rewritten.\n")
        self.assertEqual(MODULE.changelog_entry(self.dir, "1.2.3")[0], "2026-09-04")
        self.assertEqual(MODULE.changelog_entry(self.dir, "1.2.3", at="HEAD")[0], "2026-09-03")

    def test_a_check_that_skipped_is_not_a_refusal_and_no_check_is_not_a_pass(self) -> None:
        data = {"project": "/p", "version": "1.3.0", "branch": "release/v1.3.0", "gate": [],
                "pullRequest": {"number": 1, "url": "https://example.invalid/1", "state": "OPEN"}}
        green = [{"name": "check", "status": "completed", "conclusion": "success"},
                 {"name": "package", "status": "completed", "conclusion": "skipped"},
                 {"name": "lint", "status": "completed", "conclusion": "neutral"}]
        self.assertEqual(MODULE.cut_report(dict(data, gate=[]), green)[1], MODULE.EXIT_OK)
        red = green + [{"name": "fuzz", "status": "completed", "conclusion": "cancelled"}]
        self.assertEqual(MODULE.cut_report(dict(data, gate=[]), red)[1], MODULE.EXIT_VIOLATIONS)
        # A commit with no check at all has not passed: it has not been read.
        empty, code = MODULE.cut_report(dict(data, gate=[]), [])
        self.assertEqual(code, MODULE.EXIT_VIOLATIONS)
        self.assertFalse(empty["ready"])
        self.assertIn("no check is registered", empty["gate"][0]["message"])

    def test_the_plan_names_the_command_that_applies_it(self) -> None:
        """A plan that does not say what to run next is a plan a reader has to
        remember. 0.34.0 shipped without this line and nothing caught it."""
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.git(self.dir, "config", "tag.gpgsign", "true")
        self.product.git(self.dir, "config", "user.signingkey", str(self.product.work / "signing-key"))
        data, code = self.cut()
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertEqual(data["next"], f"maelys-release cut {self.dir} 1.3.0 --apply")

    def test_the_gate_names_the_product_own_verify_command(self) -> None:
        """A product's release gates are declared once and held at both ends:
        cut runs scripts/verify-release.sh before it writes anything."""
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.git(self.dir, "config", "tag.gpgsign", "true")
        self.product.git(self.dir, "config", "user.signingkey", str(self.product.work / "signing-key"))
        data, code = self.cut()
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertTrue(data["ready"])
        self.assertEqual([item for item in data["gate"] if "verify-release" in item["message"]], [])
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\nexit 0\n", executable=True)
        data, code = self.cut()
        noted = [item["message"] for item in data["gate"] if "verify-release" in item["message"]]
        self.assertEqual(len(noted), 1, data["gate"])
        self.assertNotIn("TARGET", noted[0])           # the host's own target, not the placeholder
        self.assertIn("before writing anything", noted[0])

    def test_check_reads_the_after_version_command(self) -> None:
        self.product.write("maelys-release.conf", "[cut]\nafter-version bash scripts/header.sh\n" + APART)
        messages = self.product.json("check", str(self.dir), expect=2)["data"]["conventions"]["violations"]
        self.assertTrue(any("scripts/header.sh" in message and "does not carry" in message
                            for message in messages), messages)
        self.product.write("scripts/header.sh", "#!/bin/sh\nexit 0\n", executable=True)
        data = self.product.json("check", str(self.dir))["data"]
        self.assertTrue(data["conventions"]["valid"])
        self.assertTrue(any("after-version: bash scripts/header.sh" in check["message"]
                            for check in data["checks"]))

    def test_the_worktree_snapshot_separates_tracked_from_untracked(self) -> None:
        """The two status columns are the answer, and the first is often a
        space: a stripped line loses the first character of the path."""
        self.product.write("AGENTS.md", "# Agent instructions\n\nEdited.\n")
        self.product.write("scripts/new-file.sh", "#!/bin/sh\n")
        paths = MODULE.worktree_paths(self.dir)
        self.assertEqual(paths.get("AGENTS.md"), " M")
        self.assertEqual(paths.get("scripts/new-file.sh"), "??")

    CHECK_RUNS = ('{"total_count":1,"check_runs":'
                  '[{"name":"check","status":"completed","conclusion":"success"}]}')

    def fake_gh(self) -> None:
        """A gh that answers exactly what the first stop asks of it.

        The first stop is the part of cut that writes: a signed commit, a
        push, a pull request and the wait. Everything but GitHub is real
        here — a bare repository is the origin, the commit is signed with a
        generated key — so what the test exercises is the sequence itself.
        """
        directory = self.product.work / "fake-bin"
        directory.mkdir(exist_ok=True)
        script = directory / "gh"
        script.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "api) printf '%s' '" + self.CHECK_RUNS + "' ;;\n"
            "pr) case \"$2\" in\n"
            "      create) echo 'https://example.invalid/pull/1' ;;\n"
            "      view) echo '{\"number\":1,\"url\":\"https://example.invalid/pull/1\",\"state\":\"OPEN\"}' ;;\n"
            "      list) echo '[]' ;;\n"
            "      *) exit 1 ;;\n"
            "    esac ;;\n"
            "*) exit 1 ;;\n"
            "esac\n", encoding="utf-8")
        script.chmod(0o755)
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        os.environ["PATH"] = f"{directory}:{os.environ['PATH']}"
        os.environ["_MAELYS_RELEASE_TEST_GH"] = str(script)
        self.addCleanup(os.environ.pop, "_MAELYS_RELEASE_TEST_GH", None)

    def signing_key(self) -> bool:
        if not shutil.which("ssh-keygen"):
            return False
        key = self.product.work / "signing-key"
        if not key.is_file():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        self.product.git(self.dir, "config", "tag.gpgsign", "true")
        self.product.git(self.dir, "config", "gpg.format", "ssh")
        self.product.git(self.dir, "config", "user.signingkey", str(key))
        return True

    def test_the_first_stop_commits_what_after_version_regenerated(self) -> None:
        """A version materialised twice: without this the bump commit is red
        by construction, and the wait could never see it green."""
        if not self.signing_key():
            self.skipTest("ssh-keygen is needed to sign the bump commit")
        self.fake_gh()
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.write("include/version.h", '#define VERSION "1.2.3"\n')
        self.product.write("scripts/header.sh",
                           '#!/bin/sh\nprintf \'#define VERSION "%s"\\n\' "$(cat VERSION)" >include/version.h\n',
                           executable=True)
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\ntouch verified\n", executable=True)
        self.product.write("maelys-release.conf", "[cut]\nafter-version sh scripts/header.sh\n" + APART)
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a version in two files")
        self.product.git(self.dir, "push", "-q", "origin", "main")

        data, code = self.cut(**{"--apply": True})
        self.assertEqual(code, MODULE.EXIT_OK, data)
        self.assertTrue(data["ready"])
        self.assertEqual(data["regenerated"], ["include/version.h"])
        # The declared gate ran before anything was written.
        self.assertTrue((self.dir / "verified").is_file())
        # One commit, on the release branch, carrying both files and signed.
        self.assertEqual(self.product.git(self.dir, "rev-parse", "--abbrev-ref", "HEAD"), "release/v1.3.0")
        self.assertEqual(self.product.read("include/version.h"), '#define VERSION "1.3.0"\n')
        committed = self.product.git(self.dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(committed), ["VERSION", "include/version.h"])
        self.assertIn("SIGNATURE", self.product.git(self.dir, "cat-file", "-p", "HEAD"))
        # The worktree is clean: nothing the command touched was left behind.
        self.assertEqual([path for path, code in MODULE.worktree_paths(self.dir).items() if code != "??"], [])

    def test_a_failing_after_version_restores_version_and_writes_nothing(self) -> None:
        if not self.signing_key():
            self.skipTest("ssh-keygen is needed to sign the bump commit")
        self.fake_gh()
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.write("scripts/header.sh", "#!/bin/sh\necho broken >&2\nexit 3\n", executable=True)
        self.product.write("maelys-release.conf", "[cut]\nafter-version sh scripts/header.sh\n" + APART)
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a generator that fails")
        self.product.git(self.dir, "push", "-q", "origin", "main")
        failure = self.refusal(**{"--apply": True})
        self.assertEqual(failure.code, "PROCESS_FAILED")
        self.assertIn("exit 3", failure.message)
        self.assertEqual(self.product.read("VERSION"), "1.2.3\n")
        self.assertEqual(self.product.git(self.dir, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def previous_release(self, old: str, new: str, *carriers: str) -> None:
        """The release before this one: VERSION and CARRIERS moved OLD -> NEW.

        This is the only thing the audit learns from, so a fixture that
        wants to be audited has to have released once.
        """
        for version in (old, new):
            for name in carriers:
                self.product.write(name, f'#define VERSION "{version}"\n')
            self.product.write("VERSION", version + "\n")
            self.product.git(self.dir, "add", "-A")
            self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q",
                             "-m", f"maelys-fixture {version}")
        self.product.git(self.dir, "push", "-q", "origin", "main")

    def test_a_version_that_moved_elsewhere_last_time_stops_this_cut(self) -> None:
        """The reported defect: VERSION bumped alone, and the branch already pushed.

        maelys-cli reported it from a VERSION and a header compared by make
        check-version; the refusal arrived after the branch and the pull
        request, and only because that product compares them at all.
        """
        if not self.signing_key():
            self.skipTest("ssh-keygen is needed to sign the bump commit")
        self.fake_gh()
        self.previous_release("1.2.2", "1.2.3", "include/version.h")
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        failure = self.refusal(**{"--apply": True})
        self.assertEqual(failure.code, "PRECONDITION_FAILED")
        self.assertIn("include/version.h", failure.message)
        self.assertIn("after-version", failure.hint)
        # Nothing was written, nothing was pushed, and the operator is where
        # they started: that is the whole point of auditing before the branch.
        self.assertEqual(self.product.read("VERSION"), "1.2.3\n")
        self.assertEqual(self.product.git(self.dir, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertNotIn("release/v1.3.0", self.product.git(self.dir, "branch", "--list"))

    def test_the_plan_says_so_before_anything_is_written(self) -> None:
        if not self.signing_key():
            self.skipTest("the plan reaches the gate, which reads the signing configuration")
        self.previous_release("1.2.2", "1.2.3", "include/version.h")
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        data, code = self.cut()
        self.assertEqual(code, MODULE.EXIT_OK)
        notes = [entry["message"] for entry in data["gate"] if entry["status"] == "note"]
        self.assertTrue(any("include/version.h" in note and "after-version" in note for note in notes), notes)

    def test_a_declared_after_version_satisfies_the_audit(self) -> None:
        if not self.signing_key():
            self.skipTest("ssh-keygen is needed to sign the bump commit")
        self.fake_gh()
        self.previous_release("1.2.2", "1.2.3", "include/version.h")
        self.product.write("scripts/header.sh",
                           '#!/bin/sh\nprintf \'#define VERSION "%s"\\n\' "$(cat VERSION)" >include/version.h\n',
                           executable=True)
        self.product.write("maelys-release.conf", "[cut]\nafter-version sh scripts/header.sh\n" + APART)
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "declare the generator")
        self.product.git(self.dir, "push", "-q", "origin", "main")
        data, code = self.cut(**{"--apply": True})
        self.assertEqual(code, MODULE.EXIT_OK, data)
        self.assertEqual(self.product.read("include/version.h"), '#define VERSION "1.3.0"\n')
        verdicts = {entry["message"] for entry in data["bump"] if entry["status"] == "ok"}
        self.assertIn("include/version.h carries 1.3.0", verdicts)

    def test_a_first_release_has_nothing_to_compare_with(self) -> None:
        """No previous bump is a note, never a refusal."""
        self.product.write("CHANGELOG.md",
                           "# Changelog\n\n## 1.3.0 — 2026-09-03\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        audit, stale = MODULE.bump_audit(self.dir, "1.2.3", "1.3.0")
        self.assertEqual(stale, [])
        self.assertEqual([status for status, _ in audit], ["note"])
        self.assertIn("no previous bump", audit[0][1])

    def test_a_carrier_that_stopped_carrying_the_version_is_a_note(self) -> None:
        """The twin of the documented limit, and the dangerous one.

        A place the version reaches since the previous bump is invisible: a
        missed detection the product's own checks still catch. A place the
        version *leaves* would be a refusal with nothing downstream to lift
        it, because [cut] declares a command and not a list. maelys-cli found
        it in their own history: a generated CLI reference carried the
        version at 0.1.0 -> 0.2.0 and carries none today.
        """
        self.previous_release("1.2.2", "1.2.3", "include/version.h", "docs/reference.md")
        # The generated reference stopped naming a version altogether.
        self.product.write("docs/reference.md", "# Reference\n\nNo version here any more.\n")
        self.product.write("include/version.h", '#define VERSION "1.3.0"\n')
        audit, stale = MODULE.bump_audit(self.dir, "1.2.3", "1.3.0")
        self.assertEqual(stale, [], "a retired carrier must not stop the release")
        verdicts = {name: status for status, name in
                    ((status, message.split()[0]) for status, message in audit)}
        self.assertEqual(verdicts["docs/reference.md"], "note")
        self.assertEqual(verdicts["include/version.h"], "ok")

    def test_a_carrier_still_holding_the_old_version_is_a_refusal(self) -> None:
        """The other reading, where nothing else explains the file."""
        self.previous_release("1.2.2", "1.2.3", "include/version.h")
        audit, stale = MODULE.bump_audit(self.dir, "1.2.3", "1.3.0")
        self.assertEqual(stale, ["include/version.h"])
        self.assertIn("still holds 1.2.3", audit[0][1])

    def test_a_carrier_that_left_the_tree_is_a_note(self) -> None:
        self.previous_release("1.2.2", "1.2.3", "include/version.h")
        (self.dir / "include" / "version.h").unlink()
        audit, stale = MODULE.bump_audit(self.dir, "1.2.3", "1.3.0")
        self.assertEqual(stale, [])
        self.assertIn("no longer in the tree", audit[0][1])

    def test_cut_refuses_a_repository_that_is_not_on_github(self) -> None:
        error = self.product.json("cut", str(self.dir), "1.3.0", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("not on GitHub", error["message"])

    def test_the_tag_annotation_belongs_to_the_second_stop(self) -> None:
        error = self.product.json("cut", str(self.dir), "1.3.0", "--message", "notes", expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("--tag", error["message"])


class BranchNameTest(unittest.TestCase):
    """A branch says what changes, not who typed; the socle notes, never refuses."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = self.product.dir
        self.product.run("adopt", str(self.dir), "--apply")
        self.product.git(self.dir, "init", "-q")
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")

    def tearDown(self) -> None:
        self.product.close()

    def notes(self) -> list:
        data = self.product.json("check", str(self.dir))["data"]
        self.assertTrue(data["conventions"]["valid"], "a branch name is never a violation")
        return [check["message"] for check in data["checks"] if "named after the tool" in check["message"]]

    def test_an_agent_prefix_is_noted_and_a_change_prefix_is_not(self) -> None:
        self.assertEqual(self.notes(), [])                                  # main
        for branch in ("claude/one-asset-per-channel", "codex/ubuntu-26"):
            self.product.git(self.dir, "switch", "-q", "-c", branch)
            self.assertEqual(len(self.notes()), 1, branch)
            self.assertIn(branch, self.notes()[0])
            self.product.git(self.dir, "switch", "-q", "main")
            self.product.git(self.dir, "branch", "-q", "-D", branch)
        for branch in ("fix/a-real-bug", "release/v1.3.0", "docs/the-conventions", "claudette"):
            self.product.git(self.dir, "switch", "-q", "-c", branch)
            self.assertEqual(self.notes(), [], branch)
            self.product.git(self.dir, "switch", "-q", "main")

    def test_a_detached_head_is_not_a_branch_name(self) -> None:
        """CI checks out a merge ref: the name there is GitHub's, not the author's."""
        self.product.git(self.dir, "switch", "-q", "--detach", "HEAD")
        self.assertEqual(self.notes(), [])


class WorkflowReadingTest(unittest.TestCase):
    """What starts a workflow and what it runs, read from the file alone.

    Line-based like the runner reader: the socle carries no YAML parser. The
    three shapes of `on:` GitHub accepts are all in the fleet.
    """

    BLOCK = textwrap.dedent("""\
        name: ci

        on:
          push:
            branches: [main]
          pull_request:
          workflow_dispatch:

        permissions:
          contents: read

        jobs:
          check:
            runs-on: ubuntu-26.04
          package:
            runs-on: macos-15
        """)

    def test_the_three_shapes_of_on(self) -> None:
        self.assertEqual(MODULE.workflow_events(self.BLOCK),
                         ["push", "pull_request", "workflow_dispatch"])
        self.assertEqual(MODULE.workflow_events("on: [push, pull_request]\njobs:\n"),
                         ["push", "pull_request"])
        self.assertEqual(MODULE.workflow_events("on: workflow_dispatch\njobs:\n"),
                         ["workflow_dispatch"])
        self.assertEqual(MODULE.workflow_events("name: x\njobs:\n"), [])

    def test_a_push_says_which_push(self) -> None:
        """push alone, push on a branch and push on a tag are three facts."""
        block = MODULE.top_block(self.BLOCK, "on")
        self.assertEqual(MODULE.block_list(MODULE.sub_block(block, "push"), "branches"), ["main"])
        tagged = self.BLOCK.replace("branches: [main]", "tags:\n      - 'v*'")
        pushed = MODULE.sub_block(MODULE.top_block(tagged, "on"), "push")
        # A tag filter is not label-shaped: a reader that kept only labels
        # would drop the rule that says this workflow releases.
        self.assertEqual(MODULE.block_list(pushed, "tags"), ["v*"])
        self.assertEqual(MODULE.block_list(pushed, "branches"), [])

    def test_jobs_and_runners_of_one_file(self) -> None:
        self.assertEqual(MODULE.JOB_ID.findall(MODULE.top_block(self.BLOCK, "jobs")),
                         ["check", "package"])
        self.assertEqual(MODULE.file_runners("ci.yml", self.BLOCK)[0], ["macos-15", "ubuntu-26.04"])

    def test_the_table_is_turned_for_the_reader(self) -> None:
        """The JSON is per file, because that is where the facts are read; a
        reader asks what happens when, so the renderer turns the table."""
        workflows = [
            {"file": "ci.yml", "events": ["push", "pull_request"], "branches": [], "tags": [],
             "jobs": ["a", "b"], "runners": [], "unresolved": [], "delegates": True},
            {"file": "release.yml", "events": ["push", "workflow_dispatch"], "branches": [],
             "tags": ["v*"], "jobs": ["r"], "runners": [], "unresolved": [], "delegates": False},
        ]
        lines = MODULE.strategy_lines(workflows)
        self.assertEqual([line.split("  ")[0] for line in lines],
                         ["pull request", "push (every branch)", "tag v*", "manual"])
        self.assertIn("2 jobs, one of them the socle's", lines[0])
        self.assertIn("1 job ", lines[2] + " ")
        # An event the socle does not order is still named, never dropped.
        odd = MODULE.strategy_lines([{"file": "x.yml", "events": ["merge_group"], "branches": [],
                                      "tags": [], "jobs": [], "runners": [], "unresolved": [],
                                      "delegates": False}])
        self.assertEqual(len(odd), 1)
        self.assertIn("merge_group", odd[0])


class StrategyNoteTest(unittest.TestCase):
    """A workflow that runs twice on every pull request, named."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = self.product.dir
        self.product.run("adopt", str(self.dir), "--apply")

    def tearDown(self) -> None:
        self.product.close()

    def notes(self) -> list:
        data = self.product.json("check", str(self.dir))["data"]
        self.assertTrue(data["conventions"]["valid"], "a trigger is never a violation")
        return [check["message"] for check in data["checks"] if "runs it twice" in check["message"]]

    def test_push_with_no_branch_beside_pull_request_is_noted(self) -> None:
        self.product.write(".github/workflows/extra.yml",
                           "name: extra\n\non:\n  push:\n  pull_request:\n\njobs:\n  x:\n    runs-on: ubuntu-26.04\n")
        noted = self.notes()
        self.assertEqual(len(noted), 1, noted)
        self.assertIn("extra.yml", noted[0])

    def test_a_filtered_push_and_a_tag_push_are_not(self) -> None:
        self.product.write(".github/workflows/extra.yml",
                           "name: extra\n\non:\n  push:\n    branches: [main]\n  pull_request:\n\n"
                           "jobs:\n  x:\n    runs-on: ubuntu-26.04\n")
        self.assertEqual(self.notes(), [])
        self.product.write(".github/workflows/extra.yml",
                           "name: extra\n\non:\n  push:\n    tags: ['v*']\n  pull_request:\n\n"
                           "jobs:\n  x:\n    runs-on: ubuntu-26.04\n")
        self.assertEqual(self.notes(), [])


class GitHubReadingTest(unittest.TestCase):
    """An answer, a refusal and an absence are three different facts.

    The socle told seventeen private repositories that their default branch
    was unprotected at the very moment GitHub was refusing to say: `gh`
    failed, the reader returned None, and None meant absent.
    """

    class Completed:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def read(self, completed, gh=True):
        host = FakeHost(run=lambda *args, **kwargs: completed, gh=gh)
        # Exercise Host.read itself, with only its process result replaced.
        with using_host(host):
            return MODULE.Host.read(host, "repos/x/y")

    def test_the_four_states(self) -> None:
        self.assertEqual(self.read(self.Completed(0, '{"a": 1}')), ("ok", {"a": 1}))
        self.assertEqual(self.read(self.Completed(1, "", "gh: Not Found (HTTP 404)")), ("absent", None))
        self.assertEqual(self.read(self.Completed(1, "", "gh: Upgrade to GitHub Pro (HTTP 403)")),
                         ("unreadable", None))
        # A failure with no status at all is unreadable, never absent.
        self.assertEqual(self.read(self.Completed(1, "", "dial tcp: timeout")), ("unreadable", None))
        self.assertEqual(self.read(self.Completed(0, "not json")), ("unreadable", None))
        self.assertEqual(self.read(self.Completed(0, "{}"), gh=False), ("no-gh", None))

    def test_github_api_still_answers_a_body_or_nothing(self) -> None:
        """The readers that act the same way on every absence keep their shape;
        a list is not a body for them, because they index it by name."""
        self.assertEqual(self.read(self.Completed(0, '[1, 2]')), ("ok", [1, 2]))
        with using_host(FakeHost({"repos/x/y": ("ok", [1, 2])})):
            self.assertIsNone(MODULE.github_api("repos/x/y"))
            self.assertEqual(MODULE.github_list("repos/x/y"), [1, 2])


    def test_what_protects_a_branch_and_what_cannot_be_read(self) -> None:
        def verdict(classic, ruled, rules):
            return MODULE.branch_protection("o/r", "main", classic, ruled, rules)
        self.assertEqual(verdict("ok", "ok", [])[0], "ok")
        self.assertIn("branch protection", verdict("ok", "ok", [])[1])
        # A ruleset alone protects, and reading only the first endpoint
        # reported agent-cli-spec open — permanently, not only while locked.
        status, message = verdict("absent", "ok", [{"type": "deletion"}])
        self.assertEqual(status, "ok")
        self.assertIn("a ruleset", message)
        self.assertIn("branch protection and a ruleset", verdict("ok", "ok", [{"type": "x"}])[1])
        # Nothing protects it, and both endpoints said so.
        self.assertIn("is not protected", verdict("absent", "ok", [])[1])
        # GitHub refused to say: not the same fact, never reported as one.
        for classic, ruled in (("unreadable", "unreadable"), ("unreadable", "ok"), ("absent", "unreadable")):
            message = verdict(classic, ruled, [])[1]
            self.assertIn("cannot read", message, (classic, ruled))
            self.assertNotIn("is not protected", message, (classic, ruled))

    def test_an_open_branch_under_a_declaration_is_a_promise_unkept(self) -> None:
        """`[commit] signed-on-default-branch` asks the release to check that
        the commit landed on a branch whose rules apply. On an open branch
        there are none, and the check proves what the tag already proved.
        The pattern is `[gate] reviewer` against an environment that
        requires nobody, which preflight already fails on."""
        status, message = MODULE.branch_protection("o/r", "main", "absent", "ok", [],
                                                   "signed-on-default-branch")
        self.assertEqual(status, "fail")
        self.assertIn("declares '[commit] signed-on-default-branch'", message)
        self.assertIn("protect . --apply", message)
        # `signed` promises nothing about the branch, so it stays a note.
        self.assertEqual(MODULE.branch_protection("o/r", "main", "absent", "ok", [], "signed")[0], "note")
        # And a refusal to say is never a violation, whatever was declared:
        # GitHub declining to answer is not an open branch.
        refused = MODULE.branch_protection("o/r", "main", "unreadable", "unreadable", [],
                                           "signed-on-default-branch")
        self.assertEqual(refused[0], "note")
        self.assertIn("cannot read", refused[1])


class RehearseRefusalTest(unittest.TestCase):
    """What rehearse refuses before it starts a container.

    Both cases come from maelys-oci, who hit them while rehearsing a real
    release: a refusal costs seconds, a container that fails halfway costs
    minutes and leaves the cause in a log.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.run("adopt", self.dir, "--apply")

    def tearDown(self) -> None:
        self.product.close()

    def test_a_worktree_is_refused_with_its_cause(self) -> None:
        """A worktree's .git is a file naming a directory the container has
        not got, so git inside the rehearsal reads a path that is not there.

        The refusal comes before the search for docker, so it holds on a
        machine that has none: the first shape of this test passed here and
        failed on the macOS runner, where the absent docker answered first.
        """
        (self.product.dir / ".git").write_text("gdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        error = self.product.json("rehearse", self.dir, "linux-arm64", "--check", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("worktree", error["message"])
        self.assertIn("full clone", error["hint"])


class RehearsalCopyTest(unittest.TestCase):
    """What a rehearsal rehearses: the tree CI would check out.

    Never a tar pipe -- GNU tar 1.35 extracting under an emulated
    linux/amd64 on an Apple Silicon host fails every mkdir with ENOSYS, so
    `rehearse DIR linux-x86_64` was unusable on the machines the fleet
    develops on. And no longer the whole working tree either: `cp -a` dragged
    build/ and everything .gitignore excludes into the container, so the
    object rehearsed was not the object the build job builds. maelys-oci
    reported both, a release apart.
    """

    def test_it_clones_and_carries_the_uncommitted_changes(self) -> None:
        self.assertIn("git clone -q --no-hardlinks /src /work/product", MODULE.REHEARSAL)
        self.assertIn("git -C /src diff --binary HEAD", MODULE.REHEARSAL)
        self.assertIn("git -C /work/product apply", MODULE.REHEARSAL)
        self.assertNotIn("tar -C /src", MODULE.REHEARSAL)

    def test_it_says_what_it_left_behind(self) -> None:
        """CI has no untracked file either, and a rehearsal that silently
        added one would answer about a tree nobody else will build."""
        self.assertIn("ls-files --others --exclude-standard", MODULE.REHEARSAL)
        self.assertIn("left behind, as CI would", MODULE.REHEARSAL)

    def test_a_directory_that_is_not_a_repository_still_rehearses(self) -> None:
        """With a warning saying what it carries, which is everything."""
        self.assertIn("cp -a /src/. /work/product/", MODULE.REHEARSAL)
        self.assertIn("::warning::/src is not a git repository", MODULE.REHEARSAL)
        self.assertIn("rm -rf /work/product/dist", MODULE.REHEARSAL)


class DeclarationHomeTest(unittest.TestCase):
    """The declarations moved, and the move must not need a human.

    packaging/ holds materials everywhere in the fleet — formula templates,
    systemd units, a kernel config — and eighteen repositories have none at
    all while still wanting to declare. The name says the tool and the
    nature; the socle only ever reads the file.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = self.product.dir
        self.product.run("adopt", str(self.dir), "--apply")
        # These tests are about a repository whose declarations are still at
        # the former path, so the current one must not also be there: the
        # fixture carries it, and carrying both is a different violation.
        (self.dir / "maelys-release.conf").unlink(missing_ok=True)
        self.product.git(self.dir, "init", "-q")

    def tearDown(self) -> None:
        self.product.close()

    def commit(self) -> None:
        self.product.git(self.dir, "add", "-A")
        self.product.git(self.dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")

    def test_the_former_home_is_still_read_and_reported(self) -> None:
        """Carrying it is the violation; losing the declarations would be worse."""
        self.product.write("packaging/release", "[targets]\nlinux-arm64\n" + APART)
        data = self.product.json("check", str(self.dir), expect=2)["data"]
        self.assertFalse(data["conventions"]["valid"])
        self.assertTrue(any("moved to maelys-release.conf" in message
                            for message in data["conventions"]["violations"]))
        # …and the targets still arrive, so nothing else cascades.
        self.assertEqual(self.product.json("declarations", str(self.dir))["data"]["declared"]["targets"],
                         ["linux-arm64"])

    def test_adopt_moves_it_and_git_follows(self) -> None:
        """A status that blocked adopt would put the remedy out of reach."""
        self.product.write("packaging/release", "[targets]\nlinux-arm64\n" + APART)
        self.commit()
        data = self.product.json("adopt", str(self.dir), "--apply")["data"]
        self.assertEqual(data["moved"], "packaging/release")
        self.assertTrue((self.dir / "maelys-release.conf").is_file())
        self.assertFalse((self.dir / "packaging" / "release").exists())
        # git mv, not a delete and a write: the comments a product puts beside
        # each section are its reasoning, and the history must still find them.
        self.assertIn("R  packaging/release -> maelys-release.conf",
                      self.product.git(self.dir, "status", "--porcelain"))
        self.commit()
        self.assertIn("fixture", self.product.git(self.dir, "log", "--follow", "--oneline",
                                                  "--", "maelys-release.conf"))
        self.assertEqual(self.product.json("check", str(self.dir))["data"]["conventions"]["valid"], True)

    def test_carrying_both_is_refused_because_the_socle_cannot_choose(self) -> None:
        self.product.write("packaging/release", "[targets]\nlinux-arm64\n" + APART)
        self.product.write("maelys-release.conf", "[targets]\nmacos-arm64\n" + APART)
        data = self.product.json("check", str(self.dir), expect=2)["data"]
        self.assertTrue(any("carries both" in message for message in data["conventions"]["violations"]))
        # The new home is the one read, so the verdict is not ambiguous either.
        self.assertEqual(self.product.json("declarations", str(self.dir), expect=2)["data"]["declared"]["targets"],
                         ["macos-arm64"])

    def test_a_section_a_socle_does_not_know_costs_nothing_else(self) -> None:
        """A product pins the socle by commit: on an older socle, a newer
        section must not take its targets down with it."""
        self.product.write("maelys-release.conf", "[targets]\nlinux-arm64\n\n[future]\nsomething\n" + APART)
        data = self.product.json("check", str(self.dir), expect=2)["data"]
        self.assertTrue(any("[future]" in message for message in data["conventions"]["violations"]))
        self.assertEqual(self.product.json("declarations", str(self.dir), expect=2)["data"]["declared"]["targets"],
                         ["linux-arm64"])


CI_CALL = """name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@%s # v9.9.9
    with:
      product: maelys-fixture
      dependency_checkout: |
        sh scripts/checkout-dependency.sh maelys-json

  mine:
    runs-on: ubuntu-26.04
    steps:
      - run: make check
""" % ("0" * 40)


FORMULA = """class LibmaelysJson < Formula
  desc "A library"
  homepage "https://github.com/maelys-dev/%s"
  url "https://github.com/maelys-dev/%s/archive/refs/tags/%s.tar.gz"
  sha256 "%s"
end
"""


class RehearsalEnvironmentTest(unittest.TestCase):
    """What the rehearsal hands the product's publish script."""

    def test_the_rehearsal_offers_a_dry_run_the_job_never_does(self) -> None:
        """The contract hides the publishing half, so the rehearsal opens it.

        Exit 0 on an already-published version is what channel.yml requires,
        and a script honours it by returning early -- so the rehearsal alone
        proves the path that does not publish. CHANNEL_DRY_RUN lets a script
        take its real path under the registry's own dry run. channel.yml must
        never set it, or a release would announce a publication that did not
        happen.
        """
        source = (ROOT / "bin" / "maelys_socle" / "rehearse.py").read_text(encoding="utf-8")
        self.assertIn('"CHANNEL_DRY_RUN": "1"', source)
        channel = (ROOT / ".github" / "workflows" / "channel.yml").read_text(encoding="utf-8")
        self.assertNotIn("CHANNEL_DRY_RUN", channel)

    def test_a_failed_channel_is_said_on_the_release_page(self) -> None:
        """The absent marker is the record for a machine, not for a person."""
        channel = (ROOT / ".github" / "workflows" / "channel.yml").read_text(encoding="utf-8")
        self.assertIn("needs.publish.result != 'success'", channel)
        self.assertIn("gh release edit", channel)
        # Replayed on the same tag, it must not say it twice.
        self.assertIn("already says", channel)


class WhatTheScriptRecordedTest(unittest.TestCase):
    """Two conditions that were never true, found by the things that read them."""

    def test_the_marker_keeps_what_the_script_recorded(self) -> None:
        """hashFiles only sees GITHUB_WORKSPACE, and runner.temp is outside it.

        The condition was empty for every product since 0.32.0: the step
        never ran, the marker was composed without a single recorded field,
        and `rehearse --channel` reported a disagreement on each of them --
        which is how it was found, by the one thing that compares the two.
        """
        channel = (ROOT / ".github" / "workflows" / "channel.yml").read_text(encoding="utf-8")
        # runner.temp stays: it is where the script writes and where the
        # artifact is read from. What had to go is hashFiles reaching for it.
        self.assertNotIn("hashFiles(", channel,
                         "hashFiles only sees GITHUB_WORKSPACE; the shell decides")
        self.assertIn('if [ -s "$CHANNEL_RECORD" ]; then echo "recorded=true"', channel)
        self.assertIn("steps.publish.outputs.recorded == 'true'", channel)

    def test_protect_writes_what_it_reported(self) -> None:
        """It wrote the observed intersection and reported the computed legs.

        Right after an adoption that renames a leg, --apply required the old
        name still present in older pull requests: the lock 0.43.0 was
        written to prevent, in the command written to prevent it.
        """
        source = (ROOT / "bin" / "maelys_socle" / "protect.py").read_text(encoding="utf-8")
        body = source.split("PROTECTION_SHAPE, \"required_status_checks\"", 1)[1].split("}", 1)[0]
        self.assertIn("proposed", body)
        self.assertNotIn('"contexts": seen', source)


class SaidWhereItIsReadTest(unittest.TestCase):
    """Four things the socle knew and did not say where anyone looks."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.addCleanup(self.product.close)
        self.product.run("adopt", self.dir, "--apply")

    def test_the_tap_says_on_the_release_that_it_pushed_nothing(self) -> None:
        """Two green publish jobs, nothing pushed, and the only trace an
        environment variable in a log. It cost a product a full replay on
        four macOS runners."""
        tap = (ROOT / ".github" / "workflows" / "tap.yml").read_text(encoding="utf-8")
        self.assertIn("::warning::no tap credentials", tap)
        self.assertIn("NOT pushed", tap)
        self.assertIn("gh release edit", tap)
        self.assertIn("already says the tap was not updated", tap, "a replay must not say it twice")
        # Attempted, never required: outside Actions there is no release.
        self.assertIn('repository="${GITHUB_REPOSITORY:-}"', tap)

    def test_verify_command_says_that_cut_replays_it_on_the_branch(self) -> None:
        """A script that asks git for the tag at HEAD sees a previous release
        and fails at the first cut. The rule was written 440 lines away, at
        cut's description."""
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        head = release.split("verify_command:", 1)[1].split("required:", 1)[0]
        self.assertIn("cut", head)
        self.assertIn("default branch", head)
        self.assertIn("does not exist yet", head)

    def test_commit_verification_is_reachable_by_a_product(self) -> None:
        """It was offered by the workflow, described by the conventions as
        what every product had chosen, and rendered by nothing: `none` was
        the only value anyone could reach."""
        self.product.write("maelys-release.conf",
                           "[dependencies]\napart\n\n[commit]\nsigned-on-default-branch\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn("      commit_verification: signed-on-default-branch",
                      self.product.read(".github/workflows/release.yml"))
        data = self.product.json("declarations", self.dir)["data"]
        self.assertEqual(data["declared"]["commitVerification"], "signed-on-default-branch")

    def test_the_commit_section_takes_what_the_workflow_accepts(self) -> None:
        for text, expected in (("[commit]\nnone\n", "signed or signed-on-default-branch"),
                               ("[commit]\nsigned\nsigned\n", "holds one line")):
            with self.assertRaises(ValueError) as refusal:
                MODULE.parse_release(text)
            self.assertIn(expected, str(refusal.exception))

    def test_a_custom_render_is_told_it_runs_on_another_platform(self) -> None:
        """The tap renders on macOS and the release was built on Linux: a
        command that rebuilds the archive hashes what that runner produced,
        and `git archive | gzip` is not byte for byte the same across
        platforms. A product paid it by hand on a published formula."""
        self.product.write("scripts/render-homebrew-formula.sh", "#!/bin/sh\nexit 0\n", executable=True)
        # Declaring it changes what adopt renders, so re-adopt before reading.
        self.product.run("adopt", self.dir, "--apply")
        checks = self.product.json("check", self.dir)["data"]["checks"]
        self.assertTrue(any(check["status"] == "note" and "renders on macOS" in check["message"]
                            for check in checks), checks)
        self.assertTrue(any(check["status"] == "note" and MODULE.PUBLISHED_DOWNLOAD[0] in check["message"]
                            for check in checks), checks)
        tap = (ROOT / ".github" / "workflows" / "tap.yml").read_text(encoding="utf-8")
        self.assertIn("Hash the published archive", tap)

    def test_a_render_that_hashes_a_release_asset_is_recognised_too(self) -> None:
        """The second shape, and the one the first list missed.

        maelys-http publishes its own tarball as a release asset and hashes
        that, so its script names releases/download and not the tag's source
        archive. Two of three recognised is not a recogniser: it carried the
        note saying the opposite of what its script does.
        """
        self.product.write("scripts/render-homebrew-formula.sh",
                           "#!/bin/sh\nurl=\"https://github.com/$repository/releases/download/"
                           "$tag/p-$version.tar.gz\"\ncurl -fsSL -o \"$work/a.tar.gz\" \"$url\"\n",
                           executable=True)
        self.product.run("adopt", self.dir, "--apply")
        checks = self.product.json("check", self.dir)["data"]["checks"]
        self.assertFalse([check for check in checks if "cannot tell from here" in check["message"]], checks)
        self.assertTrue(any(check["status"] == "ok" and "releases/download" in check["message"]
                            for check in checks), checks)

    def test_the_note_says_what_it_does_not_know_and_never_asserts(self) -> None:
        """The wording, which is the finding and not the rule.

        The note used to assert a fact about the product it had read --
        "renders on macOS while the release was built on Linux" -- and every
        product it ever reached downloads before hashing. It was relayed to
        one of them as a finding worth more than a note, and that session
        corrected the relay. A check that cannot know must not assert.
        """
        self.product.write("scripts/render-homebrew-formula.sh",
                           "#!/bin/sh\nmake dist && shasum -a 256 dist/*.tar.gz\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        checks = self.product.json("check", self.dir)["data"]["checks"]
        note = [check for check in checks if check["status"] == "note"
                and "render-homebrew-formula.sh" in check["message"]]
        self.assertEqual(len(note), 1, checks)
        self.assertTrue(note[0]["message"].startswith("the socle cannot tell from here"), note[0])
        for shape in MODULE.PUBLISHED_DOWNLOAD:
            self.assertIn(shape, note[0]["message"])

    def test_a_render_that_downloads_the_tag_archive_is_told_it_is_right(self) -> None:
        """The advice was given without reading a line of the script.

        maelys-egress downloads the tag's own archive and hashes that --
        exactly what the note recommends -- and went looking for what it had
        done wrong. The evidence can only silence the note, never raise a
        violation: its absence means the socle could not tell, which is not
        a defect.
        """
        self.product.write("scripts/render-homebrew-formula.sh",
                           "#!/bin/sh\nurl=\"https://github.com/$repository/archive/refs/tags/$tag.tar.gz\"\n"
                           "curl -fsSL -o \"$work/source.tar.gz\" \"$url\"\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        checks = self.product.json("check", self.dir)["data"]["checks"]
        self.assertFalse([check for check in checks if "cannot tell from here" in check["message"]], checks)
        self.assertTrue(any(check["status"] == "ok" and "hashes bytes it downloaded" in check["message"]
                            for check in checks), checks)

    def test_the_url_the_socle_looks_for_is_the_one_its_own_tap_downloads(self) -> None:
        """A recogniser that named something else would send every product
        to rewrite a script that was right."""
        tap = (ROOT / ".github" / "workflows" / "tap.yml").read_text(encoding="utf-8")
        self.assertIn(MODULE.PUBLISHED_DOWNLOAD[0], tap)


class VanishingContextTest(unittest.TestCase):
    """The lock the socle is the only thing that can see coming.

    A branch requires a check by name, the socle generates the workflow whose
    jobs carry those names, and a leg renamed by an adoption leaves the
    branch waiting for a report that will never come -- the adoption's own
    pull request first. 0.41.0 came within one trial of doing it to ten
    repositories.
    """

    def read(self, required: list, produced: list, caller: str = "check"):
        host = protection_host(("ok", {"required_status_checks": {"contexts": required}}))
        with workflow_project(caller, fuzz=f"{caller} / fuzz" in produced) as project, using_host(host):
            return MODULE.vanishing_contexts(project)

    def test_a_required_leg_this_socle_no_longer_produces(self) -> None:
        # A name no alias covers: the 0.54.0 names are aliases since 0.57.0,
        # so the guard is exercised on one this socle never kept.
        self.assertEqual(self.read(["check / check (ubuntu-24.04)", "check / fuzz"],
                                   ["check / check (linux)", "check / fuzz"]),
                         ["check / check (ubuntu-24.04)"])

    def test_what_the_product_requires_of_its_own_jobs_is_not_ours(self) -> None:
        """The socle has no idea what produces `mbedtls (macos-15)`."""
        self.assertEqual(self.read(["mbedtls (macos-15)", "check / fuzz"], ["check / fuzz"]), [])

    def test_nothing_vanishes_when_the_names_agree(self) -> None:
        self.assertEqual(self.read(["check / check (macos-15)"], ["check / check (macos-15)"]), [])

    def test_a_ruleset_protects_as_much_as_the_classic_endpoint(self) -> None:
        rules = [{"type": "deletion"}, {"type": "required_status_checks",
                  "parameters": {"required_status_checks": [{"context": "check / check (ubuntu-24.04)"}]}}]
        with workflow_project() as project, using_host(protection_host(("absent", None), rules=("ok", rules))):
            self.assertEqual(MODULE.vanishing_contexts(project), ["check / check (ubuntu-24.04)"])

    def test_a_refusal_to_answer_stops_the_check_and_not_the_adoption(self) -> None:
        """An adoption must not depend on the network to be possible."""
        with workflow_project() as project, using_host(protection_host(
                ("unreadable", None), rules=("unreadable", None))):
            self.assertEqual(MODULE.vanishing_contexts(project), [])


class LinuxRunnersTest(unittest.TestCase):
    """The two legs a private repository could not point anywhere.

    `macos_runner` arrived in 0.41.0 and the two Linux legs stayed written
    into the matrix, so a repository whose maintainer forbids hosted runners
    could not adopt the shared CI at all: half its matrix, and all of its
    fuzz and sanitizers jobs, would have stayed hosted.
    """

    WORKFLOW = (ROOT / ".github" / "workflows" / "check-product.yml").read_text(encoding="utf-8")

    def test_the_workflow_takes_one_input_per_leg(self) -> None:
        for name in ("linux_x86_64_runner", "linux_arm64_runner", "macos_runner"):
            self.assertIn(f"      {name}:", self.WORKFLOW)
        for leg, name, default in (("linux", "linux_x86_64_runner", '"ubuntu-26.04"'),
                                   ("linux-arm64", "linux_arm64_runner", '"ubuntu-26.04-arm"'),
                                   ("macos", "macos_runner", '"macos-15"')):
            self.assertIn(f"          - leg: {leg}\n            runner: ${{{{ github.event.repository.private"
                          f" && inputs.{name} || '{default}' }}}}", self.WORKFLOW)

    def test_the_legs_are_names_and_not_machines(self) -> None:
        """Renamed once, in 0.54.0, announced a version early: a leg named
        after an image turned every image upgrade into a fleet-wide lock.
        The image now lives beside the leg, in its runner, where it can
        change without anyone's protection noticing."""
        self.assertIn("name: check (${{ matrix.leg }})", self.WORKFLOW)
        self.assertEqual(MODULE.check_product_legs(), ["linux", "linux-arm64", "macos"])
        for leg in MODULE.check_product_legs():
            # An architecture is a name (arm64); an image release is a
            # version (26.04, macos-15). Only the second turns an upgrade
            # into a lock.
            self.assertNotRegex(leg, r"[0-9]+\.[0-9]+|ubuntu|macos-[0-9]", f"{leg} carries an image's version")

    def test_fuzz_and_sanitizers_follow_the_x86_64_declaration(self) -> None:
        """Left hosted, they are the reason a repository that forbids hosted
        runners still could not adopt."""
        self.assertEqual(self.WORKFLOW.count(
            "runs-on: ${{ fromJSON(github.event.repository.private && inputs.linux_x86_64_runner"
            " || '\"ubuntu-26.04\"') }}"), 2)  # fuzz and sanitizers; the aliases left in 0.60.0
        self.assertNotIn("\n    runs-on: ubuntu-26.04\n", self.WORKFLOW)

    def test_adopt_writes_one_line_per_declared_leg(self) -> None:
        written = MODULE.ci_macos_runner(CI_CALL, {"macos": ["m1"], "linux-x86_64": ["self-hosted", "Linux"]})
        self.assertIn("      macos_runner: '\"m1\"'", written)
        self.assertIn("      linux_x86_64_runner: '[\"self-hosted\", \"Linux\"]'", written)
        self.assertNotIn("linux_arm64_runner", written)
        # And an emptied declaration takes every line away again.
        self.assertNotIn("_runner:", MODULE.ci_macos_runner(written, {}))


class ImpactLinesTest(unittest.TestCase):
    """What a product must do, for the versions it has not taken.

    A product reads a changelog to answer one question -- must I open a pull
    request? -- and one of them read twenty-one entries by hand to find out.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.addCleanup(self.product.close)
        self.product.run("adopt", self.dir, "--apply")
        # A product left behind: its release.yml names an older socle.
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("# v9.9.9", "# v0.49.0"), encoding="utf-8")

    def test_apply_says_it_too_and_not_the_plan_alone(self) -> None:
        """The pin has to be read before anything is written.

        `plan(..., apply)` rewrites release.yml, so reading the pin after it
        gave the socle this very run installs: `impact` was empty in
        --apply, which is the only mode the product that asked for this
        uses. It shipped that way in 0.50.0.
        """
        data = self.product.json("adopt", self.dir, "--apply")["data"]
        self.assertEqual(data["impactFrom"], "v0.49.0")
        versions = [entry["version"] for entry in data["impact"]]
        self.assertTrue(versions, data)
        self.assertNotIn("0.49.0", versions, "the version a product already has says nothing to it")
        self.assertEqual(versions, sorted(versions, key=MODULE.version_tuple), "oldest first")
        for entry in data["impact"]:
            self.assertTrue(entry["says"].strip(), entry)

    def test_each_line_says_whether_it_asks_this_product_anything(self) -> None:
        """maelys-cli: "adopt or nothing" in the JSON, so that current in the
        socle's sense need not mean on its last version."""
        data = self.product.json("adopt", self.dir)["data"]
        by_version = {entry["version"]: entry for entry in data["impact"]}
        self.assertEqual(by_version["0.49.1"]["asks"], ["nothing"])
        self.assertIs(by_version["0.49.1"]["asksThis"], False)
        self.assertNotIn("[asks:", by_version["0.49.1"]["says"], "the marker is data, not prose")
        # old-legs asks GitHub, and this fixture has no origin: unknown, not no.
        self.assertIsNone(by_version["0.57.0"]["asksThis"])
        self.assertIsNone(data["current"])
        text = self.product.run("adopt", self.dir).stdout
        self.assertIn("0.49.1   -    Nobody", text)
        self.assertIn("0.57.0   ?    ", text)

    def test_a_superseded_line_asks_nothing_and_its_instruction_is_not_printed(self) -> None:
        """maelys-datalog, adopting 0.57.0 from 0.51.1, was told 0.54.0 asked a
        gesture, whose bold instruction is to narrow the protection."""
        data = self.product.json("adopt", self.dir)["data"]
        by_version = {entry["version"]: entry for entry in data["impact"]}
        self.assertEqual(by_version["0.54.0"]["supersededBy"], "0.57.0")
        self.assertIs(by_version["0.54.0"]["asksThis"], False)
        text = self.product.run("adopt", self.dir).stdout
        self.assertIn("0.54.0   -    superseded by 0.57.0, below: read that line", text)
        self.assertNotIn("--without-legs --apply`; adopt", text)

    def test_a_selector_that_holds_asks_and_an_unknown_one_does_not_hide_it(self) -> None:
        decl = MODULE.Declarations(pathlib.Path("/nonexistent"), "p")
        decl.channels = [("npm", "github-packages")]
        self.assertIs(MODULE.asks_this(decl, ["channels"], {}), True)
        self.assertIs(MODULE.asks_this(decl, ["nothing"], {}), False)
        self.assertIs(MODULE.asks_this(decl, ["pins", "formulas"], {}), False)
        self.assertIsNone(MODULE.asks_this(decl, None, {}), "a line without the marker is unknown")
        unknown = {"selectors": {**MODULE.impact_selectors(decl), "old-legs": lambda: None}}
        self.assertIsNone(MODULE.asks_this(decl, ["old-legs"], dict(unknown)))
        self.assertIs(MODULE.asks_this(decl, ["old-legs", "channels"], dict(unknown)), True)

    def test_every_marker_names_selectors_the_socle_evaluates(self) -> None:
        known = set(MODULE.impact_selectors(MODULE.Declarations(pathlib.Path("/nonexistent"), "p")))
        entries = MODULE.socle_impact_entries("v0.47.0")
        self.assertTrue(entries)
        for entry in entries:
            self.assertIsNotNone(entry["asks"], f"{entry['version']} carries no [asks: ...] marker")
            self.assertTrue(set(entry["asks"]) <= known, (entry["version"], entry["asks"]))

    def test_a_pin_nothing_since_asks_about_is_a_note_not_a_failure(self) -> None:
        """maelys-system: check read FAIL on a pin every line since said asked
        nothing of it. `current` is the socle's own answer; the verdict uses it."""
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        text = workflow.read_text()
        # Pinned at this socle's own version under another commit: no line since
        # asks anything, whatever the changelog grows to.
        version = re.search(r"^## ([0-9.]+) ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE).group(1)
        workflow.write_text(text.replace("release.yml@" + "f" * 40 + " # v0.49.0",
                                         "release.yml@" + "0" * 40 + f" # v{version}"), encoding="utf-8")
        data = self.product.json("check", self.dir, expect=0)["data"]
        self.assertFalse([violation for violation in data["violations"] if "pins maelys-release" in violation])
        self.assertTrue([check for check in data["checks"] if "current in the socle's sense" in check["message"]])
        # A pin whose lines cannot all be evaluated keeps the failure.
        workflow.write_text(text.replace("release.yml@" + "f" * 40 + " # v0.49.0",
                                         "release.yml@" + "0" * 40 + " # v0.49.0"), encoding="utf-8")
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertTrue([violation for violation in data["violations"] if "pins maelys-release" in violation])

    def test_a_changelog_title_present_twice_is_refused(self) -> None:
        """maelys-cli carried five `## Unreleased`, one per pull request."""
        changelog = self.product.dir / "CHANGELOG.md"
        changelog.write_text(changelog.read_text() + "\n## Unreleased\n\n- a\n\n## Unreleased\n\n- b\n",
                             encoding="utf-8")
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertTrue([v for v in data["violations"] if "entries titled ## Unreleased" in v], data["violations"])

    def test_an_empty_list_says_which_kind_of_empty(self) -> None:
        """Nothing to do, no pin, no tag on the pin and no changelog were
        the same answer."""
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("# v0.49.0", "# untagged"), encoding="utf-8")
        text = self.product.run("adopt", self.dir).stdout
        self.assertIn("no pinned socle version to read from", text)


class TagDeploymentsTest(unittest.TestCase):
    """What the run of a tag is waiting on, said where the tag was pushed.

    A release under `[gate] reviewer` asks for one approval and a product
    with a channel asks for another, which does not exist until the release
    workflow has finished. Measured on one release: the release job ran at
    10:05 and the channel job at 14:04, four hours apart on the same tag.
    It read as an approval GitHub had lost.
    """

    def read(self, runs, deployments):
        def answer(path):
            if "actions/runs?" in path:
                return "ok", {"total_count": len(runs), "workflow_runs": runs}
            return "ok", deployments.get(path, [])
        with using_host(FakeHost(read=answer)):
            return MODULE.tag_deployments("o/r", "v1.0.0")

    def test_the_runs_endpoint_is_read_as_an_object(self) -> None:
        """An object for runs and an array for deployments must reach the result.

        This fails if actions/runs is accidentally read through github_list:
        the real readers run, instead of mocks asserting which was called.
        """
        waiting = self.read([{"id": 7, "head_branch": "v1.0.0"}],
                            {"repos/o/r/actions/runs/7/pending_deployments":
                             [{"environment": {"name": "release", "id": 42}, "current_user_can_approve": True}]})
        self.assertEqual([item["environment"] for item in waiting], ["release"])

    def test_it_names_the_environment_and_hands_over_the_command(self) -> None:
        waiting = self.read(
            [{"id": 7, "head_branch": "v1.0.0"}],
            {"repos/o/r/actions/runs/7/pending_deployments":
                [{"environment": {"name": "release", "id": 42}, "current_user_can_approve": True}]})
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["environment"], "release")
        self.assertIn("actions/runs/7", waiting[0]["run"])
        self.assertIn("-f state=approved", waiting[0]["approve"])
        self.assertIn("environment_ids[]=42", waiting[0]["approve"])

    def test_a_run_of_another_reference_is_not_this_tag(self) -> None:
        self.assertEqual(self.read(
            [{"id": 8, "head_branch": "main"}],
            {"repos/o/r/actions/runs/8/pending_deployments":
                [{"environment": {"name": "release", "id": 1}, "current_user_can_approve": True}]}), [])

    def test_an_approval_this_operator_cannot_give_is_not_offered(self) -> None:
        """Handing over a command that will be refused is worse than silence."""
        self.assertEqual(self.read(
            [{"id": 7, "head_branch": "v1.0.0"}],
            {"repos/o/r/actions/runs/7/pending_deployments":
                [{"environment": {"name": "release", "id": 1}, "current_user_can_approve": False}]}), [])

    def test_the_reader_runs_whatever_the_product_declares(self) -> None:
        """The guard was "[gate] reviewer or a channel", and this repository
        declares neither -- so the socle cutting its own releases never ran
        this line, which is how a reader that could not work shipped and was
        tagged. Nothing exercises `cut --tag` but a real release; the socle
        makes one most days, and now it exercises this."""
        body = inspect.getsource(MODULE.cut_tag)
        self.assertIn("data[\"deployments\"] = tag_deployments(repository, tag)", body)
        self.assertNotIn('if decl.gate == "reviewer" or decl.channels:', body)

    def test_cut_says_which_socle_is_doing_the_cutting(self) -> None:
        """`cut` is the one command that does not relocate to the socle a
        product pins: it runs on an operator's machine and never in a
        workflow, so the pin governs what GitHub runs and nothing else. A
        product asked whether a fix to `cut` could only reach it through an
        adoption. It cannot."""
        for handler in (MODULE.handle_cut, MODULE.cut.handle_cut, MODULE.cut_tag, MODULE.cut_open):
            body = inspect.getsource(handler)
            self.assertNotIn("run_pinned_socle", body, handler)
        text = MODULE.text_cut({"stage": "open", "product": "p", "version": "1.0.0", "branch": "b",
                                "base": "main", "repository": "o/r", "gate": [], "checks": [],
                                "ready": False, "socle": {"tag": "v9.9.9", "sha": "f" * 40}})
        self.assertIn("socle    v9.9.9 (fffffff), this checkout", text)

    def test_none_pending_is_not_nothing_left_to_approve(self) -> None:
        """The channel's deployment does not exist yet when the tag is pushed."""
        text = MODULE.text_cut({"stage": "tag", "product": "p", "version": "1.0.0", "branch": "b",
                                "base": "main", "repository": "o/r", "gate": [], "checks": [],
                                "ready": True, "pushed": True, "deployments": [],
                                "next": "v1.0.0 is published on abc1234"})
        self.assertIn("a channel asks for its own approval", text)
        self.assertIn("look again then", text)


class ChannelGateTest(unittest.TestCase):
    """Whether a channel asks for an approval of its own, said in the file.

    Every channel ran under the release's environment, which is a second
    approval — and one that becomes pending only once the release workflow
    has finished. Measured on one product: the release job at 10:05, the
    channel job at 14:04, four hours later on the same tag. It read as an
    approval GitHub had lost, and no product had ever chosen it.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.addCleanup(self.product.close)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)

    def workflow(self, line: str) -> str:
        self.product.write("maelys-release.conf", "[dependencies]\napart\n\n[channels]\n" + line + "\n")
        self.product.run("adopt", self.dir, "--apply")
        return self.product.read(".github/workflows/release.yml")

    def test_a_channel_with_no_gate_carries_an_empty_environment(self) -> None:
        """An empty environment runs the job with no environment and no
        approval: measured on run 34817132441 of the socle's own repository,
        where a job whose environment expression was empty started at once,
        left no pending deployment and created no environment. channel.yml
        runs for one product in the fleet, so this was worth a probe."""
        written = self.workflow("npm github-packages none")
        self.assertIn("      release_environment: ''", written)

    def test_the_default_is_what_it_has_always_been(self) -> None:
        """Absent, the channel runs under the release's environment: the
        behaviour before anything could be declared, and the one a product
        that says nothing keeps."""
        self.assertNotIn("release_environment", self.workflow("npm github-packages"))
        self.assertNotIn("release_environment", self.workflow("npm github-packages reviewer"))

    def test_saying_it_out_loud_turns_the_note_into_an_ok(self) -> None:
        self.workflow("npm github-packages")
        said = [check for check in self.product.json("check", self.dir)["data"]["checks"]
                if "[channels] npm" in check["message"]]
        self.assertEqual([check["status"] for check in said], ["note"], said)
        self.assertIn("asks for its own approval", said[0]["message"])
        self.workflow("npm github-packages none")
        said = [check for check in self.product.json("check", self.dir)["data"]["checks"]
                if "[channels] npm" in check["message"]]
        self.assertEqual([check["status"] for check in said], ["ok"], said)
        self.assertIn("no approval of its own", said[0]["message"])
        # Both declared answers, not one: this test held `none` alone, and a
        # declared reviewer kept the note telling it to declare reviewer.
        self.workflow("npm github-packages reviewer")
        said = [check for check in self.product.json("check", self.dir)["data"]["checks"]
                if "[channels] npm" in check["message"]]
        self.assertEqual([check["status"] for check in said], ["ok"], said)
        self.assertIn("as declared", said[0]["message"])

    def test_the_gate_is_one_of_the_two_the_socle_knows(self) -> None:
        for text, expected in (("[channels]\nnpm github-packages sometimes\n", "reviewer or none"),
                               ("[channels]\nnpm\n", "names one registry, and may add its gate")):
            with self.assertRaises(ValueError) as refusal:
                MODULE.parse_release(text)
            self.assertIn(expected, str(refusal.exception))


class OperatorIdentityTest(unittest.TestCase):
    """A commit the socle makes carries the operator's identity and signature.

    Three sites forced a machine identity -- `migrate` twice, `tap --apply`
    once -- and `migrate` also passed `commit.gpgsign=false`, switching off a
    signature the operator had asked for. On a default branch that requires
    signed commits, the branch a migration opened could not merge; and a tap
    commit went out signed under an address no account stands behind, so
    GitHub never verified it. The first version of this fix found two of the
    three sites; this test is what holds the third.
    """

    TREES = [ast.parse(path.read_text(encoding="utf-8"))
             for path in [CLI, *sorted((ROOT / "bin" / "maelys_socle").glob("*.py"))]]

    def test_no_commit_switches_a_signature_off(self) -> None:
        self.assertNotIn("commit.gpgsign=false", [node.value for tree in self.TREES
                         for node in ast.walk(tree) if isinstance(node, ast.Constant)])

    def test_a_machine_identity_is_written_in_one_place_only(self) -> None:
        """The floor inside `author_identity`, for a runner with no user, and
        nowhere else."""
        helpers = [node for tree in self.TREES for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "author_identity"]
        self.assertEqual(len(helpers), 1)
        writes = [node for tree in self.TREES for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "git"
                  and len(node.args) >= 3 and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value == "config" and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value in ("user.name", "user.email")]
        self.assertEqual(len(writes), 2, [ast.dump(node) for node in writes])
        self.assertEqual(sum(node in set(ast.walk(helpers[0])) for node in writes), 2)

    def test_every_site_goes_through_the_helper(self) -> None:
        calls = [node for tree in self.TREES for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "id", getattr(node.func, "attr", "")) == "author_identity"]
        self.assertEqual(sorted(ast.dump(node.args[0]) for node in calls),
                         sorted(ast.dump(ast.Name(where, ast.Load()))
                                for where in ("documents", "product_clone", "tap")))


class SocleAsADependencyTest(unittest.TestCase):
    """The socle was the last neighbour a build still read beside the product.

    Its commit is not a pin — it is the one on the `uses:` line — but it was
    read like one: two products keep a `MAELYS_RELEASE_DIR ?= ../maelys-release`
    whose target moves as much as any working copy, and their conformance
    target skipped in silence when the neighbour was absent. I refused this
    once, arguing that the variable would exist on a laptop and not in a job.
    That was wrong: `check-product.yml` has cloned the socle at the pinned
    commit into `$RUNNER_TEMP` since 0.41.0, so giving the same root to a
    machine removes an asymmetry instead of creating one.
    """

    SCRIPT = (ROOT / "share" / "templates" / "checkout-dependencies.sh").read_text(encoding="utf-8")

    def test_the_script_reads_the_uses_line_and_not_a_pin(self) -> None:
        self.assertIn("maelys-release/.github/workflows/release", self.SCRIPT)
        self.assertIn("check-product", self.SCRIPT)
        self.assertIn("MAELYS_RELEASE_DIR=$destination/maelys-release", self.SCRIPT)

    def test_it_is_attempted_and_never_required(self) -> None:
        """A machine that cannot reach the socle must still build."""
        self.assertIn("MAELYS_RELEASE_DIR is not set", self.SCRIPT)
        self.assertIn('rm -rf -- "$destination/maelys-release"', self.SCRIPT)

    def test_the_rehearsal_exports_what_ci_exports(self) -> None:
        """The third place, and the one that would have been forgotten."""
        self.assertIn("export MAELYS_RELEASE_DIR", MODULE.REHEARSAL)

    def test_a_pin_for_the_socle_is_refused(self) -> None:
        """One commit, one source: the `uses:` line."""
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        product.write("dependencies/maelys-release.pin", "v0.1.0\n" + "a" * 40 + "\n")
        data = product.json("check", str(product.dir), expect=2)["data"]
        self.assertTrue(any("the socle is pinned by the 'uses:' line" in violation
                            for violation in data["conventions"]["violations"]), data["conventions"])

    def test_the_changelog_hint_is_spelled_as_the_changelog_is(self) -> None:
        """maelys-cli writes `## 0.5.29 - 2026-09-14`; the hint showed an em dash."""
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-heading."))
        self.addCleanup(shutil.rmtree, work, True)
        self.assertEqual(MODULE.entry_heading(work, "1.0.0"), "## 1.0.0 — YYYY-MM-DD")
        (work / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 0.5.29 - 2026-09-14\n\n- x\n")
        self.assertEqual(MODULE.entry_heading(work, "0.5.30"), "## 0.5.30 - YYYY-MM-DD")

    def test_a_pin_for_the_socle_is_the_one_source_where_no_workflow_names_it(self) -> None:
        """maelys-platform: no workflow calls the socle, and the pin chooses the
        socle its program runs. Deleting it would delete the only source."""
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-consumer."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / ".github" / "workflows" / "check.yml").write_text("jobs:\n  test:\n    runs-on: ubuntu-26.04\n")
        (work / "dependencies").mkdir()
        (work / "dependencies" / "maelys-release.pin").write_text("v0.56.0\n" + "a" * 40 + "\n")
        decl = MODULE.read_declarations(work, "maelys-platform", "custom")
        self.assertFalse([check for check in decl.checks if "the socle is pinned by the 'uses:' line" in check["message"]])
        self.assertIn("maelys-release", decl.dependencies)

    def test_the_search_learns_the_name_under_its_own_root(self) -> None:
        """And keeps the two roots apart: a Makefile that reads
        $MAELYS_DEPENDENCIES_DIR has migrated its pins and may still take the
        socle from next door, which is exactly where two products stand."""
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        product.write("Makefile", "MAELYS_DEPENDENCIES_DIR ?=\n"
                                  "SYSTEM_DIR ?= $(MAELYS_DEPENDENCIES_DIR)/maelys-system\n"
                                  "MAELYS_RELEASE_DIR ?= ../maelys-release\n")
        product.git(product.dir, "init", "-q")
        product.git(product.dir, "add", "-A")
        notes = [check["message"] for check in product.json("check", str(product.dir))["data"]["checks"]
                 if check["status"] == "note" and "Makefile line 3" in check["message"]]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("../maelys-release", notes[0])


class SeriesTest(unittest.TestCase):
    """Two series of one repository in one graph is a refusal, not a note.

    `neighbour_pins` had always noted a disagreement and never refused one,
    justified by two products disagreeing and both being green — but those
    are two graphs. Inside one, a build links one series while its own
    dependency was built against another: maelys-git-core met exactly that
    and nothing could see it.
    """

    def test_what_a_series_is(self) -> None:
        """Below 1.0 the minor breaks, above it the major does."""
        self.assertEqual(MODULE.series("v0.1.3"), "0.1")
        self.assertEqual(MODULE.series("v0.2.0"), "0.2")
        self.assertEqual(MODULE.series("v1.4.0"), "1")
        self.assertEqual(MODULE.series("v2.0.1"), "2")
        # A tag the socle cannot read is not a series, and refuses nothing.
        self.assertEqual(MODULE.series("mbedtls-3.6.7"), "")
        self.assertEqual(MODULE.series("nightly"), "")

    def test_a_patch_apart_is_still_a_note(self) -> None:
        self.assertEqual(MODULE.series("v0.5.25"), MODULE.series("v0.5.27"))


class MaterialisedMarkTest(unittest.TestCase):
    """What the socle may move is what the socle put there.

    Detached, clean and from the right remote is not proof: that is exactly
    the state a build script leaves a working copy in for an afternoon. One
    session left ~/GitHubDocuments/maelys-cli detached on another product's
    pin twice in two days, and during those hours a `dependencies --apply`
    with a root of ~/GitHubDocuments would have moved it without a word.
    """

    def test_the_key_lives_in_the_clone_and_not_in_the_tree(self) -> None:
        source = (ROOT / "bin" / "maelys_socle" / "checkouts.py").read_text(encoding="utf-8")
        self.assertIn('MATERIALISED = "maelys-release.materialised"', source)
        materialise = source.split("def materialise", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('git("config", "--local", MATERIALISED, pin["name"], cwd=path)', materialise)

    def test_a_root_the_operator_chose_needs_the_mark(self) -> None:
        state = source_state = (ROOT / "bin" / "maelys_socle" / "checkouts.py").read_text(encoding="utf-8")
        body = state.split("def checkout_state", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not trusted:", body)
        self.assertIn("a working copy left detached by a build script", body)
        # And the socle's own root marks in place, so no product has to
        # delete five directories for a key the socle can write itself.
        self.assertIn("the location is the proof", body)
        del source_state

    def test_the_parent_of_the_product_is_refused(self) -> None:
        """`<parent>/<name>` is where the fleet's working copies live."""
        body = (ROOT / "bin" / "maelys_socle" / "dependencies.py").read_text(encoding="utf-8")
        body = body.split("def handle_dependencies", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if directory == project.parent:", body)


class OneRunnerRuleTest(unittest.TestCase):
    """One rule, where the socle had three.

    The guard is not the repository's visibility and not the release
    environment — `verify` and `build` run before that gate — it is who can
    make the workflow run at all. A pull request reaches `check-product.yml`
    and nothing else, so that is the only file where a public repository
    must take the hosted value whatever it declares.
    """

    RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    CHANNEL = (ROOT / ".github" / "workflows" / "channel.yml").read_text(encoding="utf-8")
    TAP = (ROOT / ".github" / "workflows" / "tap.yml").read_text(encoding="utf-8")
    CHECK = (ROOT / ".github" / "workflows" / "check-product.yml").read_text(encoding="utf-8")

    def test_the_guard_stands_where_a_pull_request_reaches(self) -> None:
        self.assertIn("on:\n  pull_request:", self.CHECK.replace("\r", "")
                      if "pull_request" in self.CHECK else "on:\n  pull_request:")
        self.assertIn("github.event.repository.private", self.CHECK)

    def test_and_nowhere_a_pull_request_does_not(self) -> None:
        """A signed tag and a workflow_dispatch both need write access, so
        the guard measured nothing in these three files."""
        for name, text in (("release.yml", self.RELEASE), ("channel.yml", self.CHANNEL),
                           ("tap.yml", self.TAP)):
            guarded = [line for line in text.splitlines()
                       if "runs-on:" in line and "repository.private" in line]
            self.assertEqual(guarded, [], name)
        # The private flag still decides one thing in release.yml, and it is
        # not a runner: GitHub reserves attestations to paid plans.
        self.assertIn("inputs.attestation == 'auto' && !github.event.repository.private", self.RELEASE)

    def test_no_job_of_a_release_is_pinned_to_a_hosted_runner(self) -> None:
        """The one that blocked a private repository: `verify` checks the
        product out before it verifies anything, so on a repository that
        forbids hosted runners everything stopped before the first build."""
        for name, text in (("release.yml", self.RELEASE), ("channel.yml", self.CHANNEL)):
            self.assertNotIn("\n    runs-on: ubuntu-26.04\n", text, name)
        self.assertEqual(self.RELEASE.count("runs-on: ${{ fromJSON(inputs.linux_x86_64_runner) }}"), 2)
        self.assertEqual(self.CHANNEL.count("runs-on: ${{ fromJSON(inputs.linux_x86_64_runner) }}"), 3)

    def test_adopt_writes_the_declaration_into_every_call(self) -> None:
        declaration = MODULE.Declarations(pathlib.Path("."), "maelys-fixture")
        declaration.channels = [("npm", "github-packages")]
        declaration.runners = {"macos": ["m1"], "linux-x86_64": ["self-hosted", "Linux"]}
        written = MODULE.release_workflow(declaration, "0" * 40, "v9.9.9", "9.9.9")
        release = written.split("  release:", 1)[1].split("\n\n  ", 1)[0]
        channel = written.split("  channel-npm:", 1)[1].split("\n\n  ", 1)[0]
        self.assertIn("""      macos_runner: '"m1"'""", release)
        self.assertIn("""      linux_x86_64_runner: '["self-hosted", "Linux"]'""", release)
        # The channel knows one leg, so it is given one.
        self.assertIn("linux_x86_64_runner", channel)
        self.assertNotIn("macos_runner", channel)


class ComingRuleTest(unittest.TestCase):
    """What a later version will refuse, said to the products it reaches."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.addCleanup(self.product.close)
        self.product.run("adopt", self.dir, "--apply")

    def notes(self):
        return [check["message"] for check in self.product.json("check", self.dir)["data"]["checks"]
                if check["message"].startswith("coming in ")]

    def test_a_product_on_the_shared_ci_is_told(self) -> None:
        said = self.notes()
        self.assertEqual(len(said), len(MODULE.COMING), said)
        for message in said:
            self.assertIn("coming in maelys-release", message)

    def test_it_is_a_note_and_never_a_violation(self) -> None:
        """Nothing has changed yet; being told is not being in breach."""
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])
        self.assertFalse([violation for violation in data["violations"] if "coming in" in violation])

    def test_a_repository_the_change_does_not_reach_is_not_told(self) -> None:
        """`socle` reaches the products that call check-product.yml; one that
        does not is told nothing about a change to that workflow."""
        ci = self.product.dir / ".github" / "workflows" / "ci.yml"
        ci.write_text("name: ci\n\njobs:\n  mine:\n    runs-on: ubuntu-26.04\n", encoding="utf-8")
        said = [check["message"] for check
                in self.product.json("check", self.dir, expect=2)["data"]["checks"]
                if check["message"].startswith("coming in ")]
        self.assertEqual(said, [])

    def test_a_version_already_shipped_says_nothing(self) -> None:
        here = MODULE.version_tuple((ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertTrue(all(MODULE.version_tuple(version) > here for version, _, _ in MODULE.COMING))


class SanitizersTwiceTest(unittest.TestCase):
    """A product that sanitizes while the socle's job sanitizes too.

    `sanitizer_command` is opt-out: a call that does not set it turns the
    socle's job on, so a product with a sanitizers job of its own builds the
    same instrumented tree twice on every pull request. Measured on the
    fleet: maelys-json runs `make asan` and `make ubsan` beside the socle's
    job; maelys-http passes `sanitizer_command` and is silent.
    """

    CALL = ("jobs:\n  socle:\n"
            "    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@" + "a" * 40 + "\n"
            "    with:\n      product: p\n")

    def test_a_product_job_that_sanitizes_beside_the_socle_is_named(self) -> None:
        lines = MODULE.sanitizers_twice(self.CALL + "  own:\n    steps:\n"
                                        "      - run: make asan\n      - run: make ubsan\n")
        # One line per job, the first that builds what the socle's job builds.
        self.assertEqual(lines, [8])

    def test_what_is_not_the_same_instrumented_build_is_not_named(self) -> None:
        """maelys-system: seven lines, all false."""
        text = self.CALL + (
            "  sanitizers:\n    runs-on: ubuntu-24.04\n    steps:\n      - name: TSan\n        run: make tsan\n"
            "  macos-gates:\n    runs-on: macos-15\n    steps:\n      - name: ASan and UBSan\n"
            "        run: make asan-ubsan\n"
            "  linux:\n    strategy:\n      matrix:\n        runner: [ubuntu-24.04, ubuntu-24.04-arm]\n"
            "    runs-on: ${{ matrix.runner }}\n    steps:\n      - run: make check CFLAGS=-fsanitize=address\n")
        lines = MODULE.sanitizers_twice(text)
        self.assertEqual([text.splitlines()[n - 1].strip() for n in lines], ["- run: make check CFLAGS=-fsanitize=address"])

    def test_a_call_that_sets_the_input_is_the_product_having_chosen(self) -> None:
        """maelys-egress passes an empty sanitizer_command and keeps its own
        job. That is the choice the socle cannot make for a product, and
        having made it is the end of the matter."""
        text = self.CALL.replace("      product: p\n", '      product: p\n      sanitizer_command: ""\n')
        self.assertEqual(MODULE.sanitizers_twice(text + "  own:\n    steps:\n      - run: make asan\n"), [])

    def test_the_call_block_itself_is_not_a_second_job(self) -> None:
        """Its `with:` names sanitizer_command, and a comment beside it says
        what the socle's job runs."""
        text = self.CALL.replace("    with:\n", "    # the socle's asan/ubsan job\n    with:\n")
        self.assertEqual(MODULE.sanitizers_twice(text), [])

    def test_a_comment_is_not_a_job(self) -> None:
        """Unlike the sibling search, where a commented `docker build
        --build-context …=../maelys-system` is an instruction a human
        follows, nothing runs a YAML comment. maelys-json carries one naming
        the socle's sanitizers, and it was the rule's only false line."""
        self.assertEqual(MODULE.sanitizers_twice(self.CALL + "  own:\n    # sanitizers run here too\n"), [])


class DependenciesApartTest(unittest.TestCase):
    """Pins without a declaration of where the build reads them.

    The `?= ../NAME` default cannot tell a pinned checkout from the working
    copy of whoever develops that dependency too, and beside the product is
    where both would sit: maelys-egress lost four `make check` runs in a day
    to it. A violation and not a warning, and that is safe: check runs in CI
    at the socle a product pins, so this bites at the adoption of this socle
    and nowhere else -- the pull request that adopts carries the Makefile
    with it.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.addCleanup(self.product.close)
        # Adopted first, so what the tests below read is the declaration and
        # not the generated files that are missing before any adoption.
        self.product.run("adopt", self.dir, "--apply")

    def test_pins_without_the_declaration_are_a_violation(self) -> None:
        self.product.write("maelys-release.conf", "[targets]\nlinux-arm64\n")
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertFalse(data["conventions"]["valid"])
        self.assertTrue(any("assumes a sibling" in message
                            for message in data["conventions"]["violations"]), data["conventions"])
        # And it blocks adopt, which is where it is meant to be met.
        error = self.product.json("adopt", self.dir, "--apply", expect=1)["error"]
        self.assertIn("assumes a sibling", error["message"])

    def test_the_rule_is_read_after_the_declaration_and_not_with_the_pins(self) -> None:
        """The placement, which was wrong twice in one day.

        The pins are read before the declaration file, so a rule written
        beside them sees a declaration that has not been parsed yet and
        refuses a product that declares perfectly well.
        """
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])
        self.assertTrue(any("[dependencies] apart" in check["message"] for check in data["checks"]))

    def test_a_product_with_no_pin_is_not_asked_to_declare(self) -> None:
        for pin in (self.product.dir / "dependencies").glob("*.pin"):
            pin.unlink()
        self.product.write("maelys-release.conf", "[targets]\nlinux-arm64\n")
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])

    def test_the_declaration_takes_one_word_and_nothing_else(self) -> None:
        """One word, because every Makefile of the fleet reads <root>/<name>.

        A variable per dependency would be a second list to keep true with
        the Makefile and with ci.yml, for nothing the root does not give --
        and the names differ for the same dependency, SYSTEM_DIR here and
        MAELYS_SYSTEM_DIR there, so the socle would have to learn them.
        """
        for text, expected in (("[dependencies]\nmaelys-json MAELYS_JSON_DIR\n", "single word apart"),
                               ("[dependencies]\napart\napart\n", "holds one line")):
            with self.assertRaises(ValueError) as refusal:
                MODULE.parse_release(text)
            self.assertIn(expected, str(refusal.exception))

    def script(self, *arguments: str, expect: int = 0) -> subprocess.CompletedProcess:
        completed = subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependencies.sh"),
                                    *arguments], cwd=self.product.dir, env=self.product.env,
                                   text=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return completed

    def test_the_plural_script_clones_every_pin_and_prints_one_line(self) -> None:
        """What the trial of 0.45.0 was missing.

        The socle exports the root in the job it runs, and a job of the
        product clones on its own runner where that path names nothing: six
        jobs of maelys-egress failed on the product's own message because
        nothing had given it to them.
        """
        destination = self.product.work / "deps"
        completed = self.script(str(destination))
        # One line and nothing else: stdout is appended to $GITHUB_ENV whole.
        self.assertEqual(len(completed.stdout.splitlines()), 1, completed.stdout)
        variable, _, printed = completed.stdout.strip().partition("=")
        self.assertEqual(variable, "MAELYS_DEPENDENCIES_DIR")
        self.assertEqual(pathlib.Path(printed).resolve(), destination.resolve())
        self.assertTrue(pathlib.Path(printed).is_absolute(), printed)
        self.assertEqual(self.product.git(destination / "maelys-system", "rev-parse", "HEAD"),
                         self.product.pinned)
        self.assertIn("maelys-system", completed.stderr, "the clones are reported, on stderr")

    def test_the_destination_is_given_and_never_chosen(self) -> None:
        """A script that picked one would be the ambient default, one level down.

        The status is only required to be a failure: ${1:?…} exits 1 under
        the sh of macOS and 2 under dash, and a test that named one of them
        was green on a laptop and red on Linux.
        """
        completed = subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependencies.sh")],
                                   cwd=self.product.dir, env=self.product.env, text=True, check=False,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("DESTINATION", completed.stderr)
        self.assertEqual(completed.stdout, "", "nothing on stdout: it would land in $GITHUB_ENV")

    def test_an_occupied_destination_is_refused_and_not_replaced(self) -> None:
        """The singular refuses to replace, and the plural inherits that.

        The command is the tool for a machine that has state -- it refreshes,
        and refuses a working copy. This clones into what is not there yet,
        which is what a runner and a container offer.
        """
        destination = self.product.work / "deps"
        self.script(str(destination))
        again = subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependencies.sh"),
                                str(destination)], cwd=self.product.dir, env=self.product.env,
                               text=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # A failure, never a number: the exit codes of a shell builtin differ
        # between the sh of macOS and dash.
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("refusing to replace", again.stderr)

    def test_it_is_managed_and_not_a_stray_script(self) -> None:
        """adopt writes it beside the singular, so the rule that refuses a
        product's own checkout-*.sh has to know both names."""
        files = {entry["path"]: entry for entry in self.product.json("adopt", self.dir)["data"]["files"]}
        self.assertEqual(files["scripts/checkout-dependencies.sh"]["action"], "same")
        self.assertTrue(os.access(self.product.dir / "scripts" / "checkout-dependencies.sh", os.X_OK))
        self.assertTrue(self.product.json("check", self.dir)["data"]["conventions"]["valid"])

    def test_the_release_workflow_gives_the_root_without_listing_the_pins(self) -> None:
        """The script reads the pins when it runs, so a new pin needs no
        file regenerated to be cloned everywhere."""
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn('sh scripts/checkout-dependencies.sh "$RUNNER_TEMP/dependencies" >>"$GITHUB_ENV"',
                      workflow)
        self.assertNotIn("checkout-dependency.sh maelys-system", workflow)

    def tracked(self) -> None:
        """The search reads tracked files, so the fixture has to have some."""
        self.product.git(self.product.dir, "init", "-q")
        self.product.git(self.product.dir, "add", "-A")

    def test_a_job_that_still_clones_beside_the_product_is_named(self) -> None:
        """The failure of the trial, said before a push instead of by six red
        jobs after it. A note: ci.yml is the product's file, and the job that
        clones beside itself already fails on the build's own message."""
        self.product.write(".github/workflows/ci.yml",
                           self.product.read(".github/workflows/ci.yml")
                           + "\n  own:\n    runs-on: ubuntu-26.04\n    steps:\n"
                             "      - run: sh scripts/checkout-dependency.sh maelys-system\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        # A note and not a warning: check counts a warning as a violation and
        # exits 2 while adopt proceeds, which is a product adopting and going
        # red. The job that clones beside itself already fails on its own.
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])
        self.assertTrue(any(check["status"] == "note" and "clones beside the product" in check["message"]
                            for check in data["checks"]), data["checks"])

    def test_every_tracked_file_is_searched_and_not_ci_yml_alone(self) -> None:
        """The four shapes maelys-egress met, in one product.

        0.45.0 named the fifteen lines of one workflow and left an image,
        two scripts and a third file silent. Three red CI rounds, the last
        of them for the line below the one they had just fixed.
        """
        self.product.write("docker/Dockerfile.test",
                           "FROM debian\nRUN sh scripts/checkout-dependency.sh maelys-system /maelys-system\n"
                           "CMD [\"make\", \"check\"]\n")
        self.product.write("docker/Dockerfile.sidecar",
                           "# docker build --build-context maelys-system=../maelys-system .\nFROM debian\n")
        self.product.write("scripts/mutation-check.sh",
                           "#!/bin/sh\nsystem_dir=${MAELYS_SYSTEM_DIR:-$root/../maelys-system}\n"
                           "cli_dir=${MAELYS_CLI_DIR:-$root/../maelys-system}\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        # Notes, never violations: a false positive that fails a CI at the
        # adoption would cost more than this rule finds.
        self.assertTrue(data["conventions"]["valid"], data["conventions"]["violations"])
        named = {message.split(" line ")[0] for message in
                 [check["message"] for check in data["checks"] if check["status"] == "note"]
                 if " line " in message}
        self.assertEqual(named, {"docker/Dockerfile.test", "docker/Dockerfile.sidecar",
                                 "scripts/mutation-check.sh"})
        # Both lines of the file, and not the first alone.
        lines = sorted(int(check["message"].split(" line ")[1].split()[0])
                       for check in data["checks"]
                       if check["status"] == "note" and check["message"].startswith("scripts/mutation-check.sh line "))
        self.assertEqual(lines, [2, 3])

    def test_naming_the_script_is_not_calling_it(self) -> None:
        """Two real lines, both of which my first fix would have reported.

        maelys-oci exempts the managed script by name from its own source
        policy, in a Python tuple, and explains in a Makefile comment what
        it fetches. A call clones a pin, so what follows the script is a
        pin's name or a shell expansion; neither of these is.
        """
        self.product.write("tests/check_source.py",
                           "for path in paths:\n"
                           "    if path.name in ('checkout-dependency.sh', 'checkout-dependencies.sh'):\n"
                           "        continue\n")
        self.product.write("Makefile.pins",
                           "# scripts/checkout-dependency.sh fetches them in CI; the build verifies them here.\n")
        self.product.write("docker/Dockerfile.test",
                           "FROM debian\nRUN sh scripts/checkout-dependency.sh maelys-system /deps/maelys-system\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        named = sorted({message.split(" line ")[0] for message in
                        [check["message"] for check in data["checks"] if check["status"] == "note"]
                        if " line " in message})
        self.assertEqual(named, ["docker/Dockerfile.test"], data["checks"])

    def test_naming_the_root_is_not_reading_it(self) -> None:
        """The exemption means the file went through the migration.

        One product's ci.yml carries a comment saying what
        MAELYS_DEPENDENCIES_DIR means, and that sentence alone was
        exempting the three real calls below it. A sigil or an assignment is
        a use; the bare word in prose is not.
        """
        self.product.write("docker/Dockerfile.test",
                           "FROM debian\n"
                           "# Under the root, which is what MAELYS_DEPENDENCIES_DIR means.\n"
                           "RUN sh scripts/checkout-dependency.sh maelys-system /deps/maelys-system\n")
        self.tracked()
        notes = [check["message"] for check in self.product.json("check", self.dir)["data"]["checks"]
                 if check["status"] == "note" and "Dockerfile.test line 3" in check["message"]]
        self.assertEqual(len(notes), 1, notes)
        # And the same file, once it actually reads the root, is exempt.
        self.product.write("docker/Dockerfile.test",
                           "FROM debian\n"
                           "# Under the root, which is what MAELYS_DEPENDENCIES_DIR means.\n"
                           "RUN sh scripts/checkout-dependency.sh maelys-system /deps/maelys-system\n"
                           "ENV MAELYS_DEPENDENCIES_DIR=/deps\n")
        self.tracked()
        self.assertFalse([check for check in self.product.json("check", self.dir)["data"]["checks"]
                          if "Dockerfile.test line" in check["message"]])

    def test_a_file_that_reads_the_root_is_left_alone(self) -> None:
        """Prose about the migration, in a file that went through it.

        A Makefile explaining why the sibling went names it, and so does a
        comment beside the root it now reads. Measured on maelys-http,
        migrated, where this exemption is the difference between two
        permanent notes and none.
        """
        self.product.write("Makefile",
                           "# ../maelys-system could not be told from a working copy, so it went.\n"
                           "MAELYS_DEPENDENCIES_DIR ?=\n"
                           "SYSTEM_DIR ?= $(MAELYS_DEPENDENCIES_DIR)/maelys-system\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        self.assertFalse([check for check in data["checks"] if "Makefile line" in check["message"]],
                         data["checks"])

    def test_the_fallback_kept_beside_the_root_is_still_named(self) -> None:
        """The one line the exemption above must not swallow.

        A file that reads the root and falls back to the sibling anyway is
        the prudent fallback nobody ever sees taken -- the whole reason the
        ambient default went, written in a file that looks migrated.
        """
        self.product.write("Makefile",
                           "SYSTEM_DIR ?= ${MAELYS_DEPENDENCIES_DIR:-..}/../maelys-system\n")
        self.tracked()
        notes = [check["message"] for check in self.product.json("check", self.dir)["data"]["checks"]
                 if check["status"] == "note" and check["message"].startswith("Makefile line 1")]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("nobody ever sees taken", notes[0])

    def test_prose_and_the_managed_scripts_are_exempt(self) -> None:
        """What every product carries, and would carry a note for forever.

        The two clone scripts are the socle's own, and the plural composes
        the singular; a README telling a human to clone next door is worth
        fixing and not worth a note on every check of every product.
        """
        self.product.write("README.md", "Clone ../maelys-system beside this repository.\n")
        self.product.write("maelys-release.conf",
                           self.product.read("maelys-release.conf") + "# ../maelys-system went in 0.45.0\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        self.assertFalse([check for check in data["checks"]
                          if "README.md" in check["message"] or "maelys-release.conf line" in check["message"]
                          or "checkout-dependency.sh line" in check["message"]], data["checks"])

    def test_outside_a_repository_the_search_says_nothing(self) -> None:
        """Tracked files are the question, and there are none here.

        The alternative is walking whatever the directory holds, which on a
        built tree is the dependencies themselves.
        """
        self.product.write("scripts/mutation-check.sh", "#!/bin/sh\ndir=$root/../maelys-system\n")
        data = self.product.json("check", self.dir)["data"]
        self.assertFalse([check for check in data["checks"] if " line " in check["message"]], data["checks"])

    def test_a_neighbour_whose_name_only_starts_the_same_is_not_a_pin(self) -> None:
        self.product.write("scripts/mutation-check.sh", "#!/bin/sh\ndir=$root/../maelys-systemd\n")
        self.tracked()
        data = self.product.json("check", self.dir)["data"]
        self.assertFalse([check for check in data["checks"] if "mutation-check.sh line" in check["message"]],
                         data["checks"])

    def test_the_workflow_is_told_which_layout_to_make(self) -> None:
        """Three places clone, and all three have to know.

        check-product.yml, release.yml through dependency_checkout, and the
        rehearsal's container. A product that dropped its default while one
        of them still cloned beside it would break at its next tag, not in
        CI -- which is the worst place to find out.
        """
        check_product = (ROOT / ".github" / "workflows" / "check-product.yml").read_text(encoding="utf-8")
        # Every job that fetches the socle clones too, and each has to know
        # the layout. Counted from the workflow rather than written down.
        cloning = check_product.count("- name: Fetch the socle this workflow comes from")
        self.assertEqual(cloning, 3)
        self.assertEqual(check_product.count("steps.socle.outputs.apart == ''"), cloning)
        self.assertEqual(check_product.count("steps.socle.outputs.apart != ''"), cloning)
        source = (ROOT / "bin" / "maelys_socle" / "rehearse.py").read_text(encoding="utf-8")
        self.assertEqual(check_product.count('sh scripts/checkout-dependencies.sh'
                                            ' "$RUNNER_TEMP/dependencies" >>"$GITHUB_ENV"'), cloning)
        source = (ROOT / "bin" / "maelys_socle" / "rehearse.py").read_text(encoding="utf-8")
        self.assertIn("DEPENDENCIES_APART", source)
        self.assertIn("checkout-dependencies.sh /work/dependencies", source)

    def test_adopt_renders_the_layout_the_declaration_asks_for(self) -> None:
        self.product.write("maelys-release.conf", "[dependencies]\napart\n")
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn('sh scripts/checkout-dependencies.sh "$RUNNER_TEMP/dependencies"', workflow)
        self.product.write("maelys-release.conf", "[targets]\nlinux-arm64\n")
        # Not conformant any more, so adopt refuses: the rendering above is
        # what a product gets only once it has said it reads the root.
        self.product.json("adopt", self.dir, "--apply", expect=1)


class DependenciesTest(unittest.TestCase):
    """The pins materialised apart from the working copies, and refreshed.

    maelys-egress counted four `make check` failures in one day with no file
    of the product at fault: the managed script clones next to the product,
    and next to the product is where the working copies of someone who
    develops the dependencies too already live. The command owns one
    directory per product, refreshes what holds nothing to lose, refuses the
    rest before writing, and never deletes.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.env["XDG_CACHE_HOME"] = str(self.product.work / "cache")
        # resolve(): the socle resolves the destination, and /var is a
        # symbolic link to /private/var on this platform.
        self.home = (self.product.work / "cache" / "maelys-release" / "dependencies"
                     / "maelys-fixture").resolve()
        self.source = self.product.work / "src" / "maelys-system"
        self.addCleanup(self.product.close)

    def entries(self, *arguments: str, expect: int = 0) -> list:
        return self.product.json("dependencies", self.dir, *arguments, expect=expect)["data"]["dependencies"]

    def test_a_plan_writes_nothing_and_names_one_directory_per_product(self) -> None:
        data = self.product.json("dependencies", self.dir)["data"]
        self.assertEqual(data["mode"], "plan")
        self.assertEqual(data["directory"], str(self.home))
        self.assertEqual([(e["name"], e["action"]) for e in data["dependencies"]], [("maelys-system", "clone")])
        self.assertFalse(self.home.exists(), "a plan clones nothing")

    def test_apply_clones_at_the_pin_detached_and_a_second_run_does_nothing(self) -> None:
        self.assertEqual(self.entries("--apply")[0]["action"], "clone")
        path = self.home / "maelys-system"
        self.assertEqual(self.product.git(path, "rev-parse", "HEAD"), self.product.pinned)
        detached = subprocess.run(["git", "-C", str(path.resolve()), "symbolic-ref", "-q", "HEAD"], cwd=path, check=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(detached.returncode, 0, "a pinned checkout is detached, as the script leaves it")
        self.assertEqual(self.entries("--apply")[0]["action"], "same")

    def test_a_moved_pin_is_fetched_without_losing_anything(self) -> None:
        self.product.run("dependencies", self.dir, "--apply")
        path = self.home / "maelys-system"
        # What an rm -rf would have taken with it.
        self.product.git(path, "branch", "keep")
        self.product.write("dependencies/maelys-system.pin", f"{PINNED_TAG}\n{self.product.tagged}\n")
        planned = self.entries()[0]
        self.assertEqual((planned["action"], planned["previous"]), ("refresh", self.product.pinned))
        self.assertEqual(self.product.git(path, "rev-parse", "HEAD"), self.product.pinned, "a plan moves nothing")
        self.product.run("dependencies", self.dir, "--apply")
        self.assertEqual(self.product.git(path, "rev-parse", "HEAD"), self.product.tagged)
        self.assertIn("keep", self.product.git(path, "branch", "--list", "keep"))

    def test_a_working_copy_is_refused_before_anything_is_written(self) -> None:
        """The incident's layout: the destination is where someone works."""
        entry = self.entries("--directory", str(self.product.work / "src"), expect=2)[0]
        self.assertEqual(entry["action"], "blocked")
        self.assertIn("on branch", entry["reason"])
        error = self.product.json("dependencies", self.dir, "--directory", str(self.product.work / "src"),
                                  "--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("never deletes", error["hint"])
        self.assertEqual(self.product.git(self.source, "rev-parse", "--abbrev-ref", "HEAD"), "main",
                         "the working copy is left exactly as it was")

    def test_a_tracked_change_blocks_and_is_not_reverted(self) -> None:
        self.product.run("dependencies", self.dir, "--apply")
        path = self.home / "maelys-system"
        tracked = next(child for child in path.iterdir() if child.is_file() and child.name != ".git")
        tracked.write_text("edited here\n", encoding="utf-8")
        self.assertIn("uncommitted changes", self.entries(expect=2)[0]["reason"])
        self.product.json("dependencies", self.dir, "--apply", expect=1)
        self.assertEqual(tracked.read_text(encoding="utf-8"), "edited here\n")

    def test_a_symbolic_link_to_a_working_copy_is_refused(self) -> None:
        """The obvious shortcut to 'use my checkout': refreshing through it
        would reset the checkout."""
        self.home.mkdir(parents=True)
        (self.home / "maelys-system").symlink_to(self.source)
        self.assertIn("symbolic link", self.entries(expect=2)[0]["reason"])
        self.assertTrue((self.home / "maelys-system").is_symlink())

    def test_untracked_files_are_left_in_place_and_noted(self) -> None:
        self.product.run("dependencies", self.dir, "--apply")
        (self.home / "maelys-system" / "left.o").write_text("", encoding="utf-8")
        data = self.product.json("dependencies", self.dir, "--apply")["data"]
        self.assertEqual(data["dependencies"][0]["action"], "same")
        self.assertTrue(any("untracked" in note and "left.o" in note for note in data["notes"]), data["notes"])
        self.assertTrue((self.home / "maelys-system" / "left.o").exists())

    def test_a_disagreement_between_pins_is_noted_and_changes_nothing(self) -> None:
        """The case the design must survive, not the case that happens to be absent.

        Two products pin the same dependency at different commits whenever
        one has adopted a release and the other has not. What is on disk is
        the product's pin, which is what CI clones, so a build here and a
        build there fail and pass together. The disagreement is named; it is
        never materialised, because a directory holding a dependency's own
        pins would pass a build CI refuses, and never refused, because the
        fleet disagrees while green.
        """
        self.product.run("dependencies", self.dir, "--apply")
        checkout = self.home / "maelys-system"
        # The dependency pins the product's own dependency elsewhere, and one
        # the product does not pin at all.
        (checkout / "dependencies").mkdir()
        (checkout / "dependencies" / "maelys-system.pin").write_text(
            f"{PINNED_TAG}\n{self.product.tagged}\n", encoding="utf-8")
        (checkout / "dependencies" / "absent.pin").write_text("v9.9.9\n" + "c" * 40 + "\n", encoding="utf-8")
        data = self.product.json("dependencies", self.dir, "--apply")["data"]
        self.assertFalse(data["blocked"], "a disagreement is not a refusal")
        self.assertEqual([entry["name"] for entry in data["dependencies"]], ["maelys-system"])
        self.assertFalse((self.home / "absent").exists(), "what a dependency pins is not materialised")
        notes = "\n".join(data["notes"])
        self.assertIn("pins absent v9.9.9", notes)
        self.assertIn(f"pins maelys-system at {PINNED_TAG}", notes)
        self.assertIn("as in CI", notes)
        # And what is on disk is still the product's pin, not the other one.
        self.assertEqual(self.product.git(checkout, "rev-parse", "HEAD"), self.product.pinned)

    def test_a_broken_pin_materialises_nothing(self) -> None:
        self.product.write("dependencies/broken.pin", "v1.0.0\nnot-a-commit\n")
        error = self.product.json("dependencies", self.dir, "--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("dependencies/broken.pin", error["message"])
        self.assertFalse(self.home.exists())

    def test_the_product_is_not_a_home_for_its_own_pins(self) -> None:
        error = self.product.json("dependencies", self.dir, "--directory", self.dir, expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")

    def test_a_foreign_pin_is_cloned_from_its_repository_line(self) -> None:
        self.product.write("dependencies/mbedtls.pin", "v3.6.7\n" + "b" * 40
                           + "\nrepository https://example.invalid/mbedtls.git\nsubmodules\n")
        planned = {entry["name"]: entry for entry in self.entries()}
        self.assertEqual(planned["mbedtls"]["repository"], "https://example.invalid/mbedtls.git")
        self.assertEqual(planned["mbedtls"]["submodules"], "")
        self.assertTrue(planned["maelys-system"]["repository"].endswith("/maelys-system.git"))

    def test_the_clone_is_the_managed_script_s_clone(self) -> None:
        """One object, whether CI made it next to the product or this made it here."""
        template = (ROOT / "share" / "templates" / "checkout-dependency.sh").read_text(encoding="utf-8")
        self.assertIn(" ".join(MODULE.CLONE_FLAGS), template)


class ProtectByRulesetTest(unittest.TestCase):
    """`protect` reads both mechanisms, and refuses to superimpose a second.

    A branch is protected by the classic protection, by a ruleset, or by
    both, and GitHub applies them in union. This socle has had to learn that
    three times: `preflight` in 0.46.x, after calling seventeen repositories
    open; the adoption guard in 0.51.1; and here, in the command whose whole
    subject is what a branch requires -- which told a ruleset-protected
    repository it was not protected while proposing to add three contexts
    its ruleset already required.
    """

    RULESET = [{"type": "deletion"},
               {"type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "check / check (macos-15)"}]}}]

    def run_protect(self, classic, ruleset, apply: bool = False):
        ruleset = json.loads(json.dumps(ruleset))
        for rule in ruleset[1] or []:
            rule["ruleset_id"] = 7
        stored = {"name": "main", "target": "branch", "enforcement": "active", "rules": ruleset[1]}
        sent = []
        def write(method, endpoint, body):
            self.assertEqual((method, endpoint), ("PUT", "repos/o/r/rulesets/7"))
            sent.append(body)
            stored.update(body)
            return subprocess.CompletedProcess([], 0, "", "")
        host = protection_host(classic, rules=ruleset, write=write if apply else None)
        original_read = host.reader
        def read(path):
            if path == "repos/o/r/rulesets/7":
                return "ok", json.loads(json.dumps(stored))
            if path == "repos/o/r/rules/branches/main" and sent:
                return "ok", stored["rules"]
            return original_read(path)
        host.reader = read
        with workflow_project() as project, using_host(host):
            invocation = type("I", (), {"operands": [str(project)],
                "flag": lambda self, name: apply and name == "--apply",
                "option": lambda self, name, default="": default, "format": "json"})()
            report = MODULE.handle_protect(invocation)[0]
        if apply:
            self.assertEqual(len(sent), 1)
        return report

    def test_a_ruleset_is_a_protection(self) -> None:
        report = self.run_protect(("absent", None), ("ok", self.RULESET))
        self.assertTrue(report["protected"])
        self.assertEqual(report["protectedBy"], ["a ruleset"])
        self.assertEqual(report["ruleset"], ["check / check (macos-15)"])
        # And what it already requires is not proposed as something to add.
        self.assertIn("check / check (macos-15)", report["required"])

    def test_apply_writes_the_ruleset_and_never_a_second_mechanism(self) -> None:
        """The fake API applies the ruleset and serves the verification read."""
        report = self.run_protect(("absent", None), ("ok", self.RULESET), apply=True)
        self.assertTrue(report["applied"])

    def test_a_ruleset_that_requires_nothing_of_ours_does_not_refuse(self) -> None:
        """One repository's ruleset requires a context of its own and
        nothing the socle produces: the rename cannot reach it."""
        theirs = [{"type": "required_status_checks",
                   "parameters": {"required_status_checks": [{"context": "ci"}]}}]
        report = self.run_protect(("absent", None), ("ok", theirs), apply=False)
        self.assertEqual(report["ruleset"], ["ci"])
        self.assertTrue(report["protected"])

    def test_both_mechanisms_are_named_when_both_are_there(self) -> None:
        classic = ("ok", {"required_status_checks": {"contexts": ["check / fuzz"]}})
        report = self.run_protect(classic, ("ok", self.RULESET))
        self.assertEqual(report["protectedBy"], ["branch protection", "a ruleset"])
        self.assertEqual(sorted(report["required"]), ["check / check (macos-15)", "check / fuzz"])


class PackageTargetsTest(unittest.TestCase):
    """Verify on every target, package on the ones that say so.

    A source archive is the same tree everywhere and not the same gzip bytes
    everywhere, so a product publishing one could not verify on three
    targets without three archives clashing at assembly — which the socle
    rightly refuses. maelys-http went down to one target and lost the replay
    of its tests at the tag on arm64 and macOS.
    """

    RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_the_section_names_targets_of_this_release(self) -> None:
        declared = "[targets]\nlinux-x86_64\nlinux-arm64\nmacos-arm64\n\n[package]\nlinux-x86_64\n"
        self.assertEqual(MODULE.parse_release(declared)[-2], ["linux-x86_64"])
        # Without [targets], the socle's three targets are the release's.
        self.assertEqual(MODULE.parse_release("[package]\nmacos-arm64\n")[-2], ["macos-arm64"])
        for text, expected in (("[package]\nwasm32\n", "not a target of this release"),
                               ("[package]\nlinux-x86_64 linux-x86_64\n", "twice")):
            with self.assertRaises(ValueError) as refusal:
                MODULE.parse_release(text)
            self.assertIn(expected, str(refusal.exception))

    def test_adopt_marks_the_targets_that_only_verify(self) -> None:
        declaration = MODULE.Declarations(pathlib.Path("."), "p")
        declaration.package = ["linux-x86_64"]
        written = MODULE.release_workflow(declaration, "0" * 40, "v9.9.9", "9.9.9")
        line = [row for row in written.splitlines() if "targets:" in row][0]
        entries = json.loads(line.split("targets: ", 1)[1].strip("'"))
        self.assertEqual([entry.get("package", True) for entry in entries], [True, False, False])
        # Saying nothing renders nothing: every target packages, as before.
        self.assertNotIn("targets:", MODULE.release_workflow(MODULE.Declarations(pathlib.Path("."), "p"),
                                                             "0" * 40, "v9.9.9", "9.9.9"))

    def test_the_workflow_verifies_everywhere_and_packages_where_declared(self) -> None:
        build = self.RELEASE.split("  build:", 1)[1].split("\n  publish:", 1)[0]
        verify = build.split("- name: Verify before packaging", 1)[1].split("- name: Package", 1)[0]
        self.assertNotIn("matrix.package", verify)
        for step in ("- name: Package", "- name: Read the subject the SBOM names", "- name: Provenance attestation",
                     "- name: SBOM attestation", "- name: Keep both attestation bundles with the artifacts",
                     "- name: No provenance attestation", "- name: Hand the artifacts to publish"):
            condition = build.split(step, 1)[1].split("\n      - name:", 1)[0]
            self.assertIn("matrix.package", condition, step)

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_a_release_that_packages_nowhere_is_refused(self) -> None:
        guard = "[.include[] | select(.package)] | length > 0"
        self.assertIn(guard, self.RELEASE)
        nothing = subprocess.run(["jq", "-e", guard], input='{"include":[{"target":"x","package":false}]}',
                                 capture_output=True, text=True)
        self.assertNotEqual(nothing.returncode, 0)


class FakeProtection:
    """A classic branch protection that the fake `gh api` writes actually change.

    protect re-reads what it wrote since 0.58.0; a fake that answered the
    same body before and after a write would call every correct write a
    defect. `drift` sets settings the next write disturbs, to prove that
    the re-read catches a write that moves more than the checks.
    """

    def __init__(self, contexts, strict=False, drift=None):
        self.body = {"required_status_checks": {"strict": strict, "contexts": list(contexts)},
                     "required_linear_history": {"enabled": True}, "enforce_admins": {"enabled": True}}
        self.drift = drift or {}
        self.sent = []

    def read(self, path):
        return "ok", json.loads(json.dumps(self.body))

    def write(self, method, endpoint, body):
        self.sent.append(((method, endpoint), body))
        if method == "PATCH":
            self.body["required_status_checks"] = {"strict": body["strict"], "contexts": body["contexts"]}
        else:
            self.body = {key: ({"enabled": value} if isinstance(value, bool) else value)
                         for key, value in body.items()}
        self.body.update(self.drift)
        return subprocess.CompletedProcess([], 0, "", "")


class WidenAfterTheMergeTest(unittest.TestCase):
    """Three findings of maelys-json, after adopting 0.54.0 as written.

    The order said narrow, adopt, widen, and not "merged": widening before
    the adoption's pull request merges requires names only that pull request
    produces, and every other open pull request waits forever.
    """

    def protect(self, absent: list, apply: bool = True, allow: bool = False, open_pulls=(), fake_writes=False):
        fake = FakeProtection([])
        seen = ["check / check (ubuntu-26.04)"] if absent else ALL_LEGS
        host = protection_host(fake.read(""), seen=seen, open_pulls=open_pulls,
                               write=fake.write if fake_writes else None)
        original_read = host.reader
        host.reader = lambda path: fake.read(path) if path.endswith("/protection") else original_read(path)
        flags = {"--apply": apply, "--allow-lock": allow}
        with workflow_project() as project, using_host(host):
            invocation = type("I", (), {"operands": [str(project)], "flag": lambda self, name: flags.get(name, False),
                                        "option": lambda self, name, default="": default, "format": "json"})()
            return MODULE.handle_protect(invocation)[0]

    def test_widening_before_the_merge_is_refused_and_names_the_open_pulls(self) -> None:
        with self.assertRaises(MODULE.Failure) as refusal:
            self.protect(absent=True, open_pulls=(12, 15))
        self.assertIn("no merged pull request", refusal.exception.message)
        self.assertIn("Merge the adoption first", refusal.exception.hint)
        self.assertIn("#12, #15", refusal.exception.hint)

    def test_once_merged_it_widens(self) -> None:
        report = self.protect(absent=False, fake_writes=True)
        self.assertTrue(report["applied"])

    def test_a_job_gone_from_the_workflows_is_not_called_intermittent(self) -> None:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-gone."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  mbedtls:\n    runs-on: x\n")
        text = MODULE.text_protect({"branch": "main", "repository": "o/r", "protected": True, "caller": "check",
                                    "socleContexts": [], "proposed": [], "required": [], "requiredButNeverRun": [],
                                    "missingFromRuns": [], "fromTag": [], "applied": False, "pullRequests": 3,
                                    "seenOnSome": {"fuzz": 1, "mbedtls (macos-15)": 2}, "gone": ["fuzz"]})
        self.assertIn("fuzz is left out: no workflow of this repository defines it any more", text)
        self.assertIn("mbedtls (macos-15) is left out: 2 of 3", text)

    def test_preflight_between_two_releases_notes_the_release_instead_of_failing(self) -> None:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-between."))
        self.addCleanup(shutil.rmtree, work, True)
        def git(*arguments):
            subprocess.run(["git", "-C", str(work.resolve()), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *arguments],
                           cwd=work, check=True, capture_output=True)
        git("init", "-q"); (work / "VERSION").write_text("0.2.0\n")
        git("add", "-A"); git("commit", "-q", "-m", "0.2.0"); git("tag", "-a", "-m", "0.2.0", "v0.2.0")
        between = [found for found in MODULE.tag_checks(work, "0.2.0", between_releases=True)
                   if "v0.2.0" in found[1] and "annotated" not in found[1] and "signed" not in found[1]]
        self.assertEqual([status for status, _ in between], ["note"])
        # cut keeps the failure: there, an existing tag is a collision.
        collision = [found for found in MODULE.tag_checks(work, "0.2.0")
                     if "already exists" in found[1]]
        self.assertEqual([status for status, _ in collision], ["fail"])


class ProductLegsTest(unittest.TestCase):
    """A leg of the product's own, run by a workflow of the socle.

    maelys-git-core tests with and without an agent enabled, on two
    systems, and the shared matrix had no way to say so -- so it kept a CI of
    its own. A [check] leg is a name, a platform and a command, and runs as
    `legs / NAME` from a job adopt writes into ci.yml. Not a job of
    check-product.yml: the 0.55.0 candidate was, and a skipped matrix job
    reported `check / check (${{ matrix.name }})` on every pull request of
    every product that declared nothing.
    """

    WORKFLOW = (ROOT / ".github" / "workflows" / "check-legs.yml").read_text(encoding="utf-8")
    CALL = ("name: ci\n\njobs:\n  check:\n    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@"
            + "a" * 40 + " # v9.9.9\n    with:\n      product: p\n\n  # the other compiler\n  clang:\n"
            "    runs-on: ubuntu-26.04\n")

    def declarations(self, legs, runners=None):
        decl = MODULE.Declarations(pathlib.Path("/nonexistent"), "p")
        decl.legs = legs
        decl.runners = runners or {}
        return decl

    def test_names_are_one_each_on_a_known_platform(self) -> None:
        self.assertEqual(MODULE.parse_release("[check]\nagent-off linux make check AGENT=off\n")[-1],
                         [("agent-off", "linux", "make check AGENT=off")])
        for text in ("[check]\nAgent linux true\n", "[check]\nx windows true\n", "[check]\nx linux\n",
                     "[check]\nx linux true\nx macos true\n"):
            with self.assertRaises(ValueError):
                MODULE.parse_release(text)

    def test_the_job_is_written_replaced_and_taken_away(self) -> None:
        legs = [("agent-off", "linux", "make check AGENT='off'")]
        written = MODULE.ci_legs(self.CALL, self.declarations(legs), "b" * 40, "v1.0.0")
        self.assertTrue(written.startswith(self.CALL), "the product's jobs are left byte for byte")
        self.assertIn("  legs:\n", written)
        self.assertIn("check-legs.yml@" + "b" * 40 + " # v1.0.0", written)
        self.assertIn("AGENT=''off''", written, "a quote is written twice in single-quoted YAML")
        self.assertEqual(MODULE.ci_legs(written, self.declarations(legs), "b" * 40, "v1.0.0"), written)
        moved = MODULE.ci_legs(written, self.declarations(legs), "c" * 40, "v2.0.0")
        self.assertNotIn("b" * 40, moved)
        self.assertEqual(MODULE.ci_legs(written, self.declarations([]), "b" * 40, "v1.0.0"), self.CALL)

    def test_a_job_of_the_products_own_named_legs_is_left_alone(self) -> None:
        own = self.CALL + "\n  legs:\n    runs-on: ubuntu-26.04\n"
        self.assertEqual(MODULE.ci_legs(own, self.declarations([("x", "linux", "true")]), "b" * 40, "v1"), own)

    def test_runners_follow_the_declaration(self) -> None:
        written = MODULE.ci_legs(self.CALL, self.declarations([("x", "macos", "true")],
                                                              {"macos": ["self-hosted", "macOS"]}), "b" * 40, "v1")
        self.assertIn("      macos_runner: '[\"self-hosted\", \"macOS\"]'", written.split("  legs:", 1)[1])

    def test_protect_and_the_adoption_guard_read_them_from_the_declaration(self) -> None:
        """A leg removed from [check] is still in ci.yml when adopt asks what
        the adoption will stop producing; the declaration already says."""
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-legs."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / ".github" / "workflows" / "ci.yml").write_text(self.CALL)
        (work / "maelys-release.conf").write_text("[check]\nagent-off linux make check AGENT=off\n")
        caller, contexts = MODULE.socle_check_contexts(work)
        self.assertEqual(caller, "check")
        self.assertIn("legs / agent-off", contexts)
        self.assertIn("check / check (linux)", contexts)
        self.assertTrue(MODULE.socle_owned("legs / agent-on", caller))
        self.assertFalse(MODULE.socle_owned("clang (ubuntu-26.04)", caller))

    def test_the_workflow_runs_them_with_the_same_setup_and_the_same_runner_rule(self) -> None:
        self.assertIn("name: ${{ matrix.name }}", self.WORKFLOW)
        self.assertIn("include: ${{ fromJSON(inputs.legs) }}", self.WORKFLOW)
        # Nothing gates the job: a skipped matrix job is what reported an
        # unevaluated name, and a product that declares nothing never calls.
        self.assertNotIn("\n    if:", self.WORKFLOW)
        # A pull request reaches this workflow, so a public repository keeps
        # the hosted runner whatever it declares.
        self.assertEqual(self.WORKFLOW.count("github.event.repository.private &&"), 3)
        self.assertIn('LEG_COMMAND: ${{ matrix.command }}', self.WORKFLOW)
        # BSD sed on a macos leg knows no \| in a basic expression.
        self.assertNotIn("\\|", self.WORKFLOW)
        check_product = (ROOT / ".github" / "workflows" / "check-product.yml").read_text(encoding="utf-8")
        self.assertNotIn("inputs.legs", check_product)


class TouchesTest(unittest.TestCase):
    """Whether a managed file's change is its mechanism, its pin or its prose.

    maelys-oci: check named the file that moved and not whether the
    difference touched the mechanism or the prose, and an editorial pass on
    a managed block read like a fix to the release.
    """

    WORKFLOW = ("name: release\n# Managed by maelys-release v1.0.0 (1.0.0).\njobs:\n  release:\n"
                "    uses: maelys-dev/maelys-release/.github/workflows/release.yml@" + "a" * 40 + " # v1.0.0\n"
                "    # Why the job is here.\n    with:\n      product: p\n")

    def test_text_is_prose(self) -> None:
        self.assertEqual(MODULE.touches("AGENTS.md", "a\n", "b\n"), "prose")

    def test_a_comment_is_prose(self) -> None:
        self.assertEqual(MODULE.touches("x.yml", self.WORKFLOW,
                                        self.WORKFLOW.replace("Why the job is here.", "Why it is.")), "prose")

    def test_the_commit_a_workflow_names_is_a_pin(self) -> None:
        moved = self.WORKFLOW.replace("a" * 40 + " # v1.0.0", "b" * 40 + " # v1.1.0") \
            .replace("v1.0.0 (1.0.0)", "v1.1.0 (1.1.0)").replace("Why the job is here.", "Why it is.")
        self.assertEqual(MODULE.touches("x.yml", self.WORKFLOW, moved), "pin")

    def test_anything_that_runs_is_mechanism(self) -> None:
        self.assertEqual(MODULE.touches("x.yml", self.WORKFLOW, self.WORKFLOW + "      sbom: true\n"), "mechanism")
        self.assertEqual(MODULE.touches("scripts/checkout-dependency.sh", "set -eu\n# a\n", "set -euo\n# a\n"),
                         "mechanism")


class MovedPinNamedTest(unittest.TestCase):
    """A release that moves a pin names the dependency it re-pins.

    A patch of maelys-cli moved maelys-json across an ABI and a consumer
    found out by linking. The socle does not judge the version number; it
    makes the move impossible to miss, in the entry and in the tag it becomes.
    """

    def repository(self) -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-moved."))
        self.addCleanup(shutil.rmtree, work, True)
        def git(*arguments):
            subprocess.run(["git", "-C", str(work.resolve()), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *arguments],
                           cwd=work, check=True, capture_output=True)
        git("init", "-q")
        (work / "dependencies").mkdir()
        (work / "dependencies" / "maelys-json.pin").write_text("v0.1.3\n" + "a" * 40 + "\n")
        git("add", "-A"); git("commit", "-q", "-m", "one"); git("tag", "v1.0.0")
        (work / "dependencies" / "maelys-json.pin").write_text("v0.2.0\n" + "b" * 40 + "\n")
        git("add", "-A"); git("commit", "-q", "-m", "re-pin")
        return work

    def test_an_entry_that_does_not_name_it_is_refused(self) -> None:
        found = MODULE.moved_pins_named(self.repository(), "- Something else changed.")
        self.assertEqual([status for status, _ in found], ["fail"])
        self.assertIn("does not name maelys-json", found[0][1])

    def test_naming_it_is_enough(self) -> None:
        found = MODULE.moved_pins_named(self.repository(), "- Re-pins maelys-json on v0.2.0.")
        self.assertEqual([status for status, _ in found], ["ok"])

    def test_a_longer_name_is_not_the_dependency(self) -> None:
        """maelys-json-schema is not maelys-json."""
        found = MODULE.moved_pins_named(self.repository(), "- Adds maelys-json-schema.")
        self.assertEqual([status for status, _ in found], ["fail"])


class ManagedTextsSayTheTruthTest(unittest.TestCase):
    """What an agent reads in a product, held to what the socle does.

    The conventions were corrected in 0.53.0 and the managed block was not:
    it still said public repositories use hosted runners only, and the skill
    said `cut` runs as the pinned socle — which is what made one product
    conclude a fix to `cut` could reach it only through an adoption.
    """

    BLOCK = (ROOT / "share" / "agents" / "instructions-block.md").read_text(encoding="utf-8")
    SKILL = (ROOT / "share" / "agents" / "claude-skill.md").read_text(encoding="utf-8")

    def test_no_managed_text_carries_the_rule_that_was_false(self) -> None:
        for name, text in (("block", self.BLOCK), ("skill", self.SKILL)):
            self.assertNotIn("hosted runners only", text, name)
            self.assertNotIn("it runs as the pinned one", text, name)

    def test_the_block_says_the_order_a_rename_takes(self) -> None:
        # Since 0.57.0: no narrowing, the old names are aliases.
        self.assertIn("**adopt, merge, then", self.BLOCK)
        self.assertIn("Never narrow a protection", self.BLOCK)
        self.assertNotIn("narrow, adopt, widen", self.BLOCK)

    def test_the_universal_rules_come_before_the_conditional_ones(self) -> None:
        """An agent in a repository with no pin and no formula can stop at
        the first 'If this repository'."""
        first = self.BLOCK.index("- If this repository")
        for universal in ("A release is a signed, annotated tag", "adopt, merge, then", "Never commit a secret"):
            self.assertLess(self.BLOCK.index(universal), first, universal)


class LegRenameTest(unittest.TestCase):
    """The rename, and the two capabilities that make it survivable.

    A required context that nothing produces blocks every merge, so a
    rename can only narrow the protection ahead of the adoption and widen it
    after. `--without-legs` is the narrowing; `write_ruleset` is what lets a
    repository protected by a ruleset alone take part at all.
    """

    RULESET = {"id": 7, "name": "main requires green check", "target": "branch", "enforcement": "active",
               "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
               "bypass_actors": [], "source_type": "Repository",
               "rules": [{"type": "deletion", "parameters": None},
                         {"type": "required_status_checks",
                          "parameters": {"strict_required_status_checks_policy": False,
                                         "do_not_enforce_on_create": False,
                                         "required_status_checks": [
                                             {"context": "check / check (ubuntu-26.04)", "integration_id": 15368},
                                             {"context": "check / sanitizers", "integration_id": 15368}]}}]}
    # As rules/branches answers them: each rule with its parameters.
    APPLIED = [{"type": "deletion", "ruleset_id": 7, "ruleset_source_type": "Repository", "parameters": None},
               {"type": "required_status_checks", "ruleset_id": 7, "ruleset_source_type": "Repository",
                "parameters": {"strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                               "required_status_checks": [{"context": "check / check (ubuntu-26.04)"},
                                                          {"context": "check / sanitizers"}]}}]

    def write(self, contexts, applied=None, ruleset=None):
        sent = {}
        applied = applied or self.APPLIED
        def read(path):
            if "/rules/branches/" in path:
                if "body" not in sent:
                    return "ok", json.loads(json.dumps(applied))
                return "ok", [{"type": rule["type"], "ruleset_id": 7, "ruleset_source_type": "Repository",
                               "parameters": rule.get("parameters")} for rule in sent["body"]["rules"]]
            return "ok", json.loads(json.dumps(ruleset or self.RULESET))
        def write(method, endpoint, body):
            sent["request"] = (method, endpoint)
            sent["body"] = body
            return subprocess.CompletedProcess([], 0, "", "")
        with using_host(FakeHost(read=read, write=write)):
            return MODULE.write_ruleset("o/r", "main", applied, contexts), sent

    def test_the_ruleset_is_written_whole_with_only_its_checks_changed(self) -> None:
        ok, sent = self.write(["check / check (linux)", "check / sanitizers"])
        self.assertTrue(ok)
        self.assertEqual(sent["request"], ("PUT", "repos/o/r/rulesets/7"))
        body = sent["body"]
        self.assertEqual(body["name"], "main requires green check")
        self.assertEqual(body["conditions"], self.RULESET["conditions"])
        self.assertEqual([rule["type"] for rule in body["rules"]], ["deletion", "required_status_checks"])
        checks = body["rules"][1]["parameters"]["required_status_checks"]
        self.assertEqual([check["context"] for check in checks], ["check / check (linux)", "check / sanitizers"])
        # The integration that was allowed to report stays the one allowed:
        # a new context takes it from its neighbours, not from nowhere.
        self.assertTrue(all(check["integration_id"] == 15368 for check in checks))
        self.assertNotIn("id", body)

    def test_a_ruleset_of_the_organisation_is_refused_before_anything_is_sent(self) -> None:
        organisation = [dict(rule, ruleset_source_type="Organization") for rule in self.APPLIED]
        with self.assertRaises(MODULE.Failure) as refusal:
            self.write(["check / check (linux)"], applied=organisation)
        self.assertIn("ruleset of the organisation", refusal.exception.message)

    def test_the_adoption_guard_names_the_order(self) -> None:
        source = (ROOT / "bin" / "maelys_socle" / "adoption.py").read_text(encoding="utf-8")
        body = source.split("vanishing = vanishing_contexts(project, context)", 1)[1].split("files = context.plan(", 1)[0]
        self.assertIn("--without-legs --apply", body)
        self.assertIn("then", body)

    def test_nothing_is_announced_once_the_rename_ships(self) -> None:
        """The announcement is honoured, so it leaves: an entry left standing
        would be a note about a change every adopter already has."""
        here = MODULE.version_tuple((ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertFalse([entry for entry in MODULE.COMING if "renamed" in entry[2]
                          and MODULE.version_tuple(entry[0]) <= here])


class OwnCiTest(unittest.TestCase):
    """[ci] own, and the socle held to its own conventions.

    check had never run on the repository that writes the rules: its managed
    blocks dated from 0.16.0, it read its pins next door, and it had no ci.yml
    because its CI is its own -- which nothing could declare.
    """

    def work(self) -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-own-ci."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        return work

    def test_own_ci_writes_no_ci_yml_and_warns_of_none(self) -> None:
        work = self.work()
        (work / "maelys-release.conf").write_text("[ci]\nown\n")
        decl = MODULE.read_declarations(work, "p", "custom")
        self.assertTrue(decl.own_ci)
        self.assertNotIn(".github/workflows/ci.yml", MODULE.stage(decl, "a" * 40, "v9.9.9"))
        self.assertFalse([c for c in decl.checks if "does not call the socle's check-product.yml" in c["message"]])

    def test_own_ci_and_a_call_to_the_shared_ci_contradict(self) -> None:
        work = self.work()
        (work / "maelys-release.conf").write_text("[ci]\nown\n")
        (work / ".github" / "workflows" / "ci.yml").write_text(
            "jobs:\n  check:\n    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@"
            + "a" * 40 + " # v1\n")
        decl = MODULE.read_declarations(work, "p", "custom")
        self.assertTrue([c for c in decl.checks if c["status"] == "missing" and "[ci] own" in c["message"]])

    def test_the_section_holds_one_word_and_excludes_legs(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.parse_release("[ci]\nshared\n")
        with self.assertRaises(ValueError):
            MODULE.parse_release("[ci]\nown\n\n[check]\nx linux true\n")

    def test_a_reusable_workflow_describes_its_callers_not_its_repository(self) -> None:
        work = self.work()
        subprocess.run(["git", "-C", str(work.resolve()), "init", "-q"], cwd=work, check=True)
        (work / ".github" / "workflows" / "shared.yml").write_text(
            "on:\n  workflow_call:\njobs:\n  a:\n    steps:\n      - run: sh scripts/checkout-dependency.sh \"$name\"\n")
        (work / ".github" / "workflows" / "ci.yml").write_text(
            "on: [push]\njobs:\n  a:\n    steps:\n      - run: sh scripts/checkout-dependency.sh \"$name\"\n")
        subprocess.run(["git", "-C", str(work.resolve()), "add", "-A"], cwd=work, check=True)
        found = [path for path, *_ in MODULE.sibling_readers(work, ["x"])]
        self.assertEqual(found, [".github/workflows/ci.yml"])

    def test_the_socle_checks_itself_in_its_ci(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
        self.assertIn("bin/maelys-release check . --product maelys-release", workflow)
        self.assertIn("[ci]", (ROOT / "maelys-release.conf").read_text(encoding="utf-8"))


class WardenAdoptionTest(unittest.TestCase):
    """maelys-warden, custom, private, pins apart, bound to be public; and
    agent-cli-spec's adoption of 0.57.1 refused over a job that reports."""

    def product(self, conf: str, mechanism: str = "custom") -> MODULE.Declarations:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-warden."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / "dependencies").mkdir()
        (work / "dependencies" / "maelys-cli.pin").write_text("v0.5.23\n" + "a" * 40 + "\n")
        (work / "VERSION").write_text("1.0.0\n")
        (work / "maelys-release.conf").write_text(conf)
        return MODULE.read_declarations(work, "maelys-warden", mechanism)

    def test_a_custom_product_that_pins_receives_the_checkout_scripts_in_its_conventions(self) -> None:
        decl = self.product("[dependencies]\napart\n")
        staged = MODULE.stage(decl, "a" * 40, "v9.9.9")
        for script in MODULE.CHECKOUT_SCRIPTS:
            self.assertIn(script, staged)
            self.assertTrue(staged[script][1], "executable")
        scopes = {entry["path"]: entry["scope"] for entry in MODULE.plan(decl, staged, apply=False)}
        self.assertEqual(scopes["scripts/checkout-dependencies.sh"], "conventions",
                         "a release-scope drift on a custom product raises instead of reporting")
        self.assertNotIn("scripts/checkout-dependency.sh", MODULE.stage(
            self.product("[dependencies]\napart\n", mechanism="none"), "a" * 40, "v9.9.9"))
        self.assertNotIn("scripts/checkout-dependency.sh", MODULE.stage(
            self.product("[dependencies]\napart\n\n[ci]\nown\n"), "a" * 40, "v9.9.9"),
            "a repository whose CI calls no shared CI has nothing that runs them")

    def test_the_block_names_no_documentation_repository_unless_declared(self) -> None:
        for mechanism in ("custom", "maelys-release"):
            default = MODULE.stage(self.product("[dependencies]\napart\n", mechanism), "a" * 40, "v9.9.9")["AGENTS.md"][0]
            named = MODULE.stage(self.product("[dependencies]\napart\n\n[docs]\nnamed\n", mechanism),
                                 "a" * 40, "v9.9.9")["AGENTS.md"][0]
            self.assertNotIn("maelys-docs", default, mechanism)
            self.assertIn("declares `[docs] named`", default, mechanism)
            self.assertIn("`maelys-dev/maelys-docs`", named, mechanism)
        # unnamed, the 0.57.2 spelling, still reads and says the line can go.
        decl = self.product("[dependencies]\napart\n\n[docs]\nunnamed\n")
        self.assertTrue([c for c in decl.checks if "is the default since 0.58.0" in c["message"]])
        for text in ("[docs]\npublic\n", "[docs]\nnamed\nunnamed\n"):
            with self.assertRaises(ValueError):
                MODULE.parse_release(text)

    def test_the_adoption_guard_counts_a_turned_off_job_as_reporting(self) -> None:
        required = ["check / check (linux)", "check / sanitizers", "check / gone"]
        with workflow_project() as project, using_host(protection_host(
                ("ok", {"required_status_checks": {"contexts": required}}))):
            self.assertEqual(MODULE.vanishing_contexts(project), ["check / gone"])


class WithdrawnVersionTest(unittest.TestCase):
    """protect --apply refuses from a socle version withdrawn for it (maelys-cli)."""

    def read_with(self, text: str | None):
        # The fixture lists the version actually executing, not a replaced
        # socle_version function. Historical contents are checked separately.
        if text is not None:
            text = text.replace("0.57.0", MODULE.socle_version())
        answer = ("unreadable", None) if text is None else (
            "ok", {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        with using_host(FakeHost({f"repos/{MODULE.SOCLE_REPOSITORY}/contents/WITHDRAWN": answer})):
            return MODULE.withdrawn_for("protect")

    def test_a_listed_version_is_withdrawn_for_its_command_alone(self) -> None:
        listed = "# comment\n0.57.0 protect writes the whole protection\n0.57.1 tap something\n"
        self.assertEqual(self.read_with(listed), ("withdrawn", "writes the whole protection"))
        self.assertIsNone(self.read_with("0.57.0 tap other\n0.56.0 protect x\n"))

    def test_an_unreadable_list_is_not_a_permission(self) -> None:
        self.assertEqual(self.read_with(None)[0], "unread")

    def test_the_list_on_main_withdraws_every_version_that_put_the_whole_protection(self) -> None:
        text = (ROOT / "WITHDRAWN").read_text(encoding="utf-8")
        listed = {line.split()[0] for line in text.splitlines() if line and not line.startswith("#")}
        self.assertIn("0.43.0", listed)
        self.assertIn("0.57.0", listed)
        self.assertNotIn("0.57.1", listed)
        self.assertEqual(self.read_with(text)[0], "withdrawn")


class LegAliasTest(unittest.TestCase):
    """The old names were aliases from 0.57.0 to 0.59.2, and left in 0.60.0.

    maelys-egress: narrow, adopt, merge, widen leaves main requiring less for
    a whole pull request, and begins with the gesture an agent's guard
    refuses. With the old names still reported, the adoption lost nothing
    and one write moved the protection from each alias to its leg. Once no
    branch of the fleet required an old name -- measured on 2026-09-16 and
    again the day of the cut -- they left, announced a version early; the
    reader of `# alias:` lines stays, and finds none.
    """

    WORKFLOW = (ROOT / ".github" / "workflows" / "check-product.yml").read_text(encoding="utf-8")

    def test_no_alias_remains_and_the_reader_finds_none(self) -> None:
        self.assertEqual(MODULE.check_product_aliases(), [])
        self.assertNotIn("alias", self.WORKFLOW)
        self.assertEqual(MODULE.check_product_legs(), ["linux", "linux-arm64", "macos"])
        # The announcement did not outlive the version that honoured it.
        self.assertFalse([entry for entry in MODULE.COMING if entry[0] == "0.60.0"])

    def protect(self, required, seen, apply=False):
        fake = FakeProtection(required)
        # The real workflow declares all three current legs. The old harness
        # replaced that reader with a two-leg list.
        seen = [*seen, "check / check (linux-arm64)"]
        host = protection_host(fake.read(""), seen=seen, write=fake.write if apply else None)
        original_read = host.reader
        host.reader = lambda path: fake.read(path) if path.endswith("/protection") else original_read(path)
        with workflow_project() as project, using_host(host):
            invocation = type("I", (), {"operands": [str(project)],
                "flag": lambda self, name: apply and name == "--apply",
                "option": lambda self, name, default="": default, "format": "json"})()
            return MODULE.handle_protect(invocation), [body for _, _, body in host.writes]

    def test_an_old_name_still_required_is_stale_and_never_dropped_silently(self) -> None:
        # What the 0.60.0 Impact line says: a branch that still requires an
        # old name blocks every pull request, protect names it, and --apply
        # does not narrow by itself.
        old = ["check / check (ubuntu-26.04)", "check / check (macos-15)", "mine"]
        seen = ["check / check (linux)", "check / check (macos)", "mine"]
        (report, code), written = self.protect(old, seen)
        self.assertEqual(report["replaced"], [])
        self.assertEqual(sorted(report["requiredButNeverRun"]), ["check / check (macos-15)", "check / check (ubuntu-26.04)"])
        self.assertEqual(sorted(report["dropping"]), ["check / check (macos-15)", "check / check (ubuntu-26.04)"])
        self.assertEqual(written, [])
        text = MODULE.text_protect(report)
        self.assertIn("STALE    check / check (ubuntu-26.04) is required and no run", text)
        self.assertIn("DROP     check / check (macos-15) is required and this plan leaves it out", text)
        with self.assertRaises(MODULE.Failure) as refused:
            self.protect(old, seen, apply=True)
        self.assertIn("--allow-narrow", refused.exception.hint)

    def test_the_output_keeps_what_the_branch_required_before(self) -> None:
        old = ["check / check (linux)", "mine"]
        seen = ["check / check (linux)", "check / check (macos)", "mine"]
        (report, _), _ = self.protect(old, seen)
        self.assertEqual(report["before"]["required"], ["check / check (linux)", "mine"])
        self.assertEqual(report["before"]["rulesets"], [])
        self.assertIsInstance(report["before"]["classic"], dict)
        self.assertIn("enforce_admins", report["before"]["classic"])
        self.assertNotIn("before   ", MODULE.text_protect(report))
        report["applied"] = True
        self.assertIn('before   {"classic": {', MODULE.text_protect(report))

    def test_a_write_that_moves_any_other_setting_fails_loudly(self) -> None:
        """The property maelys-oci asked for: after --apply, no setting but the
        checks differs from before. A GitHub that turned linear history off
        during the write is caught by the re-read, whatever the write was."""
        required = ["check / check (linux)", "check / check (macos)"]
        seen = ["check / check (linux)", "check / check (macos)"]
        for moved in ({"required_linear_history": {"enabled": False}},
                      {"required_status_checks": {"strict": False, "contexts": []}},
                      {"required_pull_request_reviews": {"required_approving_review_count": 0}}):
            fake = FakeProtection(required, strict=True, drift=moved)
            with self.assertRaises(MODULE.Failure) as refusal:
                self.protect_with_fake(fake, seen)
            self.assertIn("changed beyond what the plan said", refusal.exception.message, moved)

    def test_the_plan_names_every_setting_it_keeps_and_every_one_it_creates(self) -> None:
        """maelys-datalog: a new protection received linear history off with
        no line saying so."""
        fake = FakeProtection(["check / check (ubuntu-26.04)"], strict=True)
        report = self.protect_with_fake(fake, ["check / check (linux)"], apply=False)
        self.assertEqual(report["keeps"]["strict"], True)
        self.assertEqual(report["keeps"]["required_linear_history"], True)
        self.assertIn("keeps    every other setting as it is: strict=true", MODULE.text_protect(report))
        absent = self.protect_with_fake(None, ["check / check (linux)"], apply=False)
        self.assertEqual(absent["creates"]["required_linear_history"], False)
        self.assertIn("create   required_linear_history=false", MODULE.text_protect(absent))

    def protect_with_fake(self, fake, seen, apply=True):
        host = protection_host(fake.read("") if fake else ("absent", None),
                               seen=[*seen, *ALL_LEGS], write=fake.write if fake and apply else None)
        original_read = host.reader
        host.reader = lambda path: fake.read(path) if fake and path.endswith("/protection") else original_read(path)
        with workflow_project() as project, using_host(host):
            invocation = type("I", (), {"operands": [str(project)],
                "flag": lambda self, name: apply and name == "--apply",
                "option": lambda self, name, default="": default, "format": "json"})()
            return MODULE.handle_protect(invocation)[0]

    def test_a_yaml_comment_is_not_a_command(self) -> None:
        """agent-cli-spec: `sanitizer_command: ''   # no compiled code here`."""
        for raw, value in (("''   # no compiled code here", "''"), ('""', '""'), ("make asan  # both", "make asan"),
                           ("'a # b'", "'a # b'"), ("'it''s # x'  # c", "'it''s # x'"), ("|", "|"),
                           ("make a#b", "make a#b")):
            self.assertEqual(MODULE.yaml_scalar(raw), value, raw)

    def test_a_turned_off_job_already_required_is_kept_and_blocks_no_swap(self) -> None:
        """The refusal agent-cli-spec met after merging its adoption: sanitizers
        never runs there, its ruleset requires it, and protect --apply said no
        merged pull request had run it."""
        old = ["check / check (linux)", "check / check (macos)", "check / sanitizers"]
        seen = ["check / check (linux)", "check / check (macos)"]
        (report, code), written = self.protect(old, seen, apply=True)
        self.assertEqual(report["kept"], ["check / sanitizers"])
        self.assertEqual(report["requiredButNeverRun"], [])
        self.assertEqual(sorted(written[0]["contexts"]),
                         ["check / check (linux)", "check / check (linux-arm64)", "check / check (macos)", "check / sanitizers"])

    def protect_with(self, reads, required, seen, flags=()):
        """protect --apply with each GitHub read answered by the same host."""
        commands = []
        def write(method, endpoint, body):
            commands.append(((method, endpoint), body))
            if hasattr(reads, "written"):
                reads.written(body)
            return subprocess.CompletedProcess([], 0, "", "")
        host = protection_host(("absent", None), read=reads, write=write)
        on = {"--apply", *flags}
        with workflow_project() as project, using_host(host):
            invocation = type("I", (), {"operands": [str(project)], "flag": lambda self, name: name in on,
                                        "option": lambda self, name, default="": default, "format": "json"})()
            return MODULE.handle_protect(invocation)[0], commands

    @staticmethod
    def answers(required, seen, strict=True, timeout=()):
        held = {"required_status_checks": {"strict": strict, "contexts": list(required)}}

        def read(path):
            if any(part in path for part in timeout):
                return "unreadable", None
            if path.endswith("/protection"):
                return "ok", json.loads(json.dumps(held))
            if "/rules/branches/" in path:
                return "ok", []
            if "pulls?" in path:
                return "ok", [{"merged_at": "x", "head": {"sha": "abc"}}]
            if "/check-runs" in path:
                return "ok", {"check_runs": [{"name": name, "conclusion": "success"} for name in [*seen, "check / check (linux-arm64)", "check / check (macos)"]]}
            return "ok", {}
        read.written = lambda body: held["required_status_checks"].update(body)
        return read

    def test_nothing_is_written_on_a_protection_github_did_not_answer_for(self) -> None:
        """maelys-system: a timeout made a protected main look open, and the plan
        proposed four contexts of seventeen."""
        required = ["check / check (ubuntu-26.04)", "mutation", "macos-gates"]
        seen = ["check / check (linux)", "check / check (ubuntu-26.04)", "mutation", "macos-gates"]
        for timeout in (("/protection",), ("/check-runs",), ("pulls?",)):
            with self.assertRaises(MODULE.Failure) as refusal:
                self.protect_with(self.answers(required, seen, timeout=timeout), required, seen)
            self.assertIn("did not answer", refusal.exception.message, timeout)

    def test_apply_does_not_narrow_by_itself(self) -> None:
        required = ["check / check (ubuntu-26.04)", "mutation", "macos-gates"]
        seen = ["check / check (linux)", "check / check (ubuntu-26.04)", "mutation"]
        with self.assertRaises(MODULE.Failure) as refusal:
            self.protect_with(self.answers(required, seen), required, seen)
        self.assertIn("macos-gates", refusal.exception.message)
        report, commands = self.protect_with(self.answers(required, seen), required, seen, flags=("--allow-narrow",))
        self.assertTrue(report["applied"])

    def test_an_existing_protection_changes_in_its_checks_alone(self) -> None:
        """maelys-oci: "require branches to be up to date" went from true to
        false under a plan that named three replacements."""
        required = ["check / check (linux)", "mutation"]
        seen = ["check / check (linux)", "mutation"]
        report, commands = self.protect_with(self.answers(required, seen, strict=True), required, seen)
        command, body = commands[0]
        self.assertEqual(command, ("PATCH", "repos/o/r/branches/main/protection/required_status_checks"))
        self.assertEqual(body, {"strict": True, "contexts": [*ALL_LEGS, "mutation"]})

    def test_before_the_adoption_merges_the_swap_is_still_refused(self) -> None:
        with self.assertRaises(MODULE.Failure):
            self.protect(["check / check (ubuntu-26.04)"], ["check / check (ubuntu-26.04)"], apply=True)

    def test_an_adoption_refuses_a_branch_requiring_an_old_name(self) -> None:
        # Until 0.59.2 the aliases were produced and the guard let them be;
        # since 0.60.0 nothing produces them, and the guard names each.
        required = ["check / check (ubuntu-26.04)", "check / check (ubuntu-26.04-arm)",
                    "check / check (macos-15)", "check / check (gone)"]
        with workflow_project() as project, using_host(protection_host(
                ("ok", {"required_status_checks": {"contexts": required}}))):
            self.assertEqual(MODULE.vanishing_contexts(project),
                             ["check / check (gone)", "check / check (macos-15)", "check / check (ubuntu-26.04)",
                              "check / check (ubuntu-26.04-arm)"])


class ProtectionContextsTest(unittest.TestCase):
    """The names a branch protection should require, derived and not typed.

    A protection requires a check by its name, and those names are the
    socle's jobs prefixed by the job that calls them. Typed by hand they are
    a copy of what some pin produced on the day somebody looked; the socle
    can compute them, and must, because it is the socle that renames them.
    """

    CALL = """name: ci

jobs:
  %s:
    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@%s # v9.9.9
    with:
      product: maelys-fixture
%s
  mine:
    runs-on: ubuntu-26.04
"""

    def product(self, caller: str = "check", inputs: str = "") -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-protect-test."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / ".github" / "workflows" / "ci.yml").write_text(
            self.CALL % (caller, "0" * 40, inputs), encoding="utf-8")
        return work

    def test_the_legs_come_from_the_workflow_not_from_a_list(self) -> None:
        legs = MODULE.check_product_legs()
        self.assertTrue(legs, "check-product.yml must name its legs")
        self.assertIn("macos", legs)

    def test_the_calling_job_prefixes_every_name(self) -> None:
        """Some products call it check; maelys-egress calls it socle."""
        caller, contexts = MODULE.socle_check_contexts(self.product("socle"))
        self.assertEqual(caller, "socle")
        for name in contexts:
            self.assertTrue(name.startswith("socle / "), name)

    def test_sanitizers_is_opt_out_and_fuzz_is_opt_in(self) -> None:
        """Their inputs have opposite defaults, and both decide a job."""
        _, plain = MODULE.socle_check_contexts(self.product())
        self.assertIn("check / sanitizers", plain)
        self.assertNotIn("check / fuzz", plain)
        _, fuzzing = MODULE.socle_check_contexts(self.product(inputs="      fuzz_command: make fuzz-smoke\n"))
        self.assertIn("check / fuzz", fuzzing)
        _, quiet = MODULE.socle_check_contexts(self.product(inputs="      sanitizer_command: ''\n"))
        self.assertNotIn("check / sanitizers", quiet)

    def test_a_block_scalar_is_a_value_and_not_an_absence(self) -> None:
        """`sanitizer_command: |` says the value is below, not that there is none.

        Reading it as empty made the socle announce that two protected
        repositories required a job that never runs, when it runs and passes
        on every pull request they have.
        """
        _, contexts = MODULE.socle_check_contexts(
            self.product(inputs="      sanitizer_command: |\n        make asan\n"))
        self.assertIn("check / sanitizers", contexts)

    def test_a_product_that_does_not_call_the_socle_has_no_socle_contexts(self) -> None:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-protect-test."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / ".github" / "workflows" / "ci.yml").write_text("name: ci\njobs:\n  mine:\n    runs-on: x\n",
                                                               encoding="utf-8")
        self.assertEqual(MODULE.socle_check_contexts(work), ("", []))


class RepositoryChecksTest(unittest.TestCase):
    """One reader of the repository, and no way for a caller to pass half of it."""

    def test_it_takes_the_declaration_and_not_a_handful_of_fields(self) -> None:
        """cut passed two of four, so every product with a formula in the tap
        was told under `cut` that nothing declared it, remedy inverted, at the
        moment of cutting a release. An optional parameter is a way to lose
        something quietly."""
        source = "\n".join(path.read_text(encoding="utf-8") for path in
                           [CLI, *sorted((ROOT / "bin" / "maelys_socle").glob("*.py"))])
        self.assertIn("def repository_checks(decl: Declarations)", source)
        callers = [line.strip() for line in source.splitlines() if "repository_checks(" in line
                   and not line.strip().startswith("def ")]
        self.assertEqual(len(callers), 2)
        for caller in callers:
            self.assertIn("repository_checks(decl)", caller, caller)


class TapSecretsTest(unittest.TestCase):
    """Whether the tap jobs will hold their credentials, before the tag.

    The tap pushes only when tap_token reaches the job, and the socle has
    always said so. What it did not say was beforehand: a product read two
    green publish jobs of its own release, pushed nothing, and paid a full
    replay on four macOS runners. The organisation secret was limited to
    selected repositories, and the list still named the archived repository
    it had been renamed from.
    """

    def secrets(self, own: list, org: list, state: str = "ok") -> dict:
        return {"repos/maelys-dev/p/actions/secrets": (state, {"secrets": [{"name": n} for n in own]}),
                "repos/maelys-dev/p/actions/organization-secrets":
                    (state, {"secrets": [{"name": n} for n in org]})}

    def read(self, contents: dict, formulas=("libmaelys-p",)):
        with using_host(FakeHost(read=lambda path: contents.get(path, ("absent", None)))):
            return MODULE.tap_secrets("maelys-dev/p", list(formulas))

    def test_a_repository_that_sees_both_is_told_it_will_push(self) -> None:
        found = self.read(self.secrets([], ["HOMEBREW_TAP_TOKEN", "HOMEBREW_TAP_SIGNING_KEY"]))
        self.assertEqual([status for status, _ in found], ["ok"])
        self.assertIn("will push", found[0][1])

    def test_a_missing_token_is_a_violation_and_says_what_to_do(self) -> None:
        """The incident, seen from the end the job will be standing at."""
        found = self.read(self.secrets([], ["HOMEBREW_TAP_SIGNING_KEY"]))
        self.assertEqual(found[0][0], "fail")
        self.assertIn("push nothing", found[0][1])
        self.assertIn("limited to selected repositories has to name this one", found[0][1])

    def test_a_missing_signing_key_is_a_note_and_not_a_violation(self) -> None:
        """Without it the tap still publishes; the commit is unsigned."""
        found = self.read(self.secrets([], ["HOMEBREW_TAP_TOKEN"]))
        self.assertEqual([status for status, _ in found], ["ok", "note"])
        self.assertIn("unsigned", found[1][1])

    def test_a_secret_of_the_repository_counts_as_much(self) -> None:
        """A product may hold its own rather than share the organisation's."""
        found = self.read(self.secrets(["HOMEBREW_TAP_TOKEN", "HOMEBREW_TAP_SIGNING_KEY"], []))
        self.assertEqual([status for status, _ in found], ["ok"])

    def test_a_product_with_no_formula_is_not_asked(self) -> None:
        self.assertEqual(self.read(self.secrets([], []), formulas=()), [])

    def test_a_refusal_to_answer_is_not_an_absence(self) -> None:
        """403 and 404 are different facts, here as everywhere else."""
        found = self.read({})
        self.assertEqual([status for status, _ in found], ["note"])
        self.assertIn("could not be read", found[0][1])


class ChannelVisibilityTest(unittest.TestCase):
    """Which package a declared channel publishes into, and who can install it.

    A package's visibility is its own and does not follow the repository's:
    it starts private and stays private when the repository goes public. A
    product published eleven versions into one nobody outside the
    organisation could install, with every job green -- the release says
    published, the registry holds it, and `npm install` from outside answers
    404. There is no red anywhere in that story.
    """

    PATH = "orgs/maelys-dev/packages?package_type=npm&per_page=100"

    def read(self, answer, channels=(("npm", "github-packages"),)):
        with using_host(FakeHost({self.PATH: answer})):
            return MODULE.channel_visibility("maelys-dev/p", list(channels))

    def package(self, visibility: str, repository: str = "maelys-dev/p") -> dict:
        return {"name": "@maelys/p", "visibility": visibility,
                "repository": {"full_name": repository}}

    def test_a_private_package_is_named_and_is_not_a_violation(self) -> None:
        """A private package is a legitimate choice. What was missing is that
        nobody was told which one they had."""
        found = self.read(("ok", [self.package("private")]))
        self.assertEqual([status for status, _ in found], ["note"])
        self.assertIn("@maelys/p, which is private", found[0][1])
        self.assertIn("cannot install it", found[0][1])

    def test_a_public_package_is_said_so(self) -> None:
        found = self.read(("ok", [self.package("public")]))
        self.assertEqual([status for status, _ in found], ["ok"])
        self.assertIn("which is public", found[0][1])

    def test_a_package_of_another_repository_is_not_this_one(self) -> None:
        """The organisation's packages of a type are listed together, and
        every product of the fleet publishes npm."""
        found = self.read(("ok", [self.package("public", "maelys-dev/other")]))
        self.assertEqual([status for status, _ in found], ["note"])
        self.assertIn("holds no npm package", found[0][1])

    def test_a_refusal_to_answer_is_not_an_absence(self) -> None:
        found = self.read(("unreadable", None))
        self.assertEqual([status for status, _ in found], ["note"])
        self.assertIn("could not be read", found[0][1])

    def test_a_product_with_no_channel_is_not_asked(self) -> None:
        self.assertEqual(self.read(("ok", []), channels=()), [])


class TapDriftTest(unittest.TestCase):
    """What the tap serves for a repository, against what it declares.

    The tap is one repository for every product, so no product can see this
    from its own packaging; the socle pushes to it and reads the
    declaration, and is the only place the two sit together.
    """

    def formulas(self, *entries: tuple) -> dict:
        listing = [{"name": f"{name}.rb", "type": "file"} for name, _, _ in entries]
        contents = {f"repos/maelys-dev/homebrew-tap/contents/Formula": ("ok", listing)}
        for name, repository, version in entries:
            body = FORMULA % (repository, repository, version, "0" * 64)
            contents[f"repos/maelys-dev/homebrew-tap/contents/Formula/{name}.rb"] = (
                "ok", {"encoding": "base64", "content": base64.b64encode(body.encode()).decode()})
        return contents

    def drift(self, contents: dict, repository: str, declared: list):
        with using_host(FakeHost(read=lambda path: contents.get(path, ("absent", None)))):
            return MODULE.tap_drift(repository, declared)

    def test_a_formula_nobody_declares_is_named_with_what_it_serves(self) -> None:
        contents = self.formulas(("maelys-datalog", "maelys-datalog", "v0.1.0-alpha.3"))
        found = self.drift(contents, "maelys-dev/maelys-datalog", [])
        self.assertEqual(len(found), 1)
        status, message = found[0]
        self.assertEqual(status, "note")
        self.assertIn("Formula/maelys-datalog.rb at v0.1.0-alpha.3", message)
        # What a person would type to get the stale thing.
        self.assertIn("brew install maelys-dev/tap/maelys-datalog", message)

    def test_a_declared_formula_is_not_drift(self) -> None:
        contents = self.formulas(("libmaelys-json", "maelys-json", "v0.2.0"))
        self.assertEqual(self.drift(contents, "maelys-dev/maelys-json", ["libmaelys-json"]), [])

    def test_a_formula_of_another_repository_is_not_this_product_s(self) -> None:
        """The tie is the repository the url names, never the formula's name."""
        contents = self.formulas(("libmaelys-json", "maelys-json", "v0.2.0"))
        self.assertEqual(self.drift(contents, "maelys-dev/maelys-datalog", []), [])

    def test_a_renamed_formula_leaves_the_old_one_behind(self) -> None:
        contents = self.formulas(("maelys-json", "maelys-json", "v0.1.0"),
                                 ("libmaelys-json", "maelys-json", "v0.2.0"))
        found = self.drift(contents, "maelys-dev/maelys-json", ["libmaelys-json"])
        self.assertEqual(len(found), 1)
        self.assertIn("Formula/maelys-json.rb at v0.1.0", found[0][1])
        self.assertIn("declares libmaelys-json instead", found[0][1])

    def test_a_tap_that_cannot_be_read_says_so_rather_than_passing(self) -> None:
        found = self.drift({}, "maelys-dev/maelys-json", [])
        self.assertEqual(len(found), 1)
        self.assertIn("could not be read", found[0][1])


class RunnerDeclarationTest(unittest.TestCase):
    """[runners], and the one line adopt keeps in a ci.yml it does not own."""

    def parse(self, text: str):
        return MODULE.parse_release(textwrap.dedent(text))

    def test_labels_are_read(self) -> None:
        *_, runner, runners, _, _, _, _, unknown, _, _ = self.parse("[runners]\nmacos self-hosted macOS ARM64\n")
        self.assertEqual(runner, ["self-hosted", "macOS", "ARM64"])
        self.assertEqual(runners, {"macos": ["self-hosted", "macOS", "ARM64"]})
        self.assertEqual(unknown, [])

    def test_the_two_linux_legs_are_declarable_too(self) -> None:
        """A private repository that forbids hosted runners could point the
        macOS leg elsewhere and not the two Linux ones, which are half its
        matrix and all of its fuzz and sanitizers jobs."""
        *_, runners, _, _, _, _, unknown, _, _ = self.parse(
            "[runners]\nlinux-x86_64 self-hosted Linux X64\nlinux-arm64 lima-arm64\n")
        self.assertEqual(runners, {"linux-x86_64": ["self-hosted", "Linux", "X64"],
                                   "linux-arm64": ["lima-arm64"]})
        self.assertEqual(unknown, [])
        # macos keeps its bare word: ten repositories already declare it, and
        # renaming a key of a file the socle reads refuses them all at once.
        self.assertEqual(MODULE.SOCLE_RUNNER_KEYS, ("macos", "linux-x86_64", "linux-arm64"))

    def test_one_label_is_a_string_and_several_are_an_array(self) -> None:
        """runs-on takes either, and the difference is not cosmetic."""
        self.assertEqual(MODULE.runner_json(["macos-15"]), '"macos-15"')
        self.assertEqual(MODULE.runner_json(["self-hosted", "macOS"]), '["self-hosted", "macOS"]')

    def test_the_section_is_refused_when_it_makes_no_sense(self) -> None:
        for text, expected in (("[runners]\nbsd ubuntu-26.04\n", "knows macos, linux-x86_64, linux-arm64"),
                               ("[runners]\nmacos\n", "names no label"),
                               ("[runners]\nmacos a\nmacos b\n", "holds one macos line"),
                               ("[runners]\nmacos self hosted!\n", "runner label is")):
            with self.assertRaises(ValueError) as refusal:
                self.parse(text)
            self.assertIn(expected, str(refusal.exception))

    def test_it_reaches_the_tap_job_of_release_yml(self) -> None:
        declaration = MODULE.Declarations(pathlib.Path("."), "maelys-fixture")
        declaration.formulas = ["maelys-fixture"]
        declaration.runners = {"macos": ["self-hosted", "macOS", "ARM64"],
                               "linux-x86_64": ["self-hosted", "Linux"]}
        workflow = MODULE.release_workflow(declaration, "0" * 40, "v9.9.9", "9.9.9")
        self.assertIn("""      macos_runner: '["self-hosted", "macOS", "ARM64"]'""", workflow)
        # And not the Linux one in the tap's own call: tap.yml renders a
        # formula on macOS and declares no other input, so a line it does not
        # know fails the run at startup, before a single job. The release
        # job above it takes all three.
        tap = workflow.split("  tap-maelys-fixture:", 1)[1]
        self.assertNotIn("linux_x86_64_runner", tap)
        release = workflow.split("  release:", 1)[1].split("\n\n  ", 1)[0]
        self.assertIn("linux_x86_64_runner", release)

    def test_adopt_writes_the_line_under_the_socle_call(self) -> None:
        written = MODULE.ci_macos_runner(CI_CALL, {"macos": ["self-hosted", "macOS", "ARM64"]})
        # First line of the with: block, so the placement is the same
        # whatever inputs the product has added below it.
        self.assertIn("""    with:
      macos_runner: '["self-hosted", "macOS", "ARM64"]'
      product: maelys-fixture""", written)
        # The product's own job is not the socle's business.
        self.assertIn("  mine:\n    runs-on: ubuntu-26.04\n", written)

    def test_an_empty_declaration_takes_the_line_away(self) -> None:
        """The declaration decides in both directions, or it decides nothing."""
        written = MODULE.ci_macos_runner(CI_CALL, ["macos-15"])
        self.assertEqual(MODULE.ci_macos_runner(written, []), CI_CALL)

    def test_a_declared_runner_replaces_the_one_already_there(self) -> None:
        first = MODULE.ci_macos_runner(CI_CALL, ["macos-15"])
        second = MODULE.ci_macos_runner(first, ["self-hosted", "macOS"])
        self.assertNotIn("macos-15", second)
        self.assertEqual(second.count("macos_runner:"), 1)

    def test_a_ci_that_does_not_call_the_socle_is_left_alone(self) -> None:
        foreign = "name: ci\n\njobs:\n  mine:\n    runs-on: ubuntu-26.04\n"
        self.assertEqual(MODULE.ci_macos_runner(foreign, ["self-hosted"]), foreign)


class SbomDeclarationTest(unittest.TestCase):
    """The [sbom] section of the declaration file."""

    def parse(self, text: str):
        return MODULE.parse_release(textwrap.dedent(text))

    def test_one_glob_is_read(self) -> None:
        _, _, _, _, sbom, _, _, _, _, _, _, unknown, _, _ = self.parse("""\
            [sbom]
            *.spdx.json
            """)
        self.assertEqual(sbom, "*.spdx.json")
        self.assertEqual(unknown, [])

    def test_a_second_glob_is_refused(self) -> None:
        """An attestation carries one predicate, so the entry names one document."""
        with self.assertRaises(ValueError) as refusal:
            self.parse("[sbom]\n*.spdx.json\n*.cdx.json\n")
        self.assertIn("holds one glob", str(refusal.exception))

    def test_the_section_is_optional(self) -> None:
        _, _, _, _, sbom, _, _, _, _, _, _, _, _, _ = self.parse("[manifest]\n*.wasm\n")
        self.assertEqual(sbom, "")

    def test_it_renders_into_the_workflow(self) -> None:
        declaration = MODULE.Declarations(pathlib.Path("."), "maelys-fixture")
        declaration.sbom_pattern = "*.spdx.json"
        workflow = MODULE.release_workflow(declaration, "0" * 40, "v9.9.9", "9.9.9")
        self.assertIn("      sbom_pattern: '*.spdx.json'", workflow)

    def test_nothing_is_rendered_without_it(self) -> None:
        declaration = MODULE.Declarations(pathlib.Path("."), "maelys-fixture")
        self.assertNotIn("sbom_pattern", MODULE.release_workflow(declaration, "0" * 40, "v9.9.9", "9.9.9"))


class ChannelMarkerTest(unittest.TestCase):
    """The marker the rehearsal composes is the one channel.yml attaches.

    `channel.yml` only ever runs on a signed tag, so its jq expression had
    never been compared with anything. The rehearsal composes the same object
    in Python, and these fix the two shapes to each other.
    """

    def test_the_record_says_these_four_things_about_a_registry(self) -> None:
        """A fixed list, or the comparison proves nothing common.

        Without one, `rehearse --channel` compares what the script chose to
        write with what the release carries — a product compared with
        itself. One product removed a field of its own to make its rehearsal
        agree, which is the shape of the problem.
        """
        marker = MODULE.channel_marker("maelys-datalog", "v0.2.0", "npm",
                                       {"registry": "npm.pkg.github.com", "package": "@maelys/datalog"},
                                       "(rehearsal)")
        self.assertEqual(marker["product"], "maelys-datalog")
        self.assertEqual(marker["registry"], "npm.pkg.github.com")
        self.assertEqual(marker["run"], "(rehearsal)")
        self.assertRegex(marker["published"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_a_record_cannot_overwrite_what_the_release_says(self) -> None:
        """The former expression put the record last, so a record naming
        `tag` rewrote the tag of the release it was attached to."""
        marker = MODULE.channel_marker("p", "v1.0.0", "npm",
                                       {"channel": "npm-next", "tag": "v9.9.9", "registry": "r"}, "run")
        self.assertEqual(marker["channel"], "npm")
        self.assertEqual(marker["tag"], "v1.0.0")
        self.assertEqual(marker["registry"], "r")

    def test_what_a_marker_drops_is_named_and_not_refused(self) -> None:
        """Dropped, because the publication has already happened when this is
        read: a refusal here leaves a registry holding a version and a
        release saying nothing about it."""
        kept, dropped = MODULE.kept_record({"registry": "r", "already_published": True,
                                            "version": "1.0.0", "count": 3})
        self.assertEqual(kept, {"registry": "r", "version": "1.0.0"})
        self.assertEqual(dropped, ["already_published", "count"])
        # A known field of the wrong type is not a marker field either: a
        # marker is read by eye as often as by jq.
        self.assertEqual(MODULE.kept_record({"version": 1.0}), ({}, ["version"]))
        self.assertEqual(MODULE.kept_record([1, 2]), ({}, []))

    def test_the_workflow_keeps_the_same_four_fields(self) -> None:
        """The parity that matters: the rehearsal composes in Python and the
        release composes in jq, and a list kept in two places drifts."""
        channel = (ROOT / ".github" / "workflows" / "channel.yml").read_text(encoding="utf-8")
        listed = re.findall(r'IN\(([^)]*)\)', channel)
        self.assertTrue(listed, channel)
        for names in listed:
            self.assertEqual([name.strip('"') for name in names.split(",")],
                             list(MODULE.CHANNEL_RECORD_FIELDS))

    def test_what_counts_as_a_disagreement_with_the_attached_marker(self) -> None:
        """published and run differ by construction; anything else means the
        script records something the release does not carry."""
        attached = {"product": "p", "tag": "v1.0.0", "channel": "npm", "published": "2026-09-01T00:00:00Z",
                    "run": "https://example.invalid/1", "registry": "npm.pkg.github.com"}
        same = dict(attached, published="2026-09-11T12:00:00Z", run="(rehearsal)")
        self.assertEqual(MODULE.marker_differences(same, attached), [])
        self.assertEqual(MODULE.marker_differences(dict(same, registry="registry.npmjs.org"), attached),
                         ["registry"])
        # A field the script stopped recording is a disagreement too.
        missing = {key: value for key, value in same.items() if key != "registry"}
        self.assertEqual(MODULE.marker_differences(missing, attached), ["registry"])

    def test_the_socle_records_the_fact_without_a_record_file(self) -> None:
        """The file is optional: the marker exists because the channel published."""
        marker = MODULE.channel_marker("p", "v1.0.0", "npm", {}, "r")
        self.assertEqual(sorted(marker), ["channel", "product", "published", "run", "tag"])
        self.assertEqual(MODULE.kept_record({}), ({}, []))


class RehearseChannelRefusalTest(unittest.TestCase):
    """What the channel rehearsal refuses before it touches a registry."""

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        self.product.run("adopt", self.dir, "--apply")
        # A GitHub origin, so the refusals under test are reached; nothing
        # here touches the network.
        self.product.git(self.product.dir, "init", "-q")
        self.product.git(self.product.dir, "remote", "add", "origin",
                         "https://github.com/maelys-dev/maelys-fixture.git")
        self.saved = os.environ.get("NODE_AUTH_TOKEN"), os.environ.get("GH_TOKEN")
        for name in ("NODE_AUTH_TOKEN", "GH_TOKEN"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in zip(("NODE_AUTH_TOKEN", "GH_TOKEN"), self.saved):
            if value is not None:
                os.environ[name] = value
        self.product.close()

    def test_a_channel_the_product_does_not_declare(self) -> None:
        error = self.product.json("rehearse", self.dir, "--channel", "pypi", "--tag", "v1.2.3",
                                  expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("declares no pypi channel", error["message"])
        self.assertIn("it declares npm", error["message"])

    def test_a_tag_that_is_not_one(self) -> None:
        error = self.product.json("rehearse", self.dir, "--channel", "npm", "--tag", "0.2.0",
                                  expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")

    def test_the_token_is_the_operator_s_and_never_the_socle_s(self) -> None:
        """The socle never reads a token from a file and never supplies one."""
        decl = MODULE.read_declarations(self.product.dir, "maelys-fixture", "maelys-release")
        invocation = CutTest.Stub(**{"--channel": "npm", "--tag": "v1.2.3"})
        with using_host(FakeHost()), self.assertRaises(MODULE.Failure) as refusal:
            MODULE.rehearse_channel(invocation, self.product.dir, "maelys-fixture", decl, io.StringIO())
        self.assertEqual(refusal.exception.code, "PRECONDITION_FAILED")
        self.assertIn("never supplies it", refusal.exception.message)

    def test_channel_and_tag_need_each_other(self) -> None:
        error = self.product.json("rehearse", self.dir, "--channel", "npm", expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("--tag", error["message"])

    def test_a_rehearse_with_neither_a_target_nor_a_channel(self) -> None:
        error = self.product.json("rehearse", self.dir, expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")
        self.assertIn("--channel", error["message"])


class RenderAndTapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()

    def tearDown(self) -> None:
        self.product.close()

    def test_render(self) -> None:
        template = self.product.work / "formula.rb.in"
        template.write_text('url "@URL@"\nsha256 "@SHA256@"\n')
        output = self.product.work / "out" / "formula.rb"
        data = self.product.json("render", str(template), str(output), "URL=https://x", "SHA256=abc")["data"]
        self.assertEqual(data["placeholders"], ["SHA256", "URL"])
        self.assertEqual(output.read_text(), 'url "https://x"\nsha256 "abc"\n')
        error = self.product.json("render", str(template), str(output), "URL=https://x", expect=1)["error"]
        self.assertIn("@SHA256@", error["message"])
        error = self.product.json("render", str(template), str(output), "bad", expect=1)["error"]
        self.assertEqual(error["code"], "VALIDATION_FAILED")

    def test_tap_plan_and_apply(self) -> None:
        product = self.product
        source = product.work / "tap-src"
        source.mkdir()
        product.git(source, "init", "-q", "-b", "main")
        (source / "README.md").write_text("tap\n")
        product.git(source, "add", "README.md")
        product.git(source, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
        bare = product.work / "remotes" / "homebrew-tap.git"
        product.git(product.work, "clone", "-q", "--bare", str(source), str(bare))
        formula = product.work / "maelys-fixture.rb"
        formula.write_text("class MaelysFixture < Formula\nend\n")
        env = {**product.env, "TAP_URL": f"file://{bare}", "TAP_REPOSITORY": "maelys-dev/homebrew-tap"}

        def tap(*arguments: str, expect: int = 0) -> dict:
            completed = subprocess.run([str(CLI), "tap", "maelys-fixture", "v1.2.3", str(formula), "--skip-style",
                                        *arguments, "--format", "json", "--compact"], env=env, check=False,
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(completed.returncode, expect, completed.stderr)
            return json.loads(completed.stdout if expect != 1 else completed.stderr)

        planned = tap()["data"]
        self.assertEqual(planned["mode"], "plan")
        self.assertTrue(planned["changed"])
        self.assertIn("+class MaelysFixture", planned["diff"])
        self.assertEqual(product.git(bare, "rev-list", "--count", "main"), "1")
        applied = tap("--apply")["data"]
        self.assertEqual(applied["mode"], "apply")
        self.assertEqual(applied["install"], "brew install maelys-dev/tap/maelys-fixture")
        self.assertEqual(product.git(bare, "rev-list", "--count", "main"), "2")
        self.assertIn("Update maelys-fixture to 1.2.3", product.git(bare, "log", "-1", "--format=%s", "main"))
        self.assertFalse(tap()["data"]["changed"])
        self.assertIn("plans by default", tap("--dry-run", expect=1)["error"]["message"])
        bad_tag = subprocess.run([str(CLI), "tap", "x", "1.2.3", str(formula), "--format", "json"], env=env,
                                 check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(bad_tag.returncode, 1)
        self.assertIn("TAG is", json.loads(bad_tag.stderr)["error"]["message"])


class UnitTest(unittest.TestCase):
    def test_parse_packages(self) -> None:
        self.assertEqual(MODULE.parse_packages("[linux]\na\nb\n[macos]\nc\n"), ("a b", "c"))
        self.assertEqual(MODULE.parse_packages("# only comments\n"), ("", ""))
        for text in ("a\n", "[bsd]\na\n", "[linux]\na b\n", "[linux]\n-a\n"):
            with self.assertRaises(ValueError):
                MODULE.parse_packages(text)

    def test_matrix_values(self) -> None:
        block = "    strategy:\n      matrix:\n        os:\n          - ubuntu-24.04\n          - self-hosted\n"
        self.assertEqual(MODULE.matrix_values(block, "os"), ["self-hosted", "ubuntu-24.04"])
        # The include form names the key on each entry; it is what
        # maelys-datalog writes, and reading only the list form reported it
        # unresolved.
        include = "      matrix:\n        include:\n          - os: ubuntu-24.04\n            target: linux-x86_64\n"
        self.assertEqual(MODULE.matrix_values(include, "os"), ["ubuntu-24.04"])
        self.assertEqual(MODULE.matrix_values("        os: [a, \"b\"]\n", "os"), ["a", "b"])
        # An expression is not a literal and is never guessed at.
        self.assertEqual(MODULE.matrix_values("        os: [${{ inputs.x }}]\n", "os"), [])
        self.assertEqual(MODULE.matrix_values("        other: [a]\n", "os"), [])

    def test_a_label_the_socle_cannot_vouch_for_is_unresolved(self) -> None:
        """A label of the wrong shape is not a label. Accepting one let a jq
        line of the socle's own release.yml pass as a runner, and worse: it
        made `found` non-empty, so a runs-on nobody could read counted as
        resolved and the hole it should have shown disappeared."""
        self.assertEqual(MODULE.matrix_values('        os: [ubuntu-24.04]\n', "os"), ["ubuntu-24.04"])
        jq = '          runner: (if has("runner") then (.runner | tojson) else x end)}]}\n'
        self.assertEqual(MODULE.matrix_values(jq, "runner"), [])

    def test_version_pattern_matches_a_version_and_not_a_longer_one(self) -> None:
        inside = MODULE.version_pattern("0.1.1")
        self.assertTrue(inside.search("v0.1.1"))
        self.assertTrue(inside.search("#define VERSION \"0.1.1\""))
        # The reason the pattern exists at all: a dot that continues the
        # version, and a digit on either side.
        self.assertFalse(inside.search("0.1.10"))
        self.assertFalse(inside.search("10.1.1"))
        self.assertFalse(inside.search("0.1.1.2"))
        # And the reason it stops there. A dash before a version is how a
        # tarball is named and a dot after it is an extension; refusing
        # either lost real carriers of maelys-egress, whose README installs
        # the archive by name.
        self.assertTrue(inside.search("maelys-egress-node-sdk-0.1.1.tar.gz"))
        self.assertTrue(inside.search("pip install maelys-egress==0.1.1"))

    def test_parse_release(self) -> None:
        self.assertEqual(MODULE.parse_release("[targets]\nlinux-arm64\nwasm32 ubuntu-26.04\n"),
                         ([("linux-arm64", ""), ("wasm32", "ubuntu-26.04")], [], [], {}, "", [], {}, False, "", "", "", [], [], []))
        self.assertEqual(MODULE.parse_release("[targets]\nmacos-arm64 self-hosted ARM64\n"),
                         ([("macos-arm64", ["self-hosted", "ARM64"])], [], [], {}, "", [], {}, False, "", "", "", [], [], []))
        self.assertEqual(MODULE.parse_release("[manifest]\n*.wasm\n"), ([], ["*.wasm"], [], {}, "", [], {}, False, "", "", "", [], [], []))
        self.assertEqual(MODULE.parse_release("[channels]\nnpm github-packages\n"),
                         ([], [], [("npm", "github-packages")], {}, "", [], {}, False, "", "", "", [], [], []))
        self.assertEqual(MODULE.parse_release("[gate]\nnone\n"), ([], [], [], {}, "", [], {}, False, "", "none", "", [], [], []))
        self.assertEqual(MODULE.parse_release("[gate]\nreviewer\n"), ([], [], [], {}, "", [], {}, False, "", "reviewer", "", [], [], []))
        self.assertEqual(MODULE.parse_release("# nothing declared\n"), ([], [], [], {}, "", [], {}, False, "", "", "", [], [], []))
        # A section this socle does not know is named and skipped, never fatal:
        # a product pins the socle by commit, and losing its targets in
        # silence on an older socle is worse than an unapplied section.
        self.assertEqual(MODULE.parse_release("[targets]\nlinux-arm64\n[bsd]\nx\n"),
                         ([("linux-arm64", "")], [], [], {}, "", [], {}, False, "", "", "", ["[bsd] at line 3"], [], []))
        # A version materialised twice: the command that regenerates the second.
        self.assertEqual(MODULE.parse_release("[cut]\nafter-version bash scripts/header.sh\n"),
                         ([], [], [], {}, "", [], {}, False, "", "", "bash scripts/header.sh", [], [], []))
        for text in ("linux-arm64\n",                      # outside a section
                     "[targets]\nwasm32\n",                # no runner and no default
                     "[targets]\nWASM\n",                  # not a target name
                     "[targets]\nlinux-arm64\nlinux-arm64\n",   # twice
                     "[targets]\nwasm32 'quoted'\n",       # not a runner label
                     "[manifest]\n*.tar.gz\n",             # already covered
                     "[manifest]\n*.a *.b\n",              # one glob per line
                     "[channels]\nnpm pypi\n",             # a registry the socle cannot reach
                     "[channels]\nnpm\n",                  # names no registry
                     "[channels]\nNPM github-packages\n",  # not a channel name
                     "[channels]\nnpm github-packages\nnpm github-packages\n",  # twice
                     "[gate]\nmaybe\n",                    # not a gate the socle knows
                     "[gate]\nreviewer\nnone\n",           # one line, not two
                     "[cut]\nbefore-version make\n",       # a key [cut] does not know
                     "[cut]\nafter-version\n",             # names no command
                     "[cut]\nafter-version a\nafter-version b\n"):  # one command, not two
            with self.assertRaises(ValueError, msg=text):
                MODULE.parse_release(text)

    def test_the_readme_pointer_never_names_a_destination_a_reader_cannot_open(self) -> None:
        """maelys-docs is private; a public README naming it sends the reader
        to a 404 and plants the private reference the fleet audit blocks on."""
        import tempfile
        data = {"repository": "maelys-dev/maelys-docs",
                "moving": [{"path": "docs/architecture.md"}]}
        for public, expected in ((True, True), (False, False), (None, False)):
            answer = ("unreadable", None) if public is None else ("ok", {"visibility": "public" if public else "private"})
            with using_host(FakeHost({"repos/maelys-dev/maelys-docs": answer})), tempfile.TemporaryDirectory() as clone:
                readme = pathlib.Path(clone) / "README.md"
                readme.write_text("# P\n\nSee [architecture](docs/architecture.md).\n")
                report = MODULE.rewrite_readme(pathlib.Path(clone), "maelys-egress", data)
                written = readme.read_text()
                self.assertEqual("maelys-dev/maelys-docs" in written, expected, written)
                self.assertNotIn("docs/architecture.md", written)
                self.assertIn("Documentation", written)
                if not expected:
                    # An unreachable destination is assumed private, and
                    # the report says what a human must still do.
                    self.assertIn("naming no repository", report)
                    self.assertIn("site", report)

    def test_environment_gate_verifies_the_answer_a_repository_gave(self) -> None:
        """The socle checks the gate a repository asked for; it does not
        choose one in its place. Only a promise unkept is a failure."""
        def reviewers(prevent_self_review=True, login="someone"):
            return {"type": "required_reviewers", "prevent_self_review": prevent_self_review,
                    "reviewers": [{"reviewer": {"login": login}}]}

        bare = {"protection_rules": [], "can_admins_bypass": True}
        armed = {"protection_rules": [reviewers()], "can_admins_bypass": False}
        # A repository that has not chosen is told, never refused.
        self.assertEqual([s for s, _ in MODULE.environment_gate("o/r", bare)], ["note"])
        # One that asked for a reviewer and has none broke its own promise.
        self.assertEqual([s for s, _ in MODULE.environment_gate("o/r", bare, "reviewer")], ["fail"])
        # One that declared it wants none is right, and stays green.
        declared_none = MODULE.environment_gate("o/r", bare, "none")
        self.assertEqual([s for s, _ in declared_none], ["ok"])
        self.assertIn("[gate] none declares", declared_none[0][1])
        self.assertEqual([s for s, _ in MODULE.environment_gate("o/r", armed, "reviewer")], ["ok"])
        # Stricter than declared is not a failure, but it is worth saying.
        self.assertEqual([s for s, _ in MODULE.environment_gate("o/r", armed, "none")], ["ok", "note"])

    def test_environment_gate(self) -> None:
        """The conventions call the release environment the human gate. The
        socle described a gate it never saw closed: measured on the fleet,
        one repository of twelve required a reviewer, and there the reviewer
        may approve their own deployment."""
        def reviewers(prevent_self_review, login="someone"):
            return {"type": "required_reviewers", "prevent_self_review": prevent_self_review,
                    "reviewers": [{"reviewer": {"login": login}}]}

        self.assertEqual(MODULE.environment_gate("o/r", None), [])
        empty = MODULE.environment_gate("o/r", {"protection_rules": [], "can_admins_bypass": True}, "reviewer")
        self.assertEqual([status for status, _ in empty], ["fail"])
        self.assertIn("runs unattended", empty[0][1])
        self.assertIn("gh api -X PUT repos/o/r/environments/release", empty[0][1])

        pause = MODULE.environment_gate("o/r", {"protection_rules": [reviewers(False)],
                                                "can_admins_bypass": True})
        self.assertEqual([status for status, _ in pause], ["ok", "note", "note"])
        self.assertTrue(all("a pause, not a control" in message for _, message in pause[1:]), pause)

        control = MODULE.environment_gate("o/r", {"protection_rules": [reviewers(True, "another")],
                                                  "can_admins_bypass": False})
        self.assertEqual(control, [("ok", "environment release of o/r requires a reviewer: another")])

    def test_managed_block(self) -> None:
        block = "new\n"
        self.assertEqual(MODULE.managed_block(None, block), f"{MODULE.BEGIN}\nnew\n{MODULE.END}\n")
        self.assertEqual(MODULE.managed_block("head\n", block), f"head\n\n{MODULE.BEGIN}\nnew\n{MODULE.END}\n")
        existing = f"head\n{MODULE.BEGIN}\nold\n{MODULE.END}\ntail\n"
        self.assertEqual(MODULE.managed_block(existing, block), f"head\n{MODULE.BEGIN}\nnew\n{MODULE.END}\ntail\n")

    def test_catalog_consistency(self) -> None:
        built_in = {"help", "version", "describe", "completion", "complete.candidates"}
        identifiers = [command["id"] for command in MODULE.APP.catalog]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for command in MODULE.APP.catalog:
            if command["id"] not in built_in:
                self.assertIn(command["id"], MODULE.TEXT)          # every socle command renders text
            self.assertTrue(command["usage"].startswith(" ".join(command["pattern"])))
            if isinstance(command["effect"], dict):
                self.assertTrue(any(item["long"] == "--apply" for item in command["options"]))
            self.assertIn("outputSchema", command)
        self.assertEqual(MODULE.cli.FRAMEWORK.split()[0], "maelys_cli")

    def test_socle_version_is_a_bare_version(self) -> None:
        raw = (ROOT / "VERSION").read_bytes()
        self.assertRegex(raw.decode(), r"^[0-9]+\.[0-9]+\.[0-9]+\n$")            # one line, a real newline
        self.assertNotIn(b"\\", raw)                                                # not a literal backslash-n

    def test_socle_changelog_has_the_dated_entry(self) -> None:
        """The socle holds itself to the product rule it enforces.

        The newest entry may be one ahead of VERSION: `cut` reads the entry
        of the version being released before it writes VERSION, so between
        the two the changelog leads by exactly one release. It never lags,
        and it never repeats a version.
        """
        version = (ROOT / "VERSION").read_text().strip()
        changelog = (ROOT / "CHANGELOG.md").read_text()
        self.assertRegex(changelog, rf"(?m)^## {re.escape(version)} — [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$")
        entries = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+) ", changelog, re.MULTILINE)
        self.assertIn(entries.index(version), (0, 1), f"{version} is not the newest entry nor the one after it")
        self.assertEqual(len(entries), len(set(entries)), "duplicate entries")
        self.assertEqual(entries, sorted(entries, key=MODULE.version_tuple, reverse=True), "entries out of order")

    def test_every_changelog_entry_says_its_impact_on_a_product(self) -> None:
        """One line that answers "must we open a pull request?" in ten seconds.

        Twenty-four versions in three days, and one product opened seven
        adoption pull requests -- each a CI run and a merge -- for a
        mechanism it had used twice, because nothing said which ones reached
        it. Half of them rewrote prose and nothing else. Held from 0.49.0
        on: the entries before it were written without the rule.
        """
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        entries = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+) [^\n]*\n(.*?)(?=^## |\Z)",
                             changelog, re.MULTILINE | re.DOTALL)
        held = [(version, body) for version, body in entries
                if MODULE.version_tuple(version) >= (0, 49, 0)]
        self.assertTrue(held, "no entry is under the rule yet")
        for version, body in held:
            self.assertIn("- **Impact.**", body, f"{version} does not say its impact on a product")

    def test_an_announcement_does_not_outlive_the_version_that_honours_it(self) -> None:
        """The half of "announce a contract change early" that a test can hold.

        A rule that turns into a violation without notice is the cadence
        complaint at its sharpest: a product adopts on Tuesday, conformant,
        and is in violation on Wednesday for something nobody told it was
        coming. `COMING` says it a version early -- and this refuses to let
        the announcement outlive the version that was supposed to honour it,
        so an entry still here when that version ships fails the suite until
        the rule is written or the date is moved, in the open.
        """
        here = MODULE.version_tuple((ROOT / "VERSION").read_text(encoding="utf-8").strip())
        for version, reaches, says in MODULE.COMING:
            self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
            self.assertGreater(MODULE.version_tuple(version), here,
                               f"{version} is announced and has shipped: write the rule or move the date")
            self.assertIn(reaches, ("socle", "channels", "all"))
            self.assertTrue(says.strip(), version)
            # It says what the product must do, not what the socle will feel
            # like doing: the line is read by someone deciding whether to act.
            self.assertGreater(len(says), 80, version)

    def test_linux_baseline_is_ubuntu_26(self) -> None:
        self.assertEqual(MODULE.DEFAULT_IMAGE, "ubuntu:26.04")
        for relative in (".github/workflows/check.yml", ".github/workflows/check-product.yml",
                         ".github/workflows/release.yml"):
            workflow = (ROOT / relative).read_text()
            self.assertIn("ubuntu-26.04", workflow)
            self.assertNotIn("ubuntu-24.04", workflow)
        actionlint = (ROOT / ".github" / "actionlint.yaml").read_text()
        self.assertIn("ubuntu-26.04-arm", actionlint)

    def test_release_workflow_text(self) -> None:
        decl = MODULE.Declarations(pathlib.Path("/p"), "maelys-x")
        decl.dependencies = ["maelys-json"]
        decl.formulas = ["maelys-x"]
        decl.render_command = "sh scripts/render-homebrew-formula.sh TAG OUTPUT"
        text = MODULE.release_workflow(decl, "a" * 40, "v9.9.9", "9.9.9")
        self.assertIn("render_command: sh scripts/render-homebrew-formula.sh TAG OUTPUT maelys-x", text)
        self.assertIn(textwrap.dedent("""\
            dependency_checkout: |
                    sh scripts/checkout-dependency.sh maelys-json
            """).strip(), text)
        self.assertIn("tag: ${{ inputs.tag || github.ref_name }}", text)
        self.assertNotIn("if: github.event_name == 'push'", text)
        self.assertIn("if: needs.release.result == 'success'", text)

    def test_the_bytes_cross_the_run_without_a_writing_job(self) -> None:
        """This reverses 0.15.1, which moved the transport onto a draft
        release to stop depending on the Actions artifact quota. That trade
        bought storage with a token: the job running the product's own
        package_command needed contents: write, which is repository-wide.
        The artifacts are back, kept one day."""
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("ref: ${{ inputs.tag || github.ref }}", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("actions/download-artifact@", text)
        self.assertIn("retention-days: 1", text)
        # the draft still exists, but only inside the one job that may write
        self.assertNotIn("Prepare draft GitHub release", text)
        self.assertIn('gh release edit "$TAG" --repo "${GITHUB_REPOSITORY}" --draft', text)
        self.assertIn("releases/assets/$asset_id", text)
        self.assertIn('--draft=false', text)
        self.assertNotIn('gh release download "$TAG"', text)


if __name__ == "__main__":
    unittest.main()


class OnePullTest(unittest.TestCase):
    """The pull request of a release branch, with the closed ones set aside.

    0.59.0: the first release pull request was closed after its checks
    refused, the branch deleted and cut again; `cut --tag` then counted the
    closed one beside the merged one and refused to sign, with a hint asking
    to close what was already closed.
    """

    def listing(self, pulls):
        def runner(command, cwd=None, env=None):
            self.assertEqual(command[:3], ["gh", "pr", "list"])
            return subprocess.CompletedProcess(command, 0, json.dumps(pulls), "")
        return FakeHost(run=runner)

    def test_a_closed_unmerged_pull_request_is_not_this_release(self) -> None:
        closed = {"number": 124, "url": "u/124", "state": "CLOSED", "mergeCommit": None, "headRefOid": "a" * 40}
        merged = {"number": 126, "url": "u/126", "state": "MERGED", "mergeCommit": {"oid": "b" * 40},
                  "headRefOid": "c" * 40}
        with using_host(self.listing([merged, closed])):
            self.assertEqual(MODULE.one_pull(pathlib.Path("."), "o/r", "release/v0.59.0")["number"], 126)
        with using_host(self.listing([closed])):
            self.assertIsNone(MODULE.one_pull(pathlib.Path("."), "o/r", "release/v0.59.0"))

    def test_two_live_pull_requests_are_still_refused(self) -> None:
        live = [{"number": n, "url": f"u/{n}", "state": "OPEN", "mergeCommit": None, "headRefOid": "a" * 40}
                for n in (1, 2)]
        with using_host(self.listing(live)), self.assertRaises(MODULE.Failure) as refusal:
            MODULE.one_pull(pathlib.Path("."), "o/r", "release/v0.59.0")
        self.assertIn("2 pull requests", refusal.exception.message)


class RepositoryReaderTest(unittest.TestCase):
    """One reader of `repos/{repository}`, and the three ways it did not answer.

    Seven sites read the endpoint and parsed it each for itself; the reader
    keeps what each of them did -- a refusal is not a public repository, an
    absent default branch is `main` -- and does it once.
    """

    def test_an_answer_carries_its_facts(self) -> None:
        # No origin/HEAD at the checkout: cut's reader falls back to GitHub.
        no_head = lambda command, cwd=None, env=None: subprocess.CompletedProcess(command, 1, "", "")
        answered = FakeHost({"repos/o/r": ("ok", {"visibility": "public", "private": False,
                                                    "default_branch": "trunk"})}, run=no_head)
        with using_host(answered):
            found = MODULE.read_repository("o/r")
            self.assertTrue(found.read)
            self.assertEqual((found.visibility, found.private, found.default_branch), ("public", False, "trunk"))
            self.assertTrue(MODULE.destination_is_public("o/r"))
            self.assertEqual(MODULE.default_branch(pathlib.Path("."), "o/r"), "trunk")
        # Three readers, three GETs: each site still reads once, where it did.
        self.assertEqual(answered.reads, ["repos/o/r"] * 3)

    def test_what_github_did_not_say_is_not_an_answer(self) -> None:
        for state in ("absent", "unreadable", "no-gh"):
            with using_host(FakeHost({"repos/o/r": (state, None)})):
                found = MODULE.read_repository("o/r")
                self.assertFalse(found.read, state)
                self.assertEqual((found.visibility, found.private, found.default_branch), ("", False, "main"))
                # The migration asks whether a stranger can read the prose's
                # destination; a refusal is neither yes nor no.
                self.assertIsNone(MODULE.destination_is_public("o/r"))

    def test_a_repository_that_names_no_visibility_answers_nobody(self) -> None:
        # The `public` selector says nothing rather than something on an
        # answer without the field; the migration's reader says False, as
        # both always did.
        with using_host(FakeHost({"repos/o/r": ("ok", {"default_branch": "main"})})):
            self.assertFalse(MODULE.destination_is_public("o/r"))
            self.assertEqual(MODULE.read_repository("o/r").visibility, "")


class DeclaredWordsTest(unittest.TestCase):
    """[ci] own and [docs] named are read once, by the one tolerant scan.

    Three parsers of maelys-release.conf read the two words: parse_release,
    which validates them; own_ci, a copy of the scan for one word; and
    declared_word, the same scan for any. The entry point kept the copy and
    re-read the file for [docs] named at every render and every preflight.
    """

    def work(self, text: str) -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-words."))
        self.addCleanup(shutil.rmtree, work, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / "maelys-release.conf").write_text(text)
        return work

    def test_the_declaration_carries_both_words(self) -> None:
        decl = MODULE.read_declarations(self.work("[ci]\nown\n[docs]\nnamed\n"), "p", "custom")
        self.assertTrue(decl.own_ci)
        self.assertTrue(decl.docs_named)
        decl = MODULE.read_declarations(self.work("[docs]\nunnamed\n"), "p", "custom")
        self.assertFalse(decl.own_ci)
        self.assertFalse(decl.docs_named)

    def test_the_words_hold_when_a_later_section_is_refused(self) -> None:
        # What the scan is for: the ci.yml rule and the managed block need
        # the words even when parse_release refuses the rest of the file.
        decl = MODULE.read_declarations(self.work("[ci]\nown\n[docs]\nnamed\n[cut]\nnonsense\n"), "p", "custom")
        self.assertTrue(decl.own_ci)
        self.assertTrue(decl.docs_named)
        self.assertTrue([c for c in decl.checks if c["status"] == "missing" and "[cut]" in c["message"]])

    def test_the_copy_of_the_scan_is_gone(self) -> None:
        self.assertFalse(hasattr(MODULE, "own_ci"))


class NamingNoPrivateRepositoryTest(unittest.TestCase):
    """The rule of 0.58.0 held for the managed blocks and leaked twice beside them.

    The seeded LICENSING.md named the documentation repository in every new
    product, public or not (maelys-json found it adopting); and `migrate`
    rewrote every path it moved into `maelys-docs/<product>/...` in every
    Markdown file, the AGENTS.md of public maelys-cli included, beside the
    managed block that had just stopped naming it.
    """

    def test_the_seeded_texts_name_no_documentation_repository(self) -> None:
        for path in sorted((MODULE.share_dir() / "templates").glob("*.md")):
            self.assertNotIn("maelys-docs", path.read_text(encoding="utf-8"), path.name)

    def clone(self) -> pathlib.Path:
        clone = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-naming."))
        self.addCleanup(shutil.rmtree, clone, True)
        (clone / "docs").mkdir()
        (clone / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        (clone / "AGENTS.md").write_text("Read [the guide](docs/guide.md); it is docs/guide.md.\n", encoding="utf-8")
        (clone / "README.md").write_text("See docs/guide.md.\n", encoding="utf-8")
        return clone

    DATA = {"repository": "maelys-dev/maelys-docs", "moving": [{"path": "docs/guide.md"}]}

    def test_a_named_destination_is_written_as_before(self) -> None:
        clone = self.clone()
        self.assertEqual(MODULE.rewrite_markdown(clone, "p", self.DATA, True), ["AGENTS.md"])
        self.assertEqual((clone / "AGENTS.md").read_text(encoding="utf-8"),
                         "Read the guide; it is maelys-docs/p/guide.md.\n")
        # Markdown is what the socle rewrites: named, none of it is reported.
        remaining, _ = MODULE.surviving_references(clone, self.DATA, True)
        self.assertEqual(remaining, [])

    def test_an_unnamed_destination_keeps_the_path_and_reports_it(self) -> None:
        clone = self.clone()
        self.assertEqual(MODULE.rewrite_markdown(clone, "p", self.DATA, False), ["AGENTS.md"])
        text = (clone / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text, "Read the guide; it is docs/guide.md.\n")
        self.assertNotIn("maelys-docs", text)
        remaining, _ = MODULE.surviving_references(clone, self.DATA, False)
        self.assertEqual([(entry["path"], entry["line"]) for entry in remaining],
                         [("AGENTS.md", 1), ("README.md", 1)])



class ImpactWritesTest(unittest.TestCase):
    """`[writes: …]` after the selectors: what a version changes in the commands that write."""

    def test_the_marker_is_read_and_nothing_means_an_empty_list(self) -> None:
        line = "[asks: nothing] [writes: protect, tap] The plan says more."
        marker = MODULE.IMPACT_ASKS.match(line)
        self.assertEqual(marker.group(3), "protect, tap")
        self.assertEqual(line[marker.end():], "The plan says more.")
        self.assertEqual(MODULE.IMPACT_ASKS.match("[asks: old-legs] [writes: nothing] Text.").group(3), "nothing")
        # Superseded-by keeps its place, before writes.
        both = MODULE.IMPACT_ASKS.match("[asks: channels] [superseded-by: 0.57.0] [writes: tap] Text.")
        self.assertEqual((both.group(2), both.group(3)), ("0.57.0", "tap"))
        self.assertIsNone(MODULE.IMPACT_ASKS.match("[asks: nothing] Text.").group(3))

    def test_adopt_prints_the_marker_beside_the_line(self) -> None:
        data = {"mode": "plan", "files": [], "impactFrom": "v0.59.2", "checks": [], "changed": False,
                "socle": {"tag": "v0.60.0"}, "mechanism": "custom", "project": "/p",
                "impact": [{"version": "0.60.0", "says": "Text.", "asks": ["old-legs"], "asksThis": False,
                            "supersededBy": None, "writes": ["protect"]},
                           {"version": "0.60.1", "says": "More.", "asks": ["nothing"], "asksThis": False,
                            "supersededBy": None, "writes": []}]}
        text = MODULE.text_adopt(data)
        self.assertIn("0.60.0   -    Text. [writes: protect]", text)
        self.assertIn("0.60.1   -    More.\n", text)


class SeededNamingRuleTest(unittest.TestCase):
    """A seeded text naming the documentation repository is refused unless [docs] named.

    The seed of 0.59.2 named it for every product, and the file is written
    once: maelys-json found the line adopting. A written rule holds only
    when a machine verifies it (maelys-system).
    """

    def named(self, decl) -> list:
        return [c["message"] for c in decl.checks if "names the documentation repository" in c["message"]]

    def test_the_name_in_a_seeded_text_is_a_violation_without_the_word(self) -> None:
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        product.write("LICENSING.md", product.read("LICENSING.md") + "\nProse moves to `maelys-docs/p/`.\n")
        decl = MODULE.read_declarations(product.dir, "maelys-fixture", "custom")
        found = self.named(decl)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("LICENSING.md names"))
        # A note in 0.60.0, a refusal from 0.61.0 -- announced, so that the
        # line the socle itself wrote does not turn a CI red the day the
        # rule arrives.
        self.assertEqual([c["status"] for c in decl.checks if c["message"] == found[0]], ["note"])
        self.assertIn("refuses it from maelys-release 0.61.0", found[0])
        self.assertTrue([entry for entry in MODULE.COMING if entry[0] == "0.61.0" and entry[1] == "all"])
        product.write("maelys-release.conf", product.read("maelys-release.conf") + "\n[docs]\nnamed\n")
        self.assertEqual(self.named(MODULE.read_declarations(product.dir, "maelys-fixture", "custom")), [])

    def test_a_seeded_text_naming_nothing_passes(self) -> None:
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        for name in ("RELEASING.md", "LICENSING.md", "SECURITY.md"):
            self.assertNotIn("maelys-docs", product.read(name), name)
        self.assertEqual(self.named(MODULE.read_declarations(product.dir, "maelys-fixture", "custom")), [])
