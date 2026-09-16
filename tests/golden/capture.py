#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Capture the CLI before refactoring it (Python 3.9+, standard library only).

Create the first reference from a clean main checkout:
    python3 tests/golden/capture.py /tmp/maelys-golden --record
Replay its pinned inputs against the current checkout:
    python3 tests/golden/capture.py /tmp/maelys-replay
    diff -ru tests/golden/baseline /tmp/maelys-replay

--record resolves main once for each fleet repository. Normal runs read the
committed manifest; they never follow those branches again. --source selects
another socle checkout to test, and --reference selects another capture.
For an intentional behavior fix, --record --candidate FULL_COMMIT records the
clean candidate's outputs while retaining the reference's original inputs.

Fixtures are built by the ORIGINAL tests and executable at the recorded socle
commit, even after those tests change. Every Product is observed on creation,
before declarations/check/adopt calls, and before disposal. Intermediate states
inside tests (including invalid declarations and drift) are therefore covered.
The reference tests may apply changes to their throwaway fixtures. The candidate
is only invoked with describe, declarations, check and adopt WITHOUT --apply.

Each fixtures/NNNN.json holds the three commands' exit codes and exact stdout
and stderr strings, apart from absolute path substitution. Identical triples
share a file; manifest.json maps every observation back to its test. No JSON
fields, hashes, ordering, whitespace or diagnostic text are discarded.

GitHub reads run without gh; git dates, locale, hash seed and configuration are
fixed. Fleet clones have a disabled push URL and must remain byte-for-byte
unchanged (including untracked files). No remote settings, PRs or tags are
written. Fixture tests can create tags and push to their LOCAL bare remotes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FLEET = ("maelys-http", "maelys-oci", "maelys-egress", "maelys-json",
         "maelys-cli", "maelys-datalog", "maelys-system", "agent-cli-spec")
COMMANDS = ("declarations", "check", "adopt")
DATE = "2026-09-16T00:00:00+0000"


def execute(argv, cwd, env, check=True):
    completed = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=180, check=False)
    if check and completed.returncode:
        raise RuntimeError(f"{argv}: exit {completed.returncode}\n"
                           + completed.stderr.decode("utf-8", errors="replace"))
    return completed


def git(directory, *args, env):
    return execute(["git", "-C", str(pathlib.Path(directory).resolve()), *args],
                   directory, env).stdout.decode("utf-8").strip()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def isolated_environment(work):
    """An allowlisted PATH: no installed or credential-helper gh can leak in."""
    bindir = work / "bin"
    bindir.mkdir()
    real_git = shutil.which("git")
    if not real_git:
        raise RuntimeError("git is required")
    # Existing tests and the pre-refactoring program use cwd= for git. The
    # wrapper makes the absolute -C explicit even for those unchanged calls.
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n" + f'exec {shlex.quote(real_git)} -C "$PWD" "$@"\n',
        encoding="utf-8")
    wrapper.chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)
    # Only these portable tools are needed by Product and its shell fixtures.
    # Optional integration tools (jq, git-filter-repo, ssh-keygen) are excluded
    # so the observation set does not depend on the developer's installations.
    for name in ("sh", "bash", "env", "cat", "chmod", "cp", "cut", "dirname",
                 "basename", "awk", "grep", "find", "head", "mkdir", "mktemp", "mv", "pwd", "rm", "sed",
                 "sort", "tail", "tar", "tr", "uname", "wc", "xargs"):
        located = shutil.which(name, path="/usr/bin:/bin")
        if located:
            (bindir / name).symlink_to(located)
    home = work / "home"
    home.mkdir()
    # This is a child-process environment, not a reassignment of the user's
    # HOME. Do not inherit MAELYS_*, CLI_REFERENCE_BUILD or credential settings.
    env = {"PATH": str(bindir), "HOME": str(home), "XDG_CACHE_HOME": str(work / "cache"),
           "TMPDIR": str(work), "LANG": "C", "LC_ALL": "C", "TZ": "UTC",
           "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE}
    # TLS trust overrides are relevant to public, read-only git fetches.
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "SYSTEMROOT"):
        if name in os.environ:
            env[name] = os.environ[name]
    if shutil.which("gh", path=env["PATH"]) is not None:
        raise RuntimeError("capture PATH must not contain gh")
    return env


def checkout(work, name, url, commit, env):
    directory = work / name
    directory.mkdir()
    git(directory, "init", "-q", "-b", "main", env=env)
    git(directory, "remote", "add", "origin", url, env=env)
    git(directory, "remote", "set-url", "--push", "origin", "disabled://golden-read-only", env=env)
    git(directory, "fetch", "-q", "--no-tags", "--depth", "1", "origin", commit, env=env)
    git(directory, "checkout", "-q", "--detach", "FETCH_HEAD", env=env)
    actual = git(directory, "rev-parse", "HEAD", env=env)
    if commit != "refs/heads/main" and actual != commit:
        raise RuntimeError(f"{name}: expected {commit}, got {actual}")
    return directory, actual


def tree_digest(directory):
    """Include content, executable bits, symlinks and untracked files, not .git."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if ".git" in relative.parts:
            continue
        digest.update(str(relative).encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0" + path.read_bytes())
        else:
            digest.update(b"directory")
    return digest.hexdigest()


def prepare_generator(project, env):
    # Rendering may fetch this pinned input. Fetch it explicitly so a transient
    # network failure cannot be recorded as "maelys-cli is not available".
    pin = project / "dependencies/maelys-cli.pin"
    if (project / "tools/generate_cli_reference.py").is_file() or not pin.is_file():
        return
    sha = pin.read_text(encoding="utf-8").splitlines()[1].strip()
    cache = pathlib.Path(env["XDG_CACHE_HOME"]) / "maelys-release"
    cache.mkdir(parents=True, exist_ok=True)
    name = "maelys-cli-" + sha
    if not (cache / name).is_dir():
        checkout(cache, name, "https://github.com/maelys-dev/maelys-cli.git", sha, env)


class Capture:
    def __init__(self, source, output, work, env, manifest):
        self.source, self.output, self.work = source, output, work
        self.env, self.manifest = env, manifest
        self.paths = {}
        self.add_path(source, "<SOCLE>")
        self.add_path(work, "<WORK>")
        self.snapshots = {}
        self.fixture_results = {}
        self.observations = []
        self.case = "Product"
        self.events = {}

    def add_path(self, path, label):
        self.paths[str(path)] = label
        self.paths[str(pathlib.Path(path).resolve())] = label

    def normalize(self, content):
        text = content.decode("utf-8")
        for old, new in sorted(self.paths.items(), key=lambda pair: -len(pair[0])):
            text = text.replace(old, new)
        return text

    def command(self, argv, cwd, env):
        environment = {**env, "PATH": self.env["PATH"], "MAELYS_RELEASE_NO_RELOCATE": "1"}
        completed = execute([sys.executable, self.source / "bin" / "maelys-release", *argv],
                            cwd, environment, check=False)
        if completed.returncode not in (0, 1, 2):
            raise RuntimeError(f"{argv}: unexpected exit {completed.returncode}: {completed.stderr!r}")
        # Reject crashes and non-contract output rather than blessing them as goldens.
        stream = completed.stderr if completed.returncode == 1 else completed.stdout
        body = json.loads(stream)
        if body.get("contract") != "agent-cli/v2" or body.get("exitCode") != completed.returncode:
            raise RuntimeError(f"{argv}: not an agent-cli/v2 response")
        return {"exitCode": completed.returncode,
                "stdout": self.normalize(completed.stdout), "stderr": self.normalize(completed.stderr)}

    def triple(self, project, env, identity, options=()):
        declared_options = [item for pair in zip(options[::2], options[1::2])
                            if pair[0] == "--product" for item in pair]
        before = tree_digest(project) if project.is_dir() else None

        def invoke(command):
            return self.command(
                [command, str(project), *(declared_options if command == "declarations" else options),
                 *(identity if command != "declarations" else ()), "--format", "json"], project.parent, env)

        # declarations does not render; check and adopt can populate the same
        # generator cache and must stay sequential with respect to each other.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(zip(COMMANDS[:2], pool.map(invoke, COMMANDS[:2])))
        results["adopt"] = invoke("adopt")
        if before != (tree_digest(project) if project.is_dir() else None):
            raise RuntimeError(f"capture commands modified {project}")
        return results

    def fixture_key(self, product, project, options):
        # Product creates the same initial state hundreds of times. Reuse a
        # triple only when ALL its files (including git objects, refs and
        # configuration), sibling dependencies, cache and environment agree.
        # The git index is read as entries and flags, excluding its filesystem
        # stat cache. Paths get the same substitution as the stored streams.
        # This avoids rerunning identical initial and
        # already-adopted states without hiding intermediate mutations.
        digest = hashlib.sha256()
        for root in (product.work, pathlib.Path(self.env["XDG_CACHE_HOME"])):
            for path in sorted(root.rglob("*")):
                digest.update(str(path.relative_to(root)).encode() + b"\0")
                if path.name == "index" and path.parent.name == ".git":
                    content = git(path.parent.parent, "ls-files", "--stage", "-v", env=product.env).encode()
                elif path.is_symlink():
                    content = b"link\0" + os.readlink(path).encode()
                elif path.is_file():
                    content = str(path.stat().st_mode & 0o111).encode() + b"\0" + path.read_bytes()
                else:
                    content = b"directory"
                for old, new in sorted(self.paths.items(), key=lambda pair: -len(pair[0])):
                    content = content.replace(old.encode(), new.encode())
                digest.update(content)
        environment = self.normalize(json.dumps(product.env, sort_keys=True).encode())
        digest.update(environment.encode())
        digest.update(self.normalize(str(project).encode()).encode())
        digest.update(json.dumps(options).encode())
        return digest.digest()

    def observe(self, product, event, arguments=(), case=None):
        case = case or self.case
        self.add_path(product.work, "<FIXTURE>")
        project = product.dir
        if len(arguments) > 1 and not arguments[1].startswith("--"):
            project = pathlib.Path(arguments[1]).resolve()
        options = []
        for name in ("--product", "--mechanism"):
            if name in arguments:
                offset = arguments.index(name)
                options.extend((name, arguments[offset + 1]))
        if "--mechanism" not in options and not (project / ".github/workflows/release.yml").is_file():
            options.extend(("--mechanism", "maelys-release"))
        key = self.fixture_key(product, project, options)
        if key not in self.fixture_results:
            self.fixture_results[key] = self.triple(project, product.env, product.SOCLE, options)
        results = self.fixture_results[key]
        serialized = json.dumps(results, ensure_ascii=False)
        if serialized not in self.snapshots:
            name = f"fixtures/{len(self.snapshots):04d}.json"
            self.snapshots[serialized] = name
            write_json(self.output / name, results)
        count = self.events.get(case, 0) + 1
        self.events[case] = count
        self.observations.append({"test": case, "event": count, "when": event,
                                  "project": self.normalize(str(project).encode()),
                                  "options": options, "snapshot": self.snapshots[serialized]})

    def fixtures(self, reference):
        self.add_path(reference, "<REFERENCE>")
        spec = importlib.util.spec_from_file_location("golden_reference_tests",
                                                    reference / "tests/test_maelys_release.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        capture = self
        original = module.Product

        class ObservedProduct(original):
            def __init__(self):
                super().__init__()
                owner = sys._getframe(1).f_locals.get("cls")
                case = owner.__name__ + ".setUpClass" if owner else None
                capture.observe(self, "created", case=case)

            def cli(self, *arguments):
                if arguments and arguments[0] in COMMANDS:
                    capture.observe(self, "before " + arguments[0], arguments)
                return super().cli(*arguments)

            def close(self):
                try:
                    if self.dir.is_dir():
                        owner = sys._getframe(1).f_locals.get("cls")
                        case = owner.__name__ + ".tearDownClass" if owner else None
                        capture.observe(self, "close", case=case)
                finally:
                    super().close()

        class Result(unittest.TextTestResult):
            def startTest(self, test):
                capture.case = test.id().removeprefix("golden_reference_tests.")
                super().startTest(test)

            def addError(self, test, err):
                super().addError(test, err)
                self.printErrors()

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self.printErrors()

        module.Product = ObservedProduct
        # The golden inputs are Product fixtures, not the pure unit tests of
        # GitHub readers. Some of those readers' tests require gh to be
        # discoverable even while mocking its answers; they belong in the
        # separate self-test, not in a deliberately no-gh fixture capture.
        classes = [value for value in vars(module).values()
                   if isinstance(value, type) and issubclass(value, unittest.TestCase)
                   and value.__module__ == module.__name__
                   and "Product()" in inspect.getsource(value)]
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                                   for cls in sorted(classes, key=lambda cls: cls.__name__))
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, failfast=True, resultclass=Result).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError("the pinned fixture tests failed; the capture is incomplete")
        # MigrateTest needs optional git-filter-repo for its test bodies, but
        # its setUp only builds local git repositories. Capture that fixture
        # too, without making the optional tool a dependency of the golden.
        setup_only = []
        for cls in classes:
            if getattr(cls, "__unittest_skip__", False):
                case = cls()
                self.case = cls.__name__ + ".setUp"
                try:
                    case.setUp()
                    case.tearDown()
                finally:
                    case.doCleanups()
                setup_only.append(cls.__name__)
        self.manifest["fixtureTests"] = {
            "run": result.testsRun,
            "setupOnly": setup_only,
            "skipped": [{"test": test.id().removeprefix("golden_reference_tests."), "reason": reason}
                        for test, reason in result.skipped],
            "observations": self.observations}


@contextlib.contextmanager
def process_environment(env, work, capture):
    old_env, old_temp = dict(os.environ), tempfile.tempdir
    old_mkdtemp = tempfile.mkdtemp
    count = 0

    def mkdtemp(*args, **kwargs):
        nonlocal count
        result = old_mkdtemp(*args, **kwargs)
        count += 1
        capture.add_path(result, f"<TEMP-{count:04d}>")
        return result

    try:
        os.environ.clear()
        os.environ.update(env)
        tempfile.tempdir = str(work)
        tempfile.mkdtemp = mkdtemp
        yield
    finally:
        tempfile.mkdtemp, tempfile.tempdir = old_mkdtemp, old_temp
        os.environ.clear()
        os.environ.update(old_env)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=pathlib.Path, help="new output directory (must not exist)")
    parser.add_argument("--source", type=pathlib.Path, default=ROOT)
    parser.add_argument("--reference", type=pathlib.Path, default=pathlib.Path(__file__).parent / "baseline")
    parser.add_argument("--record", action="store_true", help="resolve initial inputs instead of replaying the manifest")
    parser.add_argument("--candidate", help="with --record: explicitly record a committed behavior fix on the frozen inputs")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite {output}")
    if args.candidate and not args.record:
        parser.error("--candidate requires --record; ordinary refactors only replay")
    if args.record:
        raw_env = dict(os.environ)
        commit = git(source, "rev-parse", "HEAD", env=raw_env)
        expected = args.candidate or git(source, "rev-parse", "refs/remotes/origin/main", env=raw_env)
        if commit != expected or git(source, "status", "--porcelain", "--", "bin", "share",
                                      "tests/test_maelys_release.py", "VERSION", "CHANGELOG.md",
                                      "WITHDRAWN", "docs", env=raw_env):
            parser.error("--record requires the unchanged origin/main source" if not args.candidate else
                         "--candidate must name the exact clean HEAD commit of the behavior fix")
        if not args.candidate:
            manifest = {"schemaVersion": 1, "socle": {"commit": commit,
                        "tag": "v" + (source / "VERSION").read_text().strip()}, "repositories": {}}
    if not args.record or args.candidate:
        saved = json.loads((args.reference / "manifest.json").read_text(encoding="utf-8"))
        manifest = {key: saved[key] for key in ("schemaVersion", "socle", "repositories")}
    # Publish the directory only after every command and fixture completed.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maelys-golden.") as temp:
        work = pathlib.Path(temp).resolve()
        staging = work / "output"
        staging.mkdir()
        env = isolated_environment(work)
        capture = Capture(source, staging, work, env, manifest)
        reference, _ = checkout(work, "reference", str(source), manifest["socle"]["commit"], env)
        write_json(staging / "describe.json", capture.command(["describe", "--format", "json"], source, env))
        (staging / "cli-contract.json").write_bytes((source / "docs/cli-contract.json").read_bytes())
        with process_environment(env, work, capture):
            capture.fixtures(reference)
        for name in FLEET:
            print(f"capture: {name}", file=sys.stderr, flush=True)
            url = f"https://github.com/maelys-dev/{name}.git"
            pinned = "refs/heads/main" if args.record and not args.candidate else manifest["repositories"][name]["commit"]
            clone, commit = checkout(work, name, url, pinned, env)
            capture.add_path(clone, f"<FLEET>/{name}")
            prepare_generator(clone, env)
            before = tree_digest(clone)
            identity = ("--socle-sha", manifest["socle"]["commit"], "--socle-tag", manifest["socle"]["tag"])
            results = capture.triple(clone, env, identity)
            if tree_digest(clone) != before or git(clone, "status", "--porcelain", env=env):
                raise RuntimeError(f"capture modified {name}")
            manifest["repositories"][name] = {"url": url, "commit": commit}
            write_json(staging / "fleet" / f"{name}.json", results)
        write_json(staging / "manifest.json", manifest)
        shutil.copytree(staging, output)
    print(f"Captured {len(capture.observations)} fixture observations, "
          f"{len(capture.snapshots)} distinct triples and {len(FLEET)} pinned repositories in {output}")


if __name__ == "__main__":
    main()
