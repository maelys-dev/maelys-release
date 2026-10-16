# SPDX-License-Identifier: MPL-2.0
"""How each command's result reads on a terminal: one renderer per command, by its name."""
from __future__ import annotations

from .. import authorship, signers
from ..adoption import text_adopt
from ..commands.new import text_new
from ..commands.preflight import text_preflight
from ..cut import text_cut
from ..dependencies import text_dependencies
from ..inspection import text_check, text_declarations
from ..migration import text_migrate
from ..protect import text_protect
from ..rehearse import text_rehearse
from ..tap import text_tap


TEXT = {
    "migrate": text_migrate,
    "new": text_new,
    "adopt": text_adopt,
    "dependencies": text_dependencies,
    "check": text_check,
    "declarations": text_declarations,
    "preflight": text_preflight,
    "outgoing": authorship.text_outgoing,
    "hook": authorship.text_hook,
    "signers": signers.text_signers,
    "cut": text_cut,
    "protect": lambda data: text_protect(data),
    "rehearse": lambda data: text_rehearse(data),
    "render": lambda data: f"rendered {data['output']}\n",
    "tap": text_tap,
    "self-test": lambda data: "self-test: ok\n",
    "vendor": lambda data: (f"vendor: bin/maelys_cli.py is maelys-cli {data['tag']} ({data['commit'][:7]}),"
                            f" sha256 {data['sha256'][:12]}, " + ("updated" if data["changed"] else "unchanged") + "\n"),
}
