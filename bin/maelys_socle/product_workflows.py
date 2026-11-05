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
    block_list, direct_keys, file_runners, job_names, job_needs, names_alone, sub_block, top_block, unquoted,
    workflow_events, workflow_jobs, yaml_scalar,
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


# ---- how the jobs of a workflow follow one another -----------------------------
#
# A fleet observer draws each workflow's jobs and what each waits on, and
# never reads a release-mechanism file itself: it asks `declarations`. So
# the graph is read here, from a job's own keys only, and every value the
# socle cannot read as a job of the same file goes to `graphUnresolved`
# instead of to `needs`. An unread `needs` drawn as none is a job shown
# starting first that in fact waits.

LOCAL_CALL = re.compile(r"\./\.github/workflows/([A-Za-z0-9._-]+\.ya?ml)")
SOCLE_CALL = re.compile(rf"{re.escape(SOCLE_REPOSITORY)}/\.github/workflows/([A-Za-z0-9._-]+\.ya?ml)@(\S+)")
# `socle` below is (root of the running socle, its commit or None when nobody
# can say). The commit may be given as a function of no argument, asked once
# and only when a call names the socle: declarations of a product that calls
# no socle have no reason to ask git anything.


def graph_nodes(text: str, prefix: str, follow) -> tuple[list, list]:
    """(one node per job in file order, what could not be read, in the same order).

    `prefix` starts every unresolved entry, so that one inside a called
    workflow says which call it was read through. `follow(node)` resolves
    the call of a node that has one, and returns what it could not read.
    """
    jobs = workflow_jobs(text)
    nodes: list = []
    unresolved: list = []
    for job, body in jobs.items():
        keys = direct_keys(body)
        # A name holding an expression is GitHub's to evaluate: none is
        # guessed, and a reader falls back on the id as GitHub itself does
        # for a job with no name.
        name = unquoted(keys["name"][1]) if "name" in keys else None
        if name is not None and "${{" in name:
            name = None
        needs: list = []
        for value, named in job_needs(body) or []:
            if named is None:
                what = "an expression GitHub evaluates" if "${{" in value else "a value the socle does not read"
                unresolved.append(f"{prefix}job {job}: {what} in needs: {value}")
            elif named in jobs:
                needs.append(named)
            else:
                unresolved.append(f"{prefix}job {job}: needs {named}, which is not a job of this file")
        uses = unquoted(keys["uses"][1]) if "uses" in keys else None
        if "uses" in keys and uses is None:
            unresolved.append(f"{prefix}job {job}: uses {keys['uses'][1] or 'a block'}, which the socle does not read")
        node = {"job": job, "name": name, "needs": needs, "conditional": "if" in keys, "uses": uses, "called": None}
        if uses is not None:
            unresolved.extend(follow(node))
        nodes.append(node)
    return nodes, unresolved


def called_file(uses: str, project: pathlib.Path, socle: "tuple[pathlib.Path, object] | None",
                commit: list) -> "tuple[str, pathlib.Path] | str":
    """(from, path) of the workflow a job calls, or why it is not read.

    A call of the product's own tree is read from it. A call of the socle is
    read from the running socle's tree only when the commit it names is the
    running socle's: any other commit may hold another file, and drawing it
    would draw this socle's jobs under another's name. `commit` caches the
    running socle's commit once asked.
    """
    local = LOCAL_CALL.fullmatch(uses)
    if local:
        path = project / ".github" / "workflows" / local.group(1)
        return ("project", path) if path.is_file() else f"calls {uses}, which is not in this tree"
    called = SOCLE_CALL.fullmatch(uses)
    if called:
        ref = called.group(2)
        if socle is not None and not commit:
            commit.append(socle[1]() if callable(socle[1]) else socle[1])
        if socle is None or commit[0] is None:
            return f"calls the socle at {ref[:12]}, and is read here by a socle whose commit is unknown"
        if ref != commit[0]:
            return f"calls the socle at {ref[:12]}, and is read here by another socle"
        path = socle[0] / ".github" / "workflows" / called.group(1)
        if not path.is_file():
            return f"calls {called.group(1)}, which the socle at {ref[:12]} does not hold"
        return "socle", path
    if "@" in uses:
        return f"calls {uses}, a workflow of another repository, not read here"
    return f"uses {uses}, which the socle does not read"


def workflow_graph(name: str, text: str, project: pathlib.Path, socle: "tuple[pathlib.Path, object] | None",
                   commit: list) -> tuple[list, list]:
    """(graph, graphUnresolved) of one workflow file, its calls followed one level."""

    def follow(node: dict) -> list:
        prefix = f"{name}: job {node['job']}: "
        found = called_file(node["uses"], project, socle, commit)
        if isinstance(found, str):
            return [prefix + found]
        origin, path = found

        # One level: a call inside the called workflow keeps its `uses`, and
        # says it was not followed rather than reading as a job that runs
        # nothing.
        def unfollowed(inner: dict) -> list:
            return [f"{prefix}in {origin} {path.name}, job {inner['job']}: calls {inner['uses']},"
                    " not followed: a call is read one level deep"]

        inner, unresolved = graph_nodes(path.read_text(encoding="utf-8"), f"{prefix}in {origin} {path.name}, ",
                                        unfollowed)
        node["called"] = {"file": path.name, "from": origin, "graph": inner}
        return unresolved

    return graph_nodes(text, f"{name}: ", follow)


def read_workflows(project: pathlib.Path, socle: "tuple[pathlib.Path, object] | None" = None) -> list:
    """Every workflow of the repository: what starts it, and what it runs.

    `socle` is the running socle, which a call naming its commit is read
    from; without it, every call of the socle is unresolved.
    """
    workflows = project / ".github" / "workflows"
    observed = []
    commit: list = []
    for path in sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        on = top_block(text, "on")
        push = sub_block(on, "push")
        labels, unresolved = file_runners(path.name, text)
        graph, graph_unresolved = workflow_graph(path.name, text, project, socle, commit)
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
            "graph": graph,
            "graphUnresolved": graph_unresolved,
        })
    return observed
