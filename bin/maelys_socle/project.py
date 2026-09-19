# SPDX-License-Identifier: MPL-2.0
"""Resolve a product directory, its name and its publication mechanism."""
from __future__ import annotations

import pathlib
import re

from maelys_cli import Failure, Invocation
from .constants import DECLARATION_FILE, PROGRAM, RELEASE_USES, SOCLE_MECHANISM


def declared_mechanism(project: pathlib.Path, requested: str = "") -> str:
    """How the product publishes, read from the release.yml it carries.

    A repository declares its mechanism by the workflow it holds: the socle's
    generated release.yml names the socle, a workflow of its own does not,
    and no release.yml at all means the socle does not release it. The last
    case is not an invitation: maelys-warden publishes through its own
    qualify and publish workflows and carries no release.yml, and writing one
    into it uninvited would be the socle claiming a repository that never
    asked. Installing the socle's mechanism is therefore explicit,
    --mechanism maelys-release, which is what new passes.
    """
    workflow = project / ".github" / "workflows" / "release.yml"
    if not workflow.is_file():
        return requested or "custom"
    carried = SOCLE_MECHANISM if RELEASE_USES.search(workflow.read_text(encoding="utf-8")) else "custom"
    # custom and none both say "the socle does not release this"; which of the
    # two is a refinement the repository may state. Only crossing that line is
    # a contradiction: it would either abandon a generated workflow or
    # overwrite one the socle never wrote.
    if requested and requested != carried and SOCLE_MECHANISM in (requested, carried):
        raise Failure("PRECONDITION_FAILED",
                      f"{project} carries a {carried} release.yml, so --mechanism {requested} contradicts it.",
                      "Delete or move .github/workflows/release.yml to change the mechanism of a repository;"
                      " the socle never overwrites a workflow it did not generate.")
    return requested or carried


def pinned_product(project: pathlib.Path) -> str:
    """The product name the existing release.yml carries, or ''."""
    workflow = project / ".github" / "workflows" / "release.yml"
    if not workflow.is_file():
        return ""
    match = re.search(r"^\s+product:\s+(\S+)\s*$", workflow.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else ""


def project_of(invocation: Invocation) -> tuple[pathlib.Path, str, str]:
    directory = pathlib.Path(invocation.operands[0])
    if not directory.is_dir():
        raise Failure("NOT_FOUND", f"DIR is not a directory: {directory}.", "Pass the product repository.")
    project = directory.resolve()
    # The product name comes from --product, else from the release.yml the
    # product already carries, else from the directory: a worktree or a
    # scratch clone is rarely named after the product it holds.
    product = invocation.option("--product") or pinned_product(project) or project.name
    if not re.fullmatch(r"[a-z0-9-]+", product):
        raise Failure("VALIDATION_FAILED", f"Product name must be [a-z0-9-]: {product}.",
                      "Pass --product NAME with a valid name.")
    return project, product, declared_mechanism(project, str(invocation.option("--mechanism", "")))


def require_socle_mechanism(project: pathlib.Path, mechanism: str, action: str) -> None:
    """rehearse and preflight replay the socle's release; nothing else does."""
    if mechanism != SOCLE_MECHANISM:
        raise Failure("PRECONDITION_FAILED",
                      f"{project} publishes through a {mechanism} mechanism, so {action} does not apply to it.",
                      f"'{PROGRAM} check {project}' verifies the conventions of a product the socle does not release.")


def engaged_documents(project: pathlib.Path) -> set[str]:
    """The docs/ files LICENSING.md links: a public engagement stays here."""
    licensing = project / "LICENSING.md"
    if not licensing.is_file():
        return set()
    return {match.replace("./", "") for match in
            re.findall(r"\(([^)\s]*docs/[^)\s#]+)\)", licensing.read_text(encoding="utf-8"))}


def declared_value(project: pathlib.Path, section: str, key: str) -> str | None:
    """What follows `key` under `[section]` of maelys-release.conf; "" when bare; None when absent.

    The same tolerant scan as declared_word, for a key that carries a value:
    `[docs] named OWNER/NAME`. Read before the rest of the file parses, as
    the managed block needs it even when a later section is refused.
    """
    release = project / DECLARATION_FILE
    if not release.is_file():
        return None
    current = ""
    for raw in release.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line
        elif current == f"[{section}]" and (line == key or line.startswith(key + " ")):
            return line[len(key):].strip()
    return None


def declared_word(project: pathlib.Path, section: str, word: str) -> bool:
    """Whether maelys-release.conf holds `word` under `[section]`."""
    release = project / DECLARATION_FILE
    if not release.is_file():
        return False
    current = ""
    for raw in release.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line
        elif current == f"[{section}]" and line == word:
            return True
    return False
