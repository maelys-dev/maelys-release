# maelys-release

Shared release mechanics of the Maelys repositories: one reusable release
workflow, one reusable Homebrew tap workflow and the scripts they run. A
product repository keeps only what is specific to it: its `VERSION`, its
`scripts/package-release.sh`, its `packaging/homebrew/<product>.rb.in`
template and the checkout scripts of its pinned dependencies.

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

## Consuming the release workflow

```yaml
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  release:
    uses: maelys-dev/maelys-release/.github/workflows/release.yml@<sha> # vX.Y.Z
    permissions:
      contents: write
      id-token: write
      attestations: write
    with:
      product: maelys-egress
      dependency_checkout: |
        scripts/checkout-system.sh
        scripts/checkout-cli.sh
      linux_packages: build-essential dpkg-dev file rpm
      macos_runner: '"macos-15"'      # JSON: or '["self-hosted","macOS","ARM64","maelys-release"]'
  tap:
    needs: release
    uses: maelys-dev/maelys-release/.github/workflows/tap.yml@<sha> # vX.Y.Z
    with:
      product: maelys-egress
      # Products whose formula copies dependency pins supply their renderer:
      render_command: scripts/render-homebrew-formula.sh TAG OUTPUT
    secrets:
      tap_token: ${{ secrets.HOMEBREW_TAP_TOKEN }}
      tap_signing_key: ${{ secrets.HOMEBREW_TAP_SIGNING_KEY }}
```

The product's `scripts/package-release.sh TARGET` must leave its artifacts
and `.sha256` files in `dist/`. Pin the workflow by commit SHA, as for any
third-party action, and record the tag in a comment.

## Runners

`macos_runner`, `linux_x86_64_runner` and `linux_arm64_runner` are JSON
inputs: a label string, or a label array for a self-hosted runner.
GitHub-hosted runners are the default and the recommendation for public
repositories, where a self-hosted runner would execute code from any pull
request. A self-hosted Mac Studio (labels `self-hosted,macOS,ARM64,...`) is
appropriate only for jobs triggered by a signed tag or `workflow_dispatch`,
behind the `release` environment, on an ephemeral runner.

## Homebrew tap

`tap.yml` renders `packaging/homebrew/<product>.rb.in` from the *tag's* copy
of the template so that the formula and the released source cannot drift,
replaces `@VERSION@`, `@URL@`, `@SHA256@` and any `@NAME@` placeholder
passed through `extra_placeholders`, runs `brew style`, and pushes a signed
commit to `maelys-dev/homebrew-tap`. When the secrets are absent the job
prints a notice and succeeds, so a fork or a first release without tap
credentials does not fail the whole release.

Formulas build from the released source; prebuilt binaries stay on the
GitHub release. Bottles are a later, opt-in job.

## Scripts

- `scripts/render-formula.sh TEMPLATE OUTPUT KEY=VALUE...` replaces
  `@KEY@` placeholders and refuses unrendered ones.
- `scripts/update-tap.sh PRODUCT TAG FORMULA_PATH` is the same publication
  step for a developer machine: clones the tap, copies the rendered formula,
  runs `brew style`, commits with the configured signing key and pushes;
  `DRY_RUN=1` prints the diff instead. The workflows inline these steps so a
  release depends on nothing but the caller's repository.

MPL-2.0.
