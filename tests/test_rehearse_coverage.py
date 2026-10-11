# SPDX-License-Identifier: MPL-2.0
"""A rehearsal says which step it replayed, and what it did not.

Two products rehearsed their packaging on both Linux architectures, read the
green as "CI will be green", and had their pull request stopped by the check
legs of the shared CI. Their packaging exports clang; those legs name no
compiler and build with the runner's cc, which is gcc. Measured in the
rehearsal image before anything changed: `(void)read(...)` under `-O2 -Wall
-Werror` is refused by gcc and accepted by clang, and `rehearse --check`
already stopped it -- with gcc, as the leg. So the defect was never the
compiler of `--check`. It was a result that named what it built and not what
it left unproven, and a check mode that ran `make check` whatever line the
product's CI runs.

A review of the first version of this found the same defect one level down:
a result that named a job while steps of it were not run, and a reader of
ci.yml that guessed -- an empty line passes `bash -e`, so a check_command it
misread ran nothing and reported the leg replayed. These hold what came out
of it: a step is named and the rest of its job listed; the line is read for
certain or refused before anything is written; and the check runs as the
step does, as far as a container can.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from test_check_product_compiler import WORKFLOW, job_environment
from test_maelys_release import MODULE, ROOT, Product, using_host
from test_rehearse_host import RehearsalHost

from maelys_socle import constants, rehearse
from maelys_socle import workflows as rules

LEGS = ["check / check (linux)", "check / check (linux-arm64)", "check / check (macos)"]
CALL = "    with:\n      product: maelys-fixture\n"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
OWN_RELEASE = "name: release\n\non:\n  push:\n    tags: [\"v*\"]\n\njobs:\n  release:\n    runs-on: ubuntu-26.04\n" \
              "    steps:\n      - run: echo mine\n"


def named_steps(job: str) -> list[str]:
    return re.findall(r"^      - name: (.+)$", job, re.MULTILINE)


class Rehearsed(unittest.TestCase):
    """A fixture the socle adopted, and a host that stands for docker."""

    OWN_MECHANISM = False

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        if self.OWN_MECHANISM:
            self.product.write(".github/workflows/release.yml", OWN_RELEASE)
        self.product.run("adopt", str(self.product.dir), "--apply")
        self.ci = self.product.dir / ".github" / "workflows" / "ci.yml"

    def rewrite(self, old: str, new: str) -> None:
        text = self.ci.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.ci.write_text(text.replace(old, new), encoding="utf-8")

    def passes(self, lines: str) -> None:
        """Add inputs to the call of the shared CI, as a product writes them."""
        self.rewrite(CALL, CALL + lines)

    def line_of(self, needle: str) -> int:
        return next(number for number, line in enumerate(self.ci.read_text(encoding="utf-8").splitlines(), 1)
                    if needle in line)

    def invoke(self, *args: str, form: str = "json", status: int = 0):
        host = RehearsalHost(self.product, "container", status=status)
        out, err = io.StringIO(), io.StringIO()
        with using_host(host), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["rehearse", str(self.product.dir), *args, *Product.SOCLE, "--format", form])
        return code, out.getvalue(), err.getvalue(), host

    def data(self, *args: str) -> dict:
        code, out, err, _ = self.invoke(*args)
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def environment(self, *args: str) -> dict:
        """What the container is started with, by name."""
        code, _, err, host = self.invoke(*args)
        self.assertEqual(code, 0, err)
        command = host.streams[0][0]
        return dict(command[index + 1].split("=", 1) for index, word in enumerate(command) if word == "-e")

    def refusal(self, *args: str, code: str = "PRECONDITION_FAILED") -> dict:
        """A refusal that came before anything ran and before anything was written."""
        status, out, err, host = self.invoke(*args)
        self.assertEqual((status, out, host.streams), (1, "", []), err)
        self.assertFalse((self.product.dir / "dist").exists(), "a refusal follows a write")
        error = json.loads(err[err.index("{"):])["error"]
        self.assertEqual(error["code"], code, error)
        return error


class WhatAResultSaysTest(Rehearsed):
    AFTER_PACKAGE = ["Read the subject the SBOM names", "Provenance attestation", "SBOM attestation",
                     "Keep both attestation bundles with the artifacts", "No provenance attestation",
                     "Hand the artifacts to publish"]

    def test_a_package_names_its_step_and_everything_it_did_not_run(self) -> None:
        """The whole defect: a package that builds answered for none of them."""
        data = self.data("linux-arm64")
        self.assertEqual(data["mode"], "package")
        self.assertEqual(data["replayed"], "the Package step of release.yml's build job for linux-arm64")
        self.assertEqual(data["command"], "bash scripts/package-release.sh linux-arm64")
        self.assertEqual(data["notReplayedSteps"], self.AFTER_PACKAGE)
        # The sanitizers job is on by default, and counted from the workflow.
        self.assertEqual(data["notReplayed"], [*LEGS, "check / sanitizers"])
        self.assertNotIn("unlikeTheRunner", data)

    def test_a_product_that_verifies_before_it_packages_is_told_the_rehearsal_did_not(self) -> None:
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\nexit 0\n", executable=True)
        self.assertEqual(self.data("linux-arm64")["notReplayedSteps"],
                         ["Verify before packaging", *self.AFTER_PACKAGE])

    def test_a_target_that_does_not_package_replays_no_step(self) -> None:
        """`[package]` leaves a target out: the real job verifies there and
        never packages, so a rehearsal that packaged replayed nothing of it."""
        conf = self.product.read("maelys-release.conf")
        self.product.write("maelys-release.conf", conf + "\n[package]\nlinux-x86_64\n")
        self.assertEqual(self.data("linux-arm64")["replayed"], "")
        self.assertEqual(self.data("linux-x86_64")["replayed"],
                         "the Package step of release.yml's build job for linux-x86_64")
        _, out, _, _ = self.invoke("linux-arm64", form="text")
        self.assertIn("replayed      no step: the build job of the release does not run this for linux-arm64", out)

    def test_a_package_command_given_by_hand_names_no_step(self) -> None:
        """As `--check-command`: what is passed by hand is not what the workflow runs."""
        data = self.data("linux-arm64", "--package-command", "bash scripts/other.sh TARGET")
        self.assertEqual((data["replayed"], data["command"]), ("", "bash scripts/other.sh linux-arm64"))
        same = self.data("linux-arm64", "--package-command", "bash scripts/package-release.sh TARGET")
        self.assertEqual(same["replayed"], "the Package step of release.yml's build job for linux-arm64")

    def test_a_check_names_its_step_and_the_rest_of_its_job(self) -> None:
        """The linux leg ends on the socle's drift check, which no rehearsal runs."""
        for target, leg, steps in (("linux-arm64", "check / check (linux-arm64)", []),
                                   ("linux-x86_64", "check / check (linux)", ["Release socle drift"])):
            with self.subTest(target=target):
                data = self.data(target, "--check")
                self.assertEqual((data["mode"], data["replayed"], data["command"]),
                                 ("check", f"the Check step of {leg}", "make check"))
                self.assertEqual(data["notReplayedSteps"], steps)
                self.assertEqual(data["notReplayed"], [name for name in [*LEGS, "check / sanitizers"] if name != leg])
                self.assertEqual(data["unlikeTheRunner"], list(rehearse.UNLIKE_THE_RUNNER))

    def test_the_bundles_a_call_carries_are_not_what_the_rehearsal_clones(self) -> None:
        self.passes("      carried_dependencies: maelys-dependency-bundles\n")
        self.assertEqual(self.data("linux-arm64", "--check")["notReplayedSteps"],
                         ["Carried dependencies", "Point the pinned checkouts at the carried bundles"])

    def test_the_jobs_a_call_turns_on_are_counted(self) -> None:
        """`fuzz` is opt-in and `sanitizers` opt-out: read from the call, as protect reads them."""
        self.passes("      sanitizer_command: ''\n      fuzz_command: make fuzz-smoke\n")
        self.assertEqual(self.data("linux-arm64")["notReplayed"], [*LEGS, "check / fuzz"])

    def test_the_text_of_a_package_says_what_it_did_not_prove(self) -> None:
        code, out, err, _ = self.invoke("linux-arm64", form="text")
        self.assertEqual(code, 0, err)
        self.assertIn("rehearse: linux-arm64 built fixture-linux-arm64.tgz", out)
        self.assertIn("replayed      the Package step of release.yml's build job for linux-arm64:"
                      " bash scripts/package-release.sh linux-arm64", out)
        self.assertIn("not replayed  of that job: Read the subject the SBOM names", out)
        self.assertIn("              of the shared CI: check / check (linux), check / check (linux-arm64)", out)
        self.assertIn("A package that builds says nothing of them", out)
        self.assertIn("'maelys-release rehearse DIR linux-arm64 --check' replays the Check step of"
                      " check / check (linux-arm64).", out)

    def test_the_text_of_a_check_says_what_a_container_is_not(self) -> None:
        code, out, err, _ = self.invoke("linux-x86_64", "--check", form="text")
        self.assertEqual(code, 0, err)
        self.assertIn("rehearse: check passed on linux-x86_64", out)
        self.assertIn("replayed      the Check step of check / check (linux): make check", out)
        self.assertIn("not replayed  of that job: Release socle drift", out)
        self.assertIn("              of the shared CI: check / check (linux-arm64), check / check (macos),"
                      " check / sanitizers", out)
        self.assertIn("unlike        user: root here", out)
        self.assertIn("[linux] of dependencies/packages", out)
        self.assertIn("its merge commit", out)
        # Nothing invites a second check of the leg it has just replayed.
        self.assertNotIn("--check' replays", out)

    def test_the_log_names_the_step_and_where_its_line_was_read(self) -> None:
        self.passes("      check_command: make check install-check\n")
        _, _, err, _ = self.invoke("linux-arm64", "--check")
        self.assertIn("rehearse: the Check step of check / check (linux-arm64): make check install-check"
                      f" (check_command of .github/workflows/ci.yml, line {self.line_of('check_command')})", err)

    def test_a_product_whose_ci_does_not_call_the_shared_one_replays_no_step(self) -> None:
        """Said, rather than a leg invented for a workflow nothing calls --
        and never under the name of a default no call was read to take."""
        self.ci.unlink()
        data = self.data("linux-arm64", "--check")
        self.assertEqual((data["replayed"], data["notReplayed"], data["command"]), ("", [], "make check"))
        _, out, err, _ = self.invoke("linux-arm64", "--check", form="text")
        self.assertIn("no step of the shared CI, which this repository's ci.yml does not call: make check", out)
        self.assertIn("(no call of check-product.yml in .github/workflows/ci.yml)", out)
        self.assertNotIn("the default of check-product.yml", out + err)


class TheLineOfTheLegTest(Rehearsed):
    """`make check` was run whatever the product's CI runs.

    Three products of the fleet pass a line of their own; one of them has no
    `make check` in it at all. The reader is line-based: the socle carries no
    YAML parser. So it reads the shapes it reads exactly, and refuses the
    others before anything runs.
    """

    READ = (
        ("check_command: make check install-check", "make check install-check"),
        ("check_command: 'make ci-check'   # the legs only", "make ci-check"),
        ("check_command: 'make ''quoted'' check'", "make 'quoted' check"),
        ('check_command: "make -j4 check"', "make -j4 check"),
        # The reader stopped at the first inner quote and ran `make check ARGS=\`.
        ('check_command: "make check ARGS=\\"-v\\" && make more"   # two', 'make check ARGS="-v" && make more'),
        ("check_command: |\n        make check\n        make analyze", "make check\nmake analyze"),
        ("check_command: |-   # as two steps\n        make check\n\n        make analyze", "make check\n\nmake analyze"),
        # Two blank lines above the key: the indentation was counted across them.
        ("\n\n      check_command: |\n        make check\n        make analyze", "make check\nmake analyze"),
        ("check_command: >-\n        make check\n        analyze", "make check analyze"),
        # YAML folds a line break into a space and keeps a blank line as one.
        ("check_command: >\n        make a\n\n        make b", "make a\nmake b"),
        ("check_command: true", "true"),
    )

    REFUSED = (
        ("a plain value over several lines", "check_command:\n        make check\n        make analyze", "check_command:"),
        ("a plain value continued", "check_command: make check\n        install-check", "check_command:"),
        ("no value", "check_command:", "check_command:"),
        ("an empty string", "check_command: ''", "check_command:"),
        ("an empty block", "check_command: |", "check_command:"),
        ("a block of blank lines", "check_command: |\n\n\n      fuzz_command: make fuzz", "check_command:"),
        ("an expression", "check_command: make ${{ matrix.goal }}", "check_command:"),
        ("an escape YAML has and JSON has not", 'check_command: "make \\e check"', "check_command:"),
        ("a quote that does not close", "check_command: 'make check", "check_command:"),
        ("an alias", "check_command: *line", "check_command:"),
        ("a flow sequence", "check_command: [make, check]", "check_command:"),
        ("a plain value YAML reads as a mapping", "check_command: make check ARGS: -v", "check_command:"),
        ("a folded block with deeper lines", "check_command: >\n        make a\n          deeper\n        make b",
         "check_command:"),
        ("an indentation indicator", "check_command: |2\n        make check", "check_command:"),
        ("a quoted key", '"check_command": make quoted', '"check_command"'),
        ("the line twice", "check_command: make one\n      check_command: make two", "make two"),
        ("a merge key", "<<: *shared", "<<:"),
    )

    def test_each_shape_it_reads_is_the_line_run(self) -> None:
        for written, run in self.READ:
            with self.subTest(written=written):
                self.setUp()
                self.passes(f"      {written}\n      sanitizer_command: make asan-ubsan\n")
                self.assertEqual(self.environment("linux-arm64", "--check")["CHECK_COMMAND"], run)
                data = self.data("linux-arm64", "--check")
                self.assertEqual((data["command"], data["replayed"]),
                                 (run, "the Check step of check / check (linux-arm64)"))

    def test_each_shape_it_does_not_read_is_refused_with_its_line(self) -> None:
        for label, written, needle in self.REFUSED:
            with self.subTest(shape=label):
                self.setUp()
                self.passes(f"      {written}\n      sanitizer_command: make asan-ubsan\n")
                error = self.refusal("linux-arm64", "--check")
                self.assertIn(f".github/workflows/ci.yml line {self.line_of(needle)}:", error["message"])
                self.assertIn("nothing was run", error["message"])
                self.assertIn("--check-command", error["hint"])
                # A package reads no line of the leg, and is not stopped by one.
                self.assertEqual(self.invoke("linux-arm64")[0], 0)

    def test_a_with_written_on_one_line_is_refused_and_never_read_as_passing_nothing(self) -> None:
        """The default ran, and was called the default of check-product.yml."""
        self.rewrite(CALL, "    with: {product: maelys-fixture, check_command: make flow}\n")
        error = self.refusal("linux-arm64", "--check")
        self.assertIn(f"line {self.line_of('with: {')}:", error["message"])
        self.assertNotIn("default", error["message"])

    def test_a_job_line_with_a_comment_is_still_the_job(self) -> None:
        """The job was not found: the default ran although the call passes
        another line, and the result named `pull_request / check (linux)`."""
        self.rewrite("  check:\n", "  check:   # the shared legs\n")
        self.passes("      check_command: make mine\n")
        data = self.data("linux-x86_64", "--check")
        self.assertEqual((data["command"], data["replayed"]), ("make mine", "the Check step of check / check (linux)"))
        self.assertNotIn("check / check (linux)", data["notReplayed"])
        self.assertIn("check / check (linux-arm64)", data["notReplayed"])

    def test_a_call_this_reader_cannot_place_is_refused(self) -> None:
        for label, old, new in (
                ("a job in flow style", None, "jobs:\n  check: {uses: " + rehearse.CALLED + "f" * 40 + "}\n"),
                ("a quoted job", "  check:\n", '  "check":\n'),
                ("a tab", CALL, "    with:\n\t  product: maelys-fixture\n"),
                ("a second call", None, None)):
            with self.subTest(shape=label):
                self.setUp()
                text = self.ci.read_text(encoding="utf-8")
                if label == "a second call":
                    job = text[text.index("  check:\n"):]
                    self.ci.write_text(text + job.replace("  check:\n", "  again:\n"), encoding="utf-8")
                elif old is None:
                    self.ci.write_text(text[:text.index("jobs:\n")] + new, encoding="utf-8")
                else:
                    self.rewrite(old, new)
                error = self.refusal("linux-arm64", "--check")
                self.assertRegex(error["message"], r"\.github/workflows/ci\.yml line \d+:")

    def test_without_one_it_is_the_default_of_the_workflow_and_only_then(self) -> None:
        default = re.search(r"check_command:\n(?:.*\n)*?\s+default: (.+)\n", WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(self.environment("linux-arm64", "--check")["CHECK_COMMAND"], default.group(1))
        _, _, err, _ = self.invoke("linux-arm64", "--check")
        self.assertIn("(the default of check-product.yml, the call passing none)", err)
        # A call that passes one never reads as the default.
        self.passes("      check_command: make mine\n")
        self.assertNotIn("the default of check-product.yml", self.invoke("linux-arm64", "--check")[2])

    def test_an_empty_line_is_never_run(self) -> None:
        for given in ("", "   ", "\n"):
            error = self.refusal("linux-arm64", "--check", "--check-command", given, code="VALIDATION_FAILED")
            self.assertIn("empty", error["message"])

    def test_a_line_given_by_hand_runs_where_the_legs_own_was_refused(self) -> None:
        """The way out the refusal names, and it names no step."""
        self.passes("      check_command:\n        make check\n        make analyze\n")
        data = self.data("linux-arm64", "--check", "--check-command", "make check && make analyze")
        self.assertEqual((data["command"], data["replayed"]), ("make check && make analyze", ""))
        self.assertIn("check / check (linux-arm64)", data["notReplayed"])

    def test_another_line_than_the_legs_replays_no_step(self) -> None:
        """`--check-command` looks at one target of a failure. What it passes
        is not what the pull request runs, so the leg stays unproven."""
        data = self.data("linux-arm64", "--check", "--check-command", "make build/one.o")
        self.assertEqual((data["command"], data["replayed"]), ("make build/one.o", ""))
        self.assertIn("check / check (linux-arm64)", data["notReplayed"])
        _, out, _, _ = self.invoke("linux-arm64", "--check", "--check-command", "make build/one.o", form="text")
        self.assertIn("no step: --check-command ran another line than check / check (linux-arm64) runs", out)
        # The same line, typed, is the leg's.
        self.assertEqual(self.data("linux-arm64", "--check", "--check-command", "make check")["replayed"],
                         "the Check step of check / check (linux-arm64)")

    def test_an_escape_sequence_in_the_line_never_reaches_the_terminal(self) -> None:
        """ci.yml is a file a pull request writes."""
        self.passes('      check_command: "printf \\u001b[2J; make check"\n')
        self.assertEqual(self.data("linux-arm64", "--check")["command"], "printf \x1b[2J; make check")
        _, out, _, host = self.invoke("linux-arm64", "--check", form="text")
        self.assertNotIn("\x1b", out)
        self.assertIn("printf \\x1b[2J; make check", out)
        environment = self.environment("linux-arm64", "--check")
        self.assertEqual(environment["CHECK_SHOWN"], "printf \\x1b[2J; make check")
        self.assertEqual(environment["CHECK_COMMAND"], "printf \x1b[2J; make check")


class WhateverPublishesTheProductTest(Rehearsed):
    """The check leg is not the release mechanism.

    A product that publishes by its own means and calls check-product.yml
    has that leg like any other. It was refused in the name of a release it
    never asked the socle for -- and it is one of the three whose line the
    check mode was changed to read.
    """

    OWN_MECHANISM = True

    def test_its_leg_is_rehearsed(self) -> None:
        self.passes("      check_command: make ci-check\n")
        data = self.data("linux-x86_64", "--check")
        self.assertEqual((data["command"], data["replayed"]),
                         ("make ci-check", "the Check step of check / check (linux)"))

    def test_its_packaging_is_still_not_the_socles(self) -> None:
        error = self.refusal("linux-x86_64")
        self.assertIn("custom mechanism", error["message"])
        error = self.refusal("--channel", "npm", "--tag", "v1.2.3")
        self.assertIn("custom mechanism", error["message"])

    def test_without_a_call_there_is_no_leg_to_rehearse(self) -> None:
        self.ci.unlink()
        error = self.refusal("linux-x86_64", "--check")
        self.assertIn("does not call check-product.yml", error["message"])


class RefusalsBeforeWritesTest(Rehearsed):
    def test_a_check_leaves_dist_alone_because_it_is_not_given_dist(self) -> None:
        code, _, err, host = self.invoke("linux-arm64", "--check")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.product.dir / "dist").exists())
        self.assertFalse([word for word in host.streams[0][0] if word.endswith(":/dist")])

    def test_a_package_creates_dist_once_nothing_can_refuse(self) -> None:
        self.assertEqual(self.invoke("linux-arm64")[0], 0)
        self.assertTrue((self.product.dir / "dist").is_dir())
        self.setUp()
        (self.product.dir / "scripts" / "checkout-dependencies.sh").chmod(0o644)
        self.refusal("linux-arm64")


class AsTheLegTest(Rehearsed):
    """What a check leg has that a rehearsal can have, each held against the workflow."""

    def setUp(self) -> None:
        super().setUp()
        self.job = rules.workflow_jobs(WORKFLOW.read_text(encoding="utf-8"))["check"]

    def test_a_check_installs_what_the_leg_installs_and_a_package_does_not(self) -> None:
        check = self.environment("linux-arm64", "--check")["LINUX_PACKAGES"].split()
        package = self.environment("linux-arm64")["LINUX_PACKAGES"].split()
        for name in constants.CHECK_JOB_PACKAGES.split():
            self.assertIn(name, check)
        self.assertIn("clang", check)
        # The build job of the release installs no compiler but gcc.
        self.assertNotIn("clang", package)
        self.assertEqual(check.count("build-essential"), 1)
        # The declared packages last, in both.
        self.assertEqual((check[-2:], package[-2:]), (["pkg-config", "libjansson-dev"],) * 2)

    def test_the_packages_are_the_ones_the_workflow_installs(self) -> None:
        """Held against the file: a list typed twice drifts."""
        installed = re.search(r"apt-get install -y (.*) \$\{\{ steps\.socle\.outputs\.linux_packages \}\}", self.job)
        self.assertEqual(installed.group(1), constants.CHECK_JOB_PACKAGES)

    def test_a_check_installs_with_the_recommended_packages_as_the_leg_does(self) -> None:
        """Without them clang arrives without its runtime libraries."""
        self.assertNotIn("--no-install-recommends", self.job)
        script = "set -eu\n" + re.search(r'recommended="--no-install-recommends"\n.*\n', rehearse.REHEARSAL).group(0) \
            + 'printf "[%s]" "$recommended"\n'
        for mode, options in (("check", "[]"), ("package", "[--no-install-recommends]")):
            said = subprocess.run(["sh", "-c", script], env={"PATH": os.environ["PATH"], "REHEARSE_MODE": mode},
                                  capture_output=True, text=True, check=True)
            self.assertEqual(said.stdout, options)
        self.assertIn("apt-get install -y -q $recommended git ca-certificates $LINUX_PACKAGES", rehearse.REHEARSAL)

    def test_a_mirror_that_does_not_answer_is_a_warning_as_in_the_leg(self) -> None:
        update = re.search(r"sudo (timeout -k 10 120 apt-get update) \|\| echo (\"::warning::.*\")", self.job)
        self.assertRegex(rehearse.REHEARSAL,
                         re.escape(update.group(1)) + r" -q >/dev/null \\\n\s+\|\| echo " + re.escape(update.group(2)))

    def test_the_step_is_bounded_as_its_job_is(self) -> None:
        self.assertEqual(re.search(r"^    timeout-minutes: (\d+)$", self.job, re.MULTILINE).group(1),
                         str(constants.CHECK_JOB_MINUTES))
        self.assertEqual(self.environment("linux-arm64", "--check")["CHECK_MINUTES"], str(constants.CHECK_JOB_MINUTES))
        self.assertIn('timeout -k 10 "${CHECK_MINUTES}m"', rehearse.CHECK_STEP)

    def test_the_legs_are_the_ones_the_workflow_runs(self) -> None:
        legs = re.findall(r"^\s+- leg: (\S+)$", self.job, re.MULTILINE)
        self.assertEqual(sorted(constants.CHECK_LEG_OF), sorted(constants.PLATFORMS))
        for target, leg in constants.CHECK_LEG_OF.items():
            self.assertIn(leg, legs)
            # And each on the architecture the target names.
            runner = re.search(rf"- leg: {re.escape(leg)}\n\s+runner: (.+)", self.job).group(1)
            self.assertEqual("arm" in runner, target.endswith("arm64"), runner)

    def test_no_compiler_reaches_a_check_because_none_reaches_the_leg(self) -> None:
        """The legs build with the runner's cc. If the workflow ever names a
        compiler there, this fails and the rehearsal has to follow it."""
        self.assertEqual({"CC", "CXX", "MAKEFLAGS"} & set(job_environment(self.job)), set())
        self.assertNotIn("CC=", self.job)
        for arguments in (("linux-arm64", "--check"), ("linux-arm64",)):
            self.assertEqual({"CC", "CXX", "MAKEFLAGS", "CFLAGS"} & set(self.environment(*arguments)), set())

    def test_every_step_of_the_two_jobs_is_one_the_rehearsal_has_an_answer_for(self) -> None:
        """A step added to a workflow fails here until somebody has said
        whether a rehearsal replays it, does it its own way, or leaves it."""
        build = rules.workflow_jobs(RELEASE.read_text(encoding="utf-8"))["build"]
        self.assertEqual(sorted(named_steps(self.job)), sorted(rehearse.CHECK_JOB_STEPS))
        self.assertEqual(sorted(named_steps(build)), sorted(rehearse.BUILD_JOB_STEPS))
        for steps in (rehearse.CHECK_JOB_STEPS, rehearse.BUILD_JOB_STEPS):
            self.assertEqual(set(steps.values()), {"replayed", "own", "macos", "left"})
            self.assertEqual(list(steps.values()).count("replayed"), 1)
        # The step each mode names is the one that runs the product's line.
        self.assertRegex(self.job, r"- name: Check\n\s+shell: bash\n\s+run: \$\{\{ inputs\.check_command \}\}")
        self.assertRegex(build, r"- name: Package\n(?:\s+.*\n)*?\s+eval \"\$\{PACKAGE_COMMAND//TARGET/\$TARGET\}\"")
        # The drift check runs on one leg, which is the one a result lists it for.
        self.assertRegex(self.job, r"- name: Release socle drift\n\s+if: matrix\.leg == 'linux'\n")
        # A step is `macos` because the workflow says so.
        for name, kind in [*rehearse.CHECK_JOB_STEPS.items(), *rehearse.BUILD_JOB_STEPS.items()]:
            where = self.job if name in rehearse.CHECK_JOB_STEPS and f"- name: {name}\n" in self.job else build
            condition = re.search(rf"- name: {re.escape(name)}\n\s+if: (.+)\n", where)
            self.assertEqual(kind == "macos", bool(condition and "runner.os == 'macOS'" in condition.group(1)), name)

    def test_the_line_a_package_runs_by_default_is_the_workflows(self) -> None:
        default = re.search(r"      package_command:\n(?:        .*\n)*?        default: (.+)\n",
                            RELEASE.read_text(encoding="utf-8"))
        self.assertEqual(default.group(1), rehearse.PACKAGE_DEFAULT)

    def test_a_tool_the_image_lacks_is_said_where_it_is_declared(self) -> None:
        """Two products call tools from their Makefile that the runner image
        carries and they never declared: red here where CI is green."""
        code, out, err, _ = self.invoke("linux-arm64", "--check", status=127)
        self.assertEqual((code, out), (1, ""))
        error = json.loads(err[err.index("{"):])["error"]
        self.assertIn("a command the image does not have (exit 127)", error["message"])
        self.assertIn("runner image carries tools this container does not", error["hint"])
        self.assertIn("[linux] of dependencies/packages", error["hint"])
        # A packaging that exits 127 is the product's own affair.
        err = self.invoke("linux-arm64", status=127)[2]
        error = json.loads(err[err.index("{"):])["error"]
        self.assertIn("failed with exit 127", error["message"])

    def test_a_check_cut_at_the_bound_says_so(self) -> None:
        _, _, err, _ = self.invoke("linux-arm64", "--check", status=124)
        self.assertIn(f"cut after {constants.CHECK_JOB_MINUTES} minutes, the bound of the job", err)


@unittest.skipUnless(shutil.which("bash"), "bash runs the step")
class TheStepItselfTest(unittest.TestCase):
    """The lines the container runs for a check, run here.

    `shell: bash` of a workflow writes the line to a file and runs `bash
    --noprofile --norc -eo pipefail FILE`, and the step's environment has none
    of the rehearsal's variables.
    """

    OWN = {"REHEARSE_MODE": "check", "TARGET": "linux-arm64", "LINUX_PACKAGES": "build-essential",
           "DEPENDENCIES": "one", "DEPENDENCIES_APART": "1", "PACKAGE_COMMAND": "false",
           "DEBIAN_FRONTEND": "noninteractive", "CHECK_SHOWN": "shown", "CHECK_MINUTES": "30"}

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-step."))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.path = os.environ["PATH"]
        if not shutil.which("timeout"):
            # macOS has none. What the bound does is measured where there is
            # one; here the word must only hand over to what follows it.
            shim = self.work / "bin"
            shim.mkdir()
            (shim / "timeout").write_text('#!/bin/sh\nshift 3\nexec "$@"\n', encoding="utf-8")
            (shim / "timeout").chmod(0o755)
            self.path = f"{shim}:{self.path}"

    def step(self, command: str, **extra: str) -> subprocess.CompletedProcess:
        environment = {"PATH": self.path, **self.OWN, "CHECK_COMMAND": command, "REHEARSE_WORK": str(self.work),
                       **extra}
        return subprocess.run(["bash", "-c", "set -euo pipefail\nimage_cc='cc 1.0'\n" + rehearse.CHECK_STEP
                               + "\necho packaged\n"],
                              env=environment, capture_output=True, text=True, check=False)

    def test_the_step_is_in_the_script_the_container_runs(self) -> None:
        self.assertIn(rehearse.CHECK_STEP.strip("\n"), rehearse.REHEARSAL)

    def test_it_is_the_shell_line_of_a_workflow_step_on_a_file(self) -> None:
        """GitHub's own words for `shell: bash`, and a script file rather than -c."""
        self.assertIn('bash --noprofile --norc -eo pipefail "$work/check-step.sh"', rehearse.CHECK_STEP)
        ran = self.step('echo "$0"')
        self.assertEqual(ran.stdout.splitlines()[-1], str(self.work / "check-step.sh"), ran.stderr)

    def test_none_of_the_rehearsals_variables_reaches_the_command(self) -> None:
        """make reads the environment: `TARGET ?= build/tool` would build
        linux-arm64 here and build/tool in the job."""
        names = [*self.OWN, "CHECK_COMMAND", "REHEARSE_WORK"]
        ran = self.step("; ".join(f'echo "{name}=${{{name}:-unset}}"' for name in names))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout.splitlines()[-len(names):], [f"{name}=unset" for name in names])

    @unittest.skipUnless(shutil.which("make"), "make is what reads the environment")
    def test_a_makefile_keeps_its_own_target(self) -> None:
        pathlib.Path(self.work, "Makefile").write_text("TARGET ?= build/tool\ncheck:\n\t@echo $(TARGET)\n",
                                                       encoding="utf-8")
        ran = self.step(f"make -C {self.work} --no-print-directory check")
        self.assertEqual(ran.stdout.splitlines()[-1], "build/tool", ran.stderr)
        # And it is the environment that would have changed it.
        leaked = subprocess.run(["make", "-C", str(self.work), "--no-print-directory", "check"],
                                env={"PATH": self.path, "TARGET": "linux-arm64"},
                                capture_output=True, text=True, check=True)
        self.assertEqual(leaked.stdout.strip(), "linux-arm64")

    def test_what_the_job_has_does_reach_it(self) -> None:
        ran = self.step('echo "$CI $LANG $MAELYS_DEPENDENCIES_DIR"', MAELYS_DEPENDENCIES_DIR="/work/dependencies",
                        LANG="fr_FR.UTF-8")
        self.assertEqual(ran.stdout.splitlines()[-1], "true C.UTF-8 /work/dependencies")

    def test_the_first_failing_line_stops_it_with_its_status(self) -> None:
        ran = self.step("echo one\n(exit 7)\necho two")
        self.assertEqual(ran.returncode, 7)
        self.assertEqual(ran.stdout.splitlines()[-1], "one")

    def test_a_pipeline_fails_on_its_first_command(self) -> None:
        self.assertEqual(self.step("(exit 5) | cat").returncode, 5)

    def test_an_unset_variable_is_not_an_error_there(self) -> None:
        """The job's shell has no -u; the script around this step does."""
        ran = self.step('echo "[$NEVER_SET_ANYWHERE]"')
        self.assertEqual((ran.returncode, ran.stdout.splitlines()[-1]), (0, "[]"))

    def test_the_line_printed_is_the_one_made_safe_to_print(self) -> None:
        ran = self.step("printf '\\033[2J'; true", CHECK_SHOWN="printf '\\\\033[2J'; true")
        self.assertIn("== Check: printf '\\\\033[2J'; true", ran.stdout.splitlines()[1])

    def test_a_check_never_packages(self) -> None:
        ran = self.step("true")
        self.assertEqual(ran.returncode, 0)
        self.assertNotIn("packaged", ran.stdout)
        self.assertIn("== The image's cc: cc 1.0; the environment names no compiler", ran.stdout)
        # And the package mode walks past the step.
        self.assertIn("packaged", self.step("false", REHEARSE_MODE="package").stdout)

    def test_a_command_the_machine_does_not_have_is_named_and_exits_127(self) -> None:
        for label, command in (("the shell", "tool-nobody-installed-0 --version"),
                               ("a script", "sh -c 'tool-nobody-installed-0 --version'"),
                               ("env", "env tool-nobody-installed-0 --version")):
            with self.subTest(through=label):
                ran = self.step(f"echo before\n{command}\necho after")
                self.assertEqual(ran.returncode, 127, ran.stdout + ran.stderr)
                self.assertIn("== Not in the image: tool-nobody-installed-0", ran.stderr)
                self.assertNotIn("packaged", ran.stdout)

    @unittest.skipUnless(shutil.which("make"), "make is what calls the tool")
    def test_a_tool_a_makefile_calls_is_named_too(self) -> None:
        pathlib.Path(self.work, "Makefile").write_text("check:\n\ttool-nobody-installed-1 --version\n",
                                                       encoding="utf-8")
        ran = self.step(f"make -C {self.work} check")
        self.assertEqual(ran.returncode, 127, ran.stdout + ran.stderr)
        self.assertIn("== Not in the image: tool-nobody-installed-1", ran.stderr)

    def test_a_failure_that_only_mentions_one_keeps_its_own_status(self) -> None:
        """`sh` is there: the words are in a log, the tool is not missing."""
        ran = self.step("echo 'wrapper: sh: command not found'; exit 3")
        self.assertEqual(ran.returncode, 3, ran.stderr)
        self.assertNotIn("Not in the image", ran.stderr)

    @unittest.skipUnless(shutil.which("timeout"), "timeout is what bounds it")
    def test_a_line_that_never_ends_is_cut_at_the_bound(self) -> None:
        ran = self.step("sleep 60", CHECK_MINUTES="0.02")
        self.assertEqual(ran.returncode, 124, ran.stderr)
        self.assertIn("== Cut after 0.02 minutes, the bound of the job", ran.stderr)


class TheReaderItselfTest(unittest.TestCase):
    """The folding, where YAML has a rule and a guess has another."""

    def read(self, text: str) -> str:
        call = rehearse.shared_ci_call(text)
        return rehearse.input_string(*call.inputs["check_command"])

    CI = ("jobs:\n"
          "  socle:   # maelys-egress names it so\n"
          "    uses: " + rehearse.CALLED + "0" * 40 + " # v1.0.0\n"
          "    with:\n"
          "      product: one\n")

    def test_the_job_is_the_one_that_makes_the_call(self) -> None:
        text = "on:\n  pull_request:\n\n" + self.CI + "      check_command: make it\n  other:\n    runs-on: x\n"
        call = rehearse.shared_ci_call(text)
        self.assertEqual((call.job, sorted(call.inputs)), ("socle", ["check_command", "product"]))
        self.assertIsNone(rehearse.shared_ci_call("jobs:\n  mine:\n    runs-on: ubuntu-26.04\n"))
        # A comment that names the workflow calls nothing.
        self.assertIsNone(rehearse.shared_ci_call("jobs:\n  mine:\n    # " + rehearse.CALLED + "x\n    runs-on: x\n"))

    def test_a_literal_block_keeps_its_lines_and_its_comments(self) -> None:
        self.assertEqual(self.read(self.CI + "      check_command: |\n        make a\n        # not a comment\n"
                                   "          deeper\n      fuzz_command: x\n"),
                         "make a\n# not a comment\n  deeper")

    def test_a_folded_block_folds_as_yaml_does(self) -> None:
        self.assertEqual(self.read(self.CI + "      check_command: >\n        a\n        b\n\n\n        c\n        d\n"),
                         "a b\n\nc d")

    def test_a_comment_between_two_inputs_belongs_to_neither(self) -> None:
        self.assertEqual(self.read(self.CI + "      sanitizer_command: make asan\n      # why\n"
                                   "      check_command: make it   # and why\n"), "make it")

    def test_a_block_ends_at_a_comment_indented_less(self) -> None:
        with self.assertRaises(rehearse.Unread):
            rehearse.shared_ci_call(self.CI + "      check_command: |\n        make a\n      # ends it\n        make b\n")


if __name__ == "__main__":
    unittest.main()
