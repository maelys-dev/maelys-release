# SPDX-License-Identifier: MPL-2.0
"""A product budget is declared, generated and bounded even on direct calls."""
from __future__ import annotations

import os
import pathlib
import subprocess
import unittest

from test_maelys_release import APART, CI_CALL, MODULE, PACKAGE, ROOT, Product


class CITimeoutDeclarationTest(unittest.TestCase):
    def test_only_whole_minutes_in_range_are_accepted(self) -> None:
        for minutes in (5, 30, 45, 60):
            with self.subTest(minutes=minutes):
                self.assertEqual(PACKAGE.declarations.parse_release(
                    f"[ci-timeouts]\ncheck {minutes}\n")[-1], {"check": minutes})
        self.assertEqual(PACKAGE.declarations.parse_release("")[-1], {})
        for line in ("check", "check 0", "check 4", "check 61", "check -1", "check +30",
                     "check 30.5", "check 030", "check 3e1", "check ${{ 30 }}", "check true",
                     "check 45 extra", "fuzz 45", "unknown 45", "check 45\ncheck 45"):
            with self.subTest(line=line), self.assertRaises(PACKAGE.declarations.CITimeoutError):
                PACKAGE.declarations.parse_release(f"[ci-timeouts]\n{line}\n")
        for text in ("[ci]\nown\n[ci-timeouts]\ncheck 45\n",
                     "[ci-timeouts]\ncheck 45\n[ci]\nown\n"):
            with self.assertRaisesRegex(PACKAGE.declarations.CITimeoutError, "calls none"):
                PACKAGE.declarations.parse_release(text)

    def test_writer_preserves_the_rest_and_removes_the_input(self) -> None:
        original = CI_CALL.replace("      product:", "      # Keep this comment.\n      product:")
        original = original.replace("  mine:", "  mine:\n    timeout-minutes: 17")
        original = original.replace("      product: maelys-fixture", "      product: maelys-fixture\n"
                                    "      check_command: make custom-check\n"
                                    "      macos_runner: '\"macos-15\"'")
        self.assertEqual(PACKAGE.workflows.ci_check_timeout(original, {}), original)
        written = PACKAGE.workflows.ci_check_timeout(original, {"check": 45})
        self.assertEqual(written.replace("      check_timeout_minutes: 45\n", ""), original)
        self.assertEqual(PACKAGE.workflows.ci_check_timeout(written, {"check": 45}), written)
        shortened = PACKAGE.workflows.ci_check_timeout(written, {"check": 30})
        self.assertEqual(shortened.count("check_timeout_minutes:"), 1)
        self.assertIn("check_timeout_minutes: 30", shortened)
        self.assertEqual(PACKAGE.workflows.ci_check_timeout(shortened, {}), original)
        foreign = "jobs:\n  mine:\n    runs-on: ubuntu-26.04\n"
        self.assertEqual(PACKAGE.workflows.ci_check_timeout(foreign, {"check": 45}), foreign)

    def test_words_inside_the_product_command_are_not_managed_inputs(self) -> None:
        text = CI_CALL.replace("      product: maelys-fixture", "      product: maelys-fixture\n"
                               "      check_command: |\n"
                               "        check_timeout_minutes: this is command data\n"
                               "        macos_runner: this is command data too")
        self.assertEqual(PACKAGE.workflows.ci_check_timeout(text, {}), text)
        self.assertEqual(PACKAGE.workflows.ci_macos_runner(text, {}), text)

    def test_creation_and_readoption_have_identical_input_order(self) -> None:
        decl = PACKAGE.declarations.Declarations(pathlib.Path("/nonexistent"), "fixture")
        decl.ci_timeouts = {"check": 45}
        decl.runners = {"macos": ["self-hosted", "macOS"]}
        created = PACKAGE.workflows.ci_workflow(decl, "a" * 40, "v9.9.9", "9.9.9")
        rewritten = PACKAGE.workflows.ci_check_timeout(
            PACKAGE.workflows.ci_macos_runner(created, decl.runners), decl.ci_timeouts)
        self.assertEqual(created, rewritten)
        release = PACKAGE.workflows.release_workflow(decl, "a" * 40, "v9.9.9", "9.9.9")
        decl.ci_timeouts = {}
        self.assertEqual(PACKAGE.workflows.release_workflow(decl, "a" * 40, "v9.9.9", "9.9.9"), release)


class CITimeoutAdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)

    def test_default_explicit_budget_drift_and_removal(self) -> None:
        p = self.product
        p.run("adopt", p.dir, "--apply")
        original = p.read(".github/workflows/ci.yml")
        release = p.read(".github/workflows/release.yml")
        data = p.json("declarations", p.dir)["data"]
        self.assertEqual(data["ciTimeouts"], {"check": 30})
        self.assertEqual(data["declared"]["ciTimeouts"], {})
        self.assertNotIn("check_timeout_minutes", original)
        p.write("maelys-release.conf", APART + "\n[ci-timeouts]\ncheck 45\n")
        p.run("adopt", p.dir, "--apply")
        written = p.read(".github/workflows/ci.yml")
        self.assertIn("check_timeout_minutes: 45", written)
        self.assertEqual(p.read(".github/workflows/release.yml"), release)
        data = p.json("declarations", p.dir)["data"]
        self.assertEqual(data["ciTimeouts"], {"check": 45})
        self.assertEqual(data["declared"]["ciTimeouts"], {"check": 45})
        self.assertTrue(p.json("check", p.dir)["data"]["valid"])
        p.run("adopt", p.dir, "--apply")
        self.assertEqual(p.read(".github/workflows/ci.yml"), written)
        p.write(".github/workflows/ci.yml", written.replace("check_timeout_minutes: 45", "check_timeout_minutes: 60"))
        drift = p.json("check", p.dir, expect=2)["data"]
        self.assertTrue(any("ci.yml" in v for v in drift["violations"]), drift)
        p.write("maelys-release.conf", APART)
        p.run("adopt", p.dir, "--apply")
        self.assertEqual(p.read(".github/workflows/ci.yml"), original)

    def test_initial_adoption_already_has_the_budget(self) -> None:
        p = self.product
        p.write("maelys-release.conf", APART + "\n[ci-timeouts]\ncheck 45\n")
        p.run("adopt", p.dir, "--apply")
        first = p.read(".github/workflows/ci.yml")
        self.assertIn("check_timeout_minutes: 45", first)
        p.run("adopt", p.dir, "--apply")
        self.assertEqual(p.read(".github/workflows/ci.yml"), first)

    def test_invalid_budget_is_also_a_violation_for_a_custom_release(self) -> None:
        p = self.product
        p.write("maelys-release.conf", APART + "\n[ci-timeouts]\ncheck 99\n")
        decl = PACKAGE.reading.read_declarations(MODULE.CONTEXT, p.dir, "maelys-fixture", "custom")
        self.assertFalse(decl.valid)
        self.assertTrue(any(c["scope"] == "conventions" and "5 to 60" in c["message"]
                            for c in decl.applicable()))

    def test_budget_without_shared_ci_is_refused(self) -> None:
        p = self.product
        p.write("maelys-release.conf", APART + "\n[ci-timeouts]\ncheck 45\n")
        p.write(".github/workflows/ci.yml", "jobs:\n  mine:\n    runs-on: ubuntu-26.04\n")
        decl = PACKAGE.reading.read_declarations(MODULE.CONTEXT, p.dir, "maelys-fixture", "custom")
        self.assertFalse(decl.valid)
        self.assertTrue(any("no job would use it" in c["message"] for c in decl.applicable()))


class CITimeoutWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / ".github/workflows/check-product.yml").read_text(encoding="utf-8")
        self.jobs = PACKAGE.workflows.workflow_jobs(self.text)

    def test_default_and_validation_are_before_checkout(self) -> None:
        input_body = self.text.split("      check_timeout_minutes:\n", 1)[1].split("      sanitizer_command:", 1)[0]
        self.assertIn("        type: number\n", input_body)
        self.assertIn("        default: 30\n", input_body)
        body = self.jobs["check"]
        self.assertLess(body.index("- name: Validate check timeout"), body.index("- uses: actions/checkout@"))
        self.assertIn("CHECK_TIMEOUT_MINUTES: ${{ toJSON(inputs.check_timeout_minutes) }}", body)
        for job in ("fuzz", "sanitizers"):
            self.assertIn("    timeout-minutes: 30\n", self.jobs[job])
            self.assertNotIn("inputs.check_timeout_minutes", self.jobs[job])

    def test_actual_guard_rejects_invalid_values_without_running_product_code(self) -> None:
        # Execute the workflow's own script, not a reimplementation of its
        # condition. The marker stands where checkout/product commands start.
        step = self.jobs["check"].split("      - name: Validate check timeout\n", 1)[1]
        script = step.split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        script = "\n".join(line[10:] for line in script.splitlines())
        self.assertNotIn("${{", script, "inputs must enter through env, never shell source")
        for value in ("5", "30", "45", "60", "0", "4", "61", "9999", "-1", "30.5", "30.0",
                      "3e1", "", "true", '"45"', "$(echo injected)", "45\necho injected"):
            with self.subTest(value=value):
                done = subprocess.run(["bash", "-c", script + "\nprintf 'PRODUCT_REACHED\\n'\n"],
                                      env={"PATH": os.defpath, "CHECK_TIMEOUT_MINUTES": value},
                                      capture_output=True, text=True, timeout=5)
                if value in ("5", "30", "45", "60"):
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "PRODUCT_REACHED\n", ""))
                else:
                    self.assertEqual(done.returncode, 1)
                    self.assertEqual(done.stdout, "")
                    self.assertIn("must be an integer from 5 to 60", done.stderr)


if __name__ == "__main__":
    unittest.main()
