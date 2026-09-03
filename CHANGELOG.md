# Changelog

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
