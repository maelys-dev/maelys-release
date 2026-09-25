# SPDX-License-Identifier: MPL-2.0
"""The socle run from an installed prefix rather than from a checkout.

A product pins the socle by commit, so a copy that cannot name its own
commit cannot write a product's workflows: before this, an installed copy
refused every write and handed the operator a `git ls-remote` to run by
hand. The archive of a tag carries that commit in `INSTALLED.pin`, expanded
by git at archive time, and these tests hold both ends -- the expansion
itself, and what the program does with the file it finds.

The prefix is the layout the Homebrew formula installs: the executable and
its two neighbours in libexec/, and VERSION, CHANGELOG.md, LICENSE,
INSTALLED.pin and share/ beside it, which is where socle_root() looks.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORMULA = ROOT / "packaging" / "homebrew" / "maelys-release.rb.in"
WORKFLOW = ROOT / ".github" / "workflows" / "formula.yml"
COMMIT = "0" * 39 + "1"


def install_prefix(directory: pathlib.Path, pin: "str | None") -> pathlib.Path:
    """The tree the formula installs, with whatever INSTALLED.pin it carries."""
    prefix = directory / "prefix"
    (prefix / "libexec").mkdir(parents=True)
    for name in ("maelys-release", "maelys_cli.py"):
        shutil.copy2(ROOT / "bin" / name, prefix / "libexec" / name)
    shutil.copytree(ROOT / "bin" / "maelys_socle", prefix / "libexec" / "maelys_socle")
    for name in ("VERSION", "CHANGELOG.md", "LICENSE"):
        shutil.copy2(ROOT / name, prefix / name)
    shutil.copytree(ROOT / "share", prefix / "share")
    if pin is not None:
        (prefix / "INSTALLED.pin").write_text(pin, encoding="utf-8")
    return prefix


def run(prefix: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "MAELYS_RELEASE_NO_RELOCATE": "1"}
    return subprocess.run([str(prefix / "libexec" / "maelys-release"), *arguments],
                          capture_output=True, text=True, env=environment)


class InstalledCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="maelys-installed-")
        self.addCleanup(self.directory.cleanup)
        self.dir = pathlib.Path(self.directory.name)
        self.version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    def test_the_archive_of_a_tag_carries_its_commit(self) -> None:
        """Measured on git itself, not assumed: export-subst expands in the
        archive and stays a placeholder in the checkout. GitHub builds the
        tarball a formula downloads with git archive."""
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        self.assertIn("INSTALLED.pin export-subst",
                      (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        self.assertEqual((ROOT / "INSTALLED.pin").read_text(encoding="utf-8").splitlines(),
                         ["$Format:%(describe:tags)$", "$Format:%H$"])
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD", "INSTALLED.pin"],
                                 capture_output=True, check=True).stdout
        (self.dir / "archive.tar").write_bytes(archive)
        subprocess.run(["tar", "-xf", "archive.tar"], cwd=self.dir, check=True)
        commit = (self.dir / "INSTALLED.pin").read_text(encoding="utf-8").splitlines()[1]
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(commit, head)

    def test_an_installed_copy_writes_the_pin_it_was_archived_at(self) -> None:
        prefix = install_prefix(self.dir, f"v{self.version}\n{COMMIT}\n")
        product = self.dir / "product"
        done = run(prefix, "new", str(product), "--product", "maelys-probe", "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        workflow = (product / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(f"check-product.yml@{COMMIT} # v{self.version}", workflow)

    def test_a_copy_that_cannot_name_its_commit_refuses_to_write(self) -> None:
        """Fail closed: an unexpanded placeholder, a truncated file or no
        file at all read as absent, and the refusal names the commit to
        pass rather than pinning a guess."""
        for pin in (None, "$Format:%(describe:tags)$\n$Format:%H$\n", f"v{self.version}\n", "\n\n"):
            with self.subTest(pin=pin):
                directory = pathlib.Path(tempfile.mkdtemp(dir=self.dir))
                prefix = install_prefix(directory, pin)
                done = run(prefix, "new", str(directory / "product"), "--product", "maelys-probe", "--apply")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("its commit is unknown", done.stderr)
                self.assertIn("--socle-sha", done.stderr)

    def test_a_copy_whose_pin_disagrees_with_its_version_refuses(self) -> None:
        """Two halves of one archive that do not come from the same tag."""
        directory = pathlib.Path(tempfile.mkdtemp(dir=self.dir))
        prefix = install_prefix(directory, f"v9.9.9\n{COMMIT}\n")
        done = run(prefix, "new", str(directory / "product"), "--product", "maelys-probe", "--apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("the two disagree", done.stderr)

    def test_reading_commands_need_no_commit_at_all(self) -> None:
        """Nothing that only reads is held to the pin: describe and version
        answer from an installed copy carrying no INSTALLED.pin."""
        prefix = install_prefix(self.dir, None)
        version = run(prefix, "version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(self.version, version.stdout)
        described = run(prefix, "describe", "--format", "json")
        self.assertEqual(described.returncode, 0, described.stderr)
        self.assertEqual(json.loads(described.stdout)["contract"], "agent-cli/v2")

    def test_the_formula_installs_what_the_program_reads(self) -> None:
        """The template and the tag workflow, held to the layout above."""
        formula = FORMULA.read_text(encoding="utf-8")
        for name in ("VERSION", "CHANGELOG.md", "LICENSE", "INSTALLED.pin", "share"):
            self.assertIn(f'"{name}"', formula, name)
        self.assertIn('libexec.install "bin/maelys-release", "bin/maelys_cli.py", "bin/maelys_socle"', formula)
        self.assertIn('bin.install_symlink libexec/"maelys-release"', formula)
        # Rendered from the tag's own archive: no artifact is published.
        self.assertIn("url \"@URL@\"", formula)
        self.assertIn("sha256 \"@SHA256@\"", formula)
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('tags: ["v*"]', workflow)
        self.assertIn("uses: ./.github/workflows/tap.yml", workflow)
        self.assertIn("formula_template: packaging/homebrew/maelys-release.rb.in", workflow)
        self.assertIn('bottles: "[]"', workflow)


if __name__ == "__main__":
    unittest.main()
