# SPDX-License-Identifier: MPL-2.0
"""Migration's two branches use the host installed after context construction."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import test_maelys_release as fixtures

MODULE = fixtures.MODULE


class MigrationHost(fixtures.FakeHost):
    """Real Git in throwaway repositories, with all network transports disabled."""
    def __init__(self, product, fail_product_push=False):
        super().__init__(answers={"repos/maelys-dev/maelys-docs": ("ok", {"visibility": "private"})})
        self.product = product
        self.fail_product_push = fail_product_push
        self.clones = []
        self.pushes = []
        self.environment = {**product.env, "GIT_ALLOW_PROTOCOL": "file"}

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if command[0] != "git" or (env is not None and env != self.environment):
            raise AssertionError(f"undeclared command/environment: {command!r}")
        if command[:3] == ["git", "clone", "-q"] and len(command) == 5:
            source = pathlib.Path(command[3].removeprefix("file://")).resolve()
            target = pathlib.Path(command[4]).resolve()
            if self.product.work.resolve() not in source.parents or not source.exists():
                raise AssertionError(f"clone source is not a fixture: {source}")
            if target.parent.parent != pathlib.Path(tempfile.gettempdir()).resolve() \
                    or not target.parent.name.startswith("maelys-release-migrate."):
                raise AssertionError(f"clone destination is not temporary: {target}")
            self.clones.append(target)
        elif cwd is None or pathlib.Path(cwd).resolve() not in [self.product.dir.resolve(), *self.clones]:
            raise AssertionError(f"git outside the fixture/clones: {command!r}, {cwd}")
        if command[:2] == ["git", "push"]:
            self.pushes.append((command, pathlib.Path(cwd)))
            if self.fail_product_push and pathlib.Path(cwd).name == "product":
                return subprocess.CompletedProcess(command, 23, "", "fixture refuses second push")
        return MODULE.Host.run(self, command, cwd=cwd, env=self.environment)


@unittest.skipUnless(shutil.which("git-filter-repo"), "git-filter-repo is required to rewrite history")
class MigrationHostTest(unittest.TestCase):
    def setUp(self):
        # Reuse the existing real-history fixture, not another migration model.
        self.fixture = fixtures.MigrateTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.product = self.fixture.product
        self.remote = self.product.work / "remotes/maelys-fixture.git"
        self.product.git(self.product.work, "clone", "-q", "--bare", str(self.product.dir), str(self.remote))
        self.product.git(self.product.dir, "remote", "add", "origin", str(self.remote))
        self.original_head = self.product.git(self.product.dir, "rev-parse", "HEAD")

    def invoke(self, host):
        out, err = io.StringIO(), io.StringIO()
        # CONTEXT already exists; every moved reader and writer must see this host.
        with patch.dict(os.environ, {"MAELYS_GIT_BASE": self.product.env["MAELYS_GIT_BASE"]}), \
                fixtures.using_host(host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["migrate", str(self.product.dir), "--documents", self.fixture.records("guide.md"),
                                    "--documents-repository", "maelys-dev/maelys-docs", "--apply", "--push",
                                    "--format", "json"])
        self.assertEqual(self.product.git(self.product.dir, "rev-parse", "HEAD"), self.original_head)
        self.assertTrue((self.product.dir / "docs/guide.md").is_file())
        self.assertEqual(host.reads, ["repos/maelys-dev/maelys-docs"], err.getvalue())
        self.assertEqual(host.writes, [])
        self.assertEqual([cwd.name for _, cwd in host.pushes], ["documents", "product"])
        self.assertTrue(all(not clone.exists() for clone in host.clones))
        return code, out.getvalue(), err.getvalue()

    def test_both_pushed_branches_match_the_reported_commits(self):
        host = MigrationHost(self.product)
        code, out, err = self.invoke(host)
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)["data"]
        self.assertTrue(data["pushed"])
        for remote, field in ((self.fixture.documents, "documentsCommit"), (self.remote, "productCommit")):
            self.assertEqual(self.product.git(remote, "rev-parse", "refs/heads/migrate/maelys-fixture"), data[field])
        self.assertEqual(self.product.git(self.remote, "rev-parse", "main"), self.original_head)
        self.assertEqual(self.product.git(self.fixture.documents, "show", "migrate/maelys-fixture:maelys-fixture/guide.md"),
                         "# Guide\n\nProse.")
        self.assertNotIn("docs/guide.md", self.product.git(self.remote, "ls-tree", "-r", "--name-only", "migrate/maelys-fixture"))
        for command, _ in host.pushes:
            self.assertIn("--force-with-lease", command)
            self.assertEqual(command[-1], "HEAD:refs/heads/migrate/maelys-fixture")

    def test_second_push_failure_keeps_the_existing_partial_write_contract(self):
        code, out, err = self.invoke(MigrationHost(self.product, fail_product_push=True))
        self.assertEqual((code, out), (1, ""))
        error = json.loads(err)["error"]
        self.assertEqual(error["code"], "PROCESS_FAILED")
        self.assertIn("fixture refuses second push", error["message"])
        self.assertIn("refs/heads/migrate/maelys-fixture", self.product.git(self.fixture.documents, "for-each-ref", "--format=%(refname)"))
        self.assertNotIn("refs/heads/migrate/maelys-fixture", self.product.git(self.remote, "for-each-ref", "--format=%(refname)"))
