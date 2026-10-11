# SPDX-License-Identifier: MPL-2.0
"""Through `ssh -t` a command is given a terminal, and its text is paged there.

`protect --apply` run that way wrote, then handed the account of its write
to the pager: the screen stopped on `:`, and a command that had finished
looked hung. The rule is the framework's -- agent-cli/v2 pages text on a
terminal -- so nothing is changed in the vendored module. The managed block
says what to pass, and these hold that sentence to the behaviour it
describes: the day the framework stops paging the account of a write, the
last test fails, and the sentence goes with it.
"""
from __future__ import annotations

import os
import pty
import subprocess
import unittest

from test_maelys_release import CLI, ROOT, Product

BLOCKS = ("instructions-block.md", "conventions-block.md")


class PagerOverSshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        # A pager that returns, where the real one waits for a key: it
        # records that it was started, with how much, and whether the
        # write it is given the account of had already happened.
        self.log = self.product.work / "pager.log"
        pager = self.product.work / "bin" / "recording-pager"
        pager.write_text('#!/bin/sh\nlines=$(wc -l | tr -d " ")\n'
                         f'grep -q "maelys-release:begin" "{self.product.dir}/CLAUDE.md" 2>/dev/null'
                         ' && written=after || written=before\n'
                         f'echo "$lines lines, $written the write" >> "{self.log}"\n', encoding="utf-8")
        pager.chmod(0o755)
        self.env = {**self.product.env, "PAGER": str(pager)}

    def on_a_terminal(self, *arguments: str) -> str:
        """Run the command with a pseudo-terminal as stdout, as `ssh -t` gives it one."""
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen([str(CLI), *arguments, *Product.SOCLE, "--mechanism", "maelys-release"],
                                       cwd=self.product.work, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=slave, stderr=subprocess.PIPE)
        finally:
            os.close(slave)
        shown = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:      # Linux answers EIO once the other end is closed
                break
            if not chunk:
                break
            shown += chunk
        os.close(master)
        stderr = process.stderr.read()
        process.stderr.close()
        self.assertEqual(process.wait(), 0, stderr)
        return shown.decode("utf-8").replace("\r\n", "\n")

    def paged(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.is_file() else []

    def test_both_forms_of_the_block_say_what_to_pass(self) -> None:
        for name in BLOCKS:
            flat = " ".join((ROOT / "share" / "agents" / name).read_text(encoding="utf-8").split())
            self.assertIn("through `ssh -t`, pass `--pager never`", flat, name)
            self.assertIn("the account of an `--apply` included", flat, name)
            # It can stop, and does not always: the sentence says which.
            self.assertIn("An account that fits the screen returns by itself", flat, name)
            self.assertIn("a host that presets `PAGER` or `LESS`, can stop on `:`", flat, name)
            self.assertNotIn("or the screen stops", flat, name)
        # Said once, where it is read first: the skill is installed beside the block.
        self.assertNotIn("--pager", (ROOT / "share" / "agents" / "claude-skill.md").read_text(encoding="utf-8"))

    def test_the_option_the_block_names_exists(self) -> None:
        said = self.product.run("help", "conventions").stdout
        self.assertIn("--pager auto|always|never", said)
        # And the other way an agent has of saying it, which the framework documents.
        self.assertIn("never in a pipe, in JSON or under --non-interactive", " ".join(said.split()))

    def test_a_write_is_paged_on_a_terminal_and_not_with_the_option(self) -> None:
        directory = str(self.product.dir)
        # In a pipe nothing is paged, which is why no other test ever met this.
        self.product.run("adopt", directory, "--apply")
        self.assertEqual(self.paged(), [])
        (self.product.dir / "CLAUDE.md").unlink()
        # On a terminal the account goes to the pager, once the write is done.
        shown = self.on_a_terminal("adopt", directory, "--apply")
        self.assertNotIn("adopt: wrote", shown)
        self.assertEqual(len(self.paged()), 1, self.paged())
        self.assertTrue(self.paged()[0].endswith("after the write"), self.paged())
        # With the option the block names, the terminal reads it at once.
        self.log.unlink()
        (self.product.dir / "CLAUDE.md").unlink()
        shown = self.on_a_terminal("adopt", directory, "--apply", "--pager", "never")
        self.assertIn("adopt: wrote", shown)
        self.assertTrue((self.product.dir / "CLAUDE.md").is_file())
        self.assertEqual(self.paged(), [])

    def test_the_default_pager_returns_by_itself_unless_the_host_presets_it(self) -> None:
        """Why it looked intermittent. With PAGER unset the framework runs `less` and
        gives it LESS=FRX, whose F quits when the text fits the screen; a host that
        sets LESS keeps its own, and one that sets PAGER has that program run as it is."""
        seen = self.product.work / "less.log"
        less = self.product.work / "bin" / "less"       # first on the fixture's PATH
        less.write_text(f'#!/bin/sh\ncat >/dev/null\necho "LESS=${{LESS-unset}}" >> "{seen}"\n', encoding="utf-8")
        less.chmod(0o755)
        directory = str(self.product.dir)
        for preset, expected in (({}, "LESS=FRX"), ({"LESS": "R"}, "LESS=R")):
            self.env = {name: value for name, value in self.product.env.items() if name not in ("PAGER", "LESS")}
            self.env.update(preset)
            self.on_a_terminal("adopt", directory, "--apply")
            self.assertEqual(seen.read_text(encoding="utf-8").splitlines()[-1], expected, preset)
        # A pager of the host's own is run as it is named, and `less` is not.
        before = seen.read_text(encoding="utf-8")
        self.env = {**self.product.env, "PAGER": str(self.product.work / "bin" / "recording-pager")}
        self.on_a_terminal("adopt", directory, "--apply")
        self.assertEqual(seen.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.paged()), 1)


if __name__ == "__main__":
    unittest.main()
