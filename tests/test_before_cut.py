# SPDX-License-Identifier: MPL-2.0
"""`check` says beforehand what `cut` will refuse, and only what it can know.

Twice in two days a new product was handed over with the entry of its
release left undated -- once with VERSION already moved to the version to
cut. `check` answered "ok" to whoever prepared it, and `cut` refused at the
other end, in front of whoever signs. The refusals are right; they arrived
at the wrong moment.

Each rule here is measured at both ends: on a tree that has the defect, and
on the correct states that look like it. A note that fires on a correct
product costs more than the one it was written for -- a rule exact on the
product that motivated it once gave four false notes on another, and the
first version of this one was read by six products of the fleet at every
release -- so every silence below sits beside the same tree, one fact away,
where the note is read.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import unittest
from unittest import mock

from test_maelys_release import MODULE, ROOT, FakeHost, Product, using_host

DATED = "# Changelog\n\n## 1.3.0 — 2026-09-04\n\n- Next.\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n"
UNDATED = DATED.replace("## 1.3.0 — 2026-09-04", "## 1.3.0 — (undated)")


class Stub:
    """An invocation of the framework, reduced to what cut reads of it."""

    format = "json"

    def flag(self, name: str) -> bool:
        return False

    def option(self, name: str, default=None):
        return default


class BeforeCutTest(unittest.TestCase):
    """A product between two releases: VERSION 1.2.3, published by v1.2.3."""

    ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = self.product.dir
        self.product.run("adopt", str(self.dir), "--apply")
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.commit("fixture")
        self.git("tag", "v1.2.3")
        # An origin, as every product has one: cut reads it, and a clone
        # is what a job or another maintainer holds.
        self.origin = self.product.work / "origin.git"
        self.product.git(self.product.work, "init", "-q", "--bare", str(self.origin))
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "origin", "main", "v1.2.3")
        # cut is called in process below: this machine's git configuration
        # must not decide what it answers.
        saved = {name: os.environ.get(name) for name in self.ISOLATE}
        os.environ.update(self.ISOLATE)
        self.addCleanup(lambda: [os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
                                 for name, value in saved.items()])

    def git(self, *arguments: str) -> str:
        return self.product.git(self.dir, *arguments)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def notes(self, directory: pathlib.Path | None = None, *options: str) -> list[str]:
        """What `check` says about the cut to come; never a violation, never an exit code."""
        data = self.product.json("check", str(directory or self.dir), "--product", "maelys-fixture", *options)["data"]
        self.assertTrue(data["valid"], data["violations"])
        said = [check for check in data["checks"] if "`cut` will refuse" in check["message"]]
        self.assertEqual({check["status"] for check in said} - {"note"}, set(), said)
        return [check["message"] for check in said]

    def declarations(self, mechanism: str = "custom"):
        return MODULE.read_declarations(self.dir, "maelys-fixture", mechanism)

    def first_stop(self, version: str):
        """What the first stop of `cut` answers without --apply, in process: its refusal, or its gate."""
        data = {"mode": "plan", "stage": "open", "product": "maelys-fixture", "project": str(self.dir),
                "repository": "maelys-dev/maelys-fixture", "version": version, "tag": f"v{version}",
                "branch": f"release/v{version}", "base": "main", "gate": [], "checks": [], "ready": False}
        with open(self.product.work / "cut.log", "w", encoding="utf-8") as log:
            try:
                return MODULE.cut_open(Stub(), self.declarations(), data, log, 1, 1, self.product.signers)
            except MODULE.Failure as failure:
                return failure

    def refusal(self, version: str) -> MODULE.Failure:
        answer = self.first_stop(version)
        self.assertIsInstance(answer, MODULE.Failure, answer)
        return answer

    def bump(self, subject: str = "a bump in passing") -> None:
        """VERSION at 1.3.0 with its entry dated, committed: the state a release leaves, and a hand."""
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", DATED)
        self.commit(subject)

    # ---- the correct states --------------------------------------------------

    def test_a_product_between_two_releases_is_told_nothing(self) -> None:
        self.assertEqual(self.notes(), [])
        # The state a release is cut from: its entry dated, VERSION untouched.
        self.product.write("CHANGELOG.md", DATED)
        self.assertEqual(self.notes(), [])
        # Nor on a detached checkout, which holds no branch at all.
        self.git("switch", "-q", "--detach", "HEAD")
        self.assertEqual(self.notes(), [])

    # ---- the entry `cut` will not read ---------------------------------------

    def test_an_undated_entry_is_noted_in_the_words_cut_will_use(self) -> None:
        self.product.write("CHANGELOG.md", UNDATED)
        said = self.notes()
        self.assertEqual(len(said), 1, said)
        self.assertIn("'## 1.3.0 — (undated)', without a date", said[0])
        # The same sentence, from the command that will say it: a note that
        # paraphrased the refusal could drift from it unseen.
        refused = self.refusal("1.3.0")
        self.assertEqual(refused.message, "CHANGELOG.md has no dated entry '## 1.3.0 — YYYY-MM-DD'.")
        self.assertIn(refused.message.rstrip("."), said[0])
        # And the two agree on the remedy: dated, neither says it any more,
        # and cut goes on to its gate.
        self.product.write("CHANGELOG.md", DATED)
        self.assertEqual(self.notes(), [])
        passed = self.first_stop("1.3.0")
        self.assertNotIsInstance(passed, MODULE.Failure, passed)
        self.assertEqual(passed[0]["changelog"], "2026-09-04")

    def test_the_note_gives_one_rule_for_the_date_and_it_is_not_cuts_other_refusal(self) -> None:
        """Three texts gave three rules: never undated for whoever signs, dated the day
        it is cut, undated until then. A date written for the day to come is refused too."""
        self.product.write("CHANGELOG.md", UNDATED)
        said = self.notes()[0]
        self.assertIn("handed over with its entry dated that day, never a later one, which `cut` refuses too", said)
        self.assertIn("until then its changes sit under '## Unreleased'", said)
        # The later day, held to what cut does with it.
        self.product.write("CHANGELOG.md", DATED.replace("2026-09-04", "2999-01-01"))
        self.assertIn("in the future", self.refusal("1.3.0").message)
        # And `## Unreleased` is the state the rule names: no note, whatever it holds.
        self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", "## Unreleased"))
        self.assertEqual(self.notes(), [])

    def test_every_spelling_of_no_date_is_one_and_a_dated_one_in_another_spelling_is_not(self) -> None:
        for heading in ("## 1.3.0", "## 1.3.0 - unreleased", "## 1.3.0 — 2026-9-4"):
            self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", heading))
            self.assertEqual(len(self.notes()), 1, heading)
        # maelys-cli writes a hyphen; what makes an entry dated is the date.
        for heading in ("## 1.3.0 - 2026-09-04", "## 1.3.0 (2026-09-04)"):
            self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", heading))
            self.assertEqual(self.notes(), [], heading)

    def test_a_title_cut_does_not_read_is_noted_dated_or_not(self) -> None:
        """Three products of the fleet title their entries with brackets: `check` said ok,
        and `cut` answers "no dated entry" to a line that carries a date."""
        for heading in ("## [1.3.0] - 2026-09-04", "## v1.3.0 — 2026-09-04", "### 1.3.0 — 2026-09-04",
                        "##  1.3.0 — 2026-09-04", "## [v1.3.0]", "## [1.3.0]: 2026-09-04"):
            self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", heading))
            said = self.notes()
            self.assertEqual(len(said), 1, heading)
            self.assertIn(f"'{heading}', which `cut` does not read as the entry of 1.3.0", said[0])
            refused = self.refusal("1.3.0")
            self.assertEqual(refused.message, "CHANGELOG.md has no dated entry '## 1.3.0 — YYYY-MM-DD'.")
            self.assertIn(refused.message.rstrip("."), said[0])
        # What only starts like one is prose: a section about a version, a list of them.
        for heading in ("### 1.3.0 compatibility", "### Changed since 1.3.0", "## [Unreleased]", "#### 1.3.0",
                        "## 1.3.0.1 — 2026-09-04"):
            self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", "## Unreleased\n\n" + heading))
            self.assertEqual(self.notes(), [], heading)
        # And once the entry `cut` reads is there, the other spelling beside it is not a refusal to come.
        self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04",
                                                         "## 1.3.0 — 2026-09-04\n\n### 1.3.0 — notes"))
        self.assertEqual(self.notes(), [])

    def test_an_example_in_a_fenced_block_is_not_an_entry(self) -> None:
        example = "## 9.9.9 — YYYY-MM-DD"
        for fence in ("```", "~~~", "````markdown"):
            closing = fence.rstrip("markdown")
            self.product.write("CHANGELOG.md", DATED.replace(
                "- Next.", f"- Next. An entry is titled:\n\n{fence}\n{example}\n{closing}\n"))
            self.assertEqual(self.notes(), [], fence)
        # The same line outside a block is one, and so is what follows a block that closed.
        self.product.write("CHANGELOG.md", DATED.replace("- Next.", f"- Next.\n\n{example}\n"))
        self.assertEqual(len(self.notes()), 1)
        self.product.write("CHANGELOG.md", DATED.replace("- Next.", f"- Next.\n\n```\ncode\n```\n\n{example}\n"))
        self.assertEqual(len(self.notes()), 1)

    def test_only_the_releases_to_come_are_read(self) -> None:
        # An old entry nobody dated is history: cut will never be asked for it.
        self.product.write("CHANGELOG.md", DATED + "\n## 1.2.2\n\n- Before the dates.\n\n## [1.2.1]\n\n## Unreleased\n")
        self.assertEqual(self.notes(), [])
        # 1.2.30 is not 1.2.3, and an entry further ahead is one more to date.
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 2.0.0\n\n- Far.\n\n## 1.2.30 — soon\n\n- Near.\n\n"
                           + DATED.split("\n\n", 1)[1])
        said = self.notes()
        self.assertEqual(len(said), 2, said)
        self.assertIn("refuse 2.0.0", said[0])
        self.assertIn("refuse 1.2.30", said[1])

    # ---- VERSION moved by hand -----------------------------------------------

    def test_a_version_no_tag_published_is_noted_in_the_words_cut_will_use(self) -> None:
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", DATED)
        said = self.notes()
        self.assertEqual(len(said), 1, said)
        refused = self.refusal("1.3.0")
        self.assertEqual(refused.message, "1.3.0 does not come after the current version 1.3.0.")
        self.assertIn("No tag v1.3.0 exists: VERSION says what was published last.", refused.hint)
        # Its own words, apart from the grammar of a sentence that reports them.
        for noted, refusing in (("no tag v1.3.0", "No tag v1.3.0"),
                                ("VERSION says what was published last", "VERSION says what was published last"),
                                ("not coming after the current version", "does not come after the current version"),
                                ("commit, and cut 1.3.0 with its dated entry", "commit, and cut 1.3.0 with its dated entry")):
            self.assertIn(noted, said[0], noted)
            self.assertIn(refusing, f"{refused.message} {refused.hint}", refusing)
        # Published, it is the state between two releases again.
        self.git("tag", "v1.3.0")
        self.assertEqual(self.notes(), [])

    def test_the_note_says_what_is_observed_before_what_to_do(self) -> None:
        """Its first version opened on "set VERSION to the version last published": the
        wrong advice for a release waiting for its tag, which is the ordinary reader."""
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", DATED)
        said = self.notes()[0]
        self.assertTrue(said.startswith("VERSION says 1.3.0 and this clone holds no tag v1.3.0"
                                        " (the last tag this history reaches is v1.2.3). Two states read that way."),
                        said)
        waiting, moved = said.index("A release already merged and waiting for its tag: change nothing"), \
            said.index("Or a VERSION moved before `cut`")
        self.assertLess(waiting, moved)
        self.assertLess(moved, said.index("set VERSION back to 1.2.3"))
        self.assertIn("'maelys-release cut DIR 1.3.0 --tag' signs it when `cut` opened it, a tag signed by hand"
                      " otherwise", said)
        # A tag pushed after the last fetch is the third state, and the clone cannot know.
        self.assertIn("'git pull origin main' fetches none: 'git fetch --tags origin' first", said)

    def test_a_repository_with_its_own_mechanism_is_told_the_state_and_no_revert(self) -> None:
        """Its seeded RELEASING.md says: VERSION on main, merge, run `check`, sign the tag."""
        ritual = (ROOT / "share" / "templates" / "RELEASING-conventions.md").read_text(encoding="utf-8")
        self.assertIn("`VERSION` = `X.Y.Z`", ritual)
        self.assertLess(ritual.index("`maelys-release check .`"), ritual.index("git tag -s vX.Y.Z"))
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", DATED)
        (own,) = MODULE.cut.before_cut(self.declarations("custom"))
        self.assertTrue(own.startswith("VERSION says 1.3.0 and this clone holds no tag v1.3.0"), own)
        self.assertIn("A release prepared by hand stands there between its merge and its tag, as RELEASING.md"
                      " describes: nothing to change, the signed tag is what is left", own)
        self.assertNotIn("set VERSION back", own)
        self.assertNotIn("Two states", own)
        # What `cut` would do is still said, as a fact about `cut`.
        self.assertIn("`cut` will refuse 1.3.0 as not coming after the current version", own)
        self.assertIn("a release made with `cut` leaves VERSION at 1.2.3", own)
        # The product the socle releases is the one told what to undo.
        (released,) = MODULE.cut.before_cut(self.declarations("maelys-release"))
        self.assertIn("set VERSION back to 1.2.3", released)

    def test_a_first_release_bumped_by_hand_goes_back_to_zero(self) -> None:
        """The incident itself: a new product, no tag yet, VERSION at the version to cut."""
        self.git("tag", "-d", "v1.2.3")
        said = self.notes()
        self.assertEqual(len(said), 1, said)
        self.assertIn("VERSION says 1.2.3 and this clone holds no tag v1.2.3 (this history reaches no tag)", said[0])
        self.assertIn("set VERSION back to 0.0.0", said[0])

    def test_the_version_to_go_back_to_is_the_one_this_history_reaches(self) -> None:
        """On a maintenance branch at 1.2.4, the highest tag of the repository is a 1.3.0
        that 1.2.4 does not come after: following the first note made `cut 1.2.4` refuse."""
        self.git("switch", "-q", "-c", "maintenance/1.2")
        self.git("switch", "-q", "main")
        self.bump("maelys-fixture 1.3.0")
        self.git("tag", "v1.3.0")
        self.git("switch", "-q", "maintenance/1.2")
        self.product.write("VERSION", "1.2.4\n")
        self.product.write("CHANGELOG.md", DATED.replace("## 1.3.0 — 2026-09-04", "## 1.2.4 — 2026-09-05"))
        said = self.notes()
        self.assertEqual(len(said), 1, said)
        self.assertIn("(the last tag this history reaches is v1.2.3)", said[0])
        self.assertIn("set VERSION back to 1.2.3", said[0])
        self.assertNotIn("1.3.0", said[0])
        # Which is the version `cut 1.2.4` accepts to start from: it gets past
        # the order of versions and the entry, to the refusal it owes a branch.
        self.product.write("VERSION", "1.2.3\n")
        self.assertEqual(self.notes(), [])
        self.assertIn("HEAD is on maintenance/1.2", self.refusal("1.2.4").message)
        self.product.write("VERSION", "1.3.0\n")
        self.assertIn("does not come after", self.refusal("1.2.4").message)
        # A candidate is a tag this history reaches and not a version to write.
        self.git("tag", "-d", "v1.2.3")
        self.git("tag", "v1.2.3-rc.2")
        self.product.write("VERSION", "1.2.4\n")
        (said,) = self.notes()
        self.assertIn("(the last tag this history reaches is v1.2.3-rc.2)", said)
        self.assertIn("set VERSION back to the version last published", said)

    def test_zero_before_a_first_release_is_the_normal_state(self) -> None:
        self.git("tag", "-d", "v1.2.3")
        self.assertEqual(len(self.notes()), 1, "the same tree, one fact away, is noted")
        self.product.write("VERSION", "0.0.0\n")
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0 — 2026-09-03\n\n- First.\n")
        self.assertEqual(self.notes(), [])
        # And the first release left undated is the other half of the incident.
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0 — (undated)\n\n- First.\n")
        said = self.notes()
        self.assertEqual(len(said), 1, said)
        self.assertIn("`cut` will refuse 0.1.0", said[0])

    # ---- not said in a job ---------------------------------------------------

    def test_a_job_is_told_nothing_of_the_version_and_no_call_is_made_for_it(self) -> None:
        """The shared CI checks out the whole history and runs `check` on the release
        commit itself, where VERSION is by construction a version no tag carries yet."""
        self.bump()
        self.assertEqual(len(self.notes()), 1)
        for variable in ("CI", "GITHUB_ACTIONS"):
            with mock.patch.dict(self.product.env, {variable: "true"}):
                self.assertEqual(self.notes(), [], variable)
            # In process, on a host that refuses every command: nothing is asked of git.
            decl = self.declarations("maelys-release")
            with mock.patch.dict(os.environ, {variable: "true"}), using_host(FakeHost()) as host:
                self.assertEqual(MODULE.cut.unpublished_version(decl), [])
                self.assertEqual(host.commands, [])
            # An empty value is a variable nobody set; and one definition of
            # a job, shared with the address note and the privacy guard:
            # `false` and `0` are an author's machine.
            for value in ("", "false", "0"):
                with mock.patch.dict(self.product.env, {variable: value}):
                    self.assertEqual(len(self.notes()), 1, (variable, value))
        # The entry is a reading of one file, and stays said: it costs no
        # call, and its author reads the log of their own pull request.
        self.product.write("VERSION", "1.2.3\n")
        self.product.write("CHANGELOG.md", UNDATED)
        with mock.patch.dict(self.product.env, {"CI": "true", "GITHUB_ACTIONS": "true"}):
            self.assertEqual(len(self.notes()), 1)

    # ---- between the two stops -----------------------------------------------

    def test_a_release_branch_in_either_spelling_is_a_release_in_progress(self) -> None:
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", DATED)
        self.git("switch", "-q", "-c", "fix/a-bump-in-passing")
        self.commit("a bump in passing")
        self.assertEqual(len(self.notes()), 1, "on any other branch it is a version moved by hand")
        # One product of the fleet names its release branches without the v.
        for branch in ("release/v1.3.0", "release/1.3.0"):
            self.git("switch", "-q", "-c", branch)
            self.assertEqual(self.notes(), [], branch)
            # Its pull request on a checkout that holds no branch: known on the origin alone.
            self.git("push", "-q", "origin", branch)
            self.git("switch", "-q", "--detach", "HEAD")
            self.git("branch", "-q", "-D", branch)
            self.assertTrue(self.git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"))
            self.assertEqual(self.notes(), [], branch)
            # Which is what that silence rests on.
            self.git("update-ref", "-d", f"refs/remotes/origin/{branch}")
            self.assertEqual(len(self.notes()), 1, branch)
        # The branch of another version, in either spelling, covers nothing.
        for branch in ("release/v1.3.01", "release/1.2.3", "release/v1.3", "releases/v1.3.0"):
            self.git("branch", branch)
        self.assertEqual(len(self.notes()), 1)

    def test_the_subject_cut_writes_is_a_release_whatever_the_merge(self) -> None:
        """Measured on eight products at their last release commit, the tag deleted: six carry
        this subject -- merged, squashed, or pushed as it is."""
        for subject in ("maelys-fixture 1.3.0", "maelys-fixture 1.3.0 (#7)"):
            self.git("switch", "-q", "-C", "trial", "main")
            self.bump(subject)
            self.assertEqual(self.notes(), [], subject)
            # Later commits on the default branch do not bring the note back.
            self.git("commit", "-q", "--allow-empty", "-m", "docs: something else")
            self.assertEqual(self.notes(), [], subject)
        # Another product, another version, a longer subject, the subject quoted in a body.
        for subject in ("maelys-other 1.3.0", "maelys-fixture 1.3.01", "maelys-fixture 1.3.0 and a fix",
                        "xmaelys-fixture 1.3.0", "maelys-fixture 1x3y0", "maelys-fixture 1.3.0 (#7) again",
                        "release: 1.3.0\n\nmaelys-fixture 1.3.0", "0.13.0: a feature\n\nmaelys-fixture 1.3.0 (#7)"):
            self.git("switch", "-q", "-C", "trial", "main")
            self.bump(subject)
            self.assertEqual(len(self.notes()), 1, subject)

    def test_the_merge_github_writes_of_a_release_branch_is_a_release(self) -> None:
        """Five of the eight, one of them a product whose bump commit is titled by hand."""
        for branch in ("release/v1.3.0", "release/1.3.0"):
            self.git("switch", "-q", "-C", "trial", "main")
            self.bump("release: 1.3.0, and a pin")
            self.assertEqual(len(self.notes()), 1)
            self.git("commit", "-q", "--allow-empty", "-m",
                     f"Merge pull request #7 from maelys-dev/{branch}\n\nrelease: 1.3.0, and a pin")
            self.assertEqual(self.git("for-each-ref", "refs/heads/release", "refs/remotes/origin/release"), "")
            self.assertEqual(self.notes(), [], branch)
        # It is read in the subject, where GitHub writes it: a body that
        # mentions the branch is a commit about something else.
        for message in ("Merge pull request #6 from maelys-dev/release/v1.3.01",
                        "Merge pull request #5 from maelys-dev/release/v1x3y0",
                        "Merge pull request #4 from maelys-dev/feat/process\n\nprepares release/v1.3.0",
                        "feat: a feature (#3)\n\nMerge pull request #7 from maelys-dev/release/v1.3.0",
                        "Merge pull request #7 from maelys-dev/release/v1.3.0 and more"):
            self.git("switch", "-q", "-C", "trial", "main")
            self.bump("release: 1.3.0, and a pin")
            self.git("commit", "-q", "--allow-empty", "-m", message)
            self.assertEqual(len(self.notes()), 1, message)

    def test_a_bump_carried_by_another_pull_request_reads_both_states(self) -> None:
        """The eighth product: VERSION moved in the pull request of a feature, the tag
        eleven commits later. Nothing separates it from a version moved before `cut`,
        because it is one, so the note is read -- and opens on the state that asks nothing."""
        self.bump("1.3.0: a feature, started with exactly what it names")
        self.git("commit", "-q", "--allow-empty", "-m", "Merge pull request #45 from maelys-dev/feat/process")
        (said,) = self.notes()
        self.assertLess(said.index("change nothing"), said.index("set VERSION back"))

    # ---- what a checkout cannot know -----------------------------------------

    def clone(self, name: str, *options: str) -> pathlib.Path:
        target = self.product.work / name
        self.product.git(self.product.work, "clone", "-q", *options, f"file://{self.origin}", str(target))
        return target

    def test_a_checkout_that_cannot_know_its_tags_is_told_nothing(self) -> None:
        # A second commit, so that one commit of history is a shallow clone.
        self.product.write("README.md", "# maelys-fixture\n")
        self.commit("a second commit")
        self.git("push", "-q", "origin", "main")
        # Published on the origin, and the tag is simply not here.
        without = self.clone("without-tags", "--no-tags")
        self.assertEqual(self.product.git(without, "tag", "--list"), "")
        self.assertEqual(self.product.git(without, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(self.notes(without), [])
        # One commit of history: the tag is on a commit that never came,
        # and nothing in the clone says tags were left out.
        shallow = self.clone("shallow", "--depth", "1")
        self.assertEqual(self.product.git(shallow, "config", "--get", "--default", "", "remote.origin.tagOpt"), "")
        self.assertEqual(self.product.git(shallow, "tag", "--list"), "")
        self.assertEqual(self.product.git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(self.notes(shallow), [])
        # What makes those two silences worth anything: the same commit in a
        # clone that does hold its tags, of an origin that never had this one.
        self.product.git(self.origin, "tag", "-d", "v1.2.3")
        full = self.clone("full")
        self.assertEqual(self.product.git(full, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(len(self.notes(full)), 1)
        # And each silence is its own: the shallow clone, once whole and still
        # without the tag, reads the note; the other loses its setting.
        self.product.git(shallow, "fetch", "-q", "--unshallow")
        self.assertEqual(self.product.git(shallow, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(len(self.notes(shallow)), 1)
        self.product.git(without, "config", "--unset", "remote.origin.tagOpt")
        self.assertEqual(len(self.notes(without)), 1)

    def test_a_directory_that_is_not_a_repository_is_told_nothing(self) -> None:
        """An unpacked archive, wherever it sits: the tags git would answer with there are not its own."""
        self.git("tag", "-d", "v1.2.3")
        self.assertEqual(len(self.notes()), 1)
        # A worktree is a repository, and knows the tags of the one it comes from.
        worktree = self.product.work / "worktree" / "maelys-fixture"
        worktree.parent.mkdir()
        self.git("worktree", "add", "-q", "--detach", str(worktree))
        self.assertEqual(len(self.notes(worktree)), 1)
        # The same files with no repository of their own, inside another one:
        # git answers there, with the tags of the repository around them.
        inside = self.dir / "vendor" / "maelys-fixture"
        shutil.copytree(worktree, inside, ignore=shutil.ignore_patterns(".git"))
        self.assertEqual(self.git("-C", str(inside), "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(self.notes(inside), [])
        # And outside any repository, where git answers nothing at all.
        (worktree / ".git").unlink()
        self.assertEqual(self.notes(worktree), [])

    def test_a_version_tagged_as_candidates_follows_a_ceremony_of_its_own(self) -> None:
        """Measured on the fleet: one repository holds the version it prepares and tags
        candidates of it, under a mechanism of its own. It would have read the note."""
        self.git("tag", "-d", "v1.2.3")
        self.assertEqual(len(self.notes()), 1)
        self.git("tag", "v1.2.3-rc.1")
        self.assertEqual(self.notes(), [])
        # A candidate of another version says nothing about this one.
        self.git("tag", "-d", "v1.2.3-rc.1")
        self.git("tag", "v1.2.30-rc.1")
        self.git("tag", "v1.2.2-rc.1")
        self.assertEqual(len(self.notes()), 1)

    def test_a_repository_that_publishes_by_nothing_cuts_nothing(self) -> None:
        self.git("tag", "-d", "v1.2.3")
        self.product.write("CHANGELOG.md", UNDATED.replace("1.3.0", "1.4.0") + "\n")
        for mechanism, expected in (("maelys-release", 2), ("custom", 2), ("none", 0)):
            self.assertEqual(len(MODULE.cut.before_cut(self.declarations(mechanism))), expected, mechanism)

    def test_preflight_reads_version_as_the_tag_to_come_and_does_not_carry_the_note(self) -> None:
        self.git("tag", "-d", "v1.2.3")
        self.assertEqual(len(self.notes()), 1)
        data = self.product.json("preflight", str(self.dir), expect=2)["data"]
        self.assertIn("tag v1.2.3 is free", [item["message"] for item in data["preflight"]])
        self.assertEqual([check for check in data["checks"] if "`cut` will refuse" in check["message"]], [])

    # ---- the plan the texts send an agent to ---------------------------------

    def state(self) -> dict:
        """Every file of the product, every ref here and on the origin, and what git calls changed."""
        files = {path.relative_to(self.dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 + ("x" if os.access(path, os.X_OK) else "")
                 for path in sorted(self.dir.rglob("*")) if path.is_file() and ".git" not in path.relative_to(self.dir).parts}
        return {"files": files,
                "refs": self.git("for-each-ref", "--format=%(refname) %(objectname)"),
                "head": self.git("symbolic-ref", "HEAD"),
                "status": self.git("status", "--porcelain", "-uall"),
                "origin": self.product.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")}

    def test_the_plan_changes_no_file_of_the_product_and_nothing_on_the_origin(self) -> None:
        """What the managed texts promise of `cut` without --apply, held to a run of it: with
        a verify command and an after-version declared, which would both leave a file behind."""
        self.product.write("scripts/verify-release.sh", "#!/bin/sh\ntouch verified-by-the-plan\n", executable=True)
        self.product.write("maelys-release.conf", self.product.read("maelys-release.conf")
                           + "\n[cut]\nafter-version touch regenerated-by-the-plan\n")
        self.commit("the gate this product declares")
        self.git("push", "-q", "origin", "main")
        self.product.write("CHANGELOG.md", DATED)
        # A signer's configuration, so that the plan goes to its end rather
        # than stopping at the gate, and no earlier tag for it to judge.
        self.git("tag", "-d", "v1.2.3")
        self.git("config", "tag.gpgsign", "true")
        self.git("config", "gpg.format", "ssh")
        self.git("config", "user.signingkey", str(self.product.work / "signing-key"))
        decl = self.declarations()
        self.assertTrue(decl.verify_command and decl.after_version, "the fixture declares what --apply would run")
        before = self.state()
        answer = self.first_stop("1.3.0")
        # It went all the way: past every refusal, through the fetch and the gate.
        self.assertNotIsInstance(answer, MODULE.Failure, answer)
        data, code = answer
        self.assertEqual(code, MODULE.EXIT_OK, data["gate"])
        self.assertEqual(data["next"], f"maelys-release cut {self.dir} 1.3.0 --apply")
        self.assertTrue([item for item in data["gate"] if "--apply would run" in item["message"]], data["gate"])
        self.assertEqual(self.state(), before)
        # And a refusal on the way leaves as little: the version, then the entry.
        self.product.write("CHANGELOG.md", UNDATED)
        before = self.state()
        self.refusal("1.3.0")
        self.refusal("1.2.3")
        self.assertEqual(self.state(), before)

    def test_without_the_signers_key_the_plan_exits_2_on_the_signers_lines_alone(self) -> None:
        """Whoever prepares a release rarely holds the key that signs it: told this is their
        check, they read `cut: not ready` and may go and "fix" a signing configuration."""
        self.product.write("CHANGELOG.md", DATED)
        self.git("tag", "-d", "v1.2.3")     # a fixture's tag is not a signed one, and that line is the repository's
        data, code = self.first_stop("1.3.0")
        self.assertEqual(code, MODULE.EXIT_VIOLATIONS)
        refused = [item["message"] for item in data["gate"] if item["status"] == "fail"]
        self.assertTrue(refused)
        for message in refused:
            self.assertTrue(any(word in message for word in ("tag.gpgsign", "user.signingkey", "signing key")), message)
        self.assertIn("cut: not ready", MODULE.text_cut(dict(data, socle=None)))


class ScaffoldEntryTest(unittest.TestCase):
    """0.0.0 says nothing was published: no entry is owed to it, and one has a single place."""

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = self.product.dir
        self.product.write("VERSION", "0.0.0\n")

    def check(self, expect: int = 0) -> dict:
        return self.product.json("check", str(self.dir), expect=expect)["data"]

    def above(self, expect: int = 0) -> list[dict]:
        return [check for check in self.check(expect)["checks"] if "above ##" in check["message"]]

    def test_an_adopted_repository_owes_no_entry_to_zero(self) -> None:
        """It was asked for one, wrote it by hand above its first release, and has read upside down since."""
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0 — 2026-09-03\n\n- First.\n")
        # adopt refused here, and check exited 2, on `needs a dated entry: ## 0.0.0`.
        self.product.run("adopt", str(self.dir), "--apply")
        data = self.check()
        self.assertTrue(data["valid"], data["violations"])
        said = [check["message"] for check in data["checks"]]
        self.assertIn("VERSION 0.0.0 says nothing is published yet: CHANGELOG.md owes it no entry", said)
        self.assertEqual([line for line in said if "needs a dated entry" in line or "above ##" in line], [])

    def test_every_other_version_still_owes_its_entry(self) -> None:
        self.product.write("VERSION", "0.1.0\n")
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.0.0 — 2026-09-03\n\n- Created.\n")
        self.assertIn("CHANGELOG.md needs a dated entry: ## 0.1.0 — YYYY-MM-DD", self.check(expect=2)["violations"])

    def test_the_entry_new_writes_is_where_it_belongs(self) -> None:
        target = self.product.work / "maelys-widget"
        self.product.run("new", str(target), "--product", "maelys-widget", *Product.SOCLE, "--apply")
        self.assertRegex((target / "CHANGELOG.md").read_text(encoding="utf-8"), r"## 0\.0\.0 — \d{4}-\d{2}-\d{2}")
        data = self.product.json("check", str(target), "--product", "maelys-widget")["data"]
        said = [check["message"] for check in data["checks"]]
        self.assertIn("CHANGELOG.md has a dated ## 0.0.0 entry", said)
        self.assertEqual([line for line in said if "above ##" in line], [])
        # A first release written above it, as its own text asks: still nothing.
        changelog = (target / "CHANGELOG.md").read_text(encoding="utf-8")
        (target / "CHANGELOG.md").write_text(changelog.replace("## 0.0.0", "## 0.1.0 — 2026-09-03\n\n- First.\n\n## 0.0.0"),
                                             encoding="utf-8")
        data = self.product.json("check", str(target), "--product", "maelys-widget")["data"]
        self.assertEqual([check for check in data["checks"] if "above ##" in check["message"]], [])

    def test_one_written_above_a_release_is_noted_and_never_refused(self) -> None:
        upside_down = "# Changelog\n\n## 0.0.0 — 2026-09-02\n\n- Adopted.\n\n## 0.1.0 — 2026-09-03\n\n- First.\n"
        for version, changelog in (("0.0.0", upside_down), ("0.1.0", upside_down),
                                   ("0.2.0", "# Changelog\n\n## 0.2.0 — 2026-09-05\n\n- Second.\n\n"
                                             + upside_down.split("\n\n", 1)[1])):
            self.product.write("VERSION", version + "\n")
            self.product.write("CHANGELOG.md", changelog)
            self.product.run("adopt", str(self.dir), "--apply")
            data = self.check()
            self.assertTrue(data["valid"], data["violations"])
            said = [check for check in data["checks"] if "above ##" in check["message"]]
            self.assertEqual([check["status"] for check in said], ["note"], version)
            self.assertIn("CHANGELOG.md carries ## 0.0.0 above ## 0.1.0", said[0]["message"])
        # Moved to the end, or deleted: the two remedies the note names.
        for remedy in ("# Changelog\n\n## 0.1.0 — 2026-09-03\n\n- First.\n\n## 0.0.0 — 2026-09-02\n\n- Adopted.\n",
                       "# Changelog\n\n## 0.1.0 — 2026-09-03\n\n- First.\n"):
            self.product.write("VERSION", "0.1.0\n")
            self.product.write("CHANGELOG.md", remedy)
            self.assertEqual(self.above(), [])

    def test_an_order_of_versions_is_not_judged(self) -> None:
        """A maintenance release is written after a later minor, and 0.10.0 is not 0.0.0."""
        self.product.write("VERSION", "1.3.0\n")
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 1.2.4 — 2026-09-05\n\n- Backport.\n\n"
                                           "## 1.3.0 — 2026-09-04\n\n- Minor.\n\n## 10.0.0.1 — draft\n\n"
                                           "## 0.0.01 — 2026-01-01\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n")
        self.product.run("adopt", str(self.dir), "--apply")
        self.assertEqual(self.above(), [])

    def test_only_the_entry_titled_zero_is_held_and_only_above_another_version(self) -> None:
        self.product.write("VERSION", "0.1.0\n")
        first = "## 0.1.0 — 2026-09-03\n\n- First.\n"
        self.product.write("CHANGELOG.md", "# Changelog\n\n" + first)
        self.product.run("adopt", str(self.dir), "--apply")
        # A title of somebody's own: `0.0.0-draft` is not the entry that says nothing was published.
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.0.0-draft — 2026-09-02\n\n- Notes.\n\n" + first)
        self.assertEqual(self.above(), [])
        # What follows it must be a version too: `0.1.0.5` is four numbers, not a release.
        self.product.write("CHANGELOG.md", "# Changelog\n\n## 0.0.0 — 2026-09-02\n\n- Adopted.\n\n"
                                           "## 0.1.0.5 — notes\n\n" + first.replace("## 0.1.0", "## Unreleased"))
        self.product.write("VERSION", "0.0.0\n")
        self.assertEqual(self.above(), [])
        self.product.write("VERSION", "0.1.0\n")
        # An example of it in a fenced block is not an entry at all.
        self.product.write("CHANGELOG.md", "# Changelog\n\n```\n## 0.0.0 — YYYY-MM-DD\n```\n\n" + first)
        self.assertEqual(self.above(), [])
        # Written twice, it is the duplicate `check` already refuses: the
        # note does not add that a 0.0.0 stands above a 0.0.0.
        twice = "# Changelog\n\n## 0.0.0 — 2026-09-02\n\n- Adopted.\n\n## 0.0.0 — 2026-09-01\n\n- Created.\n"
        self.product.write("CHANGELOG.md", twice)
        self.product.write("VERSION", "0.0.0\n")
        data = self.check(expect=2)
        self.assertTrue([line for line in data["violations"] if "2 entries titled ## 0.0.0" in line], data["violations"])
        self.assertEqual([check for check in data["checks"] if "above ##" in check["message"]], [])
        # And with a release below the two, it is that release it stands above.
        self.product.write("CHANGELOG.md", twice + "\n" + first)
        said = self.above(expect=2)
        self.assertEqual(len(said), 1)
        self.assertIn("above ## 0.1.0", said[0]["message"])


class SaidToTheAgentTest(unittest.TestCase):
    """The managed texts say the state a release is cut from, and what the plan of `cut` is."""

    TEXTS = {name: " ".join((ROOT / "share" / "agents" / name).read_text(encoding="utf-8").split())
             for name in ("instructions-block.md", "conventions-block.md", "claude-skill.md")}
    SEEDED = {name: " ".join((ROOT / "share" / "templates" / name).read_text(encoding="utf-8").split())
              for name in ("RELEASING.md", "RELEASING-conventions.md")}

    def test_each_text_says_what_version_holds_before_a_cut(self) -> None:
        for name, text in self.TEXTS.items():
            self.assertIn("`0.0.0` before a first release", text, name)
            self.assertIn("what was published last", text, name)

    def test_each_block_says_what_is_true_of_the_repositories_that_receive_it(self) -> None:
        """The first text told a repository with its own mechanism never to move VERSION
        by hand, in the file next to a RELEASING.md telling it to."""
        own, released = self.TEXTS["conventions-block.md"], self.TEXTS["instructions-block.md"]
        # The ritual seeded beside each block, read here so that the two cannot part.
        self.assertIn("On `main`: `VERSION` = `X.Y.Z`", self.SEEDED["RELEASING-conventions.md"])
        self.assertIn("git tag -s vX.Y.Z", self.SEEDED["RELEASING-conventions.md"])
        self.assertIn("`VERSION` is written by `cut`", self.SEEDED["RELEASING.md"])
        self.assertIn("Made by hand, as `RELEASING.md` describes it, the release moves it in the pull request"
                      " that prepares the tag", own)
        self.assertIn("Made with `cut`, it is `cut` that writes it", own)
        self.assertIn("until `cut` writes it", released)
        for name, text in self.TEXTS.items():
            self.assertNotIn("never move it by hand", text, name)

    def test_one_rule_for_the_date_in_every_text(self) -> None:
        for name, text in self.TEXTS.items():
            self.assertIn("never a later one", text, name)
            self.assertIn("`## Unreleased`", text, name)
            self.assertNotIn("never leave the entry undated", text, name)
        self.assertNotIn("an entry is undated until the day it is cut",
                         " ".join((ROOT / "docs" / "conventions.md").read_text(encoding="utf-8").split()))

    def test_each_text_says_what_the_plan_is_and_what_it_is_not(self) -> None:
        for name, text in self.TEXTS.items():
            self.assertRegex(text, r"[Ww]ithout `--apply`", name)
            # What it does, in the words a run of it bears out (BeforeCutTest).
            self.assertIn("changes no file of the product and nothing on GitHub", text, name)
            self.assertNotIn("writes nothing", text, name)
            # What it needs.
            self.assertIn("needs `gh`, the network and a `main` level with `origin`", text, name)
            # Whose lines are whose.
            self.assertIn("What it says of the version, the entry and the worktree is about what was prepared", text, name)
            self.assertIn("`tag.gpgsign`, `user.signingkey` and the signing key are the signer's own", text, name)
            self.assertIn("exit 2 on those alone is expected of anyone else", text, name)
            self.assertIn("never configure a key to turn them green", text, name)
            # What it does not reach.
            self.assertNotIn("what `--apply` would refuse", text, name)
            for only_applied in ("the verify command", "`[cut] after-version`", "GitHub's verdict on the", "the checks"):
                self.assertIn(only_applied, text.replace("The verify", "the verify"), f"{name}: {only_applied}")

    def test_no_selector_asks_everyone(self) -> None:
        """A prose-only change is taken with the next release: a selector true of every
        repository turned `adopt` to ASKS and a `check` from the new socle to FAIL."""
        decl = MODULE.Declarations(pathlib.Path("/nonexistent"), "p")
        self.assertNotIn("managed-block", MODULE.impact_selectors(decl))
        self.assertNotIn("managed-block", (ROOT / "docs" / "conventions.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
