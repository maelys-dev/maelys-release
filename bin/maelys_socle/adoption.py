# SPDX-License-Identifier: MPL-2.0
"""Plan and apply the socle's managed files to a product."""
from __future__ import annotations

import pathlib
import re

from maelys_cli import EXIT_OK, Failure, Invocation

from .constants import DECLARATION_FILE, FORMER_DECLARATION_FILE, PROGRAM, SOCLE_MECHANISM
from .context import Context
from .identity import socle_data
from .declarations import require_valid
from .host import git, run
from .inspection import pinned_socle
from .project import project_of
from .texts import checks_text


def move_declaration(project: pathlib.Path, apply: bool) -> str:
    """Move the declarations to their new home, keeping their history.

    `git mv` rather than a write: the socle never composes this file. The
    comments a product puts beside each section are its reasoning — why a
    version is materialised twice, why a channel exists — and `git log
    --follow` must still find them after the move.
    """
    former, home = project / FORMER_DECLARATION_FILE, project / DECLARATION_FILE
    if not former.is_file() or home.exists():
        return ""
    if not apply:
        return FORMER_DECLARATION_FILE
    if run(["git", "mv", FORMER_DECLARATION_FILE, DECLARATION_FILE], cwd=project).returncode != 0:
        # Not a git repository, or the file is not tracked yet.
        former.rename(home)
    return FORMER_DECLARATION_FILE


def handle_adopt(invocation: Invocation, context: Context) -> tuple[dict, int]:
    project, product, mechanism = project_of(invocation)
    apply = invocation.flag("--apply")
    decl = context.read_declarations(project, product, mechanism)
    require_valid(decl, "adopt")
    sha, tag = context.socle_identity(str(invocation.option("--socle-sha", "")), str(invocation.option("--socle-tag", "")))
    # A product pins releases of the socle: a commit without a tag has no
    # changelog entry, no trial and no compatibility promise. Trials of a
    # candidate say so explicitly.
    if decl.releases_here and tag == "untagged" and not invocation.option("--socle-sha") \
            and not invocation.flag("--allow-untagged"):
        described = git("describe", "--tags", "--always", cwd=context.socle_root(), check=False) or sha[:7]
        raise Failure("PRECONDITION_FAILED", f"This socle is {described}, not a release: a product pins tagged versions only.",
                      "Check out a tag of maelys-release, or pass --allow-untagged for a trial of a candidate.")
    # Read before anything is written: `plan(..., apply)` rewrites
    # release.yml, so reading the pin after it gave the socle this run is
    # installing -- and `impact` was empty in --apply, the only mode the
    # product that asked for it uses.
    pinned = pinned_socle(project)
    if apply:
        vanishing = context.vanishing_contexts(project)
        if vanishing and not invocation.flag("--allow-lock"):
            raise Failure("PRECONDITION_FAILED",
                          f"the default branch of {project.name} requires " + ", ".join(vanishing)
                          + ", which this socle's check-product.yml no longer produces: after this"
                            " adoption every pull request would wait for a check that never reports.",
                          f"Narrow first: '{PROGRAM} protect {project} --without-legs --apply' removes the"
                          " socle's legs from the required list and nothing else; adopt, and merge the"
                          f" adoption; then '{PROGRAM} protect {project} --apply' requires the new names."
                          " Or pass"
                          " --allow-lock if the protection is being changed by hand in the same breath.")
    files = context.plan(decl, context.stage(decl, sha, tag), apply)
    moved = move_declaration(project, apply)
    changed = moved != "" or any(entry["action"] != "same" for entry in files)
    data = {"mode": "apply" if apply else "plan", "product": product, "project": str(project),
            "mechanism": mechanism, "socle": socle_data(sha, tag, context), "changed": changed,
            "impactFrom": (pinned or {}).get("tag", ""),
            "checks": decl.applicable(), "files": files}
    # Evaluated on the declarations read before this run wrote anything.
    data["impact"], data["current"] = context.impact_for(decl, (pinned or {}).get("tag", ""))
    if moved:
        data["moved"] = moved
    return data, EXIT_OK


def text_adopt(data: dict) -> str:
    lines = []
    for entry in data["files"]:
        lines.append(f"{entry['action']:<8} {entry['path']}" + (f" ({entry['reason']})" if "reason" in entry else "")
                     + (f" [{entry['touches']}]" if "touches" in entry else ""))
        if data["mode"] == "plan" and entry.get("diff"):
            lines.extend("         " + line for line in entry["diff"].rstrip("\n").split("\n"))
    listed = {entry["version"] for entry in data.get("impact", [])}
    for entry in data.get("impact", []):
        if entry.get("supersededBy") in listed:
            # Its instruction is not printed: a reader going line by line
            # would act on it before reaching the version that replaces it.
            lines.append(f"{entry['version']:<8} {'-':<4} superseded by {entry['supersededBy']}, below: read that line")
            continue
        mark = {True: "ASKS", False: "-", None: "?"}[entry.get("asksThis")]
        lines.append(f"{entry['version']:<8} {mark:<4} {entry['says']}")
    if data.get("impact"):
        asking = [entry["version"] for entry in data["impact"] if entry.get("asksThis") is True]
        unknown = [entry["version"] for entry in data["impact"] if entry.get("asksThis") is None]
        if asking:
            lines.append(f"{'impact':<8} {', '.join(asking)} ask this product a gesture: read those lines")
        elif unknown:
            lines.append(f"{'impact':<8} {', '.join(unknown)} cannot be evaluated here; nothing else asks"
                         " this product a gesture")
        else:
            lines.append(f"{'impact':<8} nothing since {data['impactFrom']} asks this product a gesture:"
                         " it is current in the socle's sense")
    # An empty list says four different things, and the useful one is rare.
    # A product reading nothing here should know whether that means "nothing
    # to do" or "the socle could not tell which versions you are missing".
    if not data.get("impact") and not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", data.get("impactFrom") or ""):
        named = data.get("impactFrom") or "nothing"
        lines.append(f"{'note':<8} no pinned socle version to read from ({named}), so nothing is said"
                     " about what the versions since ask of this product")
    if data.get("moved"):
        verb = "moves" if data["mode"] == "plan" else "moved"
        lines.append(f"{verb:<8} {data['moved']} -> {DECLARATION_FILE} (git mv, its history follows)")
    tag = data["socle"]["tag"]
    scope = "conventions" if data["mechanism"] != SOCLE_MECHANISM else "conventions and release files"
    if data["mode"] == "apply":
        lines.append(f"adopt: wrote the {scope} of maelys-release {tag} into {data['project']}"
                     f" ({data['mechanism']} mechanism)")
    elif data["changed"]:
        lines.append("adopt: plan only; add --apply to write")
    else:
        lines.append("adopt: nothing to do")
    return checks_text(data["checks"]) + "\n".join(lines) + "\n"
