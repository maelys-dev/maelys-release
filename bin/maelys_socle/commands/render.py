# SPDX-License-Identifier: MPL-2.0
"""`render`: fill the placeholders of a template into a file.
"""
from __future__ import annotations

import pathlib
import re

from maelys_cli import EXIT_OK, Failure, Invocation
from ..project import is_utf8
from ..staging import render_text


def handle_render(invocation: Invocation) -> tuple[dict, int]:
    template, output = pathlib.Path(invocation.operands[0]), pathlib.Path(invocation.operands[1])
    mapping: dict[str, str] = {}
    for pair in invocation.operands[2:]:
        key, separator, value = pair.partition("=")
        if not separator or not re.fullmatch(r"[A-Z0-9_]+", key):
            raise Failure("VALIDATION_FAILED", f"Placeholder assignments are KEY=VALUE with KEY in [A-Z0-9_]: {pair}.",
                          "Use the synopsis returned by describe and retry.")
        mapping[key] = value
    if not template.is_file():
        raise Failure("NOT_FOUND", f"Template not found: {template}.", "Pass an existing template file.")
    text = render_text(template, mapping)
    left = sorted(set(re.findall(r"@[A-Z0-9_]*@", text)))
    if left:
        raise Failure("VALIDATION_FAILED", f"Unrendered placeholders in {template}: {' '.join(left)}.",
                      "Pass a KEY=VALUE for each of them.")
    # Encoded before the output is opened: a value given as bytes that are
    # not UTF-8 reaches here as lone surrogates, and writing the text opened
    # the file, which empties it, and then failed to encode.
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        strange = sorted(key for key, value in mapping.items() if not is_utf8(value.encode("utf-8", "surrogateescape")))
        raise Failure("VALIDATION_FAILED",
                      f"The value given for {' '.join(strange) or 'a placeholder'} is not UTF-8: {output} is written"
                      " in UTF-8, and nothing was written.",
                      "Pass the value as UTF-8.") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    return {"template": str(template), "output": str(output), "placeholders": sorted(mapping)}, EXIT_OK
