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
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "maelys-release"
PINNED_TAG = "v0.0.1"


def load_module():
    loader = importlib.machinery.SourceFileLoader("maelys_release", str(CLI))
    spec = importlib.util.spec_from_loader("maelys_release", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


MODULE = load_module()


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
        self.write("packaging/homebrew/maelys-fixture.rb.in", "class MaelysFixture < Formula\nend\n")
        self.write("packaging/homebrew/libmaelys-fixture.rb.in", "class LibmaelysFixture < Formula\nend\n")
        self.write("AGENTS.md", "# Agent instructions\n\nKeep me.\n")

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def git(self, cwd: pathlib.Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main",
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
        self.assertIn("adopt DIR [--product NAME] [--mechanism MECHANISM] [--allow-untagged] [--apply]", text)
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
        self.assertEqual(json.loads(completed.stdout)["data"]["records"], [{"word": "declarations"}, {"word": "describe"}])
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
            "        sh scripts/checkout-dependency.sh maelys-system\n",
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
        shutil.copy2(CLI, copy / "bin" / "maelys-release")
        shutil.copy2(ROOT / "bin" / "maelys_cli.py", copy / "bin" / "maelys_cli.py")   # the vendored framework
        shutil.copy2(ROOT / "VERSION", copy / "VERSION")
        for directory in ("share", "dependencies"):
            shutil.rmtree(copy / directory, ignore_errors=True)
            shutil.copytree(ROOT / directory, copy / directory)
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
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn("verify_command: sh scripts/verify-release.sh TARGET", workflow)

    def test_without_that_script_the_release_workflow_asks_for_no_verification(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.assertNotIn("verify_command:", self.product.read(".github/workflows/release.yml"))

    def test_a_product_declares_the_targets_it_builds(self) -> None:
        self.product.write("packaging/release",
                           "[targets]\nlinux-x86_64\nlinux-arm64\nmacos-arm64\nwasm32 ubuntu-26.04\n")
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn('targets: \'[{"target": "linux-x86_64"}, {"target": "linux-arm64"},'
                      ' {"target": "macos-arm64"}, {"target": "wasm32", "runner": "ubuntu-26.04"}]\'', workflow)
        # A target that names no label keeps the runner release.yml holds for
        # it, so changing a default runner still reaches this product.
        self.assertNotIn('"linux-x86_64", "runner"', workflow)

    def test_a_runner_is_a_label_or_a_label_set(self) -> None:
        self.product.write("packaging/release", "[targets]\nmacos-arm64 self-hosted macOS ARM64\n")
        self.product.run("adopt", self.dir, "--apply")
        self.assertIn('{"target": "macos-arm64", "runner": ["self-hosted", "macOS", "ARM64"]}',
                      self.product.read(".github/workflows/release.yml"))

    def test_a_product_adds_an_archive_kind_to_the_manifest(self) -> None:
        self.product.write("packaging/release", "[manifest]\n*.wasm\nreceipt.json\n")
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
        self.product.write("packaging/release", "[targets]\nwasm32\n")
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(data["valid"])
        self.assertTrue(any("names no runner" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_a_product_declares_a_publication_channel(self) -> None:
        self.product.write("packaging/release", "[channels]\nnpm github-packages\n")
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.run("adopt", self.dir, "--apply")
        workflow = self.product.read(".github/workflows/release.yml")
        self.assertIn("  channel-npm:", workflow)
        # After the release, never before: a registry publication cannot be
        # withdrawn, so nothing the socle runs may fail after it.
        self.assertIn("    needs: release\n    if: needs.release.result == 'success'", workflow)
        self.assertIn("      packages: write", workflow)
        self.assertIn("      publish_command: sh scripts/publish-channel.sh TAG CHANNEL", workflow)

    def test_a_channel_without_its_script_is_a_violation(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.product.write("packaging/release", "[channels]\nnpm github-packages\n")
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
        self.product.write("packaging/release", "[channels]\nsdk pypi\n")
        data = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertTrue(any("serves no pypi channel" in check["message"] for check in data["checks"]),
                        data["checks"])

    def test_the_generated_caller_grants_every_scope_its_workflows_declare(self) -> None:
        """A caller that under-grants fails the whole run at startup, the
        release job included, so this is checked here and not on a runner."""
        self.product.write("packaging/release", "[channels]\nnpm github-packages\n")
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
        # The conventions, and the shared CI, which is not the mechanism.
        self.assertEqual(written, {"AGENTS.md", "CLAUDE.md", "RELEASING.md", "LICENSING.md", "SECURITY.md",
                                   ".github/workflows/ci.yml"})
        self.assertNotIn(".github/workflows/release.yml", written)

    def test_adopt_leaves_the_products_own_workflow_alone(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.assertEqual(self.product.read(".github/workflows/release.yml"), self.OWN_WORKFLOW)
        self.assertFalse((self.product.dir / "scripts" / "checkout-dependency.sh").exists())
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
        self.assertEqual(data["pinned"], {"sha": "f" * 40, "tag": "v9.9.9"})
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
        self.assertNotIn("check-product.yml", completed.stdout)
        self.assertNotIn("package-release.sh", completed.stdout)

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
        notes = [check["message"] for check in data["checks"] if check["status"] == "note"]
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
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(data["mechanism"], "maelys-release")
        written = {entry["path"] for entry in data["adopted"]}
        self.assertIn(".github/workflows/release.yml", written)
        self.assertIn("AGENTS.md", written)
        self.assertEqual((self.target / "VERSION").read_text(), "0.1.0\n")
        self.assertIn("Mozilla Public License", (self.target / "LICENSE").read_text().split("\n", 1)[0])
        self.assertRegex((self.target / "CHANGELOG.md").read_text(), r"## 0\.1\.0 — \d{4}-\d{2}-\d{2}")
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
        # And left the product, whose README now names where it went.
        self.assertFalse((product / "docs" / "architecture.md").exists())
        self.assertTrue((product / "docs" / "schema.json").is_file())
        readme = (product / "README.md").read_text()
        self.assertNotIn("docs/architecture.md", readme)
        self.assertIn("maelys-dev/maelys-docs", readme)
        self.assertIn("## Licence", readme)      # the rest of the README is untouched
        # The operator's checkout is not written to at all.
        self.assertTrue((self.product.dir / "docs" / "architecture.md").is_file())

    def test_an_untagged_product_describes_no_release(self) -> None:
        self.product.git(self.product.dir, "tag", "-d", "v1.2.3")
        error = self.migrate("--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn("no tag", error["message"])

    def test_every_markdown_of_the_product_is_pointed_away(self) -> None:
        # The first shape rewrote README.md alone, and a product had to
        # repoint examples/README.md by hand: reporting a problem and fixing
        # half of it is worse than either extreme.
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
        for bad in ("[build]\n/etc\n", "[build]\n../elsewhere\n", "[build]\nbuild/a\nbuild/b\n"):
            self.product.write("docs/cli.reference", bad)
            refused = self.product.json("check", self.dir, expect=2)["data"]
            self.assertTrue(any("docs/cli.reference" in violation
                                for violation in refused["conventions"]["violations"]), bad)

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
        """)
    FILES = textwrap.dedent("""\
        same     .github/workflows/release.yml
        same     scripts/checkout-dependency.sh
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
        with (self.product.dir / ".github" / "workflows" / "release.yml").open("a") as workflow:
            workflow.write("\n# edited\n")
        completed = self.product.run("check", self.dir, expect=2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout, self.CONTRACT
                         + self.FILES.replace("same     .github/workflows/release.yml", "update   .github/workflows/release.yml")
                         + self.VERDICTS.replace("release mechanism: ok", "release mechanism: FAIL")
                         + "check: maelys-fixture drifts from maelys-release v9.9.9\n")

    def test_check_pinned_elsewhere(self) -> None:
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("release.yml@" + "f" * 40 + " # v9.9.9",
                                                         "release.yml@" + "0" * 40 + " # v0.0.0"))
        completed = self.product.run("check", self.dir, expect=2)
        self.assertEqual(completed.stdout, self.CONTRACT
                         + self.VERDICTS.replace("release mechanism: ok", "release mechanism: FAIL")
                         + "drift    maelys-fixture pins maelys-release v0.0.0 (0000000) but this is v9.9.9 (fffffff):"
                           f" run the pinned socle, or 'adopt {self.product.dir.resolve()} --apply' from this one to upgrade\n"
                         + "check: maelys-fixture drifts from maelys-release v9.9.9\n")

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

    def test_parse_release(self) -> None:
        self.assertEqual(MODULE.parse_release("[targets]\nlinux-arm64\nwasm32 ubuntu-26.04\n"),
                         ([("linux-arm64", ""), ("wasm32", "ubuntu-26.04")], [], []))
        self.assertEqual(MODULE.parse_release("[targets]\nmacos-arm64 self-hosted ARM64\n"),
                         ([("macos-arm64", ["self-hosted", "ARM64"])], [], []))
        self.assertEqual(MODULE.parse_release("[manifest]\n*.wasm\n"), ([], ["*.wasm"], []))
        self.assertEqual(MODULE.parse_release("[channels]\nnpm github-packages\n"),
                         ([], [], [("npm", "github-packages")]))
        self.assertEqual(MODULE.parse_release("# nothing declared\n"), ([], [], []))
        for text in ("linux-arm64\n",                      # outside a section
                     "[bsd]\nlinux-arm64\n",               # unknown section
                     "[targets]\nwasm32\n",                # no runner and no default
                     "[targets]\nWASM\n",                  # not a target name
                     "[targets]\nlinux-arm64\nlinux-arm64\n",   # twice
                     "[targets]\nwasm32 'quoted'\n",       # not a runner label
                     "[manifest]\n*.tar.gz\n",             # already covered
                     "[manifest]\n*.a *.b\n",              # one glob per line
                     "[channels]\nnpm pypi\n",             # a registry the socle cannot reach
                     "[channels]\nnpm\n",                  # names no registry
                     "[channels]\nNPM github-packages\n",  # not a channel name
                     "[channels]\nnpm github-packages\nnpm github-packages\n"):  # twice
            with self.assertRaises(ValueError, msg=text):
                MODULE.parse_release(text)

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
        """The socle holds itself to the product rule it enforces."""
        version = (ROOT / "VERSION").read_text().strip()
        changelog = (ROOT / "CHANGELOG.md").read_text()
        self.assertRegex(changelog, rf"(?m)^## {re.escape(version)} — [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$")
        entries = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+) ", changelog, re.MULTILINE)
        self.assertEqual(entries[0], version)
        self.assertEqual(len(entries), len(set(entries)), "duplicate entries")

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

    def test_reusable_release_avoids_actions_artifact_storage(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("ref: ${{ inputs.tag || github.ref }}", text)
        self.assertIn("Prepare draft GitHub release", text)
        self.assertIn('gh release edit "$TAG" --repo "${GITHUB_REPOSITORY}" --draft', text)
        self.assertIn("releases/assets/$asset_id", text)
        self.assertIn('gh release upload "$TAG"', text)
        self.assertIn('gh release download "$TAG"', text)
        self.assertIn('--draft=false', text)
        self.assertNotIn("actions/upload-artifact", text)
        self.assertNotIn("actions/download-artifact", text)


if __name__ == "__main__":
    unittest.main()
