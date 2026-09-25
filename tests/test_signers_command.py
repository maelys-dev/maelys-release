# SPDX-License-Identifier: MPL-2.0
"""The command that prepares a line of the allowed signers, and its refusals.

A formatter: a line it writes is a line `ssh-keygen` accepts, and a line it
refuses is one the release would have refused after the tag was pushed. The
invariant is not here but in `only_grew`, held on the difference between two
versions of the file, because the command is not the only hand that edits it.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "maelys-release"
sys.path.insert(0, str(ROOT / "bin"))

from maelys_socle.signers import only_grew  # noqa: E402


def run(*arguments: str, expect: int = 0) -> dict:
    done = subprocess.run([str(CLI), *arguments, "--format", "json", "--compact"],
                          capture_output=True, text=True)
    assert done.returncode == expect, f"{arguments}: exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout if done.returncode != 1 else done.stderr)


class SignersCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is required")
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="maelys-signers-cmd-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        # A socle-shaped directory: share/agents is what says so.
        self.socle = self.dir / "socle"
        (self.socle / "share" / "agents").mkdir(parents=True)
        self.file = self.socle / "share" / "allowed-signers"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.dir / "key")],
                       check=True, capture_output=True)
        self.public = (self.dir / "key.pub").read_text(encoding="utf-8").strip()
        self.file.write_text(f'first@example.org namespaces="git" {self.public}\n', encoding="utf-8")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.dir / "other")],
                       check=True, capture_output=True)

    def test_it_lists_what_the_file_says_and_what_would_refuse_a_line(self) -> None:
        data = run("signers", str(self.socle))["data"]
        self.assertEqual([entry["principal"] for entry in data["signers"]], ["first@example.org"])
        self.assertTrue(data["signers"][0]["fingerprint"].startswith("SHA256:"))
        self.assertTrue(data["signers"][0]["signsToday"])
        self.file.write_text(f'first@example.org namespaces="git",valid-before="20250101Z" {self.public}\n',
                             encoding="utf-8")
        retired = run("signers", str(self.socle))["data"]["signers"][0]
        self.assertFalse(retired["signsToday"])
        self.assertIn("retired this key", retired["refusal"])

    def test_a_plan_writes_nothing_and_an_apply_writes_one_file(self) -> None:
        before = self.file.read_text(encoding="utf-8")
        planned = run("signers", str(self.socle), "--add", str(self.dir / "other.pub"),
                      "--principal", "second@example.org")["data"]
        self.assertEqual(self.file.read_text(encoding="utf-8"), before, "a plan writes nothing")
        self.assertFalse(planned["wrote"])
        self.assertIn("second@example.org", planned["line"])
        self.assertTrue(any("pull request" in step for step in planned["next"]))
        applied = run("signers", str(self.socle), "--add", str(self.dir / "other.pub"),
                      "--principal", "second@example.org", "--apply")["data"]
        self.assertTrue(applied["wrote"])
        self.assertEqual([entry["principal"] for entry in applied["signers"]],
                         ["first@example.org", "second@example.org"])

    def test_the_line_it_writes_is_one_ssh_keygen_accepts(self) -> None:
        """The point of a formatter: no line it writes is refused later."""
        run("signers", str(self.socle), "--add", str(self.dir / "other.pub"),
            "--principal", "second@example.org", "--apply")
        message = self.dir / "message"
        message.write_text("what the key signed\n", encoding="utf-8")
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.dir / "other"), "-n", "git", str(message)],
                       check=True, capture_output=True)
        found = subprocess.run(["ssh-keygen", "-Y", "find-principals", "-O", "verify-time=20260901000000Z",
                                "-f", str(self.file), "-s", str(self.dir / "message.sig")],
                               capture_output=True, text=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("second@example.org", found.stdout)

    def test_retiring_keeps_the_line_and_stops_the_key_after_that_moment(self) -> None:
        run("signers", str(self.socle), "--add", str(self.dir / "other.pub"),
            "--principal", "second@example.org", "--apply")
        data = run("signers", str(self.socle), "--retire", "second@example.org",
                   "--at", "20260901000000Z", "--apply")["data"]
        self.assertEqual(data["retired"], ["second@example.org"])
        text = self.file.read_text(encoding="utf-8")
        self.assertIn('valid-before="20260901000000Z"', text)
        self.assertEqual(len(text.strip().splitlines()), 2, "a retirement keeps the line")
        self.assertTrue(any("revokes nothing" in step for step in data["next"]))
        message = self.dir / "message"
        message.write_text("signed while trusted\n", encoding="utf-8")
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.dir / "other"), "-n", "git", str(message)],
                       check=True, capture_output=True)

        def verify(at: str) -> int:
            return subprocess.run(["ssh-keygen", "-Y", "verify", "-O", f"verify-time={at}", "-f", str(self.file),
                                   "-I", "second@example.org", "-n", "git", "-s", str(self.dir / "message.sig")],
                                  capture_output=True, text=True,
                                  input=message.read_text(encoding="utf-8")).returncode

        self.assertEqual(verify("20260801000000Z"), 0, "what it signed while trusted still verifies")
        self.assertNotEqual(verify("20260902000000Z"), 0, "and it signs nothing after")

    def test_every_refusal_is_one_the_release_would_have_made(self) -> None:
        for arguments, said in (
                (["--add", str(self.dir / "other"), "--principal", "x@example.org"], "private key"),
                (["--add", str(self.dir / "other.pub")], "--principal"),
                (["--add", str(self.dir / "key.pub"), "--principal", "twice@example.org"], "already names"),
                (["--add", str(self.dir / "absent.pub"), "--principal", "x@example.org"], "is not a file"),
                (["--retire", "nobody@example.org"], "names no signer"),
                (["--retire", "first@example.org", "--at", "2026-09-01"], "not a time ssh-keygen reads")):
            with self.subTest(arguments=arguments[0:2]):
                done = subprocess.run([str(CLI), "signers", str(self.socle), *arguments],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(said, done.stderr)

    def test_it_refuses_a_directory_that_is_not_the_socle(self) -> None:
        elsewhere = self.dir / "product"
        elsewhere.mkdir()
        done = subprocess.run([str(CLI), "signers", str(elsewhere)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not a maelys-release checkout", done.stderr)


class OnlyGrewTest(unittest.TestCase):
    """The invariant, on the difference between two versions of the file."""

    BASE = ('a@example.org namespaces="git" ssh-ed25519 AAAAKEEP\n'
            'b@example.org namespaces="git",valid-before="20260101Z" ssh-ed25519 AAAARETIRED\n')

    def test_adding_a_line_is_the_only_free_edit(self) -> None:
        self.assertEqual(only_grew(self.BASE, self.BASE), [])
        self.assertEqual(only_grew(self.BASE, self.BASE + 'c@example.org ssh-ed25519 AAAANEW\n'), [])

    def test_a_removed_line_is_refused(self) -> None:
        refusals = only_grew(self.BASE, self.BASE.splitlines(keepends=True)[0])
        self.assertEqual(len(refusals), 1)
        self.assertIn("was removed", refusals[0])
        self.assertIn("can no longer be replayed", refusals[0])

    def test_un_retiring_a_key_is_refused_both_ways(self) -> None:
        dropped = only_grew(self.BASE, self.BASE.replace(',valid-before="20260101Z"', ""))
        self.assertIn("valid-before disappeared", dropped[0])
        later = only_grew(self.BASE, self.BASE.replace("20260101Z", "20270101Z"))
        self.assertIn("moves earlier, never later", later[0])
        # Earlier is stricter, and stays allowed.
        self.assertEqual(only_grew(self.BASE, self.BASE.replace("20260101Z", "20250101Z")), [])


if __name__ == "__main__":
    unittest.main()
