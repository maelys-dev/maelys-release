# SPDX-License-Identifier: MPL-2.0
"""The socle an entry point runs from: its root and its version, read when asked.

The root belongs to the entry point and not to this package. An installed
copy and a checkout loaded in one interpreter share these modules, and each
must read its own VERSION, its own share/ and its own allowed signers: a
test loads both and holds it. So the entry point builds this object from
its own location, and every function that reads the running socle takes it
as its first parameter; nothing in the package finds the root from its own
file.

The context once carried the services the entry point still owned while
the package was being extracted from it -- reading a product, staging its
files, the identity of the socle, the Impact lines. They are package
functions now, and take the context; what remains is what only the entry
point can know.

Fields hold functions, never their results: construction performs no read.
The context never captures the host either: the host module resolves its
current host when a call actually runs.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Context:
    """What only the entry point knows: where the running socle is, and its version."""

    socle_root: Callable[[], pathlib.Path]
    socle_version: Callable[[], str]
