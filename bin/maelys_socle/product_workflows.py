# SPDX-License-Identifier: MPL-2.0
"""What a product's workflow files say: the socle they pin, the runners they ask for, their jobs and
their events, and the dependencies they carry.

Read from the files alone, offline, line by line: the socle carries no YAML parser, and the shared CI
asks these questions on a runner with no access to the API. Every command reads a product through
these, so they sit below the commands that report on it.
"""
from __future__ import annotations

import json
import pathlib
import re

from .constants import SOCLE_REPOSITORY
from .workflows import (
    block_list, file_runners, job_names, names_alone, sub_block, top_block, workflow_events, yaml_scalar,
)


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
        # sub_block finds a job by its name alone on a line.
        lines = names_alone(jobs.split("\n"), True)
        jobs = "\n".join(lines)
        for job in filter(None, job_names(lines, True)):
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
            "jobs": [job for job in job_names(top_block(text, "jobs").split("\n"), True) if job],
            "runners": labels,
            "unresolved": unresolved,
            "delegates": bool(re.search(rf"^\s*uses:\s*{re.escape(SOCLE_REPOSITORY)}/", text, re.M)),
        })
    return observed
