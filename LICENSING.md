# Licensing

Copyright 2026 David Bromberg.

## Source code: MPL-2.0

`bin/maelys-release`, the reusable workflows of `.github/workflows/`, the
tests and the documentation of this repository are available under the
Mozilla Public License 2.0. The complete terms are in [`LICENSE`](LICENSE).

## Installed agent texts: CC-BY-4.0

The texts under `share/agents/`, including the managed `AGENTS.md` and
`CLAUDE.md` blocks and the skill, are licensed under CC-BY-4.0, with
attribution to David Bromberg. See [`share/agents/LICENSE`](share/agents/LICENSE).
Each text carries its copyright, source and license link so installation
preserves attribution. When sharing adaptations, retain those notices and
indicate your changes. This license covers the installed block, not unrelated
content a product writes outside it.

## Other installed templates: CC0-1.0

The documents and script under `share/templates/` remain CC0-1.0
([`share/LICENSE`](share/LICENSE)). This exception does not cover
`share/agents/` and does not change the source code's MPL-2.0 license.

## Redistributed material

[`bin/maelys_cli.py`](bin/maelys_cli.py) is a verbatim copy of
`python/maelys_cli.py` of maelys-dev/maelys-cli at the commit
`dependencies/maelys-cli.pin` names, under that repository's MPL-2.0. The
contract it implements is agent-cli/v2, whose specification and schemas are
CC BY-SA 4.0 in maelys-dev/agent-cli-spec; this repository copies neither.

## Documents engaged publicly

[`docs/conventions.md`](docs/conventions.md) is normative for every
repository that adopts this socle, and [`README.md`](README.md) presents it.
Both stay here: they describe the mechanism, not a product's prose.
