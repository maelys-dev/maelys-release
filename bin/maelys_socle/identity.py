# SPDX-License-Identifier: MPL-2.0
"""Report the identity of the entry point's socle through its shared context."""
from __future__ import annotations

import pathlib

from maelys_cli import Failure
from . import host
from .context import Context


def socle_data(sha: str, tag: str, context: Context) -> dict:
    return {"version": context.socle_version(), "tag": tag, "sha": sha}


def share_dir(root: pathlib.Path) -> pathlib.Path:
    """The socle's share/, in a checkout or under an installed prefix.

    One definition, because two would drift: the entry point asks it for the
    agent texts and the templates, and `cut` for the file naming who may sign
    a release. An installed copy keeps share/ beside the directory holding
    the executable, or under share/maelys-release when a packager prefers the
    prefix's own layout.
    """
    for candidate in (root / "share", root / "share" / "maelys-release"):
        if (candidate / "agents").is_dir():
            return candidate
    raise Failure("NOT_FOUND", f"share/agents not found under {root}.",
                  "Run from a maelys-release checkout or an installed prefix.")


def allowed_signers(root: pathlib.Path) -> pathlib.Path:
    """The file naming who may sign a Maelys release, under this socle's root.

    The root is given and never found from this module's own file: two
    socles are loaded in one process -- a copied prefix and the checkout --
    and they share this package, so a root read from `__file__` here would
    make one of them answer for the other. The executable knows which socle
    it is; this does not.
    """
    named = host.HOST.test_signers()
    return pathlib.Path(named) if named else share_dir(root) / "allowed-signers"
