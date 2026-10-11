# SPDX-License-Identifier: MPL-2.0
"""A third party's commit that no tag carries is pinned on purpose, and held to its branch.

A product builds a part of a library newer than any version its authors have
published: the commit it needs is on their main branch and no tag carries it.
The socle had two things to say about such a pin and they disagreed. Its
texts said line 1 is "the nearest tag, for humans"; `dependencies` refused
exactly that pin, as a tag that points elsewhere, and what it accepted in its
place got one vague note -- born from a pin that had vanished with a rewritten
branch.

A pin says `unreleased <branch>`. Line 1 is then the last tag before the
commit, verified as an ancestor; the branch is asked whether it still holds
the commit; and nothing of it is open to a Maelys repository, which is pinned
on a tag.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager

from test_maelys_release import MODULE, PINNED_TAG, ROOT, Product, RealGitHost

THIRD = "https://third.example.invalid"


def parse(text: str, name: str = "lib") -> tuple[dict, list[str]]:
    return MODULE.parse_pin(name, text)


class UnreleasedGrammarTest(unittest.TestCase):
    """What a pin file alone can say, which is all `check` reads."""

    COMMIT = "c" * 40

    def pin(self, *attributes: str, tag: str = "v1.1.0") -> tuple[dict, list[str]]:
        return parse("\n".join([tag, self.COMMIT, *attributes]) + "\n")

    def test_the_line_names_the_branch_of_a_third_party(self) -> None:
        pin, problems = self.pin(f"repository {THIRD}/lib.git", "unreleased main")
        self.assertEqual((pin["unreleased"], problems), ("main", []))
        # A release branch is a branch too.
        self.assertEqual(self.pin(f"repository {THIRD}/lib.git", "unreleased release/4.x")[0]["unreleased"],
                         "release/4.x")

    def test_a_pin_that_does_not_say_it_is_not_one(self) -> None:
        pin, problems = self.pin(f"repository {THIRD}/lib.git")
        self.assertEqual((pin["unreleased"], problems), ("", []))
        self.assertEqual(parse(f"v1\n{self.COMMIT}\n")[0]["unreleased"], "")

    def test_it_is_refused_without_a_repository_line(self) -> None:
        """A pin with no `repository` line is a Maelys repository by definition."""
        pin, problems = self.pin("unreleased main")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dependencies/lib.pin: the unreleased line is for a third party", problems[0])
        self.assertIn("A Maelys repository is pinned on a tag", problems[0])
        self.assertEqual(pin["unreleased"], "", "a refused line is read by nothing downstream")

    def test_a_maelys_repository_may_not_try_it_through_a_repository_line(self) -> None:
        for url in ("https://github.com/maelys-dev/maelys-json.git", "https://github.com/maelys-dev/maelys-json",
                    "https://GitHub.com/Maelys-Dev/maelys-json.git", "https://www.github.com/maelys-dev/x.git"):
            with self.subTest(url=url):
                pin, problems = self.pin(f"repository {url}", "unreleased main")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"{url} is a Maelys repository", problems[0])
                self.assertEqual(pin["unreleased"], "")
        # The other end: a name that only starts like the organisation's.
        for url in ("https://github.com/maelys-dev-fork/x.git", "https://example.org/maelys-dev/x.git"):
            with self.subTest(url=url):
                self.assertEqual(self.pin(f"repository {url}", "unreleased main")[1], [])

    def test_a_repository_line_refused_for_itself_is_one_problem_not_two(self) -> None:
        pin, problems = self.pin("repository git@example.org:their/lib.git", "unreleased main")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dependencies/lib.pin: the repository line must be an https URL", problems[0])
        self.assertEqual(pin["unreleased"], "", "a pin refused for its repository line is not one of them")

    # -- one reading: what the managed script reads differently, or not at all --

    def test_a_line_the_script_would_not_match_is_refused_here(self) -> None:
        """The script matches an attribute from the first column to the last;
        this reader stripped the line first, and read a third party where
        the script read no line and cloned the fleet's repository."""
        for line in (f"repository {THIRD}/lib.git ", f" repository {THIRD}/lib.git", f"repository\t{THIRD}/lib.git",
                     f"repository  {THIRD}/lib.git", f"Repository {THIRD}/lib.git", f"repository: {THIRD}/lib.git"):
            with self.subTest(line=line):
                pin, problems = self.pin(line, "unreleased main")
                self.assertEqual(len(problems), 1, problems)
                self.assertEqual((pin["repository"], pin["unreleased"]), ("", ""))
        for line in ("unreleased\tmain", " unreleased main", "unreleased main ", "Unreleased main",
                     "UNRELEASED main", "unreleased: main", "unreleased:main", "unreleasedmain"):
            with self.subTest(line=line):
                pin, problems = self.pin(f"repository {THIRD}/lib.git", line)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(repr(line), problems[0], "the line is shown as it is spelled")
                self.assertEqual(pin["unreleased"], "")
        for line, attribute in (("on-request ", "on_request"), (" on-request", "on_request"),
                                ("On-Request", "on_request"), ("submodules recursive ", "submodules"),
                                ("submodules\trecursive", "submodules"), ("Submodules", "submodules"),
                                ("submodulesrecursive", "submodules")):
            with self.subTest(line=line):
                pin, problems = self.pin(line)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(pin[attribute], (False, None), "and it is not read as the attribute either")

    def test_an_attribute_is_given_once(self) -> None:
        """The script takes the first repository line and this reader took the last."""
        pin, problems = self.pin(f"repository {THIRD}/lib.git", "repository https://other.example/lib.git")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("the repository line is given twice", problems[0])
        for line in ("submodules", "on-request", "unreleased main"):
            with self.subTest(line=line):
                _, problems = self.pin(f"repository {THIRD}/lib.git", line, line)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"the {line.split()[0]} line is given twice", problems[0])

    def test_a_pin_is_lines_ending_in_lf_and_line_2_is_the_commit_alone(self) -> None:
        self.assertEqual(parse(f"v1\r\n{self.COMMIT}\r\n"),
                         ({}, ["dependencies/lib.pin: the file holds carriage returns; a pin is read line by"
                               " line, each ending in LF"]))
        for second in (self.COMMIT + " ", " " + self.COMMIT, self.COMMIT + "\t", self.COMMIT.upper()):
            with self.subTest(second=second):
                self.assertEqual(parse(f"v1\n{second}\n"),
                                 ({}, ["dependencies/lib.pin: line 2 must be the pinned commit (line 1 its tag)"]))
        # The other end: a last line with no newline, and a blank line, are read by both.
        self.assertEqual(parse(f"v1\n{self.COMMIT}")[1], [])
        self.assertEqual(parse(f"v1\n{self.COMMIT}\n\non-request\n")[0]["on_request"], True)

    def test_line_1_is_one_word_and_never_an_option(self) -> None:
        for first in ("v1 ", " v1", "v1 0", "v1\t", "--all", "-n2"):
            with self.subTest(first=first):
                _, problems = parse(f"{first}\n{self.COMMIT}\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("line 1 holds a tag", problems[0])

    def test_a_word_no_reader_knows_is_named_and_not_fatal(self) -> None:
        """As a section of the declaration file is: a product pins the socle by
        commit, and an attribute a newer socle reads must not refuse it."""
        pin, problems = self.pin("mirror-of somewhere", "# a comment")
        self.assertEqual((pin["unknown"], problems), (["mirror-of somewhere", "# a comment"], []))
        # The digest `vendor` writes under the socle's own pin is a line it knows.
        pin, problems = self.pin("sha256:" + "a" * 64)
        self.assertEqual((pin["unknown"], problems), ([], []))
        self.assertEqual(len(self.pin("sha256:abc")[1]), 1)

    def test_the_repository_url_takes_one_shape(self) -> None:
        """Every one of these read as "not maelys-dev" and reached it."""
        for url in ("https://github.com:443/maelys-dev/x", "https://git@github.com/maelys-dev/x",
                    "https://user:secret@github.com/maelys-dev/x", "https://github.com//maelys-dev/x",
                    "https://github.com/./maelys-dev/x", "https://github.com/x/../maelys-dev/y",
                    "https://github.com/%6daelys-dev/x", "https://github.com/maelys-dev/x%2egit",
                    "https://github.com./maelys-dev/x", "https://github.com/maelys-dev/x/",
                    "https://github.com/maelys-dev/x?ref=main", "https://github.com/maelys-dev/x#main",
                    "https://github.com/maelys-dev", "https://github.com/a/b/c", "https://github.com/",
                    "https:///maelys-dev/x", "http://github.com/their/lib", "https://[::1]/their/lib",
                    "https://-host.example/their/lib", "https://host..example/their/lib",
                    # Off the fleet's host too, where a path may be longer than owner/name.
                    "https://git.example.org/their/../lib", "https://git.example.org/./lib",
                    "https://git.example.org/lib/.."):
            with self.subTest(url=url):
                pin, problems = self.pin(f"repository {url}", "unreleased main")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("the repository line must be an https URL", problems[0])
                self.assertEqual((pin["repository"], pin["unreleased"]), ("", ""))
        # The other end: the shape, on the fleet's host and elsewhere.
        for url in ("https://github.com/Mbed-TLS/mbedtls.git", "https://github.com/their/lib",
                    "https://example.org/lib.git", "https://git.example.org/group/sub/lib.git",
                    "https://git.example.org/~them/lib", "https://example.org/their/.github"):
            with self.subTest(url=url):
                pin, problems = self.pin(f"repository {url}", "unreleased main")
                self.assertEqual((pin["repository"], pin["unreleased"], problems), (url, "main", []))

    def test_a_branch_is_a_name_git_takes_and_a_branch_of_the_repository(self) -> None:
        """`HEAD` passed the first clone, as origin/HEAD, and was refused on
        every run after it: refused where the pin is read."""
        for branch in ("HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "refs/heads/main", "refs/tags/v1",
                       "a/.b", ".a", "a.lock/b", "a.lock", "a..b", "a@{1}", "@", "a~1", "a^", "a:b", "a?", "a*",
                       "a[b", "a\\b", "/a", "a."):
            with self.subTest(branch=branch):
                pin, problems = self.pin(f"repository {THIRD}/lib.git", f"unreleased {branch}")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"{branch!r} is not one", problems[0])
                self.assertEqual(pin["unreleased"], "")
        # The other end. A name that is a tag over there cannot be told from here.
        for branch in ("main", "release_4", "feature/x.y", "v1.1.0", "head", "4.x"):
            with self.subTest(branch=branch):
                pin, problems = self.pin(f"repository {THIRD}/lib.git", f"unreleased {branch}")
                self.assertEqual((pin["unreleased"], problems), (branch, []))

    def test_line_1_is_the_name_of_a_tag_and_not_a_revision(self) -> None:
        """`v1.1.0^0` resolved, as the commit of v1.1.0, and was taken for a tag."""
        for tag in ("v1.1.0^0", "v1.1.0~1", "v1.1.0^{commit}", "v1..2", "v1.lock", "refs/tags/v1@{0}"):
            with self.subTest(tag=tag):
                pin, problems = self.pin(f"repository {THIRD}/lib.git", "unreleased main", tag=tag)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("is not the name of a tag", problems[0])
        # The other end: what third parties do name their tags.
        for tag in ("v1.1.0", "1.2", "mbedtls-3.6.0", "release_1_2", "v4.0.0-beta.1", "2026.10"):
            with self.subTest(tag=tag):
                self.assertEqual(self.pin(f"repository {THIRD}/lib.git", "unreleased main", tag=tag)[1], [])

    def test_it_takes_one_branch_and_nothing_else(self) -> None:
        for line in ("unreleased", "unreleased main and more", "unreleased ../main", "unreleased main.lock",
                     "unreleased -main", "unreleased heads//main", "unreleased main/"):
            with self.subTest(line=line):
                pin, problems = self.pin(f"repository {THIRD}/lib.git", line)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("names the branch that holds the commit, and nothing else", problems[0])
                self.assertEqual(pin["unreleased"], "")

    def test_a_commit_is_on_one_branch(self) -> None:
        pin, problems = self.pin(f"repository {THIRD}/lib.git", "unreleased main", "unreleased next")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("the unreleased line is given twice", problems[0])
        self.assertEqual(pin["unreleased"], "")

    def test_line_1_is_a_tag_and_not_what_git_describe_says(self) -> None:
        """`migrate` writes vX.Y.Z-N-gSHA for a Maelys commit past its tag, and
        a reader will try it here: the line already says "past the tag"."""
        described = f"v1.1.0-3-g{self.COMMIT[:7]}"
        pin, problems = self.pin(f"repository {THIRD}/lib.git", "unreleased main", tag=described)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("line 1 is the last tag before the commit -- v1.1.0 --", problems[0])
        # The other end: the same line 1 is what a pin without the line may say.
        self.assertEqual(self.pin(f"repository {THIRD}/lib.git", tag=described)[1], [])
        # And a tag that merely looks like one, naming another commit, is a tag.
        self.assertEqual(self.pin(f"repository {THIRD}/lib.git", "unreleased main", tag="v1.1.0-3-gabcdef0")[1], [])

    def test_line_1_is_not_empty(self) -> None:
        _, problems = self.pin(f"repository {THIRD}/lib.git", "unreleased main", tag="")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("line 1 is the last tag before the commit, and it is empty", problems[0])


class ThirdParty:
    """A third party's repository, served locally under an https name.

        c1 (v1.0.0) -- c2 (v1.1.0) -- c3 -- c4 (v2.0.0)      main
                           \\
                            s1                                next

    `c3` is the commit a product wants: on main, past v1.1.0, under no tag.
    """

    def __init__(self, product: Product) -> None:
        self.product = product
        self.source = product.work / "third-src" / "lib"
        self.source.mkdir(parents=True)
        self.git("init", "-q")
        self.c1 = self.commit("one")
        self.git("tag", "v1.0.0")
        self.c2 = self.commit("two")
        # Annotated, as a third party's releases usually are: the question
        # is about the commit a tag names, never the tag object.
        self.git("-c", "tag.gpgsign=false", "tag", "-a", "-m", "v1.1.0", "v1.1.0")
        self.c3 = self.commit("three")
        self.c4 = self.commit("four")
        self.git("tag", "v2.0.0")
        self.git("checkout", "-q", "-b", "next", self.c2)
        self.s1 = self.commit("side")
        self.git("checkout", "-q", "main")
        served = product.work / "third"
        served.mkdir()
        self.bare = served / "lib.git"
        product.git(product.work, "clone", "-q", "--bare", str(self.source), str(self.bare))
        for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            # What a public host answers: a partial clone, and a commit by
            # its hash for as long as it has not been pruned.
            product.git(self.bare, "config", key, "true")
        # git reads the https name and fetches the directory: the pin holds
        # the URL a product would write, and no test reaches a network.
        product.env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{served}/.insteadOf",
                            "GIT_CONFIG_VALUE_0": f"{THIRD}/"})

    def git(self, *arguments: str) -> str:
        return self.product.git(self.source, "-c", "commit.gpgsign=false", *arguments)

    def commit(self, content: str) -> str:
        (self.source / "file").write_text(content + "\n", encoding="utf-8")
        self.git("add", "file")
        self.git("commit", "-q", "-m", content)
        return self.git("rev-parse", "HEAD")

    def publish(self, *refs: str, force: bool = False) -> None:
        self.git("push", "-q", *(["--force"] if force else []), str(self.bare), *refs)

    @contextmanager
    def unreachable(self):
        """The https name answers nothing: what a machine without a network has.

        Also what lets a test run the command a second time. It compares a
        checkout's origin with the pin's URL through `git remote get-url`,
        which expands `insteadOf`: served from a directory, the checkout the
        command made a moment ago reads as cloned from somewhere else.
        """
        served = {key: self.product.env.pop(key) for key in list(self.product.env) if key.startswith("GIT_CONFIG_")
                  and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")}
        # And git refuses the transport itself, at once: no test waits on a
        # resolver, or learns what this machine's network answers.
        self.product.env["GIT_ALLOW_PROTOCOL"] = "file"
        try:
            yield
        finally:
            del self.product.env["GIT_ALLOW_PROTOCOL"]
            self.product.env.update(served)

    def rewrite_main(self, prune: bool = False) -> str:
        """The third party rewrites main from v1.1.0: c3 and c4 are on no branch."""
        self.git("tag", "-d", "v2.0.0")
        self.product.git(self.bare, "tag", "-d", "v2.0.0")
        self.git("reset", "-q", "--hard", self.c2)
        rewritten = self.commit("three, again")
        self.publish("main", force=True)
        if prune:
            # And collects what no ref holds: the commit is gone from there.
            self.product.git(self.bare, "reflog", "expire", "--expire=now", "--all")
            self.product.git(self.bare, "gc", "-q", "--prune=now")
        return rewritten

    def pin(self, commit: str, tag: str = "v1.1.0", branch: str = "main", *more: str) -> None:
        lines = [tag, commit, f"repository {THIRD}/lib.git", *([f"unreleased {branch}"] if branch else []), *more]
        self.product.write("dependencies/lib.pin", "\n".join(lines) + "\n")


class UnreleasedDeclarationTest(unittest.TestCase):
    """`check` and `declarations`: the pin file, offline, and no clone."""

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        self.commit = "c" * 40

    def write(self, *attributes: str) -> None:
        self.product.write("dependencies/lib.pin", "\n".join(["v1.1.0", self.commit, *attributes]) + "\n")

    def test_check_says_what_it_read_and_adds_no_note(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        before = self.product.json("check", self.dir)["data"]
        self.write(f"repository {THIRD}/lib.git", "unreleased main")
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"], data["violations"])
        said = [check for check in data["checks"] if "dependencies/lib.pin pins" in check["message"]]
        self.assertEqual([check["status"] for check in said], ["ok"], data["checks"])
        self.assertIn(f"pins ccccccc on main of {THIRD}/lib.git, past v1.1.0 and under no tag, on purpose",
                      said[0]["message"])
        # It says where its own reading stops, and which command goes further.
        self.assertIn("Read from the pin alone", said[0]["message"])
        self.assertIn("dependencies DIR --apply", said[0]["message"])
        # No parasitic note: with the pin, the notes are the ones without it.
        notes = lambda checks: sorted(check["message"] for check in checks if check["status"] == "note")
        self.assertEqual(notes(data["checks"]), notes(before["checks"]))

    def test_declarations_returns_them_under_a_key_of_their_own(self) -> None:
        self.assertEqual(self.product.json("declarations", self.dir)["data"]["unreleased"], [],
                         "the key is there for every product, empty for nearly all")
        self.write(f"repository {THIRD}/lib.git", "unreleased main")
        data = self.product.json("declarations", self.dir)["data"]
        self.assertTrue(data["valid"], data["checks"])
        self.assertEqual(data["unreleased"], [{"name": "lib", "repository": f"{THIRD}/lib.git", "branch": "main",
                                               "tag": "v1.1.0", "commit": self.commit}])
        # Still a dependency like the others, cloned by everything that clones them.
        self.assertEqual(data["dependencies"], ["lib", "maelys-system"])
        self.assertIn("unreleased    lib (main, past v1.1.0)", self.product.run("declarations", self.dir).stdout)
        # The other end: a third party pinned on a tag is not one of them.
        self.write(f"repository {THIRD}/lib.git")
        self.assertEqual(self.product.json("declarations", self.dir)["data"]["unreleased"], [])

    def refused(self, fragment: str) -> None:
        check = self.product.json("check", self.dir, expect=2)["data"]
        self.assertTrue([line for line in check["conventions"]["violations"] if fragment in line], check["violations"])
        declared = self.product.json("declarations", self.dir, expect=2)["data"]
        self.assertFalse(declared["valid"])
        self.assertEqual(declared["unreleased"], [], "a refused line names no such pin")
        error = self.product.json("dependencies", self.dir, expect=1)["error"]
        self.assertEqual(error["code"], "PRECONDITION_FAILED")
        self.assertIn(fragment, error["message"])

    def test_a_pin_refused_for_its_repository_line_is_not_listed_as_accepted(self) -> None:
        """It was: under `unreleased`, and "on purpose" in `check`, with an empty repository."""
        self.write("repository https://github.com:443/maelys-dev/lib", "unreleased main")
        self.refused("dependencies/lib.pin: the repository line must be an https URL in one shape")
        said = [check["message"] for check in self.product.json("check", self.dir, expect=2)["data"]["checks"]]
        self.assertEqual([line for line in said if "on purpose" in line], [])

    def test_check_passes_no_pin_the_managed_script_would_not_read_as_written(self) -> None:
        """A space after the URL: a third party for this reader, once, and no
        line at all for the script, which cloned the fleet's repository of
        that name. Both refuse it now, each for its own reader's reason."""
        self.product.write("dependencies/maelys-system.pin",
                           f"{PINNED_TAG}\n{self.product.pinned}\nrepository {THIRD}/lib.git \nunreleased main\n")
        self.refused("dependencies/maelys-system.pin: line 3 is 'repository https://third.example.invalid/lib.git '")

    def test_a_line_no_reader_knows_is_a_note_of_check(self) -> None:
        self.product.run("adopt", self.dir, "--apply")
        self.write(f"repository {THIRD}/lib.git", "mirror-of somewhere")
        self.product.run("adopt", self.dir, "--apply")
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"], data["violations"])
        notes = [check["message"] for check in data["checks"] if check["status"] == "note" and "lib.pin" in
                 check["message"]]
        self.assertEqual(len(notes), 1, data["checks"])
        self.assertIn("'mirror-of somewhere' starts with no word this socle knows", notes[0])

    def test_without_a_repository_line_every_command_refuses(self) -> None:
        self.write("unreleased main")
        self.refused("dependencies/lib.pin: the unreleased line is for a third party, which says where it lives")

    def test_a_maelys_repository_trying_the_line_is_refused_by_every_command(self) -> None:
        """The rule that does not move: a product pins tagged versions of the fleet."""
        self.product.write("dependencies/maelys-system.pin",
                           f"{PINNED_TAG}\n{self.product.pinned}\n"
                           "repository https://github.com/maelys-dev/maelys-system.git\nunreleased main\n")
        self.refused("https://github.com/maelys-dev/maelys-system.git is a Maelys repository."
                     " A Maelys repository is pinned on a tag")


class ScriptReadsTheRepositoryLineOrRefusesTest(unittest.TestCase):
    """The managed script, run for real: it clones the repository the file names, or nothing.

    Its reader matches `repository <https URL>` from the first column to the
    last and took any other spelling for no line at all -- and then cloned
    the organisation's repository of the pin's name. Measured before the
    fix on a pin named after a fleet repository: exit 0, and a checkout of
    the fleet's repository where the file named a third party. The reader
    in Python refuses those pins, but a product whose `check` runs an older
    pinned socle is told nothing, and the script is what clones.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.third = ThirdParty(self.product)
        # The template as it is installed: it carries no placeholder.
        script = self.product.dir / "scripts" / "checkout-dependency.sh"
        script.write_text((ROOT / "share" / "templates" / "checkout-dependency.sh").read_text(encoding="utf-8"),
                          encoding="utf-8")
        self.script = script
        self.count = 0

    def clone(self, name: str, shell: str = "sh") -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        self.count += 1
        destination = self.product.work / f"cloned-{self.count}"
        completed = subprocess.run([shell, str(self.script), name, str(destination)], cwd=self.product.dir,
                                   env=self.product.env, check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return completed, destination

    def pin(self, name: str, *lines: str) -> pathlib.Path:
        self.product.write(f"dependencies/{name}.pin", "\n".join(lines) + "\n")
        return self.product.dir / "dependencies" / f"{name}.pin"

    # The pin is named after a repository the organisation does serve, at a
    # commit that repository holds: the fallback, if taken, succeeds.
    MALFORMED = (("a space after the URL", "repository {url} "),
                 ("a space before the word", " repository {url}"),
                 ("a tab before the word", "\trepository {url}"),
                 ("a tab for the space", "repository\t{url}"),
                 ("another case", "Repository {url}"),
                 ("capitals", "REPOSITORY {url}"),
                 ("a colon", "repository: {url}"),
                 ("the word alone", "repository"),
                 ("the word and a space", "repository "),
                 ("no https URL", "repository git@example.org:their/lib.git"),
                 ("the word glued to the URL", "repository{url}"),
                 ("two words after it", "repository {url} main"))

    def test_a_repository_line_it_does_not_match_stops_it_and_nothing_is_cloned(self) -> None:
        for shell in filter(shutil.which, ("sh", "dash")):
            for what, line in self.MALFORMED:
                with self.subTest(shell=shell, line=what):
                    pin = self.pin("maelys-system", PINNED_TAG, self.product.pinned,
                                   line.format(url=f"{THIRD}/lib.git"))
                    completed, destination = self.clone("maelys-system", shell)
                    self.assertEqual(completed.returncode, 65, completed.stderr)
                    self.assertFalse(destination.exists(), "nothing is cloned")
                    self.assertEqual(completed.stdout, "")
                    self.assertIn(f"checkout-dependency: {pin} line 3 starts like a repository line and is not"
                                  " one", completed.stderr)
                    self.assertIn("Nothing is cloned", completed.stderr)

    def test_the_line_is_named_where_it_is(self) -> None:
        pin = self.pin("maelys-system", PINNED_TAG, self.product.pinned, "submodules", "on-request",
                       f"repository {THIRD}/lib.git ")
        completed, destination = self.clone("maelys-system")
        self.assertEqual(completed.returncode, 65, completed.stderr)
        self.assertIn(f"{pin} line 5 starts like a repository line", completed.stderr)
        self.assertFalse(destination.exists())

    def test_a_second_repository_line_stops_it(self) -> None:
        """It read the first, and the reader in Python read the last."""
        pin = self.pin("lib", "v1.1.0", self.third.c3, f"repository {THIRD}/lib.git", "unreleased main",
                       "repository https://other.example.invalid/lib.git")
        completed, destination = self.clone("lib")
        self.assertEqual(completed.returncode, 65, completed.stderr)
        self.assertIn(f"checkout-dependency: {pin} line 5 is a second repository line, and one is read",
                      completed.stderr)
        self.assertFalse(destination.exists())
        # The same line twice is a second line too.
        self.pin("lib", "v1.1.0", self.third.c3, f"repository {THIRD}/lib.git", f"repository {THIRD}/lib.git")
        completed, destination = self.clone("lib")
        self.assertEqual((completed.returncode, destination.exists()), (65, False), completed.stderr)

    def test_a_well_formed_pin_is_cloned_as_it_was(self) -> None:
        """A third party's, a plain pin of the fleet, and one that says
        everything a pin can say: what the script printed and where it
        left HEAD, before this change and after it."""
        cases = (("lib", ("v1.1.0", self.third.c2, f"repository {THIRD}/lib.git"), f"{THIRD}/lib.git"),
                 ("maelys-system", (f"{PINNED_TAG}-1-g{self.product.pinned[:7]}", self.product.pinned),
                  f"{self.product.env['MAELYS_GIT_BASE']}/maelys-system.git"),
                 ("lib", ("v1.1.0", self.third.c3, f"repository {THIRD}/lib.git", "submodules", "unreleased main",
                          "on-request"), f"{THIRD}/lib.git"))
        for shell in filter(shutil.which, ("sh", "dash")):
            for name, lines, source in cases:
                with self.subTest(shell=shell, pin=lines[2:]):
                    self.pin(name, *lines)
                    completed, destination = self.clone(name, shell)
                    self.assertEqual((completed.returncode, completed.stderr), (0, ""))
                    self.assertEqual(completed.stdout,
                                     f"{name} {lines[0]} ({lines[1]}) from {source} in {destination}\n")
                    self.assertEqual(self.product.git(destination, "rev-parse", "HEAD"), lines[1])
                    self.assertEqual(self.product.git(destination, "config", "--get", "remote.origin.url"), source)

    def test_what_the_script_does_not_read_it_still_ignores(self) -> None:
        """Only the repository line is refused here. Another attribute
        spelled wrong, or a word nobody knows, is the reader's in Python
        to refuse: the script clones as it did."""
        self.pin("lib", "v1.1.0", self.third.c3, f"repository {THIRD}/lib.git", "Unreleased main",
                 "unreleased\tmain", " on-request", "mirror-of somewhere", "# repository of record: theirs")
        completed, destination = self.clone("lib")
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertEqual(self.product.git(destination, "rev-parse", "HEAD"), self.third.c3)

    def test_the_loop_stops_on_it_too(self) -> None:
        """Every job clones through checkout-dependencies.sh, which calls this script per pin."""
        loop = self.product.dir / "scripts" / "checkout-dependencies.sh"
        loop.write_text((ROOT / "share" / "templates" / "checkout-dependencies.sh").read_text(encoding="utf-8"),
                        encoding="utf-8")
        self.pin("maelys-system", PINNED_TAG, self.product.pinned, f"repository {THIRD}/lib.git ")
        destination = self.product.work / "runner"
        completed = subprocess.run(["sh", str(loop), str(destination)], cwd=self.product.dir, env=self.product.env,
                                   check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 65, completed.stderr)
        self.assertEqual(completed.stdout, "", "no MAELYS_DEPENDENCIES_DIR for a root that holds nothing")
        self.assertFalse((destination / "maelys-system").exists())


class UnreleasedCheckoutTest(unittest.TestCase):
    """`dependencies`: the one command that holds a clone, and what it reads there."""

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        self.product.env["XDG_CACHE_HOME"] = str(self.product.work / "cache")
        self.home = (self.product.work / "cache" / "maelys-release" / "dependencies" / "maelys-fixture").resolve()
        self.third = ThirdParty(self.product)

    def apply(self, expect: int = 0) -> dict:
        data = self.product.json("dependencies", self.dir, "--apply", expect=expect)["data"]
        data["lib"] = [entry for entry in data["dependencies"] if entry["name"] == "lib"][0]
        data["about"] = [line for line in data["refused"] + data["notes"] if line.startswith("lib:")]
        return data

    def test_the_managed_scripts_clone_it_at_its_commit(self) -> None:
        """Line 2 is what is cloned, and the scripts read no branch: an
        unknown attribute line stops neither of them."""
        self.third.pin(self.third.c3)
        self.product.run("adopt", self.dir, "--apply")
        destination = self.product.work / "runner"
        completed = subprocess.run(["sh", str(self.product.dir / "scripts" / "checkout-dependencies.sh"),
                                    str(destination)], cwd=self.product.dir, env=self.product.env, check=False,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.product.git(destination / "lib", "rev-parse", "HEAD"), self.third.c3)
        self.assertIn(f"lib v1.1.0 ({self.third.c3}) from {THIRD}/lib.git", completed.stderr)

    def test_a_commit_on_its_branch_past_its_tag_is_materialised_and_nothing_is_said(self) -> None:
        self.third.pin(self.third.c3)
        data = self.apply()
        self.assertEqual(self.product.git(self.home / "lib", "rev-parse", "HEAD"), self.third.c3)
        self.assertEqual((data["lib"]["action"], data["lib"]["unreleased"], data["lib"]["asked"]),
                         ("clone", "main", True))
        self.assertEqual(data["about"], [], "neither a refusal nor a note: the pin is what it says")

    def test_the_plan_says_it_did_not_ask_and_refuses_nothing_on_the_branch(self) -> None:
        self.third.pin(self.third.c3)
        self.apply()
        with self.third.unreachable():
            data = self.product.json("dependencies", self.dir)["data"]
            text = self.product.run("dependencies", self.dir).stdout
        lib = [entry for entry in data["dependencies"] if entry["name"] == "lib"][0]
        self.assertEqual((lib["action"], lib["asked"]), ("same", False))
        self.assertEqual((data["refused"], data["notes"]), ([], []))
        self.assertIn(f"same     lib past v1.1.0 ({self.third.c3[:7]}) on main, not asked: --apply asks the"
                      " repository whether main still holds it", text)
        # A pin on a tag keeps the line it had.
        self.assertRegex(text, r"same     maelys-system \S+ \([0-9a-f]{7}\)\n")

    def test_every_row_of_a_plan_says_what_it_did_not_ask(self) -> None:
        """A row that would clone, or refresh, judged nothing and said nothing of it."""
        self.third.pin(self.third.c3)
        asked = f", not asked: --apply asks the repository whether main still holds it"
        with self.third.unreachable():
            data = self.product.json("dependencies", self.dir)["data"]
            text = self.product.run("dependencies", self.dir).stdout
        lib = [entry for entry in data["dependencies"] if entry["name"] == "lib"][0]
        self.assertEqual((lib["action"], lib["asked"]), ("clone", False))
        self.assertIn(f"clone    lib past v1.1.0 ({self.third.c3[:7]}) on main from {THIRD}/lib.git{asked}\n", text)
        self.apply()
        # The pin moves along main, to a commit this checkout does not hold yet.
        newer = self.third.commit("five")
        self.third.publish("main")
        self.third.pin(newer, tag="v2.0.0")
        with self.third.unreachable():
            data = self.product.json("dependencies", self.dir)["data"]
            text = self.product.run("dependencies", self.dir).stdout
        lib = [entry for entry in data["dependencies"] if entry["name"] == "lib"][0]
        self.assertEqual((lib["action"], lib["asked"]), ("refresh", False))
        self.assertIn(f"refresh  lib past v2.0.0 ({newer[:7]}) on main, here {self.third.c3[:7]}{asked}\n", text)
        # Judged only where the checkout is at the pin: of a commit it does
        # not hold, this checkout has nothing to say, and says nothing.
        self.assertEqual([line for line in data["refused"] + data["notes"] if line.startswith("lib:")], [])
        # A pin on a tag says nothing of asking: there is nothing to ask.
        self.assertRegex(text, r"same     maelys-system \S+ \([0-9a-f]{7}\)\n")

    def test_an_on_request_pin_is_neither_fetched_nor_judged_unless_asked_for(self) -> None:
        """Nothing looks at what sits at its path: with a working copy of
        another repository there, the branch was fetched from that
        repository's origin, and the pin refused by the other's tip."""
        self.third.pin(self.third.c3, "v1.1.0", "main", "on-request")
        other = self.home / "lib"
        self.product.git(self.product.work, "clone", "-q", str(self.product.work / "remotes" / "maelys-system.git"),
                         str(other))
        before = self.product.git(other, "for-each-ref")
        data = self.apply()
        self.assertEqual((data["lib"]["action"], data["lib"]["asked"], data["about"]), ("on-request", False, []))
        self.assertEqual(self.product.git(other, "for-each-ref"), before, "nothing was fetched into it")
        text = self.product.run("dependencies", self.dir, "--apply").stdout
        self.assertIn(f"request  lib past v1.1.0 ({self.third.c3[:7]}) on main is cloned on request only: pass --all"
                      " to bring it here, not asked: --all --apply asks the repository whether main still holds"
                      " it\n", text)
        # Asked for, it is read like any other -- and what sits there is seen first.
        error = self.product.json("dependencies", self.dir, "--apply", "--all", expect=1)["error"]
        self.assertIn("lib", error["message"])
        self.assertIn("Nothing written", error["message"])

    def test_the_day_the_commit_is_gone_the_failure_names_the_pin_and_its_branch(self) -> None:
        """On a new machine: git said "not our ref" for a hash, from a checkout
        nobody had judged yet, and the pin and its branch were never named."""
        self.third.pin(self.third.c3)
        self.third.rewrite_main(prune=True)
        error = self.product.json("dependencies", self.dir, "--apply", expect=1)["error"]
        self.assertEqual(error["code"], "PROCESS_FAILED")
        self.assertIn(f"lib: {self.third.c3[:7]} could not be obtained from {THIRD}/lib.git; the pin says it is on"
                      " main, under no tag.", error["message"])
        # What was read, then what it most likely means, then git's own words.
        self.assertRegex(error["message"], r"holds main at [0-9a-f]{7} and could not be brought to that commit\."
                                           r" The most likely reading: the branch was rewritten")
        self.assertIn("git said: ", error["message"])
        self.assertIn("re-pin on a commit of main", error["hint"])
        self.assertIn("blocks the next run until it is moved aside", error["hint"])
        # The other end: a pin on a tag fails in the words it had.
        shutil.rmtree(self.home / "lib")
        self.third.pin("d" * 40, branch="")
        error = self.product.json("dependencies", self.dir, "--apply", expect=1)["error"]
        self.assertTrue(error["message"].startswith("git checkout"), error["message"])

    def test_a_repository_that_does_not_answer_is_a_note_and_the_run_succeeds(self) -> None:
        """Offline, `--apply` still materialises nothing new and refuses nothing."""
        self.third.pin(self.third.c3)
        self.apply()
        with self.third.unreachable():
            data = self.apply()
            text = self.product.run("dependencies", self.dir, "--apply").stdout
        self.assertEqual((data["lib"]["action"], data["lib"]["asked"], data["refused"]), ("same", False, []))
        self.assertEqual(len(data["about"]), 1, data)
        self.assertIn(f"lib: {THIRD}/lib.git could not be asked whether main still holds {self.third.c3[:7]}",
                      data["about"][0])
        self.assertNotIn("not asked: --apply", text, "it was asked; the note says what git answered")

    def test_a_line_1_that_is_not_an_ancestor_is_refused(self) -> None:
        """v2.0.0 is a tag of that repository and comes after the commit."""
        self.third.pin(self.third.c3, tag="v2.0.0")
        data = self.apply(expect=2)
        self.assertEqual(data["about"], [f"lib: line 1 names v2.0.0, which is {self.third.c4[:7]} and not an ancestor"
                                         f" of {self.third.c3[:7]}. With an unreleased line, line 1 is the last tag"
                                         " before the commit: v1.1.0"])
        with self.third.unreachable():
            self.assertIn("REFUSED  lib: line 1 names v2.0.0",
                          self.product.run("dependencies", self.dir, expect=2).stdout,
                          "and offline too: the tag is in the checkout in hand")

    def test_a_line_1_that_names_no_tag_of_that_repository_is_refused_and_offline_it_is_said(self) -> None:
        self.third.pin(self.third.c3, tag="v1.1.5")
        data = self.apply(expect=2)
        self.assertEqual(data["about"], [f"lib: line 1 names v1.1.5, which is not a tag of {THIRD}/lib.git. With an"
                                         " unreleased line, line 1 is the last tag before the commit: v1.1.0"])
        # Offline the checkout is at the pin and holds no such tag: not a
        # refusal -- a tag may have been made since -- and not silence.
        with self.third.unreachable():
            data = self.product.json("dependencies", self.dir)["data"]
        self.assertEqual(data["refused"], [])
        self.assertEqual([line for line in data["notes"] if line.startswith("lib:")],
                         [f"lib: line 1 names v1.1.5, which this checkout does not hold, and {THIRD}/lib.git was"
                          " not asked for it: not verified"])

    def test_a_commit_that_carries_a_tag_is_a_pin_on_that_tag(self) -> None:
        """An ancestor, and not equal: the line is for a commit no tag carries."""
        self.third.pin(self.third.c2)
        data = self.apply(expect=2)
        self.assertEqual(data["about"], [f"lib: line 1 names v1.1.0, which is {self.third.c2[:7]} itself. A commit"
                                         " that carries a tag is a pin on that tag: remove the unreleased line"])
        # The other end: without the line, the same pin is a pin on a tag.
        shutil.rmtree(self.home / "lib")
        self.third.pin(self.third.c2, branch="")
        self.assertEqual(self.apply()["about"], [])

    def test_any_ancestor_tag_is_accepted_and_a_nearer_one_is_a_note(self) -> None:
        self.third.pin(self.third.c3, tag="v1.0.0")
        data = self.apply()
        self.assertEqual(data["refused"], [])
        self.assertEqual(data["about"], [f"lib: v1.0.0 is not the last tag before {self.third.c3[:7]}: v1.1.0"
                                         " lies between them. Write v1.1.0 on line 1"])

    def test_a_tag_on_a_merged_side_branch_is_not_between(self) -> None:
        """Two tags that are both ancestors and neither of the other: either is the last."""
        self.third.git("checkout", "-q", "-b", "lts", self.third.c1)
        self.third.commit("maintenance")
        self.third.git("tag", "v1.0.1")
        self.third.git("checkout", "-q", "main")
        self.third.git("merge", "-q", "--no-ff", "-X", "ours", "-m", "merge lts", "lts")
        merged = self.third.git("rev-parse", "HEAD")
        self.third.publish("main", "v1.0.1")
        for tag in ("v2.0.0", "v1.0.1"):
            with self.subTest(tag=tag):
                self.third.pin(merged, tag=tag)
                self.assertEqual(self.apply()["about"], [])
                shutil.rmtree(self.home / "lib")

    def test_a_commit_that_is_not_on_the_declared_branch_is_refused(self) -> None:
        self.third.pin(self.third.s1)
        data = self.apply(expect=2)
        self.assertEqual(len(data["about"]), 1, data)
        self.assertIn(f"lib: {self.third.s1[:7]} is not on main of {THIRD}/lib.git, which is at {self.third.c4[:7]}:"
                      " the branch was rewritten, or never held it", data["about"][0])
        self.assertIn("a mirror, under a tag of its own", data["about"][0])
        # It says what this run read: the clone it made brought the commit.
        self.assertIn("This run obtained the commit from the repository.", data["about"][0])
        self.assertNotIn("still resolves", data["about"][0])
        # It is materialised all the same: the commit resolves, which is the
        # window the refusal exists to use.
        self.assertEqual(self.product.git(self.home / "lib", "rev-parse", "HEAD"), self.third.s1)
        # The other end: the branch that does hold it.
        shutil.rmtree(self.home / "lib")
        self.third.pin(self.third.s1, branch="next")
        self.assertEqual(self.apply()["about"], [])

    def test_a_branch_the_repository_does_not_have_is_refused(self) -> None:
        self.third.pin(self.third.c3, branch="trunk")
        data = self.apply(expect=2)
        self.assertEqual(len(data["about"]), 1, data)
        self.assertIn(f"lib: {THIRD}/lib.git has no branch trunk", data["about"][0])

    def test_a_third_party_pin_without_the_line_is_told_of_it(self) -> None:
        """The pin the texts described -- the nearest tag over an untagged
        commit -- is the one the socle refuses, and now says what to write."""
        self.third.pin(self.third.c3, branch="")
        data = self.apply(expect=2)
        self.assertEqual(len(data["about"]), 1, data)
        self.assertIn(f"lib: line 1 names v1.1.0, which is {self.third.c2[:7]}, and line 2 is {self.third.c3[:7]}."
                      " A published tag never moves, so one of the two is wrong", data["about"][0])
        self.assertIn("'unreleased <branch>' line", data["about"][0])
        # What git describe says of it names the commit itself: accepted, and
        # still the untagged commit of the incident.
        shutil.rmtree(self.home / "lib")
        self.third.pin(self.third.c3, tag=f"v1.1.0-1-g{self.third.c3[:7]}", branch="")
        data = self.apply()
        self.assertEqual(data["refused"], [])
        self.assertEqual(len(data["about"]), 1, data)
        self.assertIn(f"lib: {self.third.c3[:7]} carries no tag", data["about"][0])
        self.assertIn("'unreleased <branch>' line", data["about"][0])
        # A plan asks nothing, and says so of a commit --apply asks about.
        with self.third.unreachable():
            data = self.product.json("dependencies", self.dir)["data"]
        self.assertEqual([line for line in data["notes"] if line.startswith("lib:")],
                         [f"lib: this checkout holds no tag on {self.third.c3[:7]}, and {THIRD}/lib.git was not asked"
                          " for one: --apply asks"])

    def test_a_third_party_commit_no_tag_carries_is_told_so_by_its_repository_on_a_checkout_already_there(self) -> None:
        """Above, the run that says "carries no tag" is the one that cloned,
        and the answer is the clone's. A checkout made by an earlier run is
        the one that asks: the repository answers that the commit carries no
        tag, and the note is the one it had."""
        self.third.pin(self.third.c3, tag=f"v1.1.0-1-g{self.third.c3[:7]}", branch="")
        self.apply()
        question = ["git", "ls-remote", "--tags", "origin"]

        def again(*arguments: str) -> tuple[list[str], list[list[str]]]:
            host = RealGitHost(self.product.env, as_written=True)
            code, data = host.dependencies(self.product, *arguments)
            self.assertEqual((code, data["refused"]), (0, []), data)
            self.assertEqual([entry["action"] for entry in data["dependencies"]], ["same", "same"])
            return [line for line in data["notes"] if line.startswith("lib:")], host.reaching()
        unasked = (f"lib: this checkout holds no tag on {self.third.c3[:7]}, and {THIRD}/lib.git was not asked for"
                   " one: --apply asks")
        self.assertEqual(again(), ([unasked], []))
        about, reaching = again("--apply")
        self.assertEqual(reaching, [question], "the repository's answer, and nothing to bring")
        self.assertEqual(len(about), 1, about)
        self.assertIn(f"lib: {self.third.c3[:7]} carries no tag. A rewritten branch takes an untagged commit with"
                      " it", about[0])
        self.assertIn("'unreleased <branch>' line", about[0])
        # The other end: the third party tags that very commit, and line 1
        # still names it by its hash. Nothing is said, and the tag is brought.
        self.third.git("tag", "v1.2.0", self.third.c3)
        self.third.publish("v1.2.0")
        self.assertEqual(again(), ([unasked], []))
        self.assertEqual(again("--apply"), ([], [question, ["git", "fetch", "--quiet", "--no-tags", "origin",
                                                             "refs/tags/v1.2.0:refs/tags/v1.2.0"]]))
        for arguments in ((), ("--apply",)):
            with self.subTest(arguments=arguments):
                self.assertEqual(again(*arguments), ([], []))

    # What git says of itself when it reaches a repository, a fetch it
    # starts on its own included.
    REACHES = re.compile(r"upload-pack|git-remote|built-in: git (fetch|ls-remote|clone|pull|push)"
                         r"|run_command: .*\b(fetch|ls-remote)\b|exec: .*\bssh\b")

    def traced(self, *arguments: str) -> tuple[dict, list[str]]:
        """One run, and what git's own trace of it shows reaching a repository.

        The host cannot see this: in the partial clone the socle makes, git
        fetches by itself an object it is asked about and does not hold,
        and that fetch is git's own child.
        """
        trace = self.product.work / "trace"
        trace.unlink(missing_ok=True)
        host = RealGitHost({**self.product.env, "GIT_TRACE": str(trace)}, as_written=True)
        _, data = host.dependencies(self.product, *arguments)
        lines = trace.read_text(encoding="utf-8", errors="replace").splitlines()
        self.assertTrue(lines, "git traced nothing: the test proves nothing")
        return data, [line.split("trace: ", 1)[-1] for line in lines if self.REACHES.search(line)]

    def test_no_plan_reaches_a_repository_and_git_does_not_do_it_for_one(self) -> None:
        """A line 1 that is the whole hash of a commit the checkout does not
        hold made git fetch it, from a plan: the commit was in the checkout
        afterwards, and against a repository that says nothing the plan
        never came back."""
        def plan(shape: str, *arguments: str) -> dict:
            with self.subTest(shape=shape):
                data, reaching = self.traced(*arguments)
                self.assertEqual(reaching, [], shape)
            return {entry["name"]: entry["action"] for entry in data["dependencies"]}

        def held(checkout: str, commit: str) -> bool:
            return subprocess.run(["git", "-C", str(self.home / checkout), "cat-file", "-e", commit],
                                  env={**self.product.env, "GIT_NO_LAZY_FETCH": "1"}, check=False,
                                  stderr=subprocess.DEVNULL).returncode == 0
        system = self.product.work / "src" / "maelys-system"
        maelys = "dependencies/maelys-system.pin"
        self.third.pin(self.third.c2, branch="")
        _, reaching = self.traced("--apply")
        self.assertTrue(reaching, "the trace does not show a clone: the test proves nothing")
        self.assertEqual(plan("each at its pin, on a tag or past one"), {"maelys-system": "same", "lib": "same"})
        # Commits made since, which neither checkout holds.
        (system / "file").write_text("three\n", encoding="utf-8")
        self.product.git(system, "commit", "-q", "-am", "three")
        self.product.git(system, "push", "-q", str(self.product.work / "remotes" / "maelys-system.git"), "main")
        newer = self.product.git(system, "rev-parse", "HEAD")
        five = self.third.commit("five")
        self.third.publish("main")
        for shape, spelled in (("the whole hash", str), ("the whole hash in capitals", str.upper),
                               ("what git describe says", lambda commit: f"{PINNED_TAG}-2-g{commit[:7]}"),
                               ("a tag nobody has", lambda commit: "v0.0.2")):
            self.product.write(maelys, f"{spelled(newer)}\n{newer}\n")
            self.third.pin(five, tag=spelled(five), branch="")
            self.assertEqual(plan(f"a refresh pending, line 1 {shape}"), {"maelys-system": "refresh", "lib": "refresh"})
        self.assertFalse(held("maelys-system", newer) or held("lib", five), "a plan brought a commit")
        # The whole hash of another commit, which only the repository holds.
        self.product.write(maelys, f"{newer}\n{self.product.pinned}\n")
        self.third.pin(self.third.c2, tag=five, branch="")
        self.assertEqual(plan("at its pin, line 1 the whole hash of a commit not held"),
                         {"maelys-system": "same", "lib": "same"})
        self.assertFalse(held("maelys-system", newer) or held("lib", five), "a plan brought a commit")
        # A pin that says `unreleased`: before the refresh, and at the pin.
        self.product.write(maelys, f"{PINNED_TAG}-1-g{self.product.pinned[:7]}\n{self.product.pinned}\n")
        self.third.pin(five, tag="v2.0.0")
        self.assertEqual(plan("unreleased, a refresh pending")["lib"], "refresh")
        self.third.pin(self.third.c3)
        self.assertEqual(plan("unreleased, a refresh pending to a commit held")["lib"], "refresh")
        self.traced("--apply")
        self.assertEqual(plan("unreleased, at its pin")["lib"], "same")
        # And one cloned on request only, read or not.
        self.third.pin(five, "v2.0.0", "main", "on-request")
        self.assertEqual(plan("on request, not asked for")["lib"], "on-request")
        self.assertEqual(plan("on request, asked for", "--all")["lib"], "refresh")

    def test_a_maelys_pin_keeps_the_words_it_had(self) -> None:
        """The line is not offered to a repository that may not write it."""
        self.assertEqual([line for line in self.apply_plain()["notes"] if line.startswith("maelys-system:")], [],
                         "what git describe says of a Maelys commit past its tag is what migrate writes")
        self.product.write("dependencies/maelys-system.pin", f"{PINNED_TAG}\n{self.product.pinned}\n")
        data = self.apply_plain(expect=2)
        self.assertEqual(data["refused"], [f"maelys-system: line 1 names {PINNED_TAG}, which is"
                                           f" {self.product.tagged[:7]}, and line 2 is {self.product.pinned[:7]}."
                                           " A published tag never moves, so one of the two is wrong; re-pin on"
                                           " the tag's own commit"])
        self.product.write("dependencies/maelys-system.pin", f"{PINNED_TAG}\n{self.product.tagged}\n")
        data = self.apply_plain()
        self.assertEqual((data["refused"], data["notes"]), ([], []))

    def test_a_line_1_that_names_nothing_is_noted_whatever_the_commit_carries(self) -> None:
        """The conventions said so, and over a tagged commit nothing was said."""
        self.product.write("dependencies/maelys-system.pin", f"vBogus\n{self.product.tagged}\n")
        data = self.apply_plain()
        self.assertEqual(data["refused"], [])
        self.assertEqual(data["notes"], [f"maelys-system: line 1 names vBogus, which this checkout does not hold, and"
                                         f" {self.product.tagged[:7]} carries {PINNED_TAG} in it: line 1 is wrong, or"
                                         " names a tag made since this checkout last fetched its tags"])
        # Over a commit that carries none, the note it always had.
        self.product.write("dependencies/maelys-system.pin", f"vBogus\n{self.product.pinned}\n")
        notes = self.apply_plain()["notes"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(f"maelys-system: {self.product.pinned[:7]} carries no tag", notes[0])

    def test_line_1_reaches_git_as_a_revision_and_never_as_an_option(self) -> None:
        """`--all` on line 1 was an instruction to `git rev-list`, and answered with a commit."""
        self.apply_plain()
        checkout = self.home / "maelys-system"
        named = MODULE.dependencies.named_by
        with self.environment():
            self.assertEqual(named(PINNED_TAG, checkout), self.product.tagged)
            for line in ("--all", "--branches", "-n2", "--remotes"):
                with self.subTest(line=line):
                    self.assertEqual(named(line, checkout), "")
        # And the reader refuses it before any checkout is asked.
        self.product.write("dependencies/maelys-system.pin", f"--all\n{self.product.tagged}\n")
        error = self.product.json("dependencies", self.dir, expect=1)["error"]
        self.assertIn("'--all' starts with a dash", error["message"])

    @contextmanager
    def environment(self):
        """The fixture's git configuration, for a call made in this process."""
        keys = [key for key in self.product.env if key.startswith("GIT_CONFIG")]
        previous = {key: MODULE.os.environ.get(key) for key in keys}
        MODULE.os.environ.update({key: self.product.env[key] for key in keys})
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    MODULE.os.environ.pop(key, None)
                else:
                    MODULE.os.environ[key] = value

    def apply_plain(self, expect: int = 0) -> dict:
        return self.product.json("dependencies", self.dir, "--apply", expect=expect)["data"]


class UnreleasedBranchAskedTest(unittest.TestCase):
    """The branch is the remote's to say: asked on purpose, and refused only on its answer.

    Read at the function, against a real remote and a real checkout: the
    command compares a checkout's origin with the pin's URL after git has
    expanded `insteadOf`, so a second run of it cannot be served an https
    name from a directory. The first run is, above.
    """

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.third = ThirdParty(self.product)
        self.checkout = self.clone("checkout", self.third.c3)
        self.previous = {key: MODULE.os.environ.get(key) for key in self.product.env if key.startswith("GIT_CONFIG")}
        MODULE.os.environ.update({key: self.product.env[key] for key in self.previous})
        self.addCleanup(self.restore)

    def clone(self, name: str, commit: str, *options: str) -> pathlib.Path:
        """The clone the socle makes, or with `options` one it would not have made."""
        path = self.product.work / name
        self.product.git(self.product.work, "clone", "-q", *(options or ("--filter=blob:none",)), "--no-checkout",
                         f"{THIRD}/lib.git", str(path))
        self.product.git(path, "checkout", "-q", "--detach", commit)
        return path

    def restore(self) -> None:
        for key, value in self.previous.items():
            if value is None:
                MODULE.os.environ.pop(key, None)
            else:
                MODULE.os.environ[key] = value

    def verdict(self, commit: str, fetch: bool, tag: str = "v1.1.0", branch: str = "main",
                checkout: "pathlib.Path | None" = None) -> tuple:
        pin = {"name": "lib", "tag": tag, "commit": commit, "unreleased": branch}
        return MODULE.dependencies.unreleased_pin(pin, f"{THIRD}/lib.git", checkout or self.checkout,
                                                  fetch=fetch, current=False)

    def test_asked_and_still_there(self) -> None:
        self.assertEqual(self.verdict(self.third.c3, fetch=True), ([], [], True))

    def test_the_day_the_branch_is_rewritten_the_pin_is_named_while_it_still_resolves(self) -> None:
        self.assertEqual(self.verdict(self.third.c3, fetch=True)[0], [])
        # The third party rewrites main from v1.1.0: c3 is on no branch.
        self.third.git("reset", "-q", "--hard", self.third.c2)
        rewritten = self.third.commit("three, again")
        self.third.publish("main", force=True)
        # Not asked, nothing is known, and nothing is refused.
        self.assertEqual(self.verdict(self.third.c3, fetch=False), ([], [], False))
        refusals, notes, asked = self.verdict(self.third.c3, fetch=True)
        self.assertEqual((notes, asked), ([], True))
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn(f"lib: {self.third.c3[:7]} is not on main of {THIRD}/lib.git, which is at {rewritten[:7]}",
                      refusals[0])
        # It says what was read, and that the repository was not asked for the commit.
        self.assertIn("This checkout holds the commit, and nothing here asked whether the repository still serves"
                      " it.", refusals[0])
        self.assertNotIn("still resolves", refusals[0])
        # And it still resolves, in a clone made after the rewrite: nothing
        # else would have said anything until the day it stops.
        late = self.product.work / "late"
        self.product.git(self.product.work, "clone", "-q", "--filter=blob:none", "--no-checkout",
                         f"{THIRD}/lib.git", str(late))
        self.product.git(late, "checkout", "-q", "--detach", self.third.c3)
        self.assertEqual(self.product.git(late, "rev-parse", "HEAD"), self.third.c3)

    def test_a_ref_this_checkout_did_not_move_refuses_nothing(self) -> None:
        """Measured before the rule was written: a refresh fetches the commit
        by its hash and leaves the branch's ref where the clone put it, so a
        re-pinned commit is "not an ancestor" of a branch that holds it."""
        newer = self.third.commit("five")
        self.third.publish("main")
        # What `materialise` does on a refresh.
        self.product.git(self.checkout, "fetch", "-q", "origin", newer)
        self.product.git(self.checkout, "checkout", "-q", "--detach", newer)
        stale = subprocess.run(["git", "-C", str(self.checkout), "merge-base", "--is-ancestor", newer,
                                "refs/remotes/origin/main"], env=self.product.env, check=False)
        self.assertEqual(stale.returncode, 1, "the ref in hand is the branch as of the clone")
        self.assertEqual(self.verdict(newer, fetch=False, tag="v2.0.0"), ([], [], False))
        self.assertEqual(self.verdict(newer, fetch=True, tag="v2.0.0"), ([], [], True))

    def test_a_branch_that_is_gone_is_refused_and_a_repository_that_does_not_answer_is_a_note(self) -> None:
        self.product.git(self.third.bare, "branch", "-q", "-m", "main", "trunk")
        refusals, notes, asked = self.verdict(self.third.c3, fetch=True)
        self.assertEqual((notes, asked), ([], True))
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn(f"lib: {THIRD}/lib.git has no branch main", refusals[0])
        # Not answering is not answering no.
        self.product.git(self.checkout, "remote", "set-url", "origin", f"{THIRD}/nowhere.git")
        refusals, notes, asked = self.verdict(self.third.c3, fetch=True)
        self.assertEqual((refusals, asked), ([], False))
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(f"lib: {THIRD}/lib.git could not be asked whether main still holds {self.third.c3[:7]}",
                      notes[0])
        self.assertTrue(notes[0].endswith(": not verified"), notes[0])

    def test_a_tag_the_third_party_adds_later_is_a_note_and_never_a_refusal(self) -> None:
        """A product must not turn red because its third party cut a release."""
        older = self.third.commit("five")
        pinned = self.third.commit("six")
        self.third.publish("main")
        self.assertEqual(self.verdict(pinned, fetch=True, tag="v2.0.0"), ([], [], True))
        # A release cut on an older commit of a branch that has moved on:
        # line 1 was the last tag, and is not any more.
        self.third.git("tag", "v2.1.0", older)
        self.third.publish("v2.1.0")
        refusals, notes, _ = self.verdict(pinned, fetch=True, tag="v2.0.0")
        self.assertEqual(refusals, [])
        self.assertEqual(notes, [f"lib: v2.0.0 is not the last tag before {pinned[:7]}: v2.1.0 lies between"
                                 " them. Write v2.1.0 on line 1"])
        # And on the commit itself: it has become a pin on a tag, and the
        # line no longer says the truth.
        self.third.git("tag", "v2.2.0", pinned)
        self.third.publish("v2.2.0")
        refusals, notes, _ = self.verdict(pinned, fetch=True, tag="v2.1.0")
        self.assertEqual(refusals, [])
        self.assertEqual(notes, [f"lib: {pinned[:7]} carries v2.2.0, so the unreleased line no longer says the"
                                 " truth. It is a pin on a tag: write v2.2.0 on line 1 and remove the line"])

    def test_the_commit_pruned_over_there_and_held_here_is_refused_without_a_claim(self) -> None:
        """The branch rewritten and the commit collected, an old checkout
        still at the pin: the refusal said "the commit still resolves here;
        it stops the day the third party prunes it", of a repository it had
        not asked and that had already pruned."""
        self.third.rewrite_main(prune=True)
        refusals, notes, asked = self.verdict(self.third.c3, fetch=True)
        self.assertEqual((notes, asked), ([], True))
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("is not on main", refusals[0])
        self.assertIn("nothing here asked whether the repository still serves it", refusals[0])
        self.assertIn("which this checkout can push", refusals[0])

    def test_line_1_is_exactly_the_name_of_a_tag(self) -> None:
        """`refs/tags/v1.1.0^0` resolves, and v1.1.0^0 is no tag: the reader
        refuses the spelling, and a pin that reached here unread is held to
        the same rule."""
        for tag in ("v1.1.0^0", "v1.1.0~1", "v1.1.0^{commit}"):
            with self.subTest(tag=tag):
                refusals, notes, asked = self.verdict(self.third.c3, fetch=True, tag=tag)
                self.assertEqual((notes, asked), ([], True))
                self.assertEqual(refusals, [f"lib: line 1 names {tag}, which is not a tag of {THIRD}/lib.git. With"
                                            " an unreleased line, line 1 is the last tag before the commit:"
                                            " v1.1.0"])
                # Not asked, it is not refused, and it is not passed either.
                refusals, notes, asked = self.verdict(self.third.c3, fetch=False, tag=tag)
                self.assertEqual((refusals, asked), ([], False))
                self.assertEqual(notes, [f"lib: line 1 names {tag}, which this checkout does not hold, and"
                                         f" {THIRD}/lib.git was not asked for it: not verified"])

    def test_a_shallow_checkout_cannot_say_no(self) -> None:
        """git answers "not an ancestor" for a history it was not given, and
        that read "the branch was rewritten, or never held it"."""
        tip = self.third.commit("five")
        self.third.publish("main")
        shallow = self.clone("shallow", tip, "--depth", "1")
        self.product.git(shallow, "fetch", "-q", "--depth", "1", "origin", self.third.c3)
        self.product.git(shallow, "checkout", "-q", "--detach", self.third.c3)
        refusals, notes, asked = self.verdict(self.third.c3, fetch=True, checkout=shallow)
        self.assertEqual((refusals, asked), ([], True))
        self.assertEqual(notes, [f"lib: whether main holds {self.third.c3[:7]} cannot be read from this checkout"
                                 " (it is shallow): not verified",
                                 f"lib: whether v1.1.0 is an ancestor of {self.third.c3[:7]} cannot be read from"
                                 " this checkout (it is shallow): not verified"])
        # The other end: the same question, of a checkout that has the history.
        self.assertEqual(self.verdict(self.third.c3, fetch=True), ([], [], True))

    def test_a_checkout_cloned_without_its_tags_is_asked_for_the_one_tag(self) -> None:
        """Fetching a branch follows no tag there, and "this checkout holds no
        such tag" read "line 1 names v1.1.0, which is no tag in the history"."""
        bare = self.clone("no-tags", self.third.c3, "--filter=blob:none", "--no-tags")
        self.assertEqual(self.product.git(bare, "tag"), "")
        refusals, notes, asked = self.verdict(self.third.c3, fetch=False, checkout=bare)
        self.assertEqual((refusals, asked), ([], False))
        self.assertEqual(notes, [f"lib: line 1 names v1.1.0, which this checkout does not hold, and"
                                 f" {THIRD}/lib.git was not asked for it: not verified"])
        self.assertEqual(self.verdict(self.third.c3, fetch=True, checkout=bare), ([], [], True))
        self.assertEqual(self.product.git(bare, "tag"), "v1.1.0", "that tag, and no other")
        # And a tag the repository does not have is its answer, not the checkout's.
        refusals, _, _ = self.verdict(self.third.c3, fetch=True, tag="v1.1.5", checkout=bare)
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn(f"which is not a tag of {THIRD}/lib.git", refusals[0])


class UnreleasedPinMovedTest(unittest.TestCase):
    """`cut` holds them to the rule of every pin: a release that moves one names it."""

    def repository(self) -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-unreleased."))
        self.addCleanup(shutil.rmtree, work, True)

        def git(*arguments: str) -> None:
            subprocess.run(["git", "-C", str(work.resolve()), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *arguments],
                           cwd=work, check=True, capture_output=True)
        git("init", "-q")
        (work / "dependencies").mkdir()
        pin = work / "dependencies" / "lib.pin"
        pin.write_text(f"v1.1.0\n{'a' * 40}\nrepository {THIRD}/lib.git\nunreleased main\n", encoding="utf-8")
        git("add", "-A"); git("commit", "-q", "-m", "one"); git("tag", "v1.0.0")
        pin.write_text(f"v1.1.0\n{'b' * 40}\nrepository {THIRD}/lib.git\nunreleased main\n", encoding="utf-8")
        git("add", "-A"); git("commit", "-q", "-m", "further along main")
        return work

    def test_an_entry_that_does_not_name_it_is_refused_and_naming_it_is_enough(self) -> None:
        project = self.repository()
        found = MODULE.moved_pins_named(project, "- Something else changed.")
        self.assertEqual([status for status, _ in found], ["fail"])
        self.assertIn("does not name lib", found[0][1])
        found = MODULE.moved_pins_named(project, "- Re-pins lib further along its main branch.")
        self.assertEqual([status for status, _ in found], ["ok"])


class UnreleasedPinCutTest(unittest.TestCase):
    """The same rule, where it is held: the gate of `cut`, before anything is written.

    The test above reads `moved_pins_named` alone, and passed with the
    call taken out of `cut_open`: a rule nothing calls is not held. This
    one goes through the first stop, as a plan.
    """

    ISOLATE = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")

    class Stub:
        """An invocation of the framework, reduced to what cut reads of it."""

        format = "json"

        def flag(self, name: str) -> bool:
            return False

        def option(self, name: str, default=None):
            return default

    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen is needed: the gate reads the signature of the last tag")
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = self.product.dir
        git = lambda *arguments: self.product.git(self.dir, "-c", "commit.gpgsign=false", *arguments)
        self.product.run("adopt", str(self.dir), "--apply")
        git("init", "-q")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.invalid")
        # What the gate asks of this checkout besides: tags it signs, with
        # a key the fleet names -- the last one included.
        key = self.product.work / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        self.product.name_signing_key(key)
        git("config", "tag.gpgsign", "true")
        git("config", "gpg.format", "ssh")
        git("config", "user.signingkey", str(key))
        for commit, message in (("a" * 40, "the release that was"), ("b" * 40, "further along their main")):
            self.product.write("dependencies/lib.pin",
                               f"v1.1.0\n{commit}\nrepository {THIRD}/lib.git\nunreleased main\n")
            git("add", "-A")
            git("commit", "-q", "-m", message)
            if commit.startswith("a"):
                git("tag", "-s", "-m", "v1.2.2", "v1.2.2")
        origin = self.product.work / "origin.git"
        self.product.git(self.product.work, "init", "-q", "--bare", str(origin))
        git("remote", "add", "origin", str(origin))
        git("push", "-q", "origin", "main")
        saved = {name: MODULE.os.environ.get(name) for name in self.ISOLATE}
        MODULE.os.environ.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                  "GIT_CONFIG_NOSYSTEM": "1"})
        self.addCleanup(lambda: [MODULE.os.environ.pop(name, None) if value is None
                                 else MODULE.os.environ.__setitem__(name, value) for name, value in saved.items()])
        self.log = open(self.product.work / "cut.log", "w", encoding="utf-8")
        self.addCleanup(self.log.close)

    def plan(self, entry: str) -> tuple[dict, int]:
        self.product.write("CHANGELOG.md", f"# Changelog\n\n## 1.3.0 — 2026-09-03\n\n{entry}\n\n"
                                           "## 1.2.3 — 2026-09-03\n\n- Something.\n")
        data = {"mode": "plan", "stage": "open", "product": "maelys-fixture", "project": str(self.dir),
                "repository": "maelys-dev/maelys-fixture", "version": "1.3.0", "tag": "v1.3.0",
                "branch": "release/v1.3.0", "base": "main", "gate": [], "checks": [], "ready": False}
        decl = MODULE.read_declarations(self.dir, "maelys-fixture", "custom")
        return MODULE.cut_open(self.Stub(), decl, data, self.log, 1, 1, self.product.signers)

    def test_a_release_that_moves_one_and_does_not_name_it_is_not_cut(self) -> None:
        data, code = self.plan("- Something else changed.")
        self.assertEqual((code, data["ready"]), (MODULE.EXIT_VIOLATIONS, False))
        failed = [line["message"] for line in data["gate"] if line["status"] == "fail"]
        self.assertEqual(len(failed), 1, data["gate"])
        self.assertIn("dependencies/lib.pin moved since v1.2.2 and the entry of this version does not name lib",
                      failed[0])
        self.assertNotIn("next", data)

    def test_naming_it_in_the_entry_opens_the_gate(self) -> None:
        data, code = self.plan("- Re-pins lib further along its main branch.")
        self.assertEqual((code, data["ready"]), (MODULE.EXIT_OK, True), data["gate"])
        self.assertIn("dependencies/lib.pin moved since v1.2.2, and the entry names lib",
                      [line["message"] for line in data["gate"]])


class LineOneSaysWhatTheCodeAcceptsTest(unittest.TestCase):
    """The texts a product and its agents read, held to what `dependencies` refuses.

    They said "the nearest tag on line 1, for humans". The nearest tag of a
    commit that carries none is another commit, and that pin is refused --
    `test_a_third_party_pin_without_the_line_is_told_of_it` holds the refusal.
    """

    TEXTS = ("share/templates/checkout-dependency.sh", "share/agents/claude-skill.md",
             "share/agents/instructions-block.md", "share/agents/conventions-block.md", "README.md")

    def test_no_text_installed_in_a_product_promises_the_nearest_tag(self) -> None:
        for relative in self.TEXTS:
            with self.subTest(text=relative):
                text = " ".join((ROOT / relative).read_text(encoding="utf-8").replace("#", " ").split())
                for promise in ("holds the nearest tag", "nearest tag on line 1", "the nearest tag on line 1"):
                    self.assertNotIn(promise, text)

    def test_the_script_and_the_skill_name_the_line(self) -> None:
        for relative in self.TEXTS[:2]:
            with self.subTest(text=relative):
                text = " ".join((ROOT / relative).read_text(encoding="utf-8").replace("#", " ").split())
                self.assertIn("unreleased <branch>", text)

    def test_the_conventions_give_line_1_in_each_case_the_code_accepts(self) -> None:
        conventions = (ROOT / "docs" / "conventions.md").read_text(encoding="utf-8")
        for shape in ("| the tag | it names the commit of line 2 |",
                      "what `git describe --tags` says of the commit",
                      "| the last tag before the commit | it is a tag, an ancestor of the commit, and not the"
                      " commit itself |"):
            self.assertIn(shape, conventions)


if __name__ == "__main__":
    unittest.main()
