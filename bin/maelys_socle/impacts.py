# SPDX-License-Identifier: MPL-2.0
"""The Impact lines of the socle's changelog: what each version since a product's pin asks of it.
"""
from __future__ import annotations

import re

from .constants import CLI_DECLARATIONS, CLI_REFERENCE
from .context import Context
from .declarations import version_tuple
from .github import classic_protection, repository_visibility, required_contexts


# Who an Impact line asks a gesture of -- adopting, protecting, declaring --
# in words `adopt` can evaluate on the product at hand. A consumer asked for
# "adopt or nothing" in the JSON, so that "current in the socle's sense" need
# not mean "on the last version": five versions in a day, and a product that
# adopted each paid a pull request per version whose line asked it nothing.
# A line says `[asks: nothing]` or names the readers it asks; the prose after
# it says what. A selector over-approximates rather than misses: it names who
# the line addresses, and the line says whether the case is theirs.
IMPACT_ASKS = re.compile(r"^\[asks: ([a-z][a-z, -]*)\]\s*(?:\[superseded-by: ([0-9]+\.[0-9]+\.[0-9]+)\]\s*)?"
                         # `[writes: protect, tap]`: the commands whose writes on GitHub this
                         # version changes, for a product that lets such a version mature a
                         # day before adopting it (maelys-http). `[writes: nothing]` says the
                         # version was looked at and changes no write.
                         r"(?:\[writes: ([a-z][a-z, -]*)\]\s*)?")


# The legs' names before 0.54.0, which a branch may still require.
OLD_LEGS = ("ubuntu-26.04", "ubuntu-26.04-arm", "macos-15")


def impact_selectors(decl: "Declarations") -> dict:
    """Each selector an Impact line may name, as a question on this product.

    True, False, or a callable for the one that must ask GitHub, evaluated
    only when a line names it: `old-legs` reads what the default branch
    requires, and is unknown rather than false when GitHub cannot be asked.
    """
    def old_legs():
        required = required_contexts(decl.project)
        if required is None:
            return None
        return any(re.fullmatch(r".+ / check \((?:" + "|".join(re.escape(leg) for leg in OLD_LEGS) + r")\)", name)
                   for name in required)
    return {"nothing": False,
            "channels": bool(decl.channels),
            "check-legs": bool(decl.legs),
            "pins": bool(decl.dependencies),
            "apart": bool(decl.dependencies_apart),
            "runners": bool(decl.runners),
            "formulas": bool(decl.formulas),
            "shared-ci": bool(decl.calls_socle_ci),
            "cli-reference": (decl.project / CLI_DECLARATIONS).is_file() or (decl.project / CLI_REFERENCE).is_file(),
            "signed-on-default-branch": decl.commit_verification == "signed-on-default-branch",
            "old-legs": old_legs,
            # The condition itself, not who could be subject to it: every
            # public product read `public` as "this asks you something",
            # and the gesture of 0.60.0 and 0.61.0 was to remove a line
            # that only two of them carried. What the socle already knows,
            # it answers.
            "names-documentation": bool(decl.documentation_named),
            # Not "does this product pin anything" but "does the socle write
            # that script here": a product declaring [ci] own, or no release
            # mechanism, carries none, and a line about the script asked it
            # a gesture it had no file to make.
            "checkout-script": bool(decl.dependencies) and decl.mechanism != "none" and not decl.own_ci,
            # The pointer `migrate` writes in the README is what says the
            # prose left: a product that never migrated carries none.
            "migrated-readme": migrated_readme(decl),
            # Named after who is concerned, not who asked: 0.57.2's line named
            # maelys-warden for [docs] unnamed, and maelys-egress, public and
            # concerned, read "nothing".
            "public": lambda: repository_visibility(decl.project, "public"),
            "classic-protection": lambda: classic_protection(decl.project)}


MIGRATE_POINTER = re.compile(r"^The documentation of \S+ (?:is no longer kept in this repository\."
                             r"|is in `[^`]+`, directory `[^`]+/`\.)$", re.MULTILINE)


def migrated_readme(decl: "Declarations") -> bool:
    """Whether the README carries the pointer `migrate` wrote when the prose left."""
    readme = decl.project / "README.md"
    try:
        return bool(MIGRATE_POINTER.search(readme.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return False


def asks_this(decl: "Declarations", asks: list[str] | None, cache: dict) -> bool | None:
    """Whether a line with these selectors asks this product a gesture; None when unknown."""
    if asks is None:
        return None
    selectors = cache.setdefault("selectors", impact_selectors(decl))
    unknown = False
    for name in asks:
        value = selectors.get(name)
        if callable(value):
            value = cache.setdefault(name, value())
        if value is True:
            return True
        if value is None:
            unknown = True
    return None if unknown else False


def newest_known_version(context: Context) -> tuple:
    """The newest version this socle's changelog describes, or its VERSION.

    Between two cuts the changelog is ahead of VERSION, and it is the
    changelog that holds the lines a pin is read against.
    """
    newest = version_tuple(context.socle_version())
    path = context.socle_root() / "CHANGELOG.md"
    if path.is_file():
        found = re.search(r"^## ([0-9]+\.[0-9]+\.[0-9]+) ", path.read_text(encoding="utf-8"), re.MULTILINE)
        if found:
            newest = max(newest, version_tuple(found.group(1)))
    return newest


def impact_for(context: Context, decl: "Declarations", pinned: str) -> tuple[list[dict], bool | None]:
    """The Impact lines since a pin, each marked for this product, and whether it is current.

    One reader for `adopt` and `check`: the second learned the question in
    0.58.0, from maelys-system, which read a FAIL on a pin that every line
    since said asked nothing of it.

    A line a later version supersedes asks nothing of a product whose list
    holds that later version: maelys-datalog, adopting 0.57.0 from 0.51.1,
    was told 0.54.0 asked a gesture, and read that line's instruction to
    narrow its protection -- the gesture 0.57.0 exists to forbid.
    """
    entries = socle_impact_entries(context, pinned)
    cache: dict = {}
    listed = {entry["version"] for entry in entries}
    for entry in entries:
        superseded = entry.get("supersededBy") in listed
        entry["asksThis"] = False if superseded else asks_this(decl, entry["asks"], cache)
    answers = [entry["asksThis"] for entry in entries]
    return entries, (False if True in answers else (None if None in answers else True))


def socle_impact_entries(context: Context, pinned: str) -> list[dict]:
    """The Impact lines since a pin, oldest first: version, what it says, whom it asks.

    `asks` is None for a line written before the marker, which is a line the
    socle cannot evaluate, and not a line that asks nothing.
    """
    entries = []
    for version, says in socle_impact(context, pinned):
        marker = IMPACT_ASKS.match(says)
        entries.append({"version": version, "says": says[marker.end():] if marker else says,
                        "asks": [name.strip() for name in marker.group(1).split(",") if name.strip()]
                        if marker else None,
                        "supersededBy": marker.group(2) if marker and marker.group(2) else None,
                        "writes": [name.strip() for name in marker.group(3).split(",")
                                   if name.strip() and name.strip() != "nothing"]
                        if marker and marker.group(3) else []})
    return entries


def socle_impact(context: Context, pinned: str) -> list[tuple[str, str]]:
    """The Impact line of every socle version between a product's pin and this one.

    A product reads a changelog to answer one question -- must I open a pull
    request? -- and the entries are essays. One product opened seven
    adoption pull requests in four days for a mechanism it used twice,
    reading the whole changelog by hand six times to find out. Since 0.49.0
    every entry ends with a line saying who must do what; this hands back
    only the ones the product has not taken.
    """
    path = context.socle_root() / "CHANGELOG.md"
    if not path.is_file() or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", pinned or ""):
        return []
    since = version_tuple(pinned.lstrip("v"))
    entries = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+) [^\n]*\n(.*?)(?=^## |\Z)",
                         path.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    impacts = []
    for version, body in entries:
        if version_tuple(version) <= since:
            continue
        said = re.search(r"^- \*\*Impact\.\*\*(.*?)(?=^- |\Z)", body, re.MULTILINE | re.DOTALL)
        if said:
            impacts.append((version, " ".join(said.group(1).split())))
    return list(reversed(impacts))
