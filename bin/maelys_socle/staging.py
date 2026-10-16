# SPDX-License-Identifier: MPL-2.0
"""The managed files of a product: rendered from the socle's templates, then planned or written.
"""
from __future__ import annotations

import difflib
import os
import pathlib

from maelys_cli import Failure
from .cli_reference import render_cli_reference
from .constants import CARRY_USES, CI_USES, CLI_CONTRACT, CLI_REFERENCE, CONVENTION_MANAGED, CONVENTION_SEEDED, MANAGED
from .context import Context
from .declarations import Declarations
from .dependencies import CHECKOUT_SCRIPTS
from .project import spelled
from .reading import DOCS_BULLET, DOCS_UNNAMED_BULLET
from .socle_origin import share_dir
from .texts import managed_block, names_documentation, touches
from .workflows import ci_legs, ci_macos_runner, ci_workflow, release_workflow


def render_text(template: pathlib.Path, mapping: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace(f"@{key}@", value)
    return text


def stage(context: Context, decl: Declarations, sha: str, tag: str) -> dict[str, tuple[str, bool]]:
    """Every managed file as (content, executable), rendered for the product.

    A product the socle does not release receives the conventions alone: its
    workflows are its own, and nothing here touches them.
    """
    version = context.socle_version()
    share = share_dir(context)
    formulas = ", ".join(f"packaging/homebrew/{name}.rb.in" for name in decl.formulas) \
        or "packaging/homebrew/<name>.rb.in"
    mapping = {"PRODUCT": decl.product, "FORMULAS": formulas, "SOCLE_TAG": tag, "SOCLE_VERSION": version,
               # The documentation repository a named product spells, from
               # its declaration: the socle's texts carry no name of their own.
               "DOCUMENTS": decl.documents, "DOCUMENTS_NAME": decl.documents.split("/")[-1]}
    staged: dict[str, tuple[str, bool]] = {}
    if decl.releases_here:
        staged[".github/workflows/release.yml"] = (release_workflow(decl, sha, tag, version), False)
        staged[".claude/skills/maelys-release/SKILL.md"] = (
            render_text(share / "agents" / "claude-skill.md", mapping), False)
    # The checkout scripts go to every repository that pins and runs a
    # workflow, not only to the ones the socle releases: check-product.yml
    # calls them whatever publishes the product. maelys-warden, custom with
    # [dependencies] apart, was the first such product, and copied the
    # templates by hand after adopt wrote none -- where nothing would see
    # them drift.
    # A repository declaring [ci] own calls no shared CI, so nothing there
    # runs them.
    if decl.dependencies and decl.mechanism != "none" and not decl.own_ci:
        for relative in CHECKOUT_SCRIPTS:
            staged[relative] = (render_text(share / "templates" / relative.split("/")[-1], mapping), True)
    # The managed block states the rules that hold here: the socle's release
    # mechanism for a product it releases, the conventions alone otherwise.
    block_source = "instructions-block.md" if decl.releases_here else "conventions-block.md"
    block = render_text(share / "agents" / block_source, mapping)
    # maelys-warden prepares a public life, and AGENTS.md and CLAUDE.md are as
    # public as a README there: the bullet naming the private documentation
    # repository would break the rule it states. A declaration takes it out.
    #
    # Since 0.58.0 that is the default, and naming it is what a repository
    # declares: maelys-datalog, maelys-egress and maelys-http are public and
    # each carried the name in two public files, under a bullet forbidding
    # it. A default that leaks unless someone thinks to declare is the wrong
    # way round for a private name.
    if not decl.docs_named:
        block = DOCS_BULLET.sub(DOCS_UNNAMED_BULLET, block, count=1)
        # The socle's part of the managed files, held by the same predicate
        # as everything else it writes: the block of 0.57.x named the
        # repository for every public product, and this is where it would
        # have been refused.
        if names_documentation(block):
            raise Failure("PRECONDITION_FAILED",
                          f"the managed block would name the documentation repository in {decl.project.name},"
                          " which does not declare [docs] named.",
                          "This is a defect of the socle's block texts, not of the product: report it.")
    for name in CONVENTION_MANAGED:
        path = decl.project / name
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
        staged[name] = (managed_block(existing, block), False)
    if decl.generates_reference:
        reference, contract = render_cli_reference(decl)
        # An empty rendering is what "the generator did not run here" leaves
        # for a product that has no reference yet, and writing it would
        # create an empty docs/cli.md rather than none. The note it added
        # says why; the file waits for a machine that can build the product.
        if reference:
            staged[CLI_REFERENCE] = (reference, False)
            staged[CLI_CONTRACT] = (contract, False)
    releasing = "RELEASING.md" if decl.releases_here else "RELEASING-conventions.md"
    for name, template in (("RELEASING.md", releasing), ("LICENSING.md", "LICENSING.md"),
                           ("SECURITY.md", "SECURITY.md")):
        if not (decl.project / name).is_file():
            staged[name] = (render_text(share / "templates" / template, mapping), False)
    # The shared CI is not the release mechanism: a product that publishes
    # by itself can still run check-product.yml, which finds the socle from
    # the ci.yml line that calls it. A repository that publishes by nothing
    # at all keeps its workflows to itself.
    if decl.mechanism == "none" or decl.own_ci:
        return staged
    ci = decl.project / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        staged[".github/workflows/ci.yml"] = (ci_workflow(decl, sha, tag, version), False)
    else:
        # The product owns its ci.yml; only the lines naming the socle are
        # managed: the check's, and the carry that feeds it, at one commit.
        existing = ci.read_text(encoding="utf-8")
        if CI_USES.search(existing):
            updated = CARRY_USES.sub(rf"\g<1>{sha} # {tag}", CI_USES.sub(rf"\g<1>{sha} # {tag}", existing))
            staged[".github/workflows/ci.yml"] = (ci_legs(ci_macos_runner(updated, decl.runners), decl, sha, tag),
                                                  False)
    return staged


def staged_bytes(staged: dict[str, tuple[str, bool]]) -> dict[str, bytes]:
    """What `plan` writes, as the bytes it writes: every file encoded before the first is opened.

    A file the working tree holds under a name that is not UTF-8 reaches
    the staged texts as that name, each byte a lone surrogate: a formula
    template is listed in release.yml by its name. Writing the text opened
    the file, which empties it, and then failed to encode: `adopt --apply`
    left a product without its release workflow, and a second run wrote the
    blocks of a repository that has none. Nothing is opened until every
    file encodes, and what does not is refused in words, the name spelled
    from its bytes.
    """
    encoded: dict[str, bytes] = {}
    for relative, (content, _) in staged.items():
        try:
            encoded[relative] = content.encode("utf-8")
        except UnicodeEncodeError as error:
            head = max(content.rfind(space, 0, error.start) for space in " \t\n") + 1
            ends = [end for end in (content.find(space, error.start) for space in " \t\n") if end >= 0]
            word = content[max(head, error.start - 80):min([*ends, error.start + 80])]
            try:
                raw = word.encode("utf-8", "surrogateescape")
            except UnicodeEncodeError:
                raw = word.encode("utf-8", "backslashreplace")
            raise Failure("PRECONDITION_FAILED",
                          f"The socle would write a name that is not UTF-8 into {relative}, where it reads"
                          f" {spelled(raw)} -- it writes UTF-8, and encodes every file before it opens the"
                          " first: nothing was written.",
                          "A file of this repository carries that name, and the managed files are written from"
                          " it. Renaming it is this repository's own to do: rename, commit, and run the command"
                          " again.") from None
    return encoded


def plan(decl: Declarations, staged: dict[str, tuple[str, bool]], apply: bool) -> list[dict]:
    """One entry per managed file: create, update or same; written when applying."""
    files: list[dict] = []
    encoded = staged_bytes(staged)
    for relative in MANAGED:
        if relative not in staged:
            continue
        content, executable = staged[relative]
        # The rule at the writing end: nothing the socle stages for a
        # repository that does not declare [docs] named carries the name of
        # the documentation repository. The seed of 0.58.0 to 0.59.1 did,
        # and this is where it would have been refused before a product saw
        # it -- and where the next template written without the rule will.
        # A managed file carries the product's own prose around the block;
        # the block, the socle's part, is held in stage(context) where it is built.
        # The generated reference is the product's own program rendered,
        # not a template of the socle: the socle's own says
        # `--documents-repository` and its default, and the reading end
        # notes it like any other file.
        if relative not in CONVENTION_MANAGED + (CLI_REFERENCE, CLI_CONTRACT) and not decl.docs_named \
                and names_documentation(content):
            raise Failure("PRECONDITION_FAILED",
                          f"the socle would write the name of the documentation repository into {relative},"
                          f" and {decl.project.name} does not declare [docs] named.",
                          "This is a defect of the socle's templates or rewriting, not of the product: report it."
                          " A private repository that wants the name declares the word in maelys-release.conf.")
        path = decl.project / relative
        entry: dict = {"path": relative,
                       "scope": "conventions" if relative in CONVENTION_MANAGED + CONVENTION_SEEDED
                       + (CLI_REFERENCE, CLI_CONTRACT, ".github/workflows/ci.yml")
                       or (relative in CHECKOUT_SCRIPTS and not decl.releases_here)
                       else "release"}
        if relative in CONVENTION_SEEDED:
            entry["seeded"] = True
        if not path.is_file():
            entry["action"] = "create"
        else:
            current = path.read_text(encoding="utf-8")
            if current != content:
                entry["action"] = "update"
                entry["diff"] = "".join(difflib.unified_diff(
                    current.splitlines(keepends=True), content.splitlines(keepends=True), relative, relative))
                entry["touches"] = touches(relative, current, content)
            elif executable and not os.access(path, os.X_OK):
                entry["action"] = "update"
                entry["reason"] = "not executable"
                entry["touches"] = "mechanism"
            else:
                entry["action"] = "same"
        if apply and entry["action"] != "same":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded[relative])
            if executable:
                path.chmod(path.stat().st_mode | 0o111)
        files.append(entry)
    return files
