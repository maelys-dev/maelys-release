# SPDX-License-Identifier: MPL-2.0
"""Rehearsal handlers reach the current host after the context already exists."""
from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_maelys_release import APART, MODULE, FakeHost, Product, using_host


class RehearsalHost(FakeHost):
    def __init__(self, product, mode, status=0, version="1.2.3"):
        super().__init__(answers={"repos/o/r/releases/tags/v1.2.3": ("ok", {})})
        self.product, self.mode, self.status, self.version = product, mode, status, version
        self.streams = []

    def which(self, name):
        if name == ("docker" if self.mode == "container" else "gh"):
            return "/fake/" + name
        return None

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if command == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/o/r.git\n", "")
        if cwd == self.product.dir.resolve() and command[:2] in (["git", "grep"], ["git", "rev-parse"]):
            return MODULE.Host.run(self, command, cwd=cwd, env=self.product.env)
        if self.mode == "channel" and command == ["gh", "release", "download", "v1.2.3", "--repo", "o/r",
                                                  "--dir", str(self.product.dir.resolve() / "dist")]:
            dist = self.product.dir / "dist"
            (dist / "asset.tgz").write_text("fixture", encoding="utf-8")
            (dist / "channel-npm.json").write_text(json.dumps({
                "product": "maelys-fixture", "tag": "v1.2.3", "channel": "npm", "version": "1.2.3",
                "published": "2026-09-01T00:00:00Z", "run": "https://example.invalid/run"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"undeclared command: {command!r}")

    def stream(self, command, log, cwd=None, env=None):
        self.streams.append((command, cwd, env))
        if self.mode == "container" and command[:2] == ["docker", "run"]:
            (self.product.dir / "dist" / "fixture-linux-arm64.tgz").write_text("fixture", encoding="utf-8")
        elif self.mode == "channel" and command == ["bash", "-c", "bash scripts/publish-channel.sh v1.2.3 npm"]:
            pathlib.Path(env["CHANNEL_RECORD"]).write_text(json.dumps({"version": self.version}), encoding="utf-8")
        else:
            raise AssertionError(f"undeclared streaming command: {command!r}")
        print("fixture process log", file=log)
        return subprocess.CompletedProcess(command, self.status)


class RehearseHostTest(unittest.TestCase):
    def setUp(self):
        self.product = Product()
        self.addCleanup(self.product.close)
        self.product.write("scripts/publish-channel.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.product.write("maelys-release.conf", "[channels]\nnpm github-packages\n" + APART)
        self.product.run("adopt", str(self.product.dir), "--apply")

    def invoke(self, host, *args):
        out, err = io.StringIO(), io.StringIO()
        # Both the entry point and CONTEXT were constructed before using_host.
        with using_host(host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["rehearse", str(self.product.dir), *args, *Product.SOCLE, "--format", "json"])
        return code, out.getvalue(), err.getvalue()

    def test_container_modes_use_the_replaced_host_and_keep_json_logs_separate(self):
        for options, mode, artifacts in (([], "package", ["fixture-linux-arm64.tgz"]), (["--check"], "check", [])):
            with self.subTest(mode=mode):
                host = RehearsalHost(self.product, "container")
                code, out, err = self.invoke(host, "linux-arm64", *options)
                self.assertEqual(code, 0, err)
                self.assertTrue(json.loads(out)["ok"])
                data = json.loads(out)["data"]
                self.assertEqual((data["mode"], data["artifacts"], data["platform"]), (mode, artifacts, "linux/arm64"))
                self.assertIn("fixture process log", err)
                self.assertEqual(len(host.streams), 1)
                command, cwd, env = host.streams[0]
                self.assertEqual(command[:3], ["docker", "run", "--rm"])
                self.assertIn(f"{self.product.dir.resolve()}:/src:ro", command)
                for value in ("TARGET=linux-arm64", "DEPENDENCIES=maelys-system", "DEPENDENCIES_APART=1",
                              f"REHEARSE_MODE={mode}", "CHECK_COMMAND=make check"):
                    self.assertIn(value, command)
                self.assertEqual(command[-3:], ["bash", "-c", MODULE.REHEARSAL])
                self.assertEqual((cwd, env, host.writes, host.reads), (None, None, [], []))

    def test_container_failure_keeps_the_process_code_in_the_contract_error(self):
        code, out, err = self.invoke(RehearsalHost(self.product, "container", status=23), "linux-arm64")
        self.assertEqual((code, out), (1, ""))
        self.assertIn('"code": "PROCESS_FAILED"', err)
        self.assertIn("failed with exit 23", err)

    def test_channel_uses_the_same_host_for_read_download_and_replay_then_cleans_assets(self):
        for version, status, expected in (("1.2.3", 0, 0), ("9.9.9", 0, 2), ("1.2.3", 17, 1)):
            with self.subTest(version=version, status=status):
                host = RehearsalHost(self.product, "channel", status=status, version=version)
                # A dummy value, only consumed by the fake host: no registry command runs.
                with patch.dict(os.environ, {"NODE_AUTH_TOKEN": "fixture-token"}):
                    code, out, err = self.invoke(host, "--channel", "npm", "--tag", "v1.2.3")
                self.assertEqual(code, expected, err)
                self.assertEqual(host.reads, ["repos/o/r/releases/tags/v1.2.3"])
                self.assertEqual(host.writes, [])
                self.assertEqual(len(host.streams), 1)
                _, cwd, env = host.streams[0]
                self.assertEqual(cwd, self.product.dir.resolve())
                self.assertEqual([env[key] for key in ("TAG", "CHANNEL", "PRODUCT", "CHANNEL_DRY_RUN")],
                                 ["v1.2.3", "npm", "maelys-fixture", "1"])
                self.assertEqual(list((self.product.dir / "dist").iterdir()), [])
                self.assertFalse(pathlib.Path(env["CHANNEL_RECORD"]).exists())
                if status:
                    self.assertEqual(out, "")
                    self.assertIn('"code": "PROCESS_FAILED"', err)
                    self.assertIn("exited 17", err)
                else:
                    data = json.loads(out)["data"]
                    self.assertEqual(data["assets"], ["asset.tgz", "channel-npm.json"])
                    self.assertEqual(data["recorded"], ["version"])
                    self.assertEqual(data["matches"], expected == 0)
                    self.assertEqual(data["differs"], [] if expected == 0 else ["version"])
                    self.assertIn("fixture process log", err)
