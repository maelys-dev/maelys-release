# SPDX-License-Identifier: MPL-2.0
"""The formula is rendered and linted before the tag, not by the tag.

A product published a version with no formula in the tap and had to cut the
next patch for that alone: nothing between `preflight` and the tag ever
looked at the template, and the first thing that did was the release
itself — the one moment nothing can be retried cheaply.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

from test_maelys_release import MODULE, ROOT, FakeHost, Product, using_host

from maelys_socle.release_checks import formula_checks, style_offenses


class BrewHost(FakeHost):
    """A host where brew is present or absent, and says what it was asked."""

    def __init__(self, brew: bool = True, status: int = 0, diagnostic: str = ""):
        super().__init__()
        self.brew = brew
        self.status = status
        self.diagnostic = diagnostic
        self.styled: list[str] = []

    def which(self, name):
        if name == "brew":
            return "/fake/brew" if self.brew else None
        return super().which(name)

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if command[:2] == ["brew", "style"]:
            self.styled.append(command[2])
            return subprocess.CompletedProcess(command, self.status, self.diagnostic, "")
        if command == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/maelys-dev/maelys-fixture.git\n", "")
        return super().run(command, cwd=cwd, env=env)


class FormulaChecksTest(unittest.TestCase):
    TEMPLATE = ('class MaelysFixture < Formula\n'
                '  desc "Fixture"\n'
                '  homepage "https://example.invalid"\n'
                '  url "@URL@"\n'
                '  version "@VERSION@"\n'
                '  sha256 "@SHA256@"\n'
                'end\n')

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        # One formula, so what a test reads is one line.
        (self.product.dir / "packaging" / "homebrew" / "libmaelys-fixture.rb.in").unlink()
        self.product.write("packaging/homebrew/maelys-fixture.rb.in", self.TEMPLATE)

    def declarations(self):
        return MODULE.read_declarations(self.product.dir, "maelys-fixture", "maelys-release")

    def check(self, host):
        # The declarations are read with the real host: what the fake gates
        # is the function under test, not the reading that feeds it.
        decl = self.declarations()
        with using_host(host):
            return formula_checks(decl)

    def test_a_template_that_renders_and_passes_style_is_ok(self) -> None:
        host = BrewHost()
        found = self.check(host)
        self.assertEqual(found, [("ok", "maelys-fixture renders from its template and passes brew style")])
        # Linted under its own name: brew reads the class name against the
        # file name, and a formula linted as something else is not linted.
        self.assertEqual([path.rsplit("/", 1)[-1] for path in host.styled], ["maelys-fixture.rb"])

    def test_an_unrendered_placeholder_is_refused_and_named(self) -> None:
        """The refusal the tap job makes at the tag, made before it."""
        self.product.write("packaging/homebrew/maelys-fixture.rb.in",
                           self.TEMPLATE.replace('version "@VERSION@"', 'version "@RELEASE@"'))
        host = BrewHost()
        status, message = self.check(host)[0]
        self.assertEqual(status, "fail")
        self.assertIn("@RELEASE@", message)
        self.assertEqual(host.styled, [], "a formula that does not render is not linted")

    def test_brew_refusing_the_formula_is_a_refusal_with_its_diagnostic(self) -> None:
        host = BrewHost(status=1, diagnostic="maelys-fixture.rb:4:3: C: unexpected token\n")
        status, message = self.check(host)[0]
        self.assertEqual(status, "fail")
        self.assertIn("unexpected token", message)

    def test_without_brew_the_formula_still_renders_and_the_line_says_so(self) -> None:
        """Ubuntu has no brew, and a note is not a failure: what cannot be
        read here is read at the tag."""
        host = BrewHost(brew=False)
        status, message = self.check(host)[0]
        self.assertEqual(status, "note")
        self.assertIn("brew is not installed", message)
        self.assertEqual(host.styled, [])

    def test_a_product_that_renders_with_its_own_script_is_named_not_guessed(self) -> None:
        """Its script hashes the archive of a tag that does not exist yet."""
        self.product.write("scripts/render-homebrew-formula.sh", "#!/bin/sh\nexit 0\n", executable=True)
        host = BrewHost()
        status, message = self.check(host)[0]
        self.assertEqual(status, "note")
        self.assertIn("the product's own script", message)
        self.assertEqual(host.styled, [])


class TheSocleHoldsItsOwnFormulaToItTest(unittest.TestCase):
    """And the socle's own formula is read by the same rule.

    `preflight` does not apply to this repository -- it publishes through
    its own mechanism -- so the check that now guards every product's
    formula would have left the socle's out, which is the class of defect
    two product reviews named: the socle is the repository it serves worst.
    Here it is held to it, from its own checkout.
    """

    def test_the_template_of_this_repository_renders(self) -> None:
        decl = MODULE.read_declarations(ROOT, "maelys-release", "custom")
        self.assertEqual(decl.formulas, ["maelys-release"], "this repository carries one formula template")
        found = formula_checks(decl)
        self.assertEqual([status for status, _ in found if status == "fail"], [], found)
        # Both ends of the rule, and this test first read only one: the
        # Ubuntu runner has no brew, so there the template renders and the
        # line says its style is read at the tag -- a note, reached only once
        # no placeholder is left, so it still proves the render. With brew,
        # it is an ok, and on a machine with the fleet's tap installed a
        # note follows, because brew sees the same class twice.
        if shutil.which("brew"):
            self.assertEqual(found[0][0], "ok", found)
        else:
            self.assertEqual(found[0], ("note", "maelys-release: the formula renders; brew is not installed"
                                                " here, so its style is read at the tag and not before"), found)
        self.assertEqual({status for status, _ in found[1:]} - {"note"}, set(), found)


class StyleOffenseTest(unittest.TestCase):
    """Which offenses belong to the file, and which to the machine.

    With the fleet's tap tapped, `brew style` reports
    `Lint/DuplicateMethods` on the rendered formula, naming the tap's
    installed copy: the class is defined twice in one RuboCop run. The
    runner that lints at the tag has no tap, so that offense exists on a
    maintainer's machine and nowhere else -- the worst place for a false
    refusal, since that is where `preflight` runs.
    """

    LINTED = pathlib.Path("/tmp/x/maelys-release.rb")

    def test_a_duplicate_defined_in_another_file_is_set_aside_and_reported(self) -> None:
        output = (f"{self.LINTED}:28:3: W: Lint/DuplicateMethods: Method MaelysRelease#install is defined at"
                  f" both /opt/homebrew/Library/Taps/maelys-dev/homebrew-tap/Formula/maelys-release.rb:28"
                  f" and {self.LINTED}:28.\n1 file inspected, 1 offense detected\n")
        kept, elsewhere = style_offenses(output, self.LINTED)
        self.assertEqual(kept, [])
        self.assertEqual(len(elsewhere), 1)
        self.assertIn("Lint/DuplicateMethods", elsewhere[0])

    def test_a_duplicate_inside_the_file_itself_stays_a_refusal(self) -> None:
        output = (f"{self.LINTED}:30:3: W: Lint/DuplicateMethods: Method MaelysRelease#install is defined at"
                  f" both {self.LINTED}:28 and {self.LINTED}:30.\n")
        kept, elsewhere = style_offenses(output, self.LINTED)
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(elsewhere, [])

    def test_any_other_cop_is_a_refusal_wherever_it_points(self) -> None:
        output = f"{self.LINTED}:4:3: C: FormulaAudit/Homepage: Formula should have a homepage.\n"
        kept, _ = style_offenses(output, self.LINTED)
        self.assertEqual(kept, ["FormulaAudit/Homepage: Formula should have a homepage."])

    def test_a_line_about_another_file_is_not_this_formula_s(self) -> None:
        output = "/elsewhere/other.rb:1:1: C: Style/Documentation: Missing top-level documentation.\n"
        self.assertEqual(style_offenses(output, self.LINTED), ([], []))
