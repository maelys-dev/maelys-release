# Changelog

## 0.2.9 — 2026-09-03

- The managed AGENTS.md/CLAUDE.md block and the Claude skill name the
  repository's actual formula templates (`packaging/homebrew/<name>.rb.in`
  for each template found, rendered through `@FORMULAS@`) instead of
  `packaging/homebrew/<product>.rb.in`, which does not exist when the
  formula is named after what it installs (`libmaelys-sys` in
  maelys-system).

## 0.2.8 — 2026-09-03

- The publish job of `tap.yml` styles the merged formula inside the staging
  tap instead of the loose copy: with the same class present in the staging
  tap, `brew style` on the loose file fails on `Lint/DuplicateMethods`
  (maelys-egress v0.13.1). Reproduced and verified locally.

## 0.2.7 — 2026-09-03

- The publish job of `tap.yml` no longer taps the shared tap before merging
  the bottle digests: with the product's previous formula in the shared tap,
  `brew style` saw its class twice and failed on `Lint/DuplicateMethods`
  (maelys-system v0.5.4). Only the bottle job needs the shared tap, to
  resolve dependencies.
- `scripts/update-tap.sh` accepts pre-release tags (`v0.1.0-alpha.3`).

## 0.2.6 — 2026-09-03

- `tap.yml` taps the shared tap before building bottles and merging their
  digests, so a formula that depends on another Maelys formula
  (`maelys-egress` on `libmaelys-sys`, `libmaelys-cli` on `libmaelys-json`)
  resolves it; both failed with "No available formula" on their first run.
- The generated caller workflow accepts `workflow_dispatch` with a `tag`
  input that replays the tap jobs of an existing signed tag and skips the
  release job, so a corrected socle publishes the formula of a release that
  already exists without a new tag.

## 0.2.5 — 2026-09-03

- The publish job writes `SHA256SUMS` from the archives that exist instead
  of expanding `*.deb` and `*.rpm` literally: a product that ships tarballs
  only (maelys-json v0.1.1, maelys-cli v0.5.3) failed there after its
  builds succeeded.

## 0.2.4 — 2026-09-03

- `tap.yml` trusts its local staging tap before loading the formula:
  recent Homebrew refuses formulas from untrusted taps ("Refusing to load
  formula maelys-dev/staging/libmaelys-sys from untrusted tap"), which
  failed the publish job of maelys-system v0.5.3 after the bottles were
  built and attached to the release.

## 0.2.3 — 2026-09-03

- The generated `tap-<formula>` jobs are granted `id-token` and
  `attestations` as well as `contents`: the `bottle` job of `tap.yml`
  attests the bottles, and GitHub refuses a reusable workflow whose nested
  job requests more than the calling job was granted (maelys-system v0.5.2
  failed at startup on that rule).

## 0.2.2 — 2026-09-03

- The generated caller workflow declares `contents`, `id-token` and
  `attestations` write permissions at the top level: GitHub refuses a job
  calling a reusable workflow with more permissions than its workflow
  declares (`is requesting 'attestations: write, id-token: write', but is
  only allowed ... none`), which made the first real release start-fail.

## 0.2.1 — 2026-09-03

- `adopt.sh` writes one tap job per `packaging/homebrew/*.rb.in`, named
  after the formula, so a repository can publish a command and a library or
  a formula named differently from the repository (`libmaelys-sys`). The
  product renderer receives the formula name as a third argument.

## 0.2.0 — 2026-09-03

- `scripts/adopt.sh DIR [--apply|--check]`: writes the product's
  `release.yml` from the socle version it runs from, installs the
  maelys-release managed block in `AGENTS.md` and `CLAUDE.md`, the Claude
  skill and a `RELEASING.md` template; `--check` exits 2 on drift.
  `scripts/self-test.sh` exercises it and runs in the socle's CI with
  actionlint and shellcheck.
- `tap.yml` builds bottles on the macOS runners named by the `bottles`
  input, attests them, attaches them to the GitHub release and merges their
  digests into the formula before publishing it. The formula keeps its
  source URL.
- `docs/conventions.md`: versions and signed tags, dependency pins, packaging
  contract, formula naming (a command after its binary, a library with a
  `lib` prefix), open products with source plus bottles versus closed
  products with bottles only, runner and secret policies.
- `share/` texts are CC0-1.0 like maelys-cli's.

## 0.1.1 — 2026-09-02

- The tap workflow fails explicitly on an unrendered placeholder.

## 0.1.0 — 2026-09-02

- Reusable `release.yml` and `tap.yml` workflows and the matching scripts.
