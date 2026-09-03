# maelys-release

Shared release mechanics of the Maelys repositories: one reusable release
workflow, one reusable Homebrew tap workflow and the scripts they run. A
product repository keeps only what is specific to it: its `VERSION`, its
`CHANGELOG.md`, its `scripts/package-release.sh`, its `adapter/` pins and
packages, and its `packaging/homebrew/<name>.rb.in` templates.

```text
product repository                      maelys-release
  .github/workflows/release.yml  --->   .github/workflows/release.yml (workflow_call)
     on: push tags v*                      verify signed tag + VERSION
     uses: maelys-release/...              build matrix (Linux x86_64, arm64, macOS arm64)
                                           attest provenance, publish GitHub release
                                        .github/workflows/tap.yml (workflow_call)
                                           render formula from the tag's template
                                           brew style, signed commit to homebrew-tap
```

## Adopting the socle

```sh
git clone https://github.com/maelys-dev/maelys-release && git -C maelys-release checkout vX.Y.Z
maelys-release/scripts/adopt.sh /path/to/product              # plan
maelys-release/scripts/adopt.sh /path/to/product --apply      # write the managed files
maelys-release/scripts/adopt.sh /path/to/product --check      # exit 2 on drift; add it to make check
maelys-release/scripts/adopt.sh /path/to/product --preflight  # --check, then the tag preconditions; exit 3
```

`adopt.sh` reads the product contract and writes the managed files from it:

| Product declares | `adopt.sh` checks | `adopt.sh` writes |
| --- | --- | --- |
| `VERSION` as `X.Y.Z` | a dated `## X.Y.Z` entry in `CHANGELOG.md` | |
| `scripts/package-release.sh TARGET` | executable | |
| `adapter/<NAME>_PIN` (tag, commit) | line 2 is a commit; no product `checkout-*.sh` | `scripts/checkout-dependency.sh`, one `dependency_checkout` line each |
| `adapter/PACKAGES` (`[linux]`, `[macos]`) | one package per line in a known section | `linux_packages`, `macos_packages` |
| `packaging/homebrew/<name>.rb.in` | | one `tap-<name>` job each |
| `scripts/render-homebrew-formula.sh` | executable | `render_command: ... TAG OUTPUT <name>` |

The managed files are `.github/workflows/release.yml`,
`scripts/checkout-dependency.sh`, the maelys-release block of `AGENTS.md`
and `CLAUDE.md`, the Claude skill and, when absent, `RELEASING.md`. None of
them is edited by hand; `--check` fails on any drift, and on a product
whose declarations no longer match the generated workflow.

### Dependencies and packages

A dependency on another Maelys repository is one `adapter/<NAME>_PIN` file:
the nearest tag on line 1 for humans, the pinned commit on line 2. `NAME` is
the repository name upper-cased with underscores (`MAELYS_SYSTEM_PIN` for
`maelys-system`). The managed `scripts/checkout-dependency.sh maelys-system`
clones it next to the product at that commit; the release workflow, the
product's CI and a developer all run the same script.

The packages the build needs on the runners are declared in
`adapter/PACKAGES`, one per line under `[linux]` (apt) or `[macos]` (brew):

```ini
[linux]
pkg-config
libjansson-dev
libarchive-dev

[macos]
jansson
libarchive
```

The socle's own Linux packaging tools (`build-essential dpkg-dev file rpm`)
are always installed in front of them.

### Preflight

`--preflight` runs `--check`, then what the release workflow will demand of
the next tag, on the developer machine before it exists: `tag.gpgsign` and a
`user.signingkey`, the previous `v*` tag annotated and signed, `vX.Y.Z` not
yet taken, and, when `gh` is available, the `release` environment of the
GitHub repository limiting its deployments to tags `v*`. GitHub creates a
missing environment on first use without any rule, so presence alone is
not checked. It exits 3 on the first refusal the workflow would otherwise
report.

## Consuming the release workflow

The generated workflow, for a product with two pins, packages and one
formula:

```yaml
name: release
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
    inputs:
      tag: { description: Existing signed tag whose Homebrew publication is replayed, required: true, type: string }
permissions:            # the ceiling; a calling job cannot exceed it
  contents: write
  id-token: write
  attestations: write
jobs:
  release:
    if: github.event_name == 'push'
    uses: maelys-dev/maelys-release/.github/workflows/release.yml@<sha> # vX.Y.Z
    permissions:
      contents: write
      id-token: write
      attestations: write
    with:
      product: maelys-egress
      dependency_checkout: |
        sh scripts/checkout-dependency.sh maelys-cli
        sh scripts/checkout-dependency.sh maelys-system
      linux_packages: build-essential dpkg-dev file rpm pkg-config libmbedtls-dev
      macos_packages: mbedtls
  tap-maelys-egress:
    needs: release
    if: always() && (needs.release.result == 'success' || needs.release.result == 'skipped')
    uses: maelys-dev/maelys-release/.github/workflows/tap.yml@<sha> # vX.Y.Z
    permissions:
      contents: write
      id-token: write
      attestations: write
    with:
      product: maelys-egress
      tag: ${{ inputs.tag }}
      # Products whose formula copies dependency pins supply their renderer:
      render_command: sh scripts/render-homebrew-formula.sh TAG OUTPUT maelys-egress
      bottles: '["macos-15","macos-26"]'   # [] disables bottles
    secrets:
      tap_token: ${{ secrets.HOMEBREW_TAP_TOKEN }}
      tap_signing_key: ${{ secrets.HOMEBREW_TAP_SIGNING_KEY }}
```

The product's `scripts/package-release.sh TARGET` must leave its artifacts
and `.sha256` files in `dist/`. The workflow is pinned by commit SHA, as
for any third-party action, with the tag in a comment. Other inputs of
`release.yml` (`package_command`, the three runner inputs,
`release_environment`) keep their defaults in the generated file.

## Runners

`macos_runner`, `linux_x86_64_runner` and `linux_arm64_runner` are JSON
inputs: a label string, or a label array for a self-hosted runner.
GitHub-hosted runners are the default and the recommendation for public
repositories, where a self-hosted runner would execute code from any pull
request. A self-hosted Mac Studio (labels `self-hosted,macOS,ARM64,...`) is
appropriate only for jobs triggered by a signed tag or `workflow_dispatch`,
behind the `release` environment, on an ephemeral runner.

## Homebrew tap

`tap.yml` renders `packaging/homebrew/<name>.rb.in` from the *tag's* copy
of the template so that the formula and the released source cannot drift,
replaces `@VERSION@`, `@URL@` and `@SHA256@`, runs `brew style`, and pushes
a signed commit to `maelys-dev/homebrew-tap`. A product whose formula
carries dependency pins renders it itself through `render_command`, which
receives `TAG OUTPUT NAME`. When the secrets are absent the job prints a
notice and succeeds, so a fork or a first release without tap credentials
does not fail the whole release.

Formulas build from the released source. With `bottles`, the tap workflow
also builds a bottle per listed macOS runner, attests it, attaches it to the
GitHub release and merges its digest into the formula, so `brew install`
takes the bottle when the platform matches and compiles otherwise;
`--build-from-source` always works for an open product. Closed products ship
bottles only, from a private repository.

Conventions for versions, tags, pins, packages, packaging, formula names,
runners and secrets are normative in [docs/conventions.md](docs/conventions.md).

## Scripts

- `scripts/adopt.sh DIR [--apply|--check|--preflight]` as above;
  `scripts/self-test.sh` exercises it on a fixture and runs in CI.
- `share/templates/checkout-dependency.sh` is the checkout script
  `adopt.sh` installs into products that pin dependencies.
- `scripts/render-formula.sh TEMPLATE OUTPUT KEY=VALUE...` replaces
  `@KEY@` placeholders and refuses unrendered ones.
- `scripts/update-tap.sh PRODUCT TAG FORMULA_PATH` is the same publication
  step for a developer machine: clones the tap, copies the rendered formula,
  runs `brew style`, commits with the configured signing key and pushes;
  `DRY_RUN=1` prints the diff instead. The workflows inline these steps so a
  release depends on nothing but the caller's repository.

Code is MPL-2.0; `share/` texts and scripts installed into consumer
repositories are CC0-1.0 (`share/LICENSE`).

## Replaying a tag's Homebrew publication

The generated caller workflow also accepts `workflow_dispatch` with a `tag`
input: it skips the release job and runs the tap jobs again for that
existing signed tag, from the workflow definition of the default branch. Use
it after adopting a corrected socle when a tag's release was published but
its formula or bottles were not; never re-tag for that.

```bash
gh workflow run release.yml --repo maelys-dev/PRODUCT -f tag=vX.Y.Z
```
