# SPDX-License-Identifier: MPL-2.0
"""Read the product's declarations and check its conventions and release files.

The entry point supplies the shared declaration, socle and planning functions.
They stay callable so each command keeps its original order of reads, including
running the pinned socle before checking a product.
"""
from __future__ import annotations

import pathlib
import re
from typing import Callable

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from .constants import (
    CLI_REFERENCE, JOB_ID, LEGACY_CLI_REFERENCES, PROGRAM, SOCLE_MANIFEST_PATTERNS,
    SOCLE_REPOSITORY, SOCLE_TARGETS,
)
from .declarations import Declarations
from .texts import checks_text
from .workflows import block_list, file_runners, sub_block, top_block, workflow_events


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


def check_data(
    invocation: Invocation, *,
    project_of: Callable[[Invocation], tuple[pathlib.Path, str, str]],
    run_pinned_socle: Callable[[Invocation, pathlib.Path], None],
    read_declarations: Callable[[pathlib.Path, str, str], Declarations],
    socle_identity: Callable[[str, str], tuple[str, str]],
    socle_data: Callable[[str, str], dict],
    newest_known_version: Callable[[], tuple],
    impact_for: Callable[[Declarations, str], tuple[list[dict], bool | None]],
    plan: Callable[..., list[dict]],
    stage: Callable[[Declarations, str, str], dict[str, tuple[str, bool]]],
    socle_impact: Callable[[str], list[tuple[str, str]]],
    version_tuple: Callable[[str], tuple],
) -> tuple[Declarations, dict]:
    project, product, mechanism = project_of(invocation)
    run_pinned_socle(invocation, project)
    decl = read_declarations(project, product, mechanism)
    sha, tag = socle_identity(str(invocation.option("--socle-sha", "")), str(invocation.option("--socle-tag", "")))
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
                  "socle": socle_data(sha, tag), "pinned": pinned, "checks": decl.applicable(),
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
                and version_tuple(pinned_tag) <= newest_known_version()
            if behind and impact_for(decl, data["pinned"]["tag"])[1] is True:
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
            data["files"] = plan(decl, stage(decl, sha, tag), apply=False)
            data["ahead"] = len(socle_impact(data["pinned"]["tag"]))
        else:
            data["files"] = plan(decl, stage(decl, sha, tag), apply=False)
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


def handle_declarations(
    invocation: Invocation, *,
    project_of: Callable[[Invocation], tuple[pathlib.Path, str, str]],
    read_declarations: Callable[[pathlib.Path, str, str], Declarations],
) -> tuple[dict, int]:
    """The product contract as data, for a CI job that installs what it declares."""
    project, product, mechanism = project_of(invocation)
    decl = read_declarations(project, product, mechanism)
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
            "dependencies": decl.dependencies, "linuxPackages": decl.linux_packages,
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


def handle_check(
    invocation: Invocation, *,
    check_data: Callable[[Invocation], tuple[Declarations, dict]],
) -> tuple[dict, int]:
    _, data = check_data(invocation)
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
        ("dependencies", " ".join(data["dependencies"])), ("linux", data["linuxPackages"]),
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
