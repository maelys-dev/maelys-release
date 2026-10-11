# SPDX-License-Identifier: MPL-2.0
"""A job line that ends in a comment, read by every reader of a job line and by nothing else.

`protect` names the contexts a branch requires after the job of ci.yml that
calls the shared checks, `adopt` rewrites the job it wrote there, `check`
refuses a job of the product's own under that name and notes one that
sanitizes twice, and `declarations` lists the jobs and the pins one of them
carries. Each read a job line with a pattern typed apart, and
`  check: # the shared checks` was a job to none of them.

Two rules are held here, and the first one wins where they pull apart: a
file that holds no such line keeps every answer it had, right or wrong; and
that line is read, inside the `jobs:` block and nowhere else, by all of
them alike.

The first rule has a table of its own, `ReadAsBeforeTest`, which passes on
the readers as they were before as it does after.
"""
from __future__ import annotations

import io
import json
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from test_maelys_release import ALL_LEGS, MODULE, Product, protection_host, using_host
# The entry script puts its own package on the path; the readers live there.
from maelys_socle import inspection
from maelys_socle import workflows as rules

OLD, NEW = ("b" * 40, "v1.0.0"), ("c" * 40, "v2.0.0")
USES = ("    uses: maelys-dev/maelys-release/.github/workflows/check-product.yml@" + "a" * 40 + " # v9.9.9\n"
        "    with:\n      product: p\n")
CARRIES = ("    uses: maelys-dev/maelys-release/.github/workflows/carry-dependencies.yml@" + "a" * 40
           + " # v9.9.9\n    with:\n      dependencies: pinned\n      runner: '\"ubuntu-26.04-arm\"'\n")
CARRY = "  carry:\n" + CARRIES
HEAD = "name: ci\non:\n  push:\n    branches: [main]\n  pull_request:\n\npermissions:\n  contents: read\n\njobs:\n"
BUILD = "  build:\n    runs-on: ubuntu-26.04\n    steps:\n      - run: make\n"
CHECK = "  check:\n" + USES
NOTED = "  check: # the shared checks\n" + USES
SANITIZES = "    runs-on: ubuntu-26.04\n    steps:\n      - run: make asan\n"
ASAN = "  sanitize:\n" + SANITIZES
DECLARED = [("x", "linux", "true")]


def declarations(legs: list) -> object:
    decl = MODULE.Declarations(pathlib.Path("/nonexistent"), "p")
    decl.legs, decl.runners = legs, {}
    return decl


def legs(pin: tuple = OLD) -> str:
    """The job adopt writes for DECLARED, at one pin."""
    return "".join(line + "\n" for line in rules.legs_job(declarations(DECLARED), *pin))


def appended(text: str) -> str:
    """A ci.yml with no legs job, once adopt has written one at its end."""
    return text.rstrip("\n") + "\n\n" + legs(NEW)


class Readers(unittest.TestCase):
    """A ci.yml in a directory of its own, and what every reader of a job line says of it."""

    def project(self, text: str, declaration: str = "[check]\nx linux true\n") -> pathlib.Path:
        work = pathlib.Path(tempfile.mkdtemp(prefix="maelys-release-job-lines.")) / "maelys-fixture"
        self.addCleanup(shutil.rmtree, work.parent, True)
        (work / ".github" / "workflows").mkdir(parents=True)
        # Bytes, so that a line ending written here is the one read.
        (work / ".github" / "workflows" / "ci.yml").write_bytes(text.encode("utf-8"))
        (work / "maelys-release.conf").write_text(declaration, encoding="utf-8")
        return work

    def answers(self, text: str) -> dict:
        """What each reader says of one ci.yml, read as the commands read it."""
        work = self.project(text)
        read = (work / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        return {
            "caller": MODULE.socle_check_contexts(work)[0],
            "jobs": MODULE.read_workflows(work)[0]["jobs"],
            "carried": [call["job"] for call in inspection.read_carried(work)["calls"]],
            "sanitizes": [read.splitlines()[number - 1].strip() for number in MODULE.sanitizers_twice(read)],
            "refused": any(check["status"] == "refused" and "a job of its own named legs" in check["message"]
                           for check in MODULE.read_declarations(work, "maelys-fixture").checks),
            "written": MODULE.ci_legs(read, declarations(DECLARED), *NEW),
            "removed": MODULE.ci_legs(read, declarations([]), *NEW),
        }

    def held(self, name: str, text: str, **expected: object) -> None:
        got = self.answers(text)
        for reader, answer in expected.items():
            with self.subTest(shape=name, reader=reader):
                self.assertEqual(got[reader], answer)


class ReadAsBeforeTest(Readers):
    """Files that hold no job line ending in a comment inside `jobs:`, and the answer each reader gave.

    This class passes on the readers as they were before such a line was
    read at all: it is the measure of what reading it must leave alone.
    """

    def plain(self, name: str, text: str, **expected: object) -> None:
        """A ci.yml with the call, no legs job and nothing else to say."""
        self.held(name, text, **{"caller": "check", "carried": [], "sanitizes": [], "refused": False,
                                 "written": appended(text), "removed": text, **expected})

    def test_a_key_of_another_block_that_ends_in_a_comment(self) -> None:
        """`env:` and `on:` hold keys at the depth of a job's name, and none is a job."""
        text = "name: ci\non: [pull_request]\nenv:\n  legs: # none here\n  A: 1\njobs:\n" + CHECK
        self.plain("a variable named legs, with a comment", text, jobs=["check"])
        text = ("name: ci\non:\n  workflow_dispatch: # by hand\n    inputs:\n      mode:\n"
                "        description: also build with ASan\n        type: boolean\n  pull_request:\n\njobs:\n" + CHECK)
        self.plain("an event with a comment, whose input says ASan", text, jobs=["check"])
        below = HEAD + CHECK + "env:\n  legs: # none here\n"
        self.plain("a variable named legs below the jobs", below, jobs=["check"],
                   written=below + "\n" + legs(NEW))

    def test_adopt_and_check_with_a_variable_named_legs_before_a_comment(self) -> None:
        """Read as a job of the product's own, it would refuse the legs of a product that is in order."""
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        ci = product.dir / ".github" / "workflows" / "ci.yml"
        text = ci.read_text(encoding="utf-8")
        self.assertEqual(text.count("\njobs:\n"), 1)
        ci.write_text(text.replace("\njobs:\n", "\nenv:\n  legs: # none here\n\njobs:\n"), encoding="utf-8")
        declaration = product.dir / "maelys-release.conf"
        declaration.write_text(declaration.read_text(encoding="utf-8") + "\n[check]\nx linux true\n",
                               encoding="utf-8")
        product.run("adopt", str(product.dir), "--apply")
        adopted = ci.read_text(encoding="utf-8")
        self.assertEqual((adopted.count("  legs: # none here\n"), adopted.count("\n  legs:\n"),
                          adopted.count("check-legs.yml@")), (1, 1, 1))
        self.assertEqual(product.json("check", str(product.dir))["data"]["violations"], [])

    def test_a_line_of_the_calling_job_at_two_spaces_or_at_the_margin(self) -> None:
        """A flow collection or a quoted scalar may close, or go on, anywhere, and the caller is still read."""
        for name, job in (
                ("a flow sequence continued at two spaces", "    needs: [build,\n  build]\n"),
                ("a flow sequence written at the margin", "    needs: [\nbuild\n]\n"),
                ("a quoted scalar continued at two spaces", "    name: \"the shared\n  checks\"\n"),
                ("a quoted scalar continued at the margin", "    name: 'the shared\nchecks'\n"),
                ("a flow mapping written at two spaces", "    if: ${{ github.event_name == 'pull_request' }}\n"
                                                         "    permissions: {\n  contents: read\n  }\n")):
            self.plain(name, HEAD + BUILD + "  check:\n" + job + USES, jobs=["build", "check"])
        quoted = HEAD + "  check:\n    with:\n      product: p\n      check_command: \"make check\n  && make lint\"\n" \
            + USES.splitlines(keepends=True)[0]
        self.plain("an input in quotes, continued at two spaces, above the call", quoted, jobs=["check"])

    def test_the_legs_job_wherever_it_stands(self) -> None:
        moved = legs(NEW)
        for name, before, after, without, jobs in (
                ("first", HEAD, "\n" + CHECK + "\n" + BUILD, HEAD + "\n" + CHECK + "\n" + BUILD,
                 ["legs", "check", "build"]),
                ("in the middle", HEAD + CHECK + "\n", "\n" + BUILD, HEAD + CHECK + "\n" + BUILD,
                 ["check", "legs", "build"]),
                ("last", HEAD + CHECK + "\n", "", HEAD + CHECK, ["check", "legs"]),
                ("above another top-level key", HEAD + CHECK + "\n", "env:\n  A: 1\n",
                 HEAD + CHECK + "env:\n  A: 1\n", ["check", "legs"])):
            self.held(f"the legs job {name}", before + legs() + after, caller="check", jobs=jobs, carried=[],
                      sanitizes=[], refused=False, written=before + moved + after, removed=without)
        self.held("the legs job alone", HEAD + legs(), caller="", jobs=["legs"], refused=False,
                  written=HEAD + moved, removed=HEAD)

    def test_line_endings_and_the_end_of_the_file(self) -> None:
        text = HEAD + BUILD + CHECK
        self.plain("the line endings of another system", text.replace("\n", "\r\n"), jobs=["build", "check"],
                   written=appended(text), removed=text)
        self.plain("no final newline", text.rstrip("\n"), jobs=["build", "check"])
        self.plain("a document marker above", "---\n" + text, jobs=["build", "check"])
        self.held("the jobs above what starts them", "name: ci\njobs:\n" + CHECK + "on:\n  push:\n  pull_request:\n",
                  caller="check", jobs=["check"], sanitizes=[], refused=False)

    def test_the_jobs_key_written_another_way(self) -> None:
        """The caller is looked for in the whole file, under a `jobs` key the listing does not read."""
        for name, key in (("in quotes", '"jobs":\n'), ("before a comment", "jobs: # all of them\n"),
                          ("before a space", "jobs :\n")):
            text = "name: ci\non:\n  pull_request:\n" + key + BUILD + CHECK
            self.held(f"the jobs key {name}", text, caller="check", jobs=[], sanitizes=[], refused=False,
                      written=appended(text), removed=text)

    def test_comments_at_every_depth(self) -> None:
        text = (HEAD + "# at the margin, above the first job\n"
                "  build:\n# at the margin, inside a job\n    runs-on: ubuntu-26.04\n  # at two spaces, inside a job\n"
                "    steps:\n    # at four\n      - run: make\n"
                "\n  # about the next job\n# and at the margin\n    # and deeper\n"
                + CHECK + "  # after the call\n"
                + CARRY + "  # after the carry\n# and at the margin\n"
                "  sanitize:\n    runs-on: ubuntu-26.04\n# at the margin\n    steps:\n  # at two\n"
                "      - run: make asan\n# the end\n")
        self.plain("comments at the margin, at two spaces and at four", text,
                   jobs=["build", "check", "carry", "sanitize"], carried=["carry"], sanitizes=["- run: make asan"])

    def test_a_block_scalar_that_holds_what_looks_like_jobs(self) -> None:
        text = (HEAD + "  heredoc:\n    runs-on: ubuntu-26.04\n    steps:\n      - run: |\n          cat <<EOF\n"
                "          jobs:\n            legs:\n            check: # not one\n          EOF\n"
                "      - name: after\n        run: |\n          echo done\n\n          echo more\n" + CHECK)
        self.plain("a heredoc with job lines in it", text, jobs=["heredoc", "check"])

    def test_the_jobs_a_product_writes_beside_the_call(self) -> None:
        self.plain("a carry, the call, a job that sanitizes", HEAD + CARRY + CHECK + ASAN,
                   jobs=["carry", "check", "sanitize"], carried=["carry"], sanitizes=["- run: make asan"])
        chosen = HEAD + CHECK + "      sanitizer_command: ''\n" + ASAN
        self.plain("the call sets sanitizer_command", chosen, jobs=["check", "sanitize"])
        own = HEAD + CHECK + "\n  legs:\n    runs-on: ubuntu-26.04\n"
        self.held("a job of the product's own named legs", own, caller="check", jobs=["check", "legs"],
                  refused=True, written=own, removed=own)
        self.held("no call at all", HEAD + BUILD, caller="", jobs=["build"], carried=[], sanitizes=[], refused=False)

    def test_a_job_written_in_a_shape_that_is_not_read_keeps_its_answer(self) -> None:
        """Wrong as it is: the caller is the job above, and reading more than one new shape is not this change."""
        for line in ('  "check":\n', "  check :\n", "  check: &shared\n", "  check: !!map\n", "  ? check\n  :\n",
                     "  check:# the shared checks\n"):
            self.held(f"the calling job written {line.strip()}", HEAD + BUILD + line + USES, caller="build",
                      jobs=["build"], refused=False)


class JobNamesTest(unittest.TestCase):
    """What one line names, for every reader: the single place a job line is recognised."""

    def named(self, text: str, inside: bool = False) -> list:
        return [name for name in rules.job_names(text.splitlines(), inside) if name]

    def test_the_name_alone_is_a_job_line_wherever_it_stands(self) -> None:
        """As it always was, outside the `jobs:` block too: a file read that way keeps its answers."""
        lines = "on:\n  push:\nenv:\n  legs:   \n  A: 1\njobs:\n  check:\n    uses: x\n  a-b_c9:\n".splitlines()
        self.assertEqual(rules.job_names(lines), ["", "push", "", "legs", "", "", "check", "", "a-b_c9"])

    def test_before_a_comment_it_is_one_inside_the_jobs_block_only(self) -> None:
        text = ("on:\n  push: # main\nenv:\n  legs: # none here\njobs:\n  first: # one\n    runs-on: x\n"
                "  second:\t# two\n  third: #\n  fourth:  #c: d\nenv:\n  fifth: # a variable\n")
        self.assertEqual(self.named(text), ["first", "second", "third", "fourth"])
        self.assertEqual(self.named("  first: # one\n"), [])
        self.assertEqual(self.named("  first: # one\n", inside=True), ["first"])

    def test_the_block_runs_to_the_next_line_at_the_margin_that_is_no_comment(self) -> None:
        for name, between in (("a blank line", "\n"), ("a comment at the margin", "# the product's own\n"),
                              ("a line of spaces", "   \n"), ("a deeper line", "    runs-on: x\n")):
            self.assertEqual(self.named("jobs:\n" + between + "  own: # c\n"), ["own"], name)
        for name, between in (("another key", "env:\n"), ("a document end", "...\n"), ("a sequence", "- x\n"),
                              ("a key in quotes", '"env":\n'), ("the rest of a scalar", "checks'\n")):
            self.assertEqual(self.named("jobs:\n" + between + "  own: # c\n"), [], name)
            self.assertEqual(self.named(between + "  own: # c\n", inside=True), [], name)

    def test_the_jobs_key_is_read_alone_or_before_a_comment(self) -> None:
        for key in ("jobs:", "jobs:  ", "jobs: # all of them", "jobs:\t#"):
            self.assertEqual(self.named(key + "\n  own: # c\n"), ["own"], key)
        for key in ('"jobs":', "jobs :", "jobs: {}", "jobs:# all", " jobs:", "\ufeffjobs:", "? jobs", "Jobs:"):
            self.assertEqual(self.named(key + "\n  own: # c\n"), [], key)

    def test_no_other_shape_is_read(self) -> None:
        """In quotes, before a space, an anchor, a tag or a flow mapping, after a `?`, behind a tab."""
        for line in ('  "own":', "  'own': # c", "  own :", "  own : # c", "  own: &shared", "  own: &shared # c",
                     "  own: *shared", "  own: !!map", "  own: {uses: x} # c", "  ? own", "  ? own # c", "\town: # c",
                     " \town: # c", "  own:# c", "  own: |", "  own: x # c", " own: # c", "   own: # c", "    own: # c",
                     "own: # c", "  # own:", "  our own: # c", "  own.all: # c", "  <<: *jobs # c", "  : # c"):
            self.assertEqual(self.named("jobs:\n" + line + "\n"), [], line)

    def test_the_lines_may_keep_their_endings(self) -> None:
        for ending in ("\n", "\r\n"):
            lines = f"jobs: # all{ending}  own: # c{ending}  other:{ending}".splitlines(keepends=True)
            self.assertEqual(rules.job_names(lines), ["", "own", "other"], repr(ending))


class EndsInACommentTest(Readers):
    """`  name: # comment` inside `jobs:`, which no reader took for a job line."""

    def test_the_calling_job_is_the_one_that_holds_the_call(self) -> None:
        """Not read, it left the caller at the job above, or with none above at the last event of `on:`."""
        for name, text in (("below a job", HEAD + BUILD + NOTED), ("with no job above", HEAD + NOTED),
                           ("behind a tab", HEAD + BUILD + NOTED.replace(": #", ":\t#")),
                           ("with the line endings of another system", (HEAD + BUILD + NOTED).replace("\n", "\r\n"))):
            caller, contexts = MODULE.socle_check_contexts(self.project(text))
            self.assertEqual(caller, "check", name)
            self.assertEqual(contexts[:3] + contexts[-1:], [*ALL_LEGS, "legs / x"], name)
            self.assertEqual([context for context in contexts if not context.startswith(("check / ", "legs / "))],
                             [], name)

    def test_protect_proposes_what_that_job_reports(self) -> None:
        """Under the job above, every proposed check was one nothing reports, and each blocks every pull request."""
        required = [*ALL_LEGS, "check / sanitizers", "legs / x", "build"]
        body = {"required_status_checks": {"strict": False, "contexts": list(required)},
                "required_linear_history": {"enabled": True}, "enforce_admins": {"enabled": True}}
        out, err = io.StringIO(), io.StringIO()
        with using_host(protection_host(("ok", body), seen=required)), redirect_stdout(out), redirect_stderr(err):
            code = MODULE.APP.main(["protect", str(self.project(HEAD + BUILD + NOTED + "\n" + legs())),
                                    "--format", "json", "--compact"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        data = json.loads(out.getvalue())["data"]
        self.assertEqual((data["caller"], data["socleContexts"]),
                         ("check", [*ALL_LEGS, "check / sanitizers", "legs / x"]))
        self.assertEqual(sorted(data["proposed"]), sorted(required))
        self.assertEqual((data["requiredButNeverRun"], data["missingFromRuns"]), ([], []))

    def test_the_legs_job_ends_at_the_next_job(self) -> None:
        """It ran through a next job so written: replaced with the legs, and deleted with them."""
        for name, after in (("next", "  after: # the product's\n    runs-on: ubuntu-26.04\n"),
                            ("below a blank line and a comment",
                             "\n  # about the next job\n  after: # the product's\n    runs-on: ubuntu-26.04\n"
                             "    steps:\n      - run: make\n")):
            text = HEAD + CHECK + "\n" + legs() + after
            self.held(f"a job with a comment {name}", text, caller="check", jobs=["check", "legs", "after"],
                      refused=False, written=HEAD + CHECK + "\n" + legs(NEW) + after, removed=HEAD + CHECK + after)

    def test_the_legs_job_adopt_wrote_is_found_and_not_written_twice(self) -> None:
        """`  legs: # from [check]` was not found, and a second `legs` was written at the end of the file."""
        noted = legs().replace("  legs:\n", "  legs: # from [check]\n")
        self.assertNotEqual(noted, legs())
        for name, after in (("last", ""), ("above another job", "\n" + BUILD), ("above a job with a comment",
                                                                              "  build: # ours\n    runs-on: x\n")):
            text = HEAD + CHECK + "\n" + noted + after
            self.held(f"the legs job {name}", text, caller="check", refused=False,
                      written=HEAD + CHECK + "\n" + legs(NEW) + after, removed=HEAD + CHECK + after)
            self.assertEqual(self.answers(text)["written"].count("check-legs.yml@"), 1, name)

    def test_a_job_of_the_products_own_named_legs_is_refused(self) -> None:
        """Not refused, adopt wrote the legs job below it: two jobs of one name, which stops the whole workflow."""
        own = HEAD + CHECK + "\n  legs: # ours\n    runs-on: ubuntu-26.04\n"
        self.held("a job of the product's own named legs, with a comment", own, caller="check",
                  jobs=["check", "legs"], refused=True, written=own, removed=own)

    def test_check_refuses_that_job_through_the_command(self) -> None:
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        ci = product.dir / ".github" / "workflows" / "ci.yml"
        ci.write_text(ci.read_text(encoding="utf-8") + "\n  legs: # ours\n    runs-on: ubuntu-26.04\n",
                      encoding="utf-8")
        declaration = product.dir / "maelys-release.conf"
        declaration.write_text(declaration.read_text(encoding="utf-8") + "\n[check]\nx linux true\n",
                               encoding="utf-8")
        said = [check["message"] for check in product.json("check", str(product.dir), expect=2)["data"]["checks"]
                if check["status"] == "refused"]
        self.assertEqual(len([message for message in said if "a job of its own named legs" in message]), 1, said)

    def test_declarations_list_the_job_and_its_carry(self) -> None:
        """Left out, its pins were neither carried nor unresolved."""
        text = HEAD + BUILD + "  carry: # the private pins\n" + CARRIES + "\n" + NOTED
        work = self.project(text)
        self.assertEqual(MODULE.read_workflows(work)[0]["jobs"], ["build", "carry", "check"])
        self.assertEqual(inspection.read_carried(work), {
            "dependencies": ["pinned"], "unresolved": [], "calls": [
                {"file": "ci.yml", "job": "carry", "dependencies": ["pinned"], "runner": ["ubuntu-26.04-arm"]}]})
        # Beside a carry written the old way, each with its own inputs.
        both = HEAD + CARRY + "  other: # two more pins\n" + CARRIES.replace("pinned", "first second")
        calls = inspection.read_carried(self.project(both))["calls"]
        self.assertEqual([(call["job"], call["dependencies"]) for call in calls],
                         [("carry", ["pinned"]), ("other", ["first", "second"])])
        unread = HEAD + "  carry: # the private pins\n" \
            + CARRIES.replace("'\"ubuntu-26.04-arm\"'", "${{ vars.RUNNER }}")
        self.assertEqual(inspection.read_carried(self.project(unread)),
                         {"dependencies": [], "calls": [], "unresolved": ["ci.yml: job carry"]})

    def test_a_job_that_sanitizes_is_judged_by_its_own_lines(self) -> None:
        """It was the rest of the job above: lost with the call, or counted with a Linux job."""
        mac = "    runs-on: macos-15\n    steps:\n      - run: make asan\n"
        for name, text, noted in (
                ("below the call", HEAD + CHECK + "  own: # ours\n" + SANITIZES, True),
                ("above a call written with a comment", HEAD + "  own:\n" + SANITIZES + NOTED, True),
                ("on macOS below a Linux job", HEAD + CHECK + BUILD + "  mac: # ours\n" + mac, False)):
            self.held(name, text, sanitizes=["- run: make asan"] if noted else [])

    def test_a_job_has_the_lines_below_its_own_name(self) -> None:
        jobs = rules.workflow_jobs("jobs:\n  first:\n    permissions:\n      contents: read\n"
                                   "  second: # the one that writes\n    permissions:\n      contents: write\n")
        self.assertEqual(jobs, {"first": "    permissions:\n      contents: read",
                                "second": "    permissions:\n      contents: write"})


class TheCommentIsNoLineOfTheJobTest(Readers):
    """What the comment says is not what the job runs on, nor what it calls.

    A reader that scans the lines of a job scanned its name line too, which
    was `  name:` alone until that line could end in a comment. Each file
    here is answered as the same file with the comment taken off.
    """

    CALLS = tuple(" # was maelys-dev/maelys-release/.github/workflows/" + name + "@" + "a" * 40 + " # v9.9.9"
                  for name in ("check-product.yml", "check-legs.yml", "carry-dependencies.yml"))
    MAC = "    runs-on: macos-15\n    steps:\n      - run: make asan\n"
    OTHER = "    runs-on: [self-hosted, x64]\n    steps:\n      - run: make asan\n"

    def alike(self, name: str, text: str, comment: str, **expected: object) -> None:
        """The answers for a file, which are those for the file without `comment` and the ones expected."""
        self.assertIn(comment + "\n", text, name)
        got, plain = self.answers(text), self.answers(text.replace(comment + "\n", "\n"))
        for reader in ("written", "removed"):
            got[reader] = got[reader].replace(comment + "\n", "\n")
        for reader, answer in plain.items():
            with self.subTest(shape=name, reader=reader):
                self.assertEqual(got[reader], answer)
                self.assertEqual(got[reader], expected.get(reader, answer))

    def test_a_comment_that_names_a_runner_is_not_the_runner(self) -> None:
        """`ubuntu-` beside the name of a macOS job gave a note that was false, and `macos-` took a true one away."""
        for comment, body, noted in ((" # the ubuntu-24.04 leg is the socle's", self.MAC, False),
                                     (" # not the linux runner", self.MAC, False),
                                     (" # macos-15 has no leak sanitizer", self.OTHER, True)):
            self.alike(comment, HEAD + CHECK + "  sanitize:" + comment + "\n" + body, comment,
                       sanitizes=["- run: make asan"] if noted else [])

    def test_a_comment_that_holds_the_text_of_a_call_calls_nothing(self) -> None:
        for comment in self.CALLS:
            # A job that sanitizes is still one, where the text of the call dropped it as the calling job.
            self.alike("on a job that sanitizes", HEAD + CHECK + "  sanitize:" + comment + "\n" + SANITIZES, comment,
                       caller="check", carried=[], sanitizes=["- run: make asan"])
            # The caller is the job whose lines call, above and below the comment.
            self.alike("above the calling job", HEAD + "  build:" + comment + "\n" + BUILD.split("\n", 1)[1] + CHECK,
                       comment, caller="check", jobs=["build", "check"], carried=[], refused=False)
            self.alike("below the calling job", HEAD + CHECK + CARRY + "  build:" + comment + "\n"
                       + BUILD.split("\n", 1)[1], comment, caller="check", carried=["carry"])
            none = HEAD + "  build:" + comment + "\n" + BUILD.split("\n", 1)[1]
            self.assertEqual(MODULE.socle_check_contexts(self.project(none))[0], "", comment)
            self.assertEqual(inspection.read_carried(self.project(none))["calls"], [], comment)

    def test_a_job_of_the_products_own_named_legs_is_not_the_socles_by_its_comment(self) -> None:
        """It was replaced with the legs at an adoption, and deleted with them when [check] was emptied."""
        for comment in self.CALLS:
            own = HEAD + CHECK + "\n  legs:" + comment + "\n    runs-on: ubuntu-26.04\n\n" + BUILD
            self.alike("a job of the product's own", own, comment, caller="check", refused=True,
                       written=own.replace(comment, ""), removed=own.replace(comment, ""))
            self.assertEqual(self.answers(own)["written"], own, comment)
            self.assertEqual(self.answers(own)["removed"], own, comment)
            # The job adopt wrote is the socle's by the call it makes, whatever stands beside its name.
            noted = legs().replace("  legs:\n", "  legs:" + comment + "\n")
            self.alike("the job adopt wrote", HEAD + CHECK + "\n" + noted + "\n" + BUILD, comment, refused=False,
                       written=HEAD + CHECK + "\n" + legs(NEW) + "\n" + BUILD, removed=HEAD + CHECK + "\n" + BUILD)

    def test_adopt_refuses_that_job_and_writes_nothing(self) -> None:
        product = Product()
        self.addCleanup(product.close)
        product.run("adopt", str(product.dir), "--apply")
        ci = product.dir / ".github" / "workflows" / "ci.yml"
        own = ci.read_text(encoding="utf-8") + "\n  legs:" + self.CALLS[1] + "\n    runs-on: ubuntu-26.04\n"
        ci.write_text(own, encoding="utf-8")
        declaration = product.dir / "maelys-release.conf"
        before = declaration.read_text(encoding="utf-8")
        declaration.write_text(before + "\n[check]\nx linux true\n", encoding="utf-8")
        said = [check["message"] for check in product.json("check", str(product.dir), expect=2)["data"]["checks"]
                if check["status"] == "refused"]
        self.assertEqual(len([message for message in said if "a job of its own named legs" in message]), 1, said)
        product.run("adopt", str(product.dir), "--apply", expect=1)
        self.assertEqual(ci.read_text(encoding="utf-8"), own)
        # With [check] emptied the adoption goes through, and the job is the product's still.
        declaration.write_text(before, encoding="utf-8")
        product.run("adopt", str(product.dir), "--apply")
        self.assertEqual(ci.read_text(encoding="utf-8"), own)

    def test_the_lines_are_given_with_the_name_alone(self) -> None:
        lines = ["env:", "  legs: # none here", "jobs: # all", "  own: # ubuntu-24.04", "    runs-on: x # y",
                 "  other:  "]
        self.assertEqual(rules.names_alone(lines), [*lines[:3], "  own:", *lines[4:]])
        self.assertEqual(rules.names_alone(["  own: # c"]), ["  own: # c"])
        self.assertEqual(rules.names_alone(["  own: # c"], True), ["  own:"])


if __name__ == "__main__":
    unittest.main()
