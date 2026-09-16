# SPDX-License-Identifier: MPL-2.0
"""The source package travels with the executable, without a build or PYTHONPATH."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import unittest

from test_maelys_release import CLI, ROOT, Product


class PackageLayoutTest(unittest.TestCase):
    def test_copied_checkout_and_installed_prefix_keep_the_same_roots_and_outputs(self):
        product = Product()
        self.addCleanup(product.close)
        reference = product.run("adopt", str(product.dir), "--format", "json", "--compact")
        described = subprocess.run([sys.executable, "-I", str(CLI), "describe", "--format", "json"],
                                   env=product.env, text=True, capture_output=True, check=True)
        inspections = []
        for command in ("declarations", "check"):
            for output in ("text", "json"):
                args = [command, str(product.dir), "--format", output]
                if command == "check":
                    args.extend([*Product.SOCLE, "--mechanism", "maelys-release"])
                result = subprocess.run([sys.executable, "-I", str(CLI), *args],
                                        env=product.env, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0 if command == "declarations" else 2, result.stderr)
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
                    with self.subTest(command=args[0], output=args[3]):
                        inspected = subprocess.run([sys.executable, "-I", str(executable), *args],
                                                   cwd=product.work, env=product.env, text=True,
                                                   capture_output=True, check=False)
                        self.assertEqual(inspected.returncode, expected.returncode, inspected.stderr)
                        self.assertEqual(inspected.stdout, expected.stdout)
                        self.assertEqual(inspected.stderr, expected.stderr)
                # The GitHub withdrawal reader must use this entry point's
                # version, even though its implementation now lives in a package.
                (prefix / "VERSION").write_text("99.98.97\n", encoding="utf-8")
                code = '''import base64, importlib.machinery, importlib.util, json, sys
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
class RecordedHost(module.Host):
    def read(self, path):
        assert path == "repos/maelys-dev/maelys-release/contents/WITHDRAWN"
        return "ok", {"encoding": "base64", "content": base64.b64encode(
            b"99.98.97 protect installed-version\\n").decode()}
module.host.HOST = RecordedHost()
print(json.dumps({"root": str(module.socle_root()), "share": str(module.share_dir()),
                  "withdrawal": module.withdrawn_for("protect"),
                  "otherRoot": str(other.socle_root()), "otherWithdrawal": other.withdrawn_for("protect"),
                  "modules": [value.__file__ for name, value in sys.modules.items()
                              if name == "maelys_socle" or name.startswith("maelys_socle.")]}))
'''
                loaded = subprocess.run([sys.executable, "-I", "-c", code, str(executable), str(CLI)],
                                        cwd=product.work, env=product.env, text=True, capture_output=True, check=True)
                data = json.loads(loaded.stdout)
                self.assertEqual(data["root"], str(prefix.resolve()))
                self.assertEqual(data["share"], str(share.resolve()))
                self.assertEqual(data["withdrawal"], ["withdrawn", "installed-version"])
                self.assertEqual(data["otherRoot"], str(ROOT))
                self.assertIsNone(data["otherWithdrawal"])
                self.assertEqual(len(data["modules"]), len(list((ROOT / "bin" / "maelys_socle").glob("*.py"))))
                for filename in data["modules"]:
                    self.assertEqual(pathlib.Path(filename).parent, prefix.resolve() / "bin" / "maelys_socle")
