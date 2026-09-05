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
        self.assertIn("adopt DIR [--product NAME] [--allow-untagged] [--apply]", text)
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
            "    if: github.event_name == 'push'",
            "      tag: ${{ inputs.tag }}",
            "        sh scripts/checkout-dependency.sh maelys-system\n",
            "      linux_packages: build-essential dpkg-dev file rpm pkg-config libjansson-dev\n",
            "      macos_packages: jansson\n",
            "  tap-maelys-fixture:",
            "  tap-libmaelys-fixture:",
            "      product: libmaelys-fixture",
        ):
            self.assertIn(expected, workflow)
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
            ci_file.write("  mine:\n    runs-on: ubuntu-24.04\n")
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
        ci_path.write_text("name: ci\non: [push]\njobs:\n  mine:\n    runs-on: ubuntu-24.04\n")
        data = product.json("check", self.dir, expect=2)["data"]
        self.assertTrue(any("does not call the socle's check-product.yml" in violation for violation in data["violations"]))
        self.assertEqual(product.json("adopt", self.dir)["data"]["mode"], "plan")   # a warning does not block adopt
        self.assertIn("WARN", product.run("adopt", self.dir).stdout)
        declarations = product.json("declarations", self.dir)["data"]
        self.assertEqual(declarations["dependencies"], ["maelys-system"])
        self.assertEqual(declarations["linuxPackages"], "pkg-config libjansson-dev")
        self.assertEqual(product.run("declarations", self.dir).stdout.splitlines()[-4], "dependencies  maelys-system")

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
        # clean but without a tag: refused unless the caller says it is a trial
        untagged = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--format", "json"],
                                  env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(untagged.returncode, 1)
        self.assertIn("not a release", json.loads(untagged.stderr)["error"]["message"])
        clean = subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--allow-untagged", "--format", "json"],
                               env=product.env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["data"]["socle"]["sha"], product.git(copy, "rev-parse", "HEAD"))
        # a socle that knows no tag (a depth-1 fetch in CI) takes the label the product pins
        subprocess.run([str(copy / "bin" / "maelys-release"), "adopt", self.dir, "--apply", "--allow-untagged"], env=product.env, check=True,
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

    def setUp(self) -> None:
        self.product = Product()
        self.dir = str(self.product.dir)
        self.product.run("adopt", self.dir, "--apply")

    def tearDown(self) -> None:
        self.product.close()

    def test_check_conformant(self) -> None:
        completed = self.product.run("check", self.dir)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout, self.CONTRACT + self.FILES + "check: maelys-fixture is on maelys-release v9.9.9\n")

    def test_check_drifting(self) -> None:
        with (self.product.dir / ".github" / "workflows" / "release.yml").open("a") as workflow:
            workflow.write("\n# edited\n")
        completed = self.product.run("check", self.dir, expect=2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout, self.CONTRACT
                         + self.FILES.replace("same     .github/workflows/release.yml", "update   .github/workflows/release.yml")
                         + "check: maelys-fixture drifts from maelys-release v9.9.9\n")

    def test_check_pinned_elsewhere(self) -> None:
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        workflow.write_text(workflow.read_text().replace("release.yml@" + "f" * 40 + " # v9.9.9",
                                                         "release.yml@" + "0" * 40 + " # v0.0.0"))
        completed = self.product.run("check", self.dir, expect=2)
        self.assertEqual(completed.stdout, self.CONTRACT
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
        self.assertEqual(completed.stdout, self.CONTRACT + self.FILES + textwrap.dedent("""\
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


if __name__ == "__main__":
    unittest.main()
