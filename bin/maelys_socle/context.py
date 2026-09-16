# SPDX-License-Identifier: MPL-2.0
"""One explicit context for the services shared by extracted commands.

The entry point constructs this immutable object once, after defining its
services. Every extracted module receives that same context. A service leaves
it as soon as its implementation can be imported from the package: command
moves must shrink this temporary list, not add callable parameter lists.
Ordinary inputs (invocations, paths, values) remain ordinary parameters. The
final refactoring PR reports the remaining services.

Fields hold functions, never their results: construction performs no reads,
GitHub requests or planning. In particular the context never captures HOST;
the host module resolves its current host when a service actually runs.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable

from maelys_cli import Invocation
from .declarations import Declarations


@dataclass(frozen=True)
class Context:
    """Shared services still owned by the entry point during the extraction."""

    run_pinned_socle: Callable[[Invocation, pathlib.Path], None]
    read_declarations: Callable[[pathlib.Path, str, str], Declarations]
    socle_identity: Callable[[str, str], tuple[str, str]]
    newest_known_version: Callable[[], tuple]
    impact_for: Callable[[Declarations, str], tuple[list[dict], bool | None]]
    plan: Callable[..., list[dict]]
    stage: Callable[[Declarations, str, str], dict[str, tuple[str, bool]]]
    socle_impact: Callable[[str], list[tuple[str, str]]]
    socle_version: Callable[[], str]
    socle_root: Callable[[], pathlib.Path]
