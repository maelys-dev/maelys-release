# SPDX-License-Identifier: MPL-2.0
"""`preflight`: what the release workflow will demand of the tag, before it exists.
"""
from __future__ import annotations


from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Invocation
from .. import identity, inspection
from ..context import Context
from ..inspection import text_check
from ..project import require_socle_mechanism
from ..release_checks import formula_checks, repository_checks, tag_checks
from ..texts import LABELS


def handle_preflight(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """What the release workflow will demand of the tag, before it exists."""
    decl, data = inspection.check_data(invocation, context)
    require_socle_mechanism(decl.project, decl.mechanism, "preflight")
    gate = (tag_checks(decl.project, decl.version, identity.allowed_signers(context.socle_root()), between_releases=True)
            + formula_checks(decl)
            + repository_checks(decl)) \
        if data["valid"] else []
    data["preflight"] = [{"status": status, "message": message} for status, message in gate]
    data["violations"].extend(message for status, message in gate if status == "fail")
    data["ready"] = data["valid"] and not any(status == "fail" for status, _ in gate)
    return data, EXIT_OK if data["ready"] else EXIT_VIOLATIONS


def text_preflight(data: dict) -> str:
    # the check part first, with its drift lines when it fails; then the tag preconditions
    lines = [text_check(data).rstrip("\n")]
    lines.extend(f"{LABELS[item['status']]:<8} {item['message']}" for item in data["preflight"])
    state = "ready to tag" if data["ready"] else "not ready to tag"
    lines.append(f"preflight: {data['product']} is {state}")
    return "\n".join(lines) + "\n"
