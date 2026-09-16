# SPDX-License-Identifier: MPL-2.0
"""Pure document recognition, managed-block rendering and change classification."""
from __future__ import annotations

import re
import urllib.parse

from .constants import (
    BEGIN, END, GENERATED_HEAD_LINES, GENERATED_MARK, GENERATED_REFUSAL, PIN_SPELLINGS,
    README_LINK, VERIFIED_MARK,
)


def readme_links(readme: str, repository: str) -> set[str]:
    """The paths of docs/ a README points at, files or directories.

    maelys-platform's `readme_links`, kept identical: a document is linked
    when the README names it or a directory holding it, and a plain mention
    of a path in a sentence is not a link.
    """
    own = re.compile(rf"^https?://github\.com/{re.escape(repository)}/(?:blob|tree)/[^/]+/", re.I) \
        if repository else None
    found = set()
    for match in README_LINK.finditer(readme or ""):
        target = urllib.parse.unquote(match.group("target").split("#", 1)[0].split("?", 1)[0])
        if own:
            target = own.sub("", target)
        while target.startswith("./"):
            target = target[2:]
        target = target.rstrip("/")
        if target == "docs" or target.startswith("docs/"):
            found.add(target)
    return found


def verified_by(text: str) -> str:
    """The check a verified reference names in its head, or ''."""
    head = "\n".join(text.lstrip().splitlines()[:GENERATED_HEAD_LINES])
    found = VERIFIED_MARK.search(head)
    return " ".join(found.group("check").split()) if found else ""


def is_generated(text: str) -> bool:
    head = "\n".join(text.split("\n", GENERATED_HEAD_LINES)[:GENERATED_HEAD_LINES])
    return bool(GENERATED_MARK.search(head) and GENERATED_REFUSAL.search(head))


def managed_block(existing: str | None, block: str) -> str:
    """Replace the block between the markers, or append it."""
    if existing is not None and BEGIN in existing:
        out: list[str] = []
        skipping = False
        for line in existing.splitlines():
            if line.strip() == BEGIN:
                out.extend([BEGIN, *block.rstrip("\n").splitlines(), END])
                skipping = True
            elif line.strip() == END:
                skipping = False
            elif not skipping:
                out.append(line)
        return "\n".join(out) + "\n"
    head = (existing.rstrip("\n") + "\n\n") if existing else ""
    return head + BEGIN + "\n" + block.rstrip("\n") + "\n" + END + "\n"


def touches(relative: str, current: str, content: str) -> str:
    """What a change to a managed file touches: prose, pin or mechanism.

    Asked by maelys-oci, sixty-eight tags into the fleet: check named the
    file that moved and the diff, and a reader still had to read the diff to
    learn whether an adoption rewrote a sentence or the release. A text file
    is prose. A workflow or a script is prose when only its comments moved,
    pin when only the commit it names moved (comments aside), and mechanism
    otherwise -- the only kind that changes what runs.
    """
    if relative.endswith(".md"):
        return "prose"

    def normalised(text: str, comments: bool) -> list[str]:
        out = []
        for line in text.splitlines():
            for spelling in PIN_SPELLINGS:
                line = spelling.sub("PIN", line)
            if not comments and (not line.strip() or line.lstrip().startswith("#")):
                continue
            out.append(line)
        return out

    def uncommented(text: str) -> list[str]:
        return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    if normalised(current, False) != normalised(content, False):
        return "mechanism"
    if uncommented(current) != uncommented(content):
        return "pin"
    return "prose"


LABELS = {"ok": "ok", "note": "note", "warn": "WARN", "missing": "MISSING", "refused": "REFUSED", "fail": "FAIL"}


def checks_text(checks: list[dict]) -> str:
    return "".join(f"{LABELS.get(check['status'], check['status']):<8} {check['message']}\n" for check in checks)
