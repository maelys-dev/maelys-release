# SPDX-License-Identifier: MPL-2.0
"""What is pushed is read back from the remote before it is reported.

`protect` learned this in 0.58.0, after two products lost a setting under a
write whose plan named something else. A product review of 2026-09-29 asked
whether `cut`, `tap` and `migrate` do the same; they did not. The exit
status of `git push` says the transport succeeded, not that the remote
holds what was meant.

These measure the three readings against real repositories, and then that
each command issues its own.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

import test_maelys_release  # noqa: F401  -- puts the socle's package on the path
from maelys_cli import Failure
from maelys_socle import writes

# The machine's own git configuration must not decide what these measure:
# with tag.gpgsign = true, which the operator's does carry, a lightweight
# tag cannot even be created.
NEUTRAL = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: pathlib.Path, *arguments: str) -> str:
    completed = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture",
                                "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                                "-c", "init.defaultBranch=main", *arguments],
                               check=True, text=True, stdout=subprocess.PIPE, env=NEUTRAL)
    return completed.stdout.strip()


class ReadBackTest(unittest.TestCase):
    """The three readings, against a real remote and a real clone."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        work = pathlib.Path(self.temp.name)
        self.remote = work / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True, env=NEUTRAL)
        self.clone = work / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-q")
        (self.clone / "file").write_text("one\n", encoding="utf-8")
        git(self.clone, "add", "file")
        git(self.clone, "commit", "-q", "-m", "one")
        self.commit = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "push", "-q", str(self.remote), "HEAD:refs/heads/main")

    def test_a_ref_the_remote_holds_is_confirmed(self) -> None:
        writes.confirm_ref(self.clone, str(self.remote), "refs/heads/main", self.commit, "the branch", "hint")

    def test_a_ref_the_remote_does_not_hold_is_refused(self) -> None:
        with self.assertRaises(Failure) as refused:
            writes.confirm_ref(self.clone, str(self.remote), "refs/heads/absent", self.commit, "the branch", "hint")
        self.assertIn("does not hold refs/heads/absent", refused.exception.message)

    def test_a_ref_that_moved_names_both_commits(self) -> None:
        (self.clone / "file").write_text("two\n", encoding="utf-8")
        git(self.clone, "commit", "-q", "-am", "two")
        other = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "push", "-q", str(self.remote), "HEAD:refs/heads/main")
        with self.assertRaises(Failure) as refused:
            writes.confirm_ref(self.clone, str(self.remote), "refs/heads/main", self.commit, "the branch", "hint")
        self.assertIn(other[:12], refused.exception.message)
        self.assertIn(self.commit[:12], refused.exception.message)

    def test_a_remote_that_cannot_be_read_is_not_an_absence(self) -> None:
        """The mistake this package made once, in its reading of the
        deployment policies: unreadable reported as missing."""
        with self.assertRaises(Failure) as refused:
            writes.confirm_ref(self.clone, str(self.remote) + "-gone", "refs/heads/main",
                               self.commit, "the branch", "hint")
        self.assertIn("could not be read back", refused.exception.message)

    def test_an_annotated_tag_is_confirmed_by_the_commit_it_names(self) -> None:
        git(self.clone, "tag", "-a", "-m", "release", "v1.0.0")
        git(self.clone, "push", "-q", str(self.remote), "refs/tags/v1.0.0")
        writes.confirm_tag(self.clone, str(self.remote), "v1.0.0", self.commit, "the tag", "hint")

    def test_a_tag_on_another_commit_is_refused(self) -> None:
        (self.clone / "file").write_text("two\n", encoding="utf-8")
        git(self.clone, "commit", "-q", "-am", "two")
        elsewhere = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "tag", "-a", "-m", "release", "v1.0.0")
        git(self.clone, "push", "-q", str(self.remote), "refs/tags/v1.0.0")
        with self.assertRaises(Failure) as refused:
            writes.confirm_tag(self.clone, str(self.remote), "v1.0.0", self.commit, "the tag", "hint")
        self.assertIn(elsewhere[:12], refused.exception.message)

    def test_a_lightweight_tag_is_refused(self) -> None:
        git(self.clone, "tag", "v1.0.0")
        git(self.clone, "push", "-q", str(self.remote), "refs/tags/v1.0.0")
        with self.assertRaises(Failure) as refused:
            writes.confirm_tag(self.clone, str(self.remote), "v1.0.0", self.commit, "the tag", "hint")
        self.assertIn("lightweight", refused.exception.message)

    def test_a_shared_branch_is_read_for_what_it_carries(self) -> None:
        """Containment, not equality: every product publishes its formula on
        the tap's one branch, and one that lands a second later loses
        nothing."""
        other = self.clone.parent / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True, env=NEUTRAL)
        (other / "second").write_text("from another product\n", encoding="utf-8")
        git(other, "add", "second")
        git(other, "commit", "-q", "-m", "another formula")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        writes.confirm_contains(self.clone, str(self.remote), "main", self.commit, "the formula commit", "hint")

    def test_a_branch_without_the_commit_is_refused(self) -> None:
        (self.clone / "file").write_text("never pushed\n", encoding="utf-8")
        git(self.clone, "commit", "-q", "-am", "never pushed")
        missing = git(self.clone, "rev-parse", "HEAD")
        with self.assertRaises(Failure) as refused:
            writes.confirm_contains(self.clone, str(self.remote), "main", missing, "the formula commit", "hint")
        self.assertIn(missing[:12], refused.exception.message)
