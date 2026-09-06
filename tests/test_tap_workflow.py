# SPDX-License-Identifier: MPL-2.0
"""Tests of the publish step of tap.yml against a fake tap.

Every product pushes to the same tap, so two publications overlap. The
step's `push_rebased` shell function is extracted from the workflow text
and run against a bare repository that another publication moved first;
the whole step runs once end to end, its clone URL rewritten to that
repository by `url.<base>.insteadOf`, with a pre-push hook that plays the
competing publication. The `tap --apply` command does the same in Python
and is held to the same race. They need git and bash; ssh-keygen for the
signed case.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "maelys-release"
WORKFLOW = ROOT / ".github" / "workflows" / "tap.yml"
BASH = shutil.which("bash") or "bash"
TOKEN = "secret-token"
REPOSITORY = "maelys-dev/homebrew-tap"
FORMULA = "class MaelysFixture < Formula\nend\n"
OTHER = "class Other < Formula\nend\n"


def publish_script() -> str:
    """The run block of the 'Publish to the tap' step, dedented."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Publish to the tap")
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


def push_function() -> str:
    """The push_rebased shell function alone, from the step's script."""
    lines = publish_script().splitlines()
    start = lines.index("push_rebased() {")
    end = lines.index("}", start)
    return "\n".join(lines[start:end + 1]) + "\n"


class Tap:
    """A bare tap on main, clones of it, and a global git configuration under the test's control."""

    def __init__(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-tap-test."))
        self.config = self.work / "gitconfig"
        # No test reaches the network: any URL left unrewritten fails on a
        # closed port instead of touching the real tap, and git never prompts.
        self.config.write_text("[http]\n\tproxy = http://127.0.0.1:1\n")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(self.config), "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "TMPDIR": str(self.work)}
        self.env.pop("MAELYS_CLI_FORMAT", None)
        # macOS mktemp ignores TMPDIR: the step's clone lands under the test directory anyway.
        shim = self.work / "bin"
        shim.mkdir()
        (shim / "mktemp").write_text('#!/bin/sh\ndir="$TMPDIR/step.$$"\nmkdir "$dir" && echo "$dir"\n')
        (shim / "mktemp").chmod(0o755)
        self.env["PATH"] = f"{shim}:{self.env['PATH']}"
        source = self.work / "tap-src"
        source.mkdir()
        self.git(source, "init", "-q", "-b", "main")
        (source / "README.md").write_text("tap\n")
        self.git(source, "add", "README.md")
        self.git(source, "commit", "-q", "-m", "init")
        self.bare = self.work / "remotes" / "homebrew-tap.git"
        self.git(self.work, "clone", "-q", "--bare", str(source), str(self.bare))

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def git(self, cwd: pathlib.Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
             *arguments], cwd=cwd, env=self.env, check=True, text=True, stdout=subprocess.PIPE)
        return completed.stdout.strip()

    def clone(self, name: str) -> pathlib.Path:
        """A clone with its identity in its configuration, as the publish step sets it up."""
        clone = self.work / name
        self.git(self.work, "clone", "-q", str(self.bare), str(clone))
        self.git(clone, "config", "user.name", "test")
        self.git(clone, "config", "user.email", "test@example.invalid")
        return clone

    def commit_formula(self, clone: pathlib.Path, product: str, content: str, push: bool = False) -> str:
        (clone / "Formula").mkdir(exist_ok=True)
        (clone / "Formula" / f"{product}.rb").write_text(content)
        self.git(clone, "add", f"Formula/{product}.rb")
        self.git(clone, "commit", "-q", "-m", f"Update {product} to 0.1.0")
        if push:
            self.git(clone, "push", "-q", "origin", "HEAD:main")
        return self.git(clone, "rev-parse", "HEAD")

    def subjects(self) -> list[str]:
        return self.git(self.bare, "log", "--format=%s", "main").splitlines()

    def rewrite_urls(self) -> None:
        """Route the step's GitHub URLs, with and without token, to the bare tap."""
        for url in (f"https://x-access-token:{TOKEN}@github.com/{REPOSITORY}.git", f"https://github.com/{REPOSITORY}.git"):
            self.git(self.work, "config", "--global", "--add", f"url.file://{self.bare}.insteadOf", url)

    def race_on_push(self) -> None:
        """A pre-push hook that lands another product's formula on the tap before the first push."""
        hooks = self.work / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text(textwrap.dedent("""\
            #!/bin/sh
            set -e
            unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX
            [ -e "$RACE_MARKER" ] && exit 0
            : >"$RACE_MARKER"
            git clone -q "$RACE_BARE" "$RACE_MARKER.clone"
            mkdir -p "$RACE_MARKER.clone/Formula"
            printf 'class Other < Formula\\nend\\n' >"$RACE_MARKER.clone/Formula/other.rb"
            git -C "$RACE_MARKER.clone" add Formula/other.rb
            git -C "$RACE_MARKER.clone" -c user.name=other -c user.email=other@example.invalid \\
              -c commit.gpgsign=false commit -q -m "Update other to 0.1.0"
            git -C "$RACE_MARKER.clone" push -q origin HEAD:main
            """))
        hook.chmod(0o755)
        self.git(self.work, "config", "--global", "core.hooksPath", str(hooks))
        self.env["RACE_MARKER"] = str(self.work / "raced")
        self.env["RACE_BARE"] = str(self.bare)

    def reject_pushes(self) -> None:
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'tap refuses the push' >&2\nexit 1\n")
        hook.chmod(0o755)


class PushFunctionTest(unittest.TestCase):
    """push_rebased DIR BRANCH ATTEMPTS, as the workflow defines it."""

    def setUp(self) -> None:
        self.tap = Tap()

    def tearDown(self) -> None:
        self.tap.close()

    def push(self, clone: pathlib.Path, attempts: str = "3") -> subprocess.CompletedProcess:
        script = push_function() + 'push_rebased "$@"\n'
        return subprocess.run([BASH, "-c", script, "push_rebased", str(clone), "main", attempts], env=self.tap.env,
                              check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_function_is_extracted(self) -> None:
        text = push_function()
        self.assertTrue(text.startswith("push_rebased() {"))
        self.assertIn('git -C "$dir" rebase -q "origin/$branch"', text)
        self.assertIn('rebase --abort', text)
        self.assertEqual(subprocess.run([BASH, "-n"], input=text, text=True, check=False).returncode, 0)

    def test_rebases_after_a_rejected_push(self) -> None:
        tap = self.tap
        mine = tap.clone("mine")
        tap.commit_formula(mine, "maelys-fixture", FORMULA)
        tap.commit_formula(tap.clone("other"), "other", OTHER, push=True)
        completed = self.push(mine)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "push to main rejected on attempt 1 of 3\npushed to main on attempt 2 of 3\n")
        self.assertEqual(tap.subjects(), ["Update maelys-fixture to 0.1.0", "Update other to 0.1.0", "init"])
        self.assertEqual(sorted(tap.git(tap.bare, "ls-tree", "--name-only", "main", "Formula/").splitlines()),
                         ["Formula/maelys-fixture.rb", "Formula/other.rb"])

    def test_pushes_first_time_when_nothing_moved(self) -> None:
        mine = self.tap.clone("mine")
        self.tap.commit_formula(mine, "maelys-fixture", FORMULA)
        completed = self.push(mine)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "pushed to main on attempt 1 of 3\n")

    def test_gives_up_after_the_bounded_attempts(self) -> None:
        tap = self.tap
        mine = tap.clone("mine")
        tap.commit_formula(mine, "maelys-fixture", FORMULA)
        tap.reject_pushes()
        completed = self.push(mine)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "".join(f"push to main rejected on attempt {n} of 3\n" for n in (1, 2, 3)))
        self.assertIn("push to main rejected 3 times", completed.stderr)
        self.assertEqual(tap.subjects(), ["init"])

    def test_aborts_a_conflicting_rebase(self) -> None:
        tap = self.tap
        mine = tap.clone("mine")
        head = tap.commit_formula(mine, "maelys-fixture", FORMULA)
        # The same formula published twice at once: excluded by the signed-tag model, so it fails.
        tap.commit_formula(tap.clone("twin"), "maelys-fixture", "class MaelysFixture < Formula\n  # twin\nend\n", push=True)
        completed = self.push(mine)
        self.assertEqual(completed.returncode, 1)
        # git reports the conflict on stdout even under -q; the log keeps it.
        self.assertTrue(completed.stdout.startswith("push to main rejected on attempt 1 of 3\n"), completed.stdout)
        self.assertIn("CONFLICT", completed.stdout)
        self.assertNotIn("attempt 2", completed.stdout)
        self.assertIn("rebase onto origin/main conflicts", completed.stderr)
        self.assertFalse((mine / ".git" / "rebase-merge").exists())
        self.assertFalse((mine / ".git" / "rebase-apply").exists())
        self.assertEqual(tap.git(mine, "status", "--porcelain"), "")
        self.assertEqual(tap.git(mine, "rev-parse", "HEAD"), head)
        self.assertEqual(tap.subjects(), ["Update maelys-fixture to 0.1.0", "init"])


class PublishStepTest(unittest.TestCase):
    """The whole step, from the clone to the push, against the fake tap."""

    def setUp(self) -> None:
        self.tap = Tap()
        self.tap.rewrite_urls()
        self.cwd = self.tap.work / "step"
        (self.cwd / "formula").mkdir(parents=True)
        (self.cwd / "formula" / "maelys-fixture.rb").write_text(FORMULA)

    def tearDown(self) -> None:
        self.tap.close()

    def step(self, token: str = TOKEN, signing_key: str = "") -> subprocess.CompletedProcess:
        env = {**self.tap.env, "PRODUCT": "maelys-fixture", "INPUT_TAG": "v1.2.3", "GITHUB_REF_NAME": "v1.2.3",
               "TAP_REPOSITORY": REPOSITORY, "TAP_TOKEN": token, "TAP_SIGNING_KEY": signing_key,
               "HAS_TAP_TOKEN": "true" if token else "false", "ACTOR": "releaser"}
        return subprocess.run([BASH, "-c", publish_script()], cwd=self.cwd, env=env, check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_publishes_over_a_concurrent_publication(self) -> None:
        tap = self.tap
        tap.race_on_push()
        completed = self.step()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "push to main rejected on attempt 1 of 3\npushed to main on attempt 2 of 3\n"
                         "brew install maelys-dev/tap/maelys-fixture\n")
        self.assertEqual(tap.subjects(), ["Update maelys-fixture to 1.2.3", "Update other to 0.1.0", "init"])
        self.assertEqual(tap.git(tap.bare, "show", "main:Formula/maelys-fixture.rb"), FORMULA.strip())
        self.assertEqual(tap.git(tap.bare, "log", "-1", "--format=%an <%ae>", "main"),
                         "releaser <releaser@users.noreply.github.com>")
        # Published: the step is idempotent.
        again = self.step()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, "tap already carries maelys-fixture 1.2.3\n")
        self.assertEqual(len(tap.subjects()), 3)

    def test_without_credentials_reports_and_succeeds(self) -> None:
        completed = self.step(token="")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("::notice::no tap credentials configured", completed.stdout)
        self.assertIn("Formula/maelys-fixture.rb", completed.stdout)
        self.assertEqual(self.tap.subjects(), ["init"])

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is needed to sign")
    def test_rebased_commit_stays_signed(self) -> None:
        tap = self.tap
        key = tap.work / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        tap.race_on_push()
        completed = self.step(signing_key=key.read_text())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("pushed to main on attempt 2 of 3", completed.stdout)
        commit = tap.git(tap.bare, "cat-file", "commit", "main")
        self.assertIn("gpgsig", commit)
        self.assertIn("Update maelys-fixture to 1.2.3", commit)


class TapCommandRaceTest(unittest.TestCase):
    """`tap --apply` retries like the workflow step."""

    def setUp(self) -> None:
        self.tap = Tap()

    def tearDown(self) -> None:
        self.tap.close()

    def apply(self, expect: int = 0) -> dict:
        formula = self.tap.work / "maelys-fixture.rb"
        formula.write_text(FORMULA)
        env = {**self.tap.env, "TAP_URL": f"file://{self.tap.bare}", "TAP_REPOSITORY": REPOSITORY}
        completed = subprocess.run([str(CLI), "tap", "maelys-fixture", "v1.2.3", str(formula), "--skip-style", "--apply",
                                    "--format", "json", "--compact"], env=env, check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return json.loads(completed.stdout if expect != 1 else completed.stderr)

    def test_apply_rebases_over_a_concurrent_publication(self) -> None:
        tap = self.tap
        tap.race_on_push()
        data = self.apply()["data"]
        self.assertEqual(data["pushAttempts"], 2)
        self.assertEqual(data["commit"], tap.git(tap.bare, "rev-parse", "main"))
        self.assertEqual(tap.subjects(), ["Update maelys-fixture to 1.2.3", "Update other to 0.1.0", "init"])

    def test_apply_gives_up_after_three_attempts(self) -> None:
        self.tap.reject_pushes()
        error = self.apply(expect=1)["error"]
        self.assertEqual(error["code"], "PROCESS_FAILED")
        self.assertIn("rejected 3 times", error["message"])
        self.assertEqual(self.tap.subjects(), ["init"])


class WorkflowTextTest(unittest.TestCase):
    def test_publish_job_is_serialized_per_tap_in_the_reusable_workflow(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        publish = text[text.index("\n  publish:\n"):]
        self.assertIn("group: tap-${{ inputs.tap_repository }}", publish)
        self.assertIn("cancel-in-progress: false", publish)
        self.assertIn('push_rebased "$tmp/tap" main 3', publish)
        self.assertNotIn("git -C \"$tmp/tap\" push -q origin HEAD:main\n", publish)
        # The job, not the workflow: render and bottles keep running in parallel.
        self.assertNotIn("\nconcurrency:", text)


if __name__ == "__main__":
    unittest.main()
