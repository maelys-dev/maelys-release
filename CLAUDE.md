<!-- maelys-release:begin -->
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
  json` everywhere, `describe` for the catalog, and `--field NAME` to read
  one member of the result without a `jq` expression.
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
- A branch is named after the change it carries, with the prefix that
  change would take in a commit message (`fix/`, `docs/`, `release/`…),
  never after the tool that created it: a name says what changes, not who
  typed. No list is closed; the commit prefixes this repository already
  uses are its vocabulary.
- `RELEASING.md` states how this repository publishes. The socle writes it
  once if it is missing, then it belongs to this repository: it is the place
  to describe the mechanism the socle does not provide here.
- `LICENSING.md` states what each part of this repository is licensed under,
  and names every document it engages publicly. `SECURITY.md` states how to
  report a vulnerability.
- `docs/` carries what a machine writes and what this repository engages
  publicly, and no other prose. Where the prose lives is not named from this
  repository's files, which a public reader may see: do not name a
  documentation repository here. A private repository that wants the name
  declares `[docs] named` in `maelys-release.conf`.
- Never commit a secret, a token or a signing key.
<!-- maelys-release:end -->

## This repository is the socle

`bin/maelys-release` installs and verifies the files above in every Maelys
product; this repository is the one that writes them, so a change here reaches
the fleet at each product's next adoption.

- **A change is tried on one product before it is tagged.** `self-test` green,
  then the candidate applied to a scratch clone of a real product
  (`adopt --apply --allow-untagged`, `check`, `rehearse` when packaging moved),
  its CI green on a branch, and only then the signed tag. A tag pushed on a red
  trial cannot be undone.
- **`bin/maelys_cli.py` is vendored, never edited here.** It is
  `python/maelys_cli.py` of maelys-cli at `dependencies/maelys-cli.pin`, with
  its sha256 on line 3; `maelys-release vendor` refreshes it. The contract it
  implements is agent-cli/v2, pinned in `dependencies/agent-cli-spec.pin`, and
  `tests/test_contract_conformance.py` runs that repository's kit.
- **The managed texts carry no socle version** (`docs/conventions.md`,
  "Compatibility of the managed files"): a patch changes the workflows only, a
  minor may change a managed text and says so in the changelog, a major breaks
  the product contract.
- **Use `git -C /abs/path` for every git call**, never a `cd` chain: a script
  that changed directory once tagged the wrong repository.
- **`docs/cli.md` and `docs/cli-contract.json` are generated**, from
  `describe`, by maelys-cli's generator at `dependencies/maelys-cli.pin`, as
  the socle generates a product's. A change to a command changes them in the
  same commit; `check.yml` refuses a drift.
- This repository publishes no artifact. Its release is the signed tag alone,
  which products pin by commit in their `release.yml`.
