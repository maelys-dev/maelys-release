# SPDX-License-Identifier: MPL-2.0
"""The program: maelys_cli's, with the private environment validated before any handler runs."""
from __future__ import annotations

import maelys_cli as cli
from maelys_cli import Invocation

from .. import host


class SocleProgram(cli.Program):
    """Validate the private environment inside the contract, before any handler."""

    def parse(self, argv: list) -> tuple[Invocation, dict]:
        invocation, command = super().parse(argv)

        def checked(invocation: Invocation):
            host.HOST._self_test()
            return command["handler"](invocation)

        return invocation, {**command, "handler": checked}
