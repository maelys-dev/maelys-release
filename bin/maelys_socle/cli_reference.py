# SPDX-License-Identifier: MPL-2.0
"""docs/cli.md and docs/cli-contract.json, generated from a product's describe by the generator of
maelys-cli at the commit the socle pins.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

from .constants import CLI_CONTRACT, CLI_GENERATOR, CLI_REFERENCE
from .declarations import Declarations
from .host import run
from .socle_origin import pinned_checkout


def cli_generator(decl: Declarations) -> pathlib.Path | None:
    """maelys-cli's reference generator, at the commit the product pins.

    The product declares maelys-cli once, in dependencies/maelys-cli.pin; the
    socle fetches that commit into its own cache. A checkout beside the
    product at the same commit is used as it stands, which keeps this offline
    and matches what scripts/checkout-dependency.sh leaves there.
    """
    # The framework holds the generator; every other product pins it.
    own = decl.project / CLI_GENERATOR
    if own.is_file():
        return own
    pin = decl.project / "dependencies" / "maelys-cli.pin"
    if not pin.is_file():
        return None
    sha = pin.read_text(encoding="utf-8").splitlines()[1].strip()
    relative = pathlib.Path(CLI_GENERATOR)
    sibling = decl.project.parent / "maelys-cli"
    if (sibling / relative).is_file():
        head = run(["git", "-C", str(sibling), "rev-parse", "HEAD"]).stdout.strip()
        if head == sha:
            return sibling / relative
    checkout = pinned_checkout("maelys-cli", sha, str(relative))
    return checkout / relative if checkout and (checkout / relative).is_file() else None


def render_cli_reference(decl: Declarations) -> tuple[str, str]:
    """The reference and its contract, generated as maelys-cli generates them.

    The socle runs the generator itself, so no product carries a rule, a path
    or a drift check of its own for this. What cannot be produced here (an
    unbuilt product, an unreachable maelys-cli) leaves both files as they
    stand rather than reporting a drift the socle cannot substantiate.
    """
    def existing() -> tuple[str, str]:
        return tuple((decl.project / name).read_text(encoding="utf-8")
                     if (decl.project / name).is_file() else ""
                     for name in (CLI_REFERENCE, CLI_CONTRACT))  # type: ignore[return-value]

    generator = cli_generator(decl)
    if generator is None:
        decl.add("note", f"{CLI_REFERENCE}: maelys-cli is not available at the pinned commit;"
                 " the reference is left as it stands", "conventions")
        return existing()
    # Three products, three build layouts (build/release, build/<profile>,
    # a platform tree): the directory holding the programs is declared, not
    # guessed, with the commonest as the default.
    named = [os.environ["CLI_REFERENCE_BUILD"]] if os.environ.get("CLI_REFERENCE_BUILD") \
        else (decl.cli_build or ["build/bin"])
    # The first that holds the programs, and not the first that exists: a
    # machine may carry a cross-compiled tree it cannot run, and choosing a
    # directory by its name would hand the generator a binary that answers
    # nothing.
    candidates = [decl.project / where for where in named]
    build = next((where for where in candidates
                  if all(os.access(where / program, os.X_OK) for program in decl.cli_programs)),
                 candidates[0])
    with tempfile.TemporaryDirectory(prefix="maelys-release-cli-reference.") as temp:
        markdown = pathlib.Path(temp) / "cli.md"
        contract = pathlib.Path(temp) / "cli-contract.json"
        completed = run([sys.executable, str(generator), "--build", str(build),
                         "--markdown", str(markdown), "--json", str(contract),
                         *decl.cli_flags, *decl.cli_programs], cwd=decl.project)
        if completed.returncode != 0 or not markdown.is_file():
            decl.add("note", f"{CLI_REFERENCE}: the generator did not run here"
                     " (an unbuilt product, usually); the reference is left as it stands."
                     + (f" CLI_REFERENCE_BUILD named {named[0]}" if os.environ.get("CLI_REFERENCE_BUILD")
                        else f" [build] named {', '.join(named)}"), "conventions")
            return existing()
        return markdown.read_text(encoding="utf-8"), contract.read_text(encoding="utf-8")
