# SPDX-License-Identifier: MPL-2.0
"""Read the product's declarations and check its conventions and release files.

The entry point supplies its shared services through one context. They stay
callable so each command keeps its original order of reads, including running
the pinned socle before checking a product.
"""
from __future__ import annotations

import json
import pathlib
import re

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from .constants import (
    CLI_REFERENCE, JOB_ID, LEGACY_CLI_REFERENCES, PROGRAM, PROSE_SUFFIXES, SOCLE_MANIFEST_PATTERNS,
    SOCLE_REPOSITORY, SOCLE_TARGETS,
)
from .context import Context
from . import privacy
from .cut import before_cut
from .host import run
from .identity import socle_data
from .declarations import Declarations, version_tuple
from .project import project_of
from .texts import checks_text
from .workflows import block_list, file_runners, sub_block, top_block, workflow_events, yaml_scalar


def pinned_socle(project: pathlib.Path) -> dict | None:
    """The socle sha and tag the product pins, from release.yml or from ci.yml.

    A product the socle releases names it in release.yml. A product that
    keeps its own release names it in the ci.yml line that calls
    check-product.yml, and nowhere else: reading release.yml alone left that
    product with no pin at all, so the tag of a socle fetched by commit
    stayed "untagged" and the regenerated ci.yml drifted against its own
    committed line.
    """
    workflows = project / ".github" / "workflows"
    for name, workflow in (("release.yml", "release"), ("ci.yml", "check-product")):
        path = workflows / name
        if not path.is_file():
            continue
        match = re.search(rf"{re.escape(SOCLE_REPOSITORY)}/\.github/workflows/{workflow}\.yml@([0-9a-f]{{40}}) # (\S+)",
                          path.read_text(encoding="utf-8"))
        if match:
            # The file is part of the answer: a fleet observer comparing pins
            # needs to know which of the two a product names it in, and
            # reading that back out of the repository is the regular
            # expression this field exists to retire.
            return {"sha": match.group(1), "tag": match.group(2),
                    "file": f".github/workflows/{name}"}
    return None


def read_runners(project: pathlib.Path) -> dict:
    """Which runners this repository's workflows select, and what could not be read.

    A public repository may only use runners GitHub hosts, and the fleet
    checks that before opening one. Searching the text of `runs-on` for a
    label misses every workflow that selects through a matrix, and reads a
    repository it could not decide about as clean. So the socle resolves a
    matrix reference within the same file, and everything it still cannot
    name goes to `unresolved`: an observer blocks on that rather than
    passing.
    """
    labels: set[str] = set()
    unresolved: list[str] = []
    delegated = False
    workflows = project / ".github" / "workflows"
    for path in sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        found, could_not = file_runners(path.name, text)
        labels.update(found)
        unresolved.extend(could_not)
        if re.search(rf"^\s*uses:\s*{re.escape(SOCLE_REPOSITORY)}/", text, re.M):
            delegated = True
    # maelys-oci names no runner at all: every job of its tree calls a
    # reusable workflow. An empty label list therefore means "this repository
    # chooses none", not "this repository is safe": the runners it uses are
    # those of the socle it pins.
    return {"labels": sorted(labels), "unresolved": unresolved, "delegated": delegated}


# ---- what runs, and when ------------------------------------------------------
#
# A repository's strategy is spread over its workflow files: what runs on a
# pull request, what runs on a push, what a signed tag releases, what only a
# hand starts. Reading it meant opening every file of every repository, and
# the fleet had no way to ask. These readers answer the question from the
# files alone, offline, because the shared CI calls `declarations` on a
# runner with no access to the API.
#
# Line-based, like the runner reader above: the socle carries no YAML parser
# and vendors none. What it cannot read it leaves out rather than guessing.

# The events the socle names; anything else a workflow declares is reported
# verbatim, because an event nobody named must not read as absence.


CARRY_CALL = re.compile(
    rf"^\s*uses:\s*{re.escape(SOCLE_REPOSITORY)}/\.github/workflows/carry-dependencies\.yml@", re.M)
PIN_NAME = re.compile(r"[a-z0-9-]+")


def with_value(job: str, key: str) -> "str | None":
    """One `key:` of a job's `with:`, unquoted; None when it is not one line the socle reads."""
    match = re.search(rf"^\s*{re.escape(key)}:(.*)$", sub_block(job, "with"), re.M)
    if not match:
        return None
    value = yaml_scalar(match.group(1))
    if value in ("", "|", ">", "|-", ">-"):
        return None
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def read_carried(project: pathlib.Path) -> dict:
    """The pins a product carries to runners that may not read them, and where they are read.

    carry-dependencies.yml is called by hand: what travels is the product's
    decision. The fleet asks this, rather than running a regular expression of
    its own over the product's workflows, to know which runner must read a
    private pin directly -- the carry's -- and which receives it instead: a
    runner that holds no credential for a carried pin is configured as
    intended, not drifting. What the socle cannot read goes to `unresolved`,
    and an observer concludes nothing from it.
    """
    calls: list = []
    unresolved: list = []
    workflows = project / ".github" / "workflows"
    for path in sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []:
        jobs = top_block(path.read_text(encoding="utf-8"), "jobs")
        for job in JOB_ID.findall(jobs):
            block = sub_block(jobs, job)
            if not CARRY_CALL.search(block):
                continue
            names = (with_value(block, "dependencies") or "").split()
            try:
                runner = json.loads(with_value(block, "runner") or "")
            except ValueError:
                runner = None
            if isinstance(runner, str):
                runner = [runner]
            if (not names or not all(PIN_NAME.fullmatch(name) for name in names)
                    or not isinstance(runner, list) or not runner
                    or not all(isinstance(label, str) and label for label in runner)):
                unresolved.append(f"{path.name}: job {job}")
                continue
            calls.append({"file": path.name, "job": job, "dependencies": names, "runner": runner})
    return {"dependencies": sorted({name for call in calls for name in call["dependencies"]}),
            "calls": calls, "unresolved": unresolved}


def read_workflows(project: pathlib.Path) -> list:
    """Every workflow of the repository: what starts it, and what it runs."""
    workflows = project / ".github" / "workflows"
    observed = []
    for path in sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        on = top_block(text, "on")
        push = sub_block(on, "push")
        labels, unresolved = file_runners(path.name, text)
        observed.append({
            "file": path.name,
            "events": workflow_events(text),
            # Empty branches with a push event means every branch, which is
            # not the same fact as no push at all: the renderer says which.
            "branches": block_list(push, "branches"),
            "tags": block_list(push, "tags"),
            "jobs": JOB_ID.findall(top_block(text, "jobs")),
            "runners": labels,
            "unresolved": unresolved,
            "delegates": bool(re.search(rf"^\s*uses:\s*{re.escape(SOCLE_REPOSITORY)}/", text, re.M)),
        })
    return observed


def check_data(invocation: Invocation, context: Context) -> tuple[Declarations, dict]:
    project, product, mechanism = project_of(invocation)
    context.run_pinned_socle(invocation, project)
    decl = context.read_declarations(project, product, mechanism)
    sha, tag = context.socle_identity(str(invocation.option("--socle-sha", "")), str(invocation.option("--socle-tag", "")))
    pinned = pinned_socle(project)
    # A socle fetched by commit alone (a depth-1 fetch in CI) knows no tag;
    # the tag is only a label next to the pinned commit, so when the commits
    # agree the product's label stands and regeneration compares content.
    if tag == "untagged" and pinned and pinned["sha"] == sha:
        tag = pinned["tag"]
    # Two verdicts: the conventions, which hold for every product, and the
    # release mechanism, which holds only for a product the socle releases.
    conventions: list[str] = []
    release: list[str] = []
    data: dict = {"product": product, "project": str(project), "mechanism": decl.mechanism,
                  "socle": socle_data(sha, tag, context), "pinned": pinned, "checks": decl.applicable(),
                  "files": [], "violations": []}
    for check in decl.applicable():
        if check["status"] not in ("ok", "note"):
            (conventions if check["scope"] == "conventions" else release).append(check["message"])
    if decl.valid:
        if decl.releases_here and data["pinned"] and data["pinned"]["sha"] != sha:
            mismatch = (f"{product} pins maelys-release {data['pinned']['tag']} ({data['pinned']['sha'][:7]}) but this"
                        f" is {tag} ({sha[:7]})")
            # A mismatch nothing since the pin asks anything about is not a
            # failure of the product: `current` is the socle's own answer, and
            # the verdict now uses it. Unknown -- a selector GitHub must answer
            # -- keeps the failure.
            # Only for a pin this socle is not older than: a pin ahead of the
            # running socle has no lines to read here, and "nothing asks" would
            # be vacuous rather than true.
            pinned_tag = data["pinned"]["tag"].lstrip("v")
            behind = re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pinned_tag) \
                and version_tuple(pinned_tag) <= context.newest_known_version()
            if behind and context.impact_for(decl, data["pinned"]["tag"])[1] is True:
                decl.add("note", f"{mismatch}: nothing since {data['pinned']['tag']} asks this product a gesture,"
                                 " so it is current in the socle's sense", "release")
            else:
                release.append(f"{mismatch}: run the pinned socle, or 'adopt {project} --apply' from this one to"
                               " upgrade")
            # And say what that upgrade would write. This branch used to
            # return one sentence and no file: a product three versions
            # behind read a FAIL naming a version number, with nothing about
            # which generated file moved or what the change was, so an
            # editorial pass on a managed block and a fix to the release
            # mechanism read identically. The plan is not drift -- the
            # product is conformant to the socle it pins -- so it is
            # reported beside the verdict and never counted as a violation.
            data["files"] = context.plan(decl, context.stage(decl, sha, tag), apply=False)
            data["ahead"] = len(context.socle_impact(data["pinned"]["tag"]))
        else:
            data["files"] = context.plan(decl, context.stage(decl, sha, tag), apply=False)
            for entry in data["files"]:
                if entry["action"] == "same":
                    continue
                # A seeded file is written once and owned by the product
                # afterwards: adopt offers it, check never demands it.
                if entry.get("seeded"):
                    decl.add("note", f"{entry['path']}: absent; 'adopt --apply' writes it once", "conventions")
                    continue
                message = f"{entry['path']}: {entry['action']}" + (f" ({entry['reason']})" if "reason" in entry else "")
                (conventions if entry["scope"] == "conventions" else release).append(message)
    # The documentation contract is opt-in while maelys-docs does not exist:
    # a product cannot move prose to a repository nobody has created yet, so
    # check names the destination and only refuses when asked to.
    if invocation.flag("--docs-contract"):
        conventions.extend(f"{relative}: prose belongs in maelys-docs/{product}/{relative[len('docs/'):]};"
                           f" move it with '{PROGRAM} migrate'"
                           for relative in decl.prose if relative not in decl.readme_held)
        conventions.extend(f"{name}: the generated CLI reference is {CLI_REFERENCE} for every product"
                           for name in LEGACY_CLI_REFERENCES if (project / name).is_file())
    # What the tracked files say of the machine and the person behind them.
    # Read last, and here alone -- by `check` and by `preflight`, which
    # checks first: it opens every tracked file, which `declarations` on a
    # runner and `adopt` have no use for, and a finding is not a reason to
    # refuse an adoption. A note, until the repository itself asks for more
    # in [privacy].
    lines, data["privacy"] = privacy.check_tree(project, history=invocation.flag("--history"))
    for status, message in lines:
        decl.add(status, message, "conventions")
        if status not in ("ok", "note"):
            conventions.append(message)
    # The seeded notes are added while planning, so the checks are taken last.
    data["checks"] = decl.applicable()
    data["conventions"] = {"valid": not conventions, "violations": conventions}
    if not decl.releases_here and release:
        raise Failure("FAILED", "A release-scope violation was reported for a product the socle does not release: "
                      + "; ".join(release) + ".",
                      "This is a defect of the socle: the exit code would contradict the verdict shown.")
    data["release"] = {"applicable": decl.releases_here, "valid": not release, "violations": release}
    data["violations"] = conventions + release
    data["valid"] = not data["violations"]
    return decl, data


def handle_declarations(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """The product contract as data, for a CI job that installs what it declares."""
    project, product, mechanism = project_of(invocation)
    decl = context.read_declarations(project, product, mechanism)
    release_workflow_text = (project / ".github" / "workflows" / "release.yml")
    stamp = re.search(r"# Managed by maelys-release (\S+) \(([0-9.]+)\)",
                      release_workflow_text.read_text(encoding="utf-8")
                      if release_workflow_text.is_file() else "")
    return {"valid": decl.valid, "product": product, "project": str(project), "version": decl.version,
            "mechanism": mechanism,
            # What the socle would run, and separately what the product asked
            # for. A field that folds the two says "everyone targets these
            # three" when it means "nobody declared anything".
            "declared": {"targets": [name for name, _ in decl.targets],
                         "manifestPatterns": list(decl.manifest_patterns),
                         "sbomPattern": decl.sbom_pattern,
                         "macosRunner": list(decl.macos_runner),
                         "dependenciesApart": decl.dependencies_apart,
                         "commitVerification": decl.commit_verification,
                         "legs": [{"name": name, "platform": platform, "command": command}
                                  for name, platform, command in decl.legs]},
            "pinned": pinned_socle(project),
            "managedBy": stamp.group(1) if stamp else "",
            "runners": read_runners(project),
            "workflows": read_workflows(project),
            "carried": read_carried(project),
            "dependencies": decl.dependencies,
            # The pins on a third party's commit that no tag carries, apart
            # from the names above: what holds each is a branch, which can be
            # rewritten, and a fleet observer asks which products stand there
            # without opening a pin. Always present, empty for nearly all.
            "unreleased": [{"name": name, "repository": decl.pins[name]["repository"],
                            "branch": decl.unreleased[name], "tag": decl.pins[name]["tag"],
                            "commit": decl.pins[name]["commit"]}
                           for name in decl.dependencies if name in decl.unreleased],
            "linuxPackages": decl.linux_packages,
            "macosPackages": decl.macos_packages, "formulas": decl.formulas,
            "targets": [name for name, _ in decl.targets] or list(SOCLE_TARGETS),
            "manifestPatterns": (SOCLE_MANIFEST_PATTERNS + " "
                                 + " ".join(decl.manifest_patterns)).strip(),
            "channels": [{"name": name, "registry": kind} for name, kind in decl.channels],
            "gate": decl.gate,
            "afterVersion": decl.after_version,
            "fuzz": {"harnesses": decl.fuzz_harnesses, "runs": decl.fuzz_runs},
            "checks": decl.applicable()}, \
        EXIT_OK if decl.valid else EXIT_VIOLATIONS


# What a user's git configuration may change in the output or in the reach of
# `git grep`, each set back on the command itself. Colour puts escapes around
# the line number, and the note vanished without a word under color.ui=always
# -- color.grep is the setting that decides for grep, and it overrides the
# general one. A column is one field more; full names are relative to another
# directory; the fallback searches untracked files outside a repository;
# recursion reads the files of another repository. And paths are quoted, so
# that the output is ASCII whatever a file is called. As configuration and
# not as options: a git that does not know a key ignores it, where it refuses
# an option it does not know, and a refusal here is silence.
PIN_SEARCH_SETTINGS = ("color.grep=false", "grep.column=false", "grep.fullName=false",
                       "grep.fallbackToNoIndex=false", "submodule.recurse=false", "core.quotePath=true")
_QUOTED = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def unquoted_path(path: str) -> str:
    """A path as git wrote it, back to its characters.

    git quotes a path that holds a control character, a quote, a backslash
    or a byte above 127: in double quotes, C escapes and three octal digits.
    A name that is not UTF-8 comes back with its bytes spelled out rather
    than raising: it is named in a message, never opened.
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body, raw, position = path[1:-1], bytearray(), 0
    while position < len(body):
        following = body[position + 1:position + 2]
        if body[position] != "\\":
            raw += body[position].encode("utf-8", "backslashreplace")
            position += 1
        elif following in _QUOTED:
            raw.append(_QUOTED[following])
            position += 2
        elif re.fullmatch(r"[0-3][0-7]{2}", body[position + 1:position + 4]):
            raw.append(int(body[position + 1:position + 4], 8))
            position += 4
        else:
            raw += b"\\"
            position += 1
    return raw.decode("utf-8", "backslashreplace")


def pin_copies(project: pathlib.Path, pins: dict[str, dict]) -> dict[str, dict[str, list[int]]]:
    """The tracked files that write out the commit of a pin, per pin.

    In one product the commit of a dependency lived in three places: its
    pin, a table in a bootstrap script and a document. Moving it took three
    edits, held together by whoever remembered the other two. The pin is the
    one place a dependency is named; this finds the others, so that they are
    a list and not a memory.

    The forty characters, and nothing shorter: seven of them are a word any
    log, any digest and any other commit may contain, and a rule that
    matched them would name files that have nothing to do with the pin.

    Three kinds of file write the commit without being a copy to keep, and
    they are left out. Another `dependencies/*.pin` is a declaration of its
    own. A `CHANGELOG.md` says which commit a version pinned: that is
    history, and the next pin does not rewrite it. And under `docs/`, what
    is not prose is data -- the kind `check` already sorts it into, written
    by a machine and written again by it: an audit, the evidence of a run.
    Prose there is read in every form the socle calls prose, `.md`,
    `.markdown`, `.txt` and `.rst`.

    Nothing else is exempt, and no lock file by name: on the fleet not one
    names a pin's commit, and a list of the names tools give their lock
    files would be a list to keep. What is left may be a record -- the
    notes of a milestone, a provenance file -- or a file a tool writes
    again, and neither can be told from here from a document that must
    follow. So the note claims no hand edit: it says the files are there.
    A record leaves the list by itself when the pin moves, since it keeps
    the commit it recorded.

    One search for every pin, over the tracked files as they are in the
    working tree: `check` runs on every product and in every CI run, and a
    search per pin would be eleven of them on the repository that pins most.
    None at all for a product that pins nothing. Outside a repository
    `git grep` answers nothing and so does this.

    Only the match is asked for, never the line that holds it, and paths
    come quoted: what is read back is a path, a number and forty
    hexadecimal characters, ASCII whatever the file contains and whatever
    it is called. A line of Latin-1 beside the commit took `check` down
    with a decoding error, exit 1 and no envelope, for a rule that is a
    note; and a lone carriage return, a form feed or U+2028 before the
    commit cut the line in two and the copy went unseen.
    """
    commits = sorted({pin["commit"] for pin in pins.values()})
    if not commits:
        return {}
    found = run(["git", *(word for setting in PIN_SEARCH_SETTINGS for word in ("-c", setting)),
                 "grep", "-n", "-I", "--only-matching", "--fixed-strings",
                 *(argument for commit in commits for argument in ("-e", commit))], cwd=project)
    if found.returncode != 0:
        return {}
    names: dict[str, list[str]] = {}
    for name, pin in pins.items():
        names.setdefault(pin["commit"], []).append(name)
    copies: dict[str, dict[str, list[int]]] = {}
    for line in found.stdout.split("\n"):
        record = re.fullmatch(r"(.+):([0-9]+):([0-9a-f]{40})", line)
        if not record or record.group(3) not in names:
            continue
        path, number = unquoted_path(record.group(1)), int(record.group(2))
        if re.fullmatch(r"dependencies/[^/]+\.pin", path) or path.rpartition("/")[2] == "CHANGELOG.md" \
                or path.startswith("docs/") and not path.lower().endswith(PROSE_SUFFIXES):
            continue
        for name in names[record.group(3)]:
            lines = copies.setdefault(name, {}).setdefault(path, [])
            if number not in lines:
                lines.append(number)
    return copies


def named_path(path: str) -> str:
    """A path in a list a reader can cut again: quoted when it holds
    anything but letters, digits and the punctuation of an ordinary name."""
    return path if re.fullmatch(r"[\w./@+~-]+", path) else json.dumps(path)


def handle_check(invocation: Invocation, context: Context) -> tuple[dict, int]:
    decl, data = check_data(invocation, context)
    # Said by `check` and not by `preflight`, which shares everything above:
    # `preflight` reads VERSION as the version about to be tagged, and says
    # "tag vX.Y.Z is free" of the very state this reads as a version `cut`
    # will refuse. Two readings of one file do not belong in one report.
    for message in before_cut(decl):
        decl.add("note", message, "conventions")
    # In `check` and nowhere else: `preflight` reads the same data before a
    # tag, and what a release needs to know of a pin is its tag and its
    # commit, which it is told. A note, and never more: a file that names
    # the commit it was verified against may be exactly right.
    for name, files in sorted(pin_copies(decl.project, decl.pins).items()):
        commit = decl.pins[name]["commit"]
        listed = ", ".join(f"{named_path(path)}:{','.join(str(number) for number in numbers)}"
                           for path, numbers in sorted(files.items()))
        decl.add("note", f"dependencies/{name}.pin: the commit it pins ({commit[:7]}) is also written in"
                 f" {len(files)} other tracked file{'s' if len(files) != 1 else ''}: {listed}. Nothing ties"
                 f" {'them' if len(files) != 1 else 'it'} to the pin: when it moves, each is edited, written"
                 " again by its tool, or left as the record it is. A file that can read the pin should,"
                 " and this line is the list of the rest", "conventions")
    data["checks"] = decl.applicable()
    return data, EXIT_OK if data["valid"] else EXIT_VIOLATIONS


def text_check(data: dict, with_checks: bool = True) -> str:
    lines = []
    if data.get("ahead") is not None:
        lines.append(f"{'note':<8} the plan below is what {data['socle']['tag']} would write, not a drift"
                     f" against the socle this product pins; {data['ahead']} version(s) separate them and"
                     f" '{PROGRAM} adopt {data['project']}' says what each asks of a product")
    for entry in data["files"]:
        lines.append(f"{entry['action']:<8} {entry['path']}" + (f" ({entry['reason']})" if "reason" in entry else "")
                     + (f" [{entry['touches']}]" if "touches" in entry else ""))
        # The diff, under the file that drifted. `check` computed it either
        # way and the JSON has carried it all along; the text printed the
        # word "update" and threw the rest, so a product that declared
        # something without adopting again read a FAIL naming a file and
        # nothing about the three lines in it. That is the defect 0.46.0
        # corrected three times -- knowing, and putting it out of sight --
        # on the path everybody reads. `adopt` has printed it since it had
        # one; this is the same shape.
        if entry.get("diff"):
            lines.extend("         " + line for line in entry["diff"].rstrip("\n").split("\n"))
    kinds = [entry["touches"] for entry in data["files"] if "touches" in entry]
    if kinds:
        counted = ", ".join(f"{kinds.count(kind)} {kind}" for kind in ("mechanism", "pin", "prose") if kind in kinds)
        lines.append(f"{'touches':<8} {counted}: 'mechanism' changes what runs, 'pin' only the socle commit"
                     " a workflow names, 'prose' only text and comments")
    conventions = "ok" if data["conventions"]["valid"] else "FAIL"
    release = ("ok" if data["release"]["valid"] else "FAIL") if data["release"]["applicable"] \
        else f"not applicable ({data['mechanism']} mechanism)"
    lines.append(f"verdict  conventions: {conventions}")
    lines.append(f"verdict  release mechanism: {release}")
    if data["valid"]:
        if data["release"]["applicable"]:
            lines.append(f"check: {data['product']} is on maelys-release {data['socle']['tag']}")
        else:
            lines.append(f"check: {data['product']} follows the conventions of maelys-release {data['socle']['tag']}")
    else:
        # the contract lines and the file lines already show their violations
        shown = {check["message"] for check in data["checks"]}
        shown.update(f"{entry['path']}: {entry['action']}" + (f" ({entry['reason']})" if "reason" in entry else "")
                     for entry in data["files"])
        lines.extend(f"drift    {violation}" for violation in data["violations"] if violation not in shown)
        staged = [entry["path"] for entry in data["files"] if entry["action"] in ("create", "update")]
        if staged:
            # Naming the command, because the order that produces this state
            # is the natural one: adopt, read what the socle advises,
            # declare it -- and a declaration changes what the generated
            # files must hold, so the last step leaves a half-adoption that
            # said only "FAIL".
            target = staged[0] if len(staged) == 1 else f"the {len(staged)} files above"
            lines.append(f"write    'maelys-release adopt {data['project']} --apply' writes {target}")
        lines.append(f"check: {data['product']} drifts from maelys-release {data['socle']['tag']}")
    return (checks_text(data["checks"]) if with_checks else "") + "\n".join(lines) + "\n"


# The order a reader thinks in, which is not the order the files are in:
# what a contributor triggers, what landing triggers, what releasing
# triggers, what only a hand triggers, and what another workflow calls.
MOMENTS = (("pull request", ("pull_request", "pull_request_target")),
           ("push", ("push",)),
           ("manual", ("workflow_dispatch", "repository_dispatch")),
           ("scheduled", ("schedule",)),
           ("released", ("release",)),
           ("called", ("workflow_call",)))


def strategy_lines(workflows: list) -> list:
    """One line per moment: what starts, and which workflow answers.

    The JSON is per file, because that is where the facts are read. A reader
    asking "what happens when I open a pull request" needs the other axis,
    and turning the table is the renderer's job, not a second command's.
    """
    lines = []
    for label, events in MOMENTS:
        for workflow in workflows:
            for event in workflow["events"]:
                if event not in events:
                    continue
                moment = label
                if event == "push":
                    moment = ("tag " + " ".join(workflow["tags"]) if workflow["tags"]
                              else "push " + " ".join(workflow["branches"]) if workflow["branches"]
                              else "push (every branch)")
                runs = f"{len(workflow['jobs'])} job" + ("s" if len(workflow["jobs"]) != 1 else "")
                if workflow["delegates"]:
                    runs += ", one of them the socle's"
                if workflow["unresolved"]:
                    runs += f", {len(workflow['unresolved'])} runner(s) unread"
                lines.append(f"{moment:<20} {workflow['file']} — {runs}")
    named = {event for _, events in MOMENTS for event in events}
    for workflow in workflows:
        for event in workflow["events"]:
            if event not in named:
                lines.append(f"{event:<20} {workflow['file']}")
    return lines


def text_declarations(data: dict) -> str:
    return checks_text(data["checks"]) + "".join(f"{key:<13} {value}\n" for key, value in (
        ("dependencies", " ".join(data["dependencies"])),
        ("unreleased", " ".join(f"{pin['name']} ({pin['branch']}, past {pin['tag']})"
                                for pin in data["unreleased"])),
        ("linux", data["linuxPackages"]),
        ("macos", data["macosPackages"]), ("formulas", " ".join(data["formulas"])),
        ("targets", " ".join(data["targets"]) + ("" if data["declared"]["targets"] else " (default)")),
        ("manifest", data["manifestPatterns"]
                     + ("" if data["declared"]["manifestPatterns"] else " (default)")),
        ("pinned", f"{data['pinned']['tag']} in {data['pinned']['file']}" if data["pinned"] else ""),
        ("runners", " ".join(data["runners"]["labels"])
                    + (f" + {len(data['runners']['unresolved'])} unresolved"
                       if data["runners"]["unresolved"] else "")
                    + (" (and the socle's, delegated)" if data["runners"]["delegated"] else "")),
        ("channels", " ".join(f"{c['name']} ({c['registry']})" for c in data["channels"])),
        ("fuzz", f"{data['fuzz']['harnesses']}/ run by {data['fuzz']['runs']}"
                 if data["fuzz"]["harnesses"] else ""))) \
        + "".join(f"{('strategy' if index == 0 else ''):<13} {line}\n"
                  for index, line in enumerate(strategy_lines(data["workflows"])))
