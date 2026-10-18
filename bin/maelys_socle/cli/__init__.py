# SPDX-License-Identifier: MPL-2.0
"""The command line of the socle: its program, the catalog of its commands, their schemas and their text.

The layer the framework drives. It reads arguments, hands an invocation to a command with the entry
point's context, and renders what the command returns; it decides nothing a command decides.
"""
