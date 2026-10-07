# SPDX-License-Identifier: MPL-2.0
"""A pin one job reads is cloned by that job, and by nothing that clones them all.

maelys-http pins Mbed TLS twice. Its second pin, `mbedtls-4`, serves one job
of its own, which clones it by name. The socle's five shared jobs and its
three release builds cloned it too -- submodules and all, without reading a
file of it: forty seconds a job on Linux, measured by comparing two runs,
because nothing said so.

A pin says `on-request`, and the loop that clones every pin skips it. It is
still a pin: one declaration, judged by `check` and `cut` as any other.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

from test_maelys_release import MODULE, PINNED_TAG, Product


class OnRequestPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        # A second repository to pin, served like the first.
        remotes = self.product.work / "remotes"
        self.product.git(self.product.work, "clone", "-q", "--bare",
                         str(remotes / "maelys-system.git"), str(remotes / "maelys-extra.git"))
        self.product.git(remotes / "maelys-extra.git", "config", "uploadpack.allowFilter", "true")
        self.product.write("dependencies/maelys-extra.pin",
                           f"{PINNED_TAG}-1-g{self.product.pinned[:7]}\n{self.product.pinned}\non-request\n")
        self.home = (self.product.work / "cache" / "maelys-release" / "dependencies" / "maelys-fixture").resolve()

    def test_the_grammar_takes_the_word_and_nothing_after_it(self) -> None:
        pin, problems = MODULE.parse_pin("x", f"v1\n{'a' * 40}\non-request\n")
        self.assertEqual((pin["on_request"], problems), (True, []))
        self.assertFalse(MODULE.parse_pin("x", f"v1\n{'a' * 40}\n")[0]["on_request"])
        _, problems = MODULE.parse_pin("x", f"v1\n{'a' * 40}\non-request sometimes\n")
        self.assertEqual(problems, ["dependencies/x.pin: the on-request line takes nothing"])

    def test_it_is_still_a_pin_and_check_says_what_it_is(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        said = [check["message"] for check in data["checks"]]
        self.assertIn("pinned dependencies: maelys-extra maelys-system", said)
        self.assertTrue([line for line in said
                         if line.startswith("dependencies/maelys-extra.pin is cloned on request only")], said)

    def loop(self, destination: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependencies.sh"), str(destination)],
                              cwd=self.product.dir, env=self.product.env, check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_the_loop_skips_it_says_so_and_a_job_still_clones_it_by_name(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        destination = self.product.work / "runner"
        completed = self.loop(destination)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Assignments on stdout and nothing else, as before: it lands in
        # $GITHUB_ENV whole, and a word about a skipped pin there would be
        # read as a variable.
        assigned = dict(line.split("=", 1) for line in completed.stdout.splitlines())
        self.assertEqual(pathlib.Path(assigned.pop("MAELYS_DEPENDENCIES_DIR")).resolve(), destination.resolve())
        self.assertEqual(set(assigned) - {"MAELYS_RELEASE_DIR"}, set(), completed.stdout)
        self.assertTrue((destination / "maelys-system" / ".git").exists())
        self.assertFalse((destination / "maelys-extra").exists())
        self.assertIn("checkout-dependencies: maelys-extra is cloned on request only"
                      " (dependencies/maelys-extra.pin): skipped", completed.stderr)
        # The job that reads it asks for it, with the script it always used.
        by_name = subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependency.sh"),
                                  "maelys-extra", str(destination / "maelys-extra")],
                                 cwd=self.product.dir, env=self.product.env, check=False, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(by_name.returncode, 0, by_name.stderr)
        self.assertEqual(self.product.git(destination / "maelys-extra", "rev-parse", "HEAD"), self.product.pinned)

    def test_each_clone_says_how_long_it_took(self) -> None:
        """That cost was invisible: it took comparing two runs to see it."""
        self.product.run("adopt", self.dir, "--apply")
        completed = self.loop(self.product.work / "timed")
        self.assertRegex(completed.stderr, r"checkout-dependencies: maelys-system cloned in \d+s")
        self.assertNotRegex(completed.stderr, r"maelys-extra cloned in")

    def test_the_command_names_it_and_brings_it_only_when_asked(self) -> None:
        self.product.env["XDG_CACHE_HOME"] = str(self.product.work / "cache")
        planned = {entry["name"]: entry["action"]
                   for entry in self.product.json("dependencies", self.dir)["data"]["dependencies"]}
        self.assertEqual(planned, {"maelys-extra": "on-request", "maelys-system": "clone"})
        text = self.product.run("dependencies", self.dir).stdout
        self.assertRegex(text, r"request  maelys-extra \S+ \([0-9a-f]{7}\) is cloned on request only:"
                               r" pass --all to bring it here")
        self.product.run("dependencies", self.dir, "--apply")
        self.assertTrue((self.home / "maelys-system" / ".git").exists())
        self.assertFalse((self.home / "maelys-extra").exists())
        everything = {entry["name"]: entry["action"]
                      for entry in self.product.json("dependencies", self.dir, "--all")["data"]["dependencies"]}
        self.assertEqual(everything, {"maelys-extra": "clone", "maelys-system": "same"})
        self.product.run("dependencies", self.dir, "--all", "--apply")
        self.assertEqual(self.product.git(self.home / "maelys-extra", "rev-parse", "HEAD"), self.product.pinned)

    def test_a_release_build_that_names_its_pins_leaves_it_out(self) -> None:
        """The older layout writes one line per pin into release.yml; the
        current one calls the loop, which skips it by itself."""
        decl = MODULE.read_declarations(self.product.dir, "maelys-fixture", "maelys-release")
        apart = MODULE.release_workflow(decl, "f" * 40, "v9.9.9", "0.0.0")
        self.assertIn("sh scripts/checkout-dependencies.sh", apart)
        decl.dependencies_apart = False
        named = MODULE.release_workflow(decl, "f" * 40, "v9.9.9", "0.0.0")
        self.assertEqual(re.findall(r"sh scripts/checkout-dependency\.sh (\S+)", named), ["maelys-system"])

    def test_a_product_whose_only_pin_is_on_request_asks_the_release_for_no_checkout(self) -> None:
        (self.product.dir / "dependencies" / "maelys-system.pin").unlink()
        decl = MODULE.read_declarations(self.product.dir, "maelys-fixture", "maelys-release")
        self.assertEqual(decl.dependencies, ["maelys-extra"])
        self.assertNotIn("dependency_checkout", MODULE.release_workflow(decl, "f" * 40, "v9.9.9", "0.0.0"))
