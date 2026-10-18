# SPDX-License-Identifier: MPL-2.0
"""The source package travels with the executable, without a build or PYTHONPATH."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import unittest

from test_maelys_release import CLI, MODULE, PACKAGE, ROOT, Product


class PackageLayoutTest(unittest.TestCase):
    def test_copied_checkout_and_installed_prefix_keep_the_same_roots_and_outputs(self):
        product = Product()
        self.addCleanup(product.close)
        product.write("docs/migration-fixture.md", "# Prose to move\n")
        documents = product.work / "documents.jsonl"
        documents.write_text(json.dumps({"repository": "maelys-fixture", "path": "docs/migration-fixture.md",
                                        "destination": "documents/maelys-fixture/migration-fixture.md"}) + "\n")
        formula = product.work / "maelys-fixture.rb"
        formula.write_text("class MaelysFixture < Formula\nend\n")
        # A plan clones and stages locally but never pushes. Reuse a fixture's
        # bare repository so all three layouts inspect the same input tree.
        product.env["TAP_URL"] = f"file://{product.work / 'remotes/maelys-system.git'}"
        product.env["TAP_REPOSITORY"] = "maelys-dev/homebrew-tap"
        reference = product.run("adopt", str(product.dir), "--format", "json", "--compact")
        described = subprocess.run([sys.executable, "-I", str(CLI), "describe", "--format", "json"],
                                   env=product.env, text=True, capture_output=True, check=True)
        inspections = []
        for command in ("declarations", "check", "dependencies", "rehearse", "migrate", "cut", "tap"):
            for output in ("text", "json"):
                args = [command, str(product.dir), "--format", output]
                if command == "check":
                    args.extend([*Product.SOCLE, "--mechanism", "maelys-release"])
                if command == "dependencies":
                    args.extend(["--directory", str(product.work / "materialised")])
                if command == "rehearse":
                    args.extend(Product.SOCLE)
                if command == "migrate":
                    args.extend(["--documents", str(documents), "--documents-repository", "example/documents"])
                if command == "cut":
                    args.append("1.3.0")
                if command == "tap":
                    args = ["tap", "maelys-fixture", "v1.2.3", str(formula), "--skip-style", "--format", output]
                result = subprocess.run([sys.executable, "-I", str(CLI), *args],
                                        env=product.env, text=True, capture_output=True, check=False)
                expected_code = {"check": 2, "rehearse": 1, "cut": 1}.get(command, 0)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                inspections.append((args, result))
        for layout in ("checkout", "installed"):
            with self.subTest(layout=layout):
                prefix = product.work / layout
                shutil.copytree(ROOT / "bin", prefix / "bin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                share = prefix / "share" if layout == "checkout" else prefix / "share" / "maelys-release"
                shutil.copytree(ROOT / "share", share)
                # These resources remain rooted at socle_root(), including when
                # managed templates use share/maelys-release in an installation.
                for name in ("VERSION", "CHANGELOG.md", "WITHDRAWN"):
                    shutil.copy2(ROOT / name, prefix / name)
                (prefix / "docs").mkdir()
                shutil.copy2(ROOT / "docs" / "cli.reference", prefix / "docs" / "cli.reference")
                executable = prefix / "bin" / "maelys-release"
                # Isolated interpreters cannot find the original source via PYTHONPATH
                # or the working directory; each entry point must find its own package.
                describe = subprocess.run([sys.executable, "-I", str(executable), "describe", "--format", "json"],
                                          cwd=product.work, env=product.env, text=True, capture_output=True, check=False)
                self.assertEqual(describe.returncode, 0, describe.stderr)
                self.assertEqual(describe.stdout, described.stdout)
                self.assertEqual(describe.stderr, "")
                adopted = subprocess.run([sys.executable, "-I", str(executable), "adopt", str(product.dir),
                                          *Product.SOCLE, "--mechanism", "maelys-release", "--format", "json", "--compact"],
                                         cwd=product.work, env=product.env, text=True, capture_output=True, check=False)
                self.assertEqual(adopted.returncode, reference.returncode, adopted.stderr)
                self.assertEqual(adopted.stdout, reference.stdout)
                self.assertEqual(adopted.stderr, reference.stderr)
                for args, expected in inspections:
                    with self.subTest(command=args[0], output=args[args.index("--format") + 1]):
                        inspected = subprocess.run([sys.executable, "-I", str(executable), *args],
                                                   cwd=product.work, env=product.env, text=True,
                                                   capture_output=True, check=False)
                        self.assertEqual(inspected.returncode, expected.returncode, inspected.stderr)
                        self.assertEqual(inspected.stdout, expected.stdout)
                        self.assertEqual(inspected.stderr, expected.stderr)
                # The GitHub withdrawal reader must use this entry point's
                # version, even though its implementation now lives in a package.
                (prefix / "VERSION").write_text("99.98.97\n", encoding="utf-8")
                code = '''import base64, importlib.machinery, importlib.util, json, pathlib, sys
loader = importlib.machinery.SourceFileLoader("maelys_release", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
# Load a second entry point in the same interpreter: cached package modules
# must not make either context read the other checkout's VERSION.
other_loader = importlib.machinery.SourceFileLoader("other_socle", sys.argv[2])
other_spec = importlib.util.spec_from_loader(other_loader.name, other_loader)
other = importlib.util.module_from_spec(other_spec)
other_loader.exec_module(other)
# One package for both, and each entry point's own context: every read of
# the running socle below is given one of the two, and must answer for it.
from maelys_socle import github, host, protect, socle_origin
# Resource reads remain lazy and rooted in each entry point, even though
# the second loader reuses the first loader's already imported package.
workflow = module.socle_root() / ".github/workflows/check-product.yml"
workflow.parent.mkdir(parents=True)
workflow.write_text("- leg: installed\\n# alias: former -> installed\\n"
                    "      fuzz_command:\\n        default: prefix-fuzz\\n"
                    "      sanitizer_command:\\n        default: ''\\n")
project = module.socle_root() / "product"
ci = project / ".github/workflows/ci.yml"
ci.parent.mkdir(parents=True)
ci.write_text("jobs:\\n  check:\\n    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@"
              + "f" * 40 + " # v9.9.9\\n")
class RecordedHost(host.Host):
    def read(self, path):
        assert path == "repos/maelys-dev/maelys-release/contents/WITHDRAWN"
        return "ok", {"encoding": "base64", "content": base64.b64encode(
            b"99.98.97 protect installed-version\\n").decode()}
host.HOST = RecordedHost()
one, two = module.CONTEXT, other.CONTEXT
print(json.dumps({"root": str(module.socle_root()), "share": str(socle_origin.share_dir(one)),
                  "identity": socle_origin.socle_data(one, "f" * 40, "v99.98.97"),
                  "otherIdentity": socle_origin.socle_data(two, "e" * 40, "other"),
                  "withdrawal": github.withdrawn_for("protect", one),
                  "otherRoot": str(other.socle_root()), "otherWithdrawal": github.withdrawn_for("protect", two),
                  "legs": protect.check_product_legs(one), "otherLegs": protect.check_product_legs(two),
                  "aliases": protect.check_product_aliases(one), "otherAliases": protect.check_product_aliases(two),
                  "default": protect.check_product_default("fuzz_command", one),
                  "otherDefault": protect.check_product_default("fuzz_command", two),
                  "contexts": protect.socle_check_contexts(project, one),
                  "otherContexts": protect.socle_check_contexts(project, two),
                  "modules": [value.__file__ for name, value in sys.modules.items()
                              if name == "maelys_socle" or name.startswith("maelys_socle.")]}))
'''
                loaded = subprocess.run([sys.executable, "-I", "-c", code, str(executable), str(CLI)],
                                        cwd=product.work, env=product.env, text=True, capture_output=True, check=True)
                data = json.loads(loaded.stdout)
                self.assertEqual(data["root"], str(prefix.resolve()))
                self.assertEqual(data["share"], str(share.resolve()))
                self.assertEqual(data["identity"], {"version": "99.98.97", "tag": "v99.98.97", "sha": "f" * 40})
                self.assertEqual(data["otherIdentity"]["version"], (ROOT / "VERSION").read_text().strip())
                self.assertEqual(data["withdrawal"], ["withdrawn", "installed-version"])
                self.assertEqual(data["otherRoot"], str(ROOT))
                self.assertIsNone(data["otherWithdrawal"])
                self.assertEqual(data["legs"], ["installed"])
                self.assertEqual(data["otherLegs"], PACKAGE.protect.check_product_legs(MODULE.CONTEXT))
                self.assertEqual(data["aliases"], [["former", "installed"]])
                self.assertEqual(data["otherAliases"], [list(pair) for pair in PACKAGE.protect.check_product_aliases(MODULE.CONTEXT)])
                self.assertEqual(data["default"], " prefix-fuzz")
                self.assertEqual(data["otherDefault"], PACKAGE.protect.check_product_default("fuzz_command", MODULE.CONTEXT))
                self.assertEqual(data["contexts"], ["check", ["check / check (installed)", "check / fuzz"]])
                self.assertEqual(data["otherContexts"], ["check", [
                    *[f"check / check ({leg})" for leg in PACKAGE.protect.check_product_legs(MODULE.CONTEXT)], "check / sanitizers"]])
                self.assertEqual(len(data["modules"]), len(list((ROOT / "bin" / "maelys_socle").rglob("*.py"))))
                for filename in data["modules"]:
                    self.assertIn(prefix.resolve() / "bin" / "maelys_socle", pathlib.Path(filename).parents)
