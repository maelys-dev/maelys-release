# SPDX-License-Identifier: MPL-2.0
"""One explicit context for the services shared by extracted commands.

The entry point constructs this immutable object once, after defining its
services. Every extracted module receives that same context; further command
moves extend it instead of adding callable parameters to command signatures.
Ordinary inputs (invocations, paths, values) remain ordinary parameters.

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

    project_of: Callable[[Invocation], tuple[pathlib.Path, str, str]]
    run_pinned_socle: Callable[[Invocation, pathlib.Path], None]
    read_declarations: Callable[[pathlib.Path, str, str], Declarations]
    socle_identity: Callable[[str, str], tuple[str, str]]
    socle_data: Callable[[str, str], dict]
    newest_known_version: Callable[[], tuple]
    impact_for: Callable[[Declarations, str], tuple[list[dict], bool | None]]
    plan: Callable[..., list[dict]]
    stage: Callable[[Declarations, str, str], dict[str, tuple[str, bool]]]
    socle_impact: Callable[[str], list[tuple[str, str]]]
    version_tuple: Callable[[str], tuple]
    socle_version: Callable[[], str]
    require_valid: Callable[[Declarations, str], None]
    socle_root: Callable[[], pathlib.Path]
    vanishing_contexts: Callable[[pathlib.Path], list[str]]
