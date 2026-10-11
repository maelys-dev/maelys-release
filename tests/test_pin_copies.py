# SPDX-License-Identifier: MPL-2.0
"""A pinned commit written out again, outside its pin.

In one product the commit of a dependency lived in three places: its
`dependencies/<name>.pin`, a table in a bootstrap script, and a document.
Moving the pin took three edits, held together by whoever remembered the
other two.

`check` notes it: the pin, and every other tracked file that writes the
forty characters out. A note and never a refusal -- a file that names the
commit it was verified against may be exactly right -- and a note that
cannot take the command down, whatever a file contains, whatever it is
called and whatever the user's git is configured to do.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from test_maelys_release import MODULE, PINNED_TAG, Product, using_host

SAID = " is also written in "
HOSTILE = """\
[color]
\tui = always
\tgrep = always
[grep]
\tcolumn = true
\tfullName = true
\tlineNumber = false
\tpatternType = perl
\textendedRegexp = true
\tfallbackToNoIndex = true
[submodule]
\trecurse = true
[core]
\tquotePath = false
"""


class Recording(MODULE.Host):
    """The real host, with every command it was asked for kept."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command, cwd=None, env=None):
        self.commands.append(list(command))
        return super().run(command, cwd=cwd, env=env)


class PinCopiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        self.commit = self.product.pinned
        self.product.run("adopt", self.dir, "--apply")
        self.product.git(self.product.dir, "init", "-q")
        self.track()

    def track(self) -> None:
        self.product.git(self.product.dir, "add", "-A")
        self.product.git(self.product.dir, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "state")

    def write_bytes(self, relative: str, content: bytes) -> None:
        path = self.product.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def notes(self) -> list[str]:
        data = self.product.json("check", self.dir)["data"]
        # Whatever is found, it is a note: the verdict does not move.
        self.assertTrue(data["valid"], data["violations"])
        self.assertTrue(data["conventions"]["valid"])
        return [check["message"] for check in data["checks"]
                if check["status"] == "note" and SAID in check["message"]]

    def listed(self) -> str:
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        return notes[0].split(": ", 2)[2].split(". Nothing ties ")[0]

    def hostile(self, text: str = HOSTILE) -> None:
        """The user's git, configured to change what `git grep` prints and reaches."""
        configuration = self.product.work / "hostile.gitconfig"
        configuration.write_text(text, encoding="utf-8")
        self.product.env["GIT_CONFIG_GLOBAL"] = str(configuration)

    def test_a_product_whose_pin_is_its_only_copy_says_nothing(self) -> None:
        self.assertEqual(self.notes(), [])
        # And the fixture is not silent for want of a pin or of a repository.
        self.assertEqual(self.product.read("dependencies/maelys-system.pin").splitlines()[1], self.commit)
        self.assertIn("dependencies/maelys-system.pin", self.product.git(self.product.dir, "ls-files"))

    def test_a_commit_repeated_in_a_script_is_noted_with_the_pin_and_the_file(self) -> None:
        self.product.write("scripts/bootstrap.sh", f"#!/bin/sh\n# the table\nmaelys_system={self.commit}\n")
        self.track()
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith(
            f"dependencies/maelys-system.pin: the commit it pins ({self.commit[:7]}) is also written in"
            " 1 other tracked file: scripts/bootstrap.sh:3. Nothing ties it to the pin: "), notes[0])
        # The text form says it too, as a note.
        text = self.product.run("check", self.dir).stdout
        self.assertRegex(text, r"(?m)^note +dependencies/maelys-system\.pin: the commit it pins .* scripts/bootstrap\.sh:3\. ")

    def test_the_note_claims_no_edit_by_hand(self) -> None:
        """A file may be a lock a tool writes again, or a record that stays:
        the note says the files are there and what may become of each."""
        self.product.write("tool.lock", f"maelys-system {self.commit}\n")
        self.track()
        note = self.notes()[0]
        self.assertIn("tool.lock:1. Nothing ties it to the pin: when it moves, each is edited, written again by"
                      " its tool, or left as the record it is.", note)
        self.assertNotIn("edits more", note)

    def test_the_three_places_become_one_list(self) -> None:
        """The case as it was met: the pin, a table in a script, a document."""
        self.product.write("scripts/bootstrap.sh", f"maelys_system={self.commit}\n")
        self.product.write("docs/build.md", f"# Build\n\nBuilt against\n`{self.commit}`,\nand `{self.commit}` again.\n")
        self.track()
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(f"{SAID}2 other tracked files: docs/build.md:4,5, scripts/bootstrap.sh:1. Nothing ties them ",
                      notes[0])

    def test_a_line_that_names_it_twice_is_one_line(self) -> None:
        self.product.write("scripts/twice.sh", f"a={self.commit} b={self.commit}\n")
        self.track()
        self.assertEqual(self.listed(), "scripts/twice.sh:1")

    def test_a_shorter_prefix_is_not_the_commit(self) -> None:
        for length in (7, 12, 39):
            self.product.write(f"scripts/short-{length}.sh", f"echo {self.commit[:length]}\n")
        # Thirty-nine characters followed by another: still not the commit.
        other = "0" if self.commit[-1] != "0" else "1"
        self.product.write("scripts/almost.sh", f"echo {self.commit[:39]}{other}\n")
        self.track()
        self.assertEqual(self.notes(), [])
        # The same file with the fortieth character is the commit.
        self.product.write("scripts/short-39.sh", f"echo {self.commit[:39]}{self.commit[39]}\n")
        self.assertEqual(len(self.notes()), 1)

    def test_a_pin_is_not_a_copy_of_itself_nor_of_another_pin(self) -> None:
        remotes = self.product.work / "remotes"
        self.product.git(self.product.work, "clone", "-q", "--bare",
                         str(remotes / "maelys-system.git"), str(remotes / "maelys-extra.git"))
        self.product.write("dependencies/maelys-extra.pin",
                           f"{PINNED_TAG}-1-g{self.commit[:7]}\n{self.commit}\non-request\n")
        self.track()
        self.assertEqual(self.notes(), [])
        # One file naming that commit is then a copy of both pins, said twice:
        # each pin has its own list.
        self.product.write("scripts/bootstrap.sh", f"both={self.commit}\n")
        self.track()
        notes = self.notes()
        self.assertEqual([note.split(":")[0] for note in notes],
                         ["dependencies/maelys-extra.pin", "dependencies/maelys-system.pin"])
        for note in notes:
            self.assertIn(" 1 other tracked file: scripts/bootstrap.sh:1. ", note)
            self.assertNotIn(".pin:2", note)

    def test_a_changelog_says_what_a_version_pinned_and_is_history(self) -> None:
        changelog = self.product.read("CHANGELOG.md")
        self.product.write("CHANGELOG.md", changelog + f"\n- maelys-system moves to `{self.commit}`.\n")
        self.product.write("packaging/CHANGELOG.md", f"- pinned `{self.commit}`\n")
        self.track()
        self.assertEqual(self.notes(), [])
        # Only the name: a file that merely ends like one is a file like any other.
        self.product.write("docs/NOT-A-CHANGELOG.md", f"`{self.commit}`\n")
        self.track()
        self.assertEqual(self.listed(), "docs/NOT-A-CHANGELOG.md:1")

    def test_data_under_docs_is_what_a_machine_writes(self) -> None:
        """The kind `check` already sorts it into: under docs/, what is not
        prose is data. An audit or the evidence of a run names the commit
        it ran against, and nobody edits it when the pin moves."""
        self.product.write("docs/evidence/run.json", f'{{"commit": "{self.commit}"}}\n')
        self.product.write("docs/audits/map.tsv", f"maelys-system\t{self.commit}\n")
        self.track()
        self.assertEqual(self.notes(), [])
        # Data anywhere else is read: docs/ is the one directory the
        # conventions say what holds.
        self.product.write("evidence/run.json", f'{{"commit": "{self.commit}"}}\n')
        self.track()
        self.assertEqual(self.listed(), "evidence/run.json:1")

    def test_prose_under_docs_is_read_in_every_form_it_takes(self) -> None:
        """The exemption is for data. A `.rst` or a `.txt` is prose as much
        as a `.md`, by the socle's own list, and was passing for data."""
        for suffix in MODULE.PROSE_SUFFIXES:
            self.product.write(f"docs/build{suffix.upper() if suffix == '.md' else suffix}", f"`{self.commit}`\n")
        self.track()
        self.assertEqual(sorted(MODULE.PROSE_SUFFIXES), [".markdown", ".md", ".rst", ".txt"])
        self.assertEqual(self.listed(),
                         "docs/build.MD:1, docs/build.markdown:1, docs/build.rst:1, docs/build.txt:1")

    def test_only_tracked_files_are_read(self) -> None:
        self.product.write("build/table.txt", f"{self.commit}\n")
        self.assertEqual(self.notes(), [])
        self.track()
        self.assertEqual(self.listed(), "build/table.txt:1")

    def test_a_binary_file_is_not_a_place_anyone_edits(self) -> None:
        self.write_bytes("blob.bin", b"\0\1\2" + self.commit.encode() + b"\0\n")
        self.track()
        self.assertEqual(self.notes(), [])

    def test_a_line_that_is_not_utf8_does_not_take_check_down(self) -> None:
        """A tracked Latin-1 file with an accented word and the commit on the
        same line: `check` ended on a UnicodeDecodeError, exit 1 and no
        envelope -- for a rule whose whole promise is to change no verdict."""
        self.write_bytes("docs/notes.txt", "résumé du dépôt : ".encode("latin-1")
                         + self.commit.encode() + b"\n")
        self.write_bytes("scripts/legacy.sh", b"# \xe9t\xe9\nsystem=" + self.commit.encode() + b" # \xff\xfe\n")
        self.track()
        self.assertEqual(self.listed(), "docs/notes.txt:1, scripts/legacy.sh:2")
        # The text form too, which decodes nothing less.
        self.assertIn("scripts/legacy.sh:2", self.product.run("check", self.dir).stdout)

    def test_a_line_is_ended_by_a_line_feed_and_by_nothing_else(self) -> None:
        """A lone carriage return, a form feed, a vertical tab, U+2028 or
        U+0085 before the commit: `str.splitlines` cut there, and the copy
        after the cut went unseen."""
        cuts = {"cr": b"\r", "ff": b"\x0c", "vt": b"\x0b", "fs": b"\x1c", "ls": " ".encode(),
                "nel": "\u0085".encode(), "crlf": None}
        for name, cut in cuts.items():
            content = b"before" + cut + self.commit.encode() + b"\n" if cut \
                else b"first\r\n" + self.commit.encode() + b"\r\n"
            self.write_bytes(f"scripts/{name}.txt", content)
        self.track()
        self.assertEqual(self.listed(), ", ".join(
            f"scripts/{name}.txt:{2 if name == 'crlf' else 1}" for name in sorted(cuts)))

    def test_the_user_s_git_configuration_changes_nothing(self) -> None:
        """Under color.ui=always the line number came wrapped in escapes and
        the note vanished without a word. A column, full names, another
        pattern type, unquoted paths: each is set back on the command."""
        self.product.write("scripts/bootstrap.sh", f"maelys_system={self.commit}\n")
        self.product.write("docs/café.md", f"`{self.commit}`\n")
        self.track()
        plain = self.notes()
        self.assertEqual(len(plain), 1)
        self.hostile()
        # The configuration is read, and does what it says to a bare call.
        bare = self.product.git(self.product.dir, "grep", "-n", "-F", "-e", self.commit, "--", "scripts")
        self.assertIn("\x1b[", bare)
        self.assertEqual(self.notes(), plain)
        # The general setting alone does it too, with nothing said of grep.
        self.hostile("[color]\n\tui = always\n")
        self.assertIn("\x1b[", self.product.git(self.product.dir, "grep", "-n", "-F", "-e", self.commit,
                                                "--", "scripts"))
        self.assertEqual(self.notes(), plain)
        self.hostile()
        # And a path is told from the directory that was asked about, not
        # from the top of whatever repository holds it.
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": self.product.env["GIT_CONFIG_GLOBAL"]}):
            self.assertIn("scripts/bootstrap.sh", subprocess.run(
                ["git", "-C", str(self.product.dir / "scripts"), "-c", "color.ui=false", "grep", "-l", "-F",
                 "-e", self.commit], check=True, text=True, stdout=subprocess.PIPE).stdout)
            self.assertEqual(MODULE.inspection.pin_copies(self.product.dir / "scripts", self.pins()),
                             {"maelys-system": {"bootstrap.sh": [1]}})

    def test_a_submodule_is_another_repository_s_files(self) -> None:
        inner = self.product.work / "src" / "inner"
        inner.mkdir(parents=True)
        self.product.git(inner, "init", "-q")
        (inner / "table.txt").write_text(f"{self.commit}\n", encoding="utf-8")
        self.product.git(inner, "add", "-A")
        self.product.git(inner, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "inner")
        self.product.git(self.product.dir, "-c", "protocol.file.allow=always", "submodule", "--quiet", "add",
                         str(inner), "vendor/inner")
        self.track()
        self.hostile()
        # The hazard is real: asked to recurse, git does find the file.
        self.assertIn("vendor/inner/table.txt", self.product.git(
            self.product.dir, "-c", "color.ui=false", "grep", "-l", "-F", "-e", self.commit))
        self.assertEqual(self.notes(), [])

    def test_outside_a_repository_it_says_nothing_and_fails_nothing(self) -> None:
        self.product.write("scripts/bootstrap.sh", f"maelys_system={self.commit}\n")
        self.track()
        self.assertEqual(len(self.notes()), 1)
        subprocess.run(["rm", "-rf", str(self.product.dir / ".git")], check=True)
        self.assertEqual(self.notes(), [])
        # Nor when the user's git falls back to searching whatever is there.
        self.hostile()
        fallen_back = subprocess.run(["git", "-C", self.dir, "-c", "color.ui=false", "grep", "-l", "-F",
                                      "-e", self.commit], env=self.product.env, check=False, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertIn("scripts/bootstrap.sh", fallen_back)
        self.assertEqual(self.notes(), [])

    def test_it_is_said_by_check_and_not_before_a_tag(self) -> None:
        """`preflight` reads the same declarations; what a release needs of
        a pin is its tag and its commit, and the list is not its business."""
        self.product.write("scripts/bootstrap.sh", f"maelys_system={self.commit}\n")
        self.track()
        self.assertEqual(len(self.notes()), 1)
        completed = self.product.cli("preflight", self.dir, "--format", "json")
        self.assertIn(completed.returncode, (0, 2), completed.stderr)
        checks = json.loads(completed.stdout)["data"]["checks"]
        self.assertTrue(checks)
        self.assertEqual([check for check in checks if SAID in check["message"]], [])

    def pins(self) -> dict[str, dict]:
        return MODULE.read_declarations(self.product.dir, "maelys-fixture", "maelys-release").pins

    def test_every_pin_is_searched_in_one_call(self) -> None:
        """`check` runs on every product and in every CI run."""
        remotes = self.product.work / "remotes"
        self.product.git(self.product.work, "clone", "-q", "--bare",
                         str(remotes / "maelys-system.git"), str(remotes / "maelys-extra.git"))
        self.product.write("dependencies/maelys-extra.pin", f"{PINNED_TAG}\n{self.product.tagged}\n")
        self.product.write("scripts/one.sh", f"echo {self.commit}\n")
        self.product.write("scripts/two.sh", f"echo {self.product.tagged}\necho {self.commit}\n")
        self.track()
        pins = self.pins()
        self.assertEqual(sorted(pins), ["maelys-extra", "maelys-system"])
        with using_host(Recording()) as host:
            copies = MODULE.inspection.pin_copies(self.product.dir, pins)
        self.assertEqual(copies, {"maelys-system": {"scripts/one.sh": [1], "scripts/two.sh": [2]},
                                  "maelys-extra": {"scripts/two.sh": [1]}})
        self.assertEqual(len(host.commands), 1, host.commands)
        search = host.commands[0]
        self.assertEqual(search[0], "git")
        self.assertEqual(search.count("grep"), 1)
        for commit in (self.commit, self.product.tagged):
            self.assertIn(commit, search)
        self.assertIn("--fixed-strings", search)
        # Every setting is on the command, before the word that runs it.
        for setting in MODULE.inspection.PIN_SEARCH_SETTINGS:
            self.assertLess(search.index(setting), search.index("grep"))
            self.assertEqual(search[search.index(setting) - 1], "-c")

    def test_a_product_that_pins_nothing_is_not_searched(self) -> None:
        with using_host(Recording()) as host:
            self.assertEqual(MODULE.inspection.pin_copies(self.product.dir, {}), {})
        self.assertEqual(host.commands, [])

    def test_a_path_is_named_whole_and_the_list_can_be_cut_again(self) -> None:
        """A path may hold the colon that ends it, the comma and the space
        that separate two of them, a quote, a line feed. Each is named whole,
        quoted when it is not an ordinary name, and the list parses back."""
        odd = ["scripts/a:b.txt", "scripts/a b.txt", "scripts/a, b:3.txt", 'scripts/q"uote.txt',
               "scripts/new\nline.txt", "scripts/back\\slash.txt", "scripts/café.txt"]
        for relative in odd:
            self.product.write(relative, f"x\n{self.commit}\n")
        self.product.write("scripts/plain.txt", f"{self.commit}\n")
        self.track()
        copies = MODULE.inspection.pin_copies(self.product.dir, self.pins())
        self.assertEqual(copies, {"maelys-system": {**{relative: [2] for relative in odd}, "scripts/plain.txt": [1]}})
        listed = self.listed()
        self.assertNotIn("\n", listed)
        # Cut again by a reader who knows the form and nothing of the files.
        decoder, position, read = json.JSONDecoder(), 0, {}
        while position < len(listed):
            if listed[position] == '"':
                path, position = decoder.raw_decode(listed, position)
            else:
                end = listed.index(":", position)
                path, position = listed[position:end], end
            end = listed.find(", ", position)
            end = len(listed) if end < 0 else end
            read[path] = [int(number) for number in listed[position + 1:end].split(",")]
            position = end + 2
        self.assertEqual(read, copies["maelys-system"])
        # An ordinary name, accents included, is left as it is.
        self.assertIn("scripts/café.txt:2", listed)
        self.assertIn("scripts/plain.txt:1", listed)
        self.assertIn('"scripts/a b.txt":2', listed)

    def test_a_name_git_quotes_is_read_back_and_a_name_that_is_not_utf8_raises_nothing(self) -> None:
        """A file system that takes any byte in a name is not the one this
        suite runs on everywhere; what git prints for such a name is."""
        unquoted = MODULE.inspection.unquoted_path
        self.assertEqual(unquoted("scripts/plain.txt"), "scripts/plain.txt")
        self.assertEqual(unquoted('"caf\\303\\251.md"'), "café.md")
        self.assertEqual(unquoted('"new\\nline \\"q\\" back\\\\slash\\ttab"'), 'new\nline "q" back\\slash\ttab')
        self.assertEqual(unquoted('"lat\\351.txt"'), "lat\\xe9.txt")
        self.assertEqual(unquoted('"'), '"')
        json.dumps(unquoted('"lat\\351\\377.txt"')).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
