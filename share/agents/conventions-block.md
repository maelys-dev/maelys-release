<!-- SPDX-License-Identifier: CC-BY-4.0
Copyright 2026 David Bromberg.
Source: https://github.com/maelys-dev/maelys-release/blob/main/share/agents/conventions-block.md
License: https://creativecommons.org/licenses/by/4.0/
When sharing adaptations, retain attribution and indicate your changes.
-->

# Maelys repository conventions (maelys-release)

This repository follows the shared Maelys conventions without publishing
through the maelys-release workflows: its release mechanism is its own. The
rules below hold whatever that mechanism is; the complete conventions are in
`docs/conventions.md` of maelys-release.

- These conventions are installed by `bin/maelys-release adopt` of
  maelys-release and verified by `maelys-release check DIR` (exit 2 on any
  violation). The block between the `maelys-release` markers of `AGENTS.md`
  and `CLAUDE.md` is regenerated: never edit it by hand. Everything else here
  belongs to this repository. The command follows agent-cli/v2: `--format
  json` everywhere, `describe` for the catalog.
- `check` reports two verdicts. The conventions apply to this repository; the
  release mechanism of the socle does not, so `check` says nothing about
  `.github/workflows/release.yml`, which this repository owns entirely.
- A version is `X.Y.Z` in `VERSION`, and a release is a signed, annotated tag
  `vX.Y.Z` on `main` whose commit carries that `VERSION` and a dated
  `CHANGELOG.md` entry `## X.Y.Z — YYYY-MM-DD`. A published tag is never
  moved, recreated or force-pushed; a failed publication is replayed on the
  existing tag.
- Never push a tag before this repository's own checks pass on that exact
  commit, and never publish from a branch. `maelys-release cut DIR X.Y.Z
  --apply` holds both rules: it writes `VERSION`, commits it signed on
  `release/vX.Y.Z`, opens the pull request and waits for its checks, and
  `cut DIR X.Y.Z --tag --apply` then signs the tag on the merge commit
  those checks ran on. It never merges its own pull request.
- `RELEASING.md` states how this repository publishes. The socle writes it
  once if it is missing, then it belongs to this repository: it is the place
  to describe the mechanism the socle does not provide here.
- `LICENSING.md` states what each part of this repository is licensed under,
  and names every document it engages publicly. `SECURITY.md` states how to
  report a vulnerability.
- The prose of this repository lives in `maelys-dev/maelys-docs`, directory
  `@PRODUCT@/`, with a neighbouring checkout at `../maelys-docs`.
  Documenting means opening a pull request there, not writing in `docs/`
  here, which carries what a machine writes and what this repository engages
  publicly. An agent that finds prose in `docs/` moves it rather than
  enriching it, and `maelys-release migrate` moves it with its history.
  **That repository is private: never name it from a public README.** The
  reader of this block has access to it; the reader of a README may not.
- Never commit a secret, a token or a signing key.
