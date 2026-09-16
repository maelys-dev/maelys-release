# SPDX-License-Identifier: MPL-2.0
"""Report the identity of the entry point's socle through its shared context."""
from __future__ import annotations

from .context import Context


def socle_data(sha: str, tag: str, context: Context) -> dict:
    return {"version": context.socle_version(), "tag": tag, "sha": sha}
