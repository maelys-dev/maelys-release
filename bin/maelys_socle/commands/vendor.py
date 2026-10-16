# SPDX-License-Identifier: MPL-2.0
"""`vendor`: refresh bin/maelys_cli.py from maelys-cli at its pin, and record its digest.
"""
from __future__ import annotations

import hashlib
import pathlib
import tempfile

from maelys_cli import EXIT_OK, Failure, Invocation
from ..context import Context
from ..forge import forge
from ..host import git, run
from ..socle_origin import vendored_pin


def handle_vendor(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Refresh bin/maelys_cli.py from maelys-cli at the pinned commit, and record its digest."""
    del invocation
    tag, commit, previous = vendored_pin(context)
    base = forge().clone_base()
    with tempfile.TemporaryDirectory(prefix="maelys-release-vendor.") as temp:
        work = pathlib.Path(temp)
        git("init", "-q", str(work))
        git("fetch", "-q", "--depth", "1", f"{base}/maelys-cli.git", commit, cwd=work)
        shown = run(["git", "show", "FETCH_HEAD:python/maelys_cli.py"], cwd=work)
        if shown.returncode != 0:
            raise Failure("NOT_FOUND", f"python/maelys_cli.py is not in maelys-cli at {commit[:7]}.",
                          "Pin a maelys-cli release that ships the Python framework (0.5.12 or later).")
        content = shown.stdout.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    target = context.socle_root() / "bin" / "maelys_cli.py"
    changed = not target.is_file() or target.read_bytes() != content or previous != digest
    target.write_bytes(content)
    (context.socle_root() / "dependencies" / "maelys-cli.pin").write_text(f"{tag}\n{commit}\nsha256:{digest}\n", encoding="utf-8")
    return {"tag": tag, "commit": commit, "sha256": digest, "path": str(target), "changed": changed}, EXIT_OK
