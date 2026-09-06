# Changelog

## 0.15.2 — 2026-09-06

- Two products releasing at the same time raced on `maelys-dev/homebrew-tap`:
  the publish job of `tap.yml` cloned, committed and pushed once, so the
  second push was rejected and two releases out of three needed a replay
  with `gh workflow run release.yml -f tag=vX.Y.Z`. The push now fetches,
  rebases the formula commit onto the tap's `main` and pushes again, three
  attempts at most, each logged. A rebase that conflicts (the same formula
  published twice at once, which the signed-tag model excludes) is aborted
  and the job fails. Identity and signing are set in the clone's
  configuration instead of on the commit command, so a rebased commit is
  signed like the original. `maelys-release tap --apply` retries the same
  way and reports `pushAttempts`; the self-test runs the step's shell
  function and the whole step against a fake tap that another publication
  moves first.
- The publish job of `tap.yml` runs in the concurrency group
  `tap-<tap repository>` with `cancel-in-progress: false`: the formula jobs
  of one release, or a replay during a release, wait for each other. The
  block sits on the job of the reusable workflow, where it covers every
  caller of a pin without touching the generated `release.yml`, and leaves
  the render and bottle jobs parallel. GitHub scopes a concurrency group to
  one repository and keeps one pending job per group, so across products
  the rebased push is the guarantee, and a third publication of one
  repository queued at once cancels the pending one, to be replayed.
- Only the workflows change: the `release.yml` that `adopt` generates and
  the managed texts are the same, so a product picks this up by moving its
  pin at its next re-adoption, as a patch.

## 0.15.1 — 2026-09-05

- Release packages are uploaded directly to a protected draft GitHub release,
  verified there, and then published. Release publication no longer depends on
  the repository's GitHub Actions artifact-storage quota.
- A `workflow_dispatch` replay now runs the complete release and Homebrew flow
  for an existing signed tag through the current socle, while building the
  requested tag rather than the default branch.

## 0.15.0 — 2026-09-05

- Linux checks, sanitizers, tag verification and publication now run on the
  GitHub-hosted Ubuntu 26.04 x86_64 and arm64 runners. The rehearsal image is
  `ubuntu:26.04`. This raises the shared build baseline past Ubuntu 24.04's
  Mbed TLS 2.28.8, which lacks the security fix required by current Maelys
  products.
- `maelys-cli` is pinned at v0.5.15 and the vendored Python framework is
  refreshed from that immutable commit. `agent-cli-spec` remains current at
  v2.2.0.
- The workflow supply-chain pins are refreshed to `actions/checkout` v7.0.1,
  `upload-artifact` v7.0.1, `download-artifact` v8.0.1,
  `attest-build-provenance` v4.2.2 and actionlint v1.7.12.
- The relocation test uses a fully qualified destination ref, so it also runs
  from the detached checkout used for pull requests.

## 0.14.2 — 2026-09-05

- The socle's `VERSION` of 0.12.0, 0.13.1, 0.14.0 and 0.14.1 ended with a
  literal backslash-n instead of a newline, so every file generated from
  those tags carried `(0.14.1\n)` in its header (found by maelys-json,
  planning its adoption from a `git archive` of v0.14.1); 0.13.0 was
  correct. `VERSION` is fixed, `socle_version` refuses anything but
  `X.Y.Z`, and the self-test reads the file's bytes. A product that
  adopted one of those tags regenerates its header at its next
  re-adoption.
- The same escape had dropped the changelog entries of 0.13.1, 0.14.0 and
  0.14.1; they are restored below, and the self-test now holds the socle
  to its own rule: a dated entry for `VERSION`.
- The refusal to run from an archive of a tag gives the complete command:
  `--socle-sha` with the tag's commit obtained by `git ls-remote`, and
  `--socle-tag`.

## 0.14.1 — 2026-09-05

- `check`, `preflight` and `rehearse` on a product pinned before 0.5.0
  (maelys-json, on 0.2.8) relocated to a socle that had no
  `bin/maelys-release` and failed on a missing file. They now say that the
  pinned socle predates the command and cannot answer for itself, and name
  the way out: `adopt DIR --apply` from a current socle at a tag.

## 0.14.0 — 2026-09-05

Breaking, without a transition: the declarations of a product live in
`dependencies/`, named after what they hold.

- `dependencies/<name>.pin` replaces `adapter/<NAME>_PIN`: the file is named
  after the repository (`maelys-system.pin`), line 1 the nearest tag, line 2
  the pinned commit; no more upper-casing in two scripts.
  `dependencies/packages` replaces `adapter/PACKAGES`, same content. The
  socle's own pins move the same way (`dependencies/agent-cli-spec.pin`,
  `dependencies/maelys-cli.pin`).
- A product with an `adapter/` directory is refused by `adopt`, `check` and
  `preflight` with the migration in the message: `git mv` the files, delete
  `adapter/`, update the Makefile lines that read the pins. Every product
  re-adopts anyway for the unstamped texts; this is the same re-adoption.
- The managed `scripts/checkout-dependency.sh` reads `dependencies/NAME.pin`;
  `check-product.yml` is unchanged, it reads the declarations through the
  pinned socle.

## 0.13.1 — 2026-09-05

Found by maelys-egress's adoption of 0.13.0, the first by a product that
did not take part in the socle's design.

- The product name defaults to the `product:` of the `release.yml` the
  product already carries before falling back to the directory name: from
  a git worktree or a scratch clone, `adopt`, `check` and `preflight` used
  the directory name, regenerated `release.yml` for that wrong product and
  reported a drift, and Egress had to pass `--product` by hand. The drift
  hint names the command with its directory.

## 0.13.0 — 2026-09-05

- agent-cli-spec pinned at v2.2.0 and maelys-cli at v0.5.14, `bin/maelys_cli.py`
  refreshed by `vendor`: the trial options `--socle-sha` and `--socle-tag`
  are hidden as the contract now allows, listed by `describe` with
  `hidden: true`, accepted by the parser, absent from the synopses, the
  help and the completion. The kit's checks of hidden options pass.

## 0.12.0 — 2026-09-05

- `bin/maelys-release` is built on `maelys_cli`, the Python framework that
  maelys-cli 0.5.12 ships (`python/maelys_cli.py`), instead of its own
  implementation of the agent-cli/v2 contract: the parser, the envelopes,
  `help`, `describe`, the completion and the causal order of refusals come
  from the framework; the socle keeps its business logic and its text
  renderers. About 500 lines gone, one implementation of the contract in
  Python left, held by maelys-cli's rule that the Python follows the C.
- The framework is vendored: `bin/maelys_cli.py` is `python/maelys_cli.py`
  at the commit `dependencies/maelys-cli.pin` names (then
  `adapter/MAELYS_CLI_PIN`), byte for byte, its digest on the pin's third
  line. `self-test` verifies the digest offline and the file at the pinned
  commit online; `maelys-release vendor` refreshes the copy after a pin
  bump. The socle stays one fetch of one commit, in every product's CI and
  in the relocation cache; no checkout of maelys-cli is needed anywhere.
- Visible consequences: `version` reports `framework: maelys_cli python
  3.x`; the synopses listed every option, so `--socle-sha` and
  `--socle-tag` appeared in `adopt`, `check`, `preflight` and `rehearse`
  until 0.13.0 hid them.

## 0.11.0 — 2026-09-05

- `adopt` refuses a socle commit without a tag: a product pins releases of
  the socle, and a commit past the tag (maelys-cli's sibling checkout was
  `v0.10.0-2-g9dccfe0`) has no changelog entry, no trial and no
  compatibility promise. The refusal names the commit and the way out:
  check out a tag, or `--allow-untagged` for the trial of a candidate,
  which is how the socle itself is tried on a product before its tag.

## 0.10.0 — 2026-09-05

Feedback from maelys-cli after four socle versions in three days: the
socle's speed had become the maintenance of every product.

- The managed texts carry no socle version any more: `AGENTS.md` and
  `CLAUDE.md` blocks, the skill and `scripts/checkout-dependency.sh` were
  stamped with the tag, so every socle tag changed them in every product
  and `check` called it a drift. The version now lives in the `uses:`
  lines of `release.yml` and `ci.yml` alone; a socle tag that changes no
  text changes nothing in a product. One last re-adoption removes the
  stamps; after it, patch versions of the socle never touch a managed
  text (conventions: "Compatibility of the managed files").
- `rehearse DIR TARGET --check` replays `make check` (`--check-command`
  overrides) on the Linux target in the container instead of packaging,
  so a Linux-only failure (`EFTYPE` on maelys-cli) is found before the
  push, not by CI.
- Golden tests of the text rendering of `check` and `preflight` in their
  three states (conformant, drifting, not ready): the defects found since
  0.5.0 were all in the text rendering, which the self-test only sampled.

## 0.9.0 — 2026-09-05

- The provenance attestation follows the repository's visibility:
  `release.yml` and `tap.yml` take an `attestation` input, `auto` by
  default, which attests on a public repository and skips the step on a
  private one, where GitHub reserves attestations to paid plans (`Feature
  not available for the maelys-dev organization`, maelys-oci v0.3.0, three
  builds failed after packaging). `always` and `never` force it. A private
  release keeps the signed tag, the `.sha256` files and `SHA256SUMS`.
- `preflight` notes a private repository and what its release will lack,
  before the tag.

## 0.8.0 — 2026-09-04

Feedback from the maelys-cli release on 0.6.1, with the sibling socle
checkout already at 0.7.0.

- `check`, `preflight` and `rehearse` answer as the socle the product pins:
  started from another checkout, they fetch the pinned commit once into
  `~/.cache/maelys-release/<sha>` (`MAELYS_GIT_BASE` overrides the origin)
  and re-execute themselves from it, with a note on stderr in text mode. A
  sibling checkout that moved on no longer blocks a release;
  `MAELYS_RELEASE_NO_RELOCATE=1` keeps the running socle, `--socle-sha`
  names one. `adopt` does not relocate: moving the pin is its purpose.
- `preflight` in text mode prints the check part as `check` does, drift
  lines included, so the cause of "not ready to tag" is on the terminal
  and not only in `data.violations`.
- Not a change: an exit 2 envelope carries `ok: true`. The contract says so
  (agent-cli-spec §8: "a validation correctly executed that found
  violations (the envelope has `ok: true` and reports them in `data`)"),
  as maelys-cli's `MAELYS_CLI_EXIT_VIOLATIONS` and `maelys-hello check` do.

## 0.7.0 — 2026-09-04

- `describe --summary --prefix PREFIX`, the filtered discovery form that
  agent-cli-spec v2.1.0 adds: the descriptors of one command namespace,
  `filter: {"kind": "command-prefix", "value": PREFIX}`, `INVALID_COMMAND`
  when nothing matches, `VALIDATION_FAILED` on a misuse. `describe` declares
  the option with its grammar, `requires` and `conflictsWith`, and its
  `input.constraints`. The pin moves to v2.1.0 (kit: 111 checks).
- The attestation of a release is signed by the socle's reusable workflow,
  so verifying it needs `--signer-repo maelys-dev/maelys-release`; the skill
  and the `RELEASING.md` template said `--repo` alone, which fails with
  "verifying with issuer sigstore.dev" (found on agent-cli-spec v2.1.0).

## 0.6.1 — 2026-09-04

- `check` from a socle fetched by commit alone, as `check-product.yml` does
  (a depth-1 fetch, no tags), regenerated every managed file with the label
  `untagged` and reported a drift on all of them: the first CI run of
  agent-cli-spec on 0.6.0. When the running commit is the pinned one, the
  label the product pins stands; the commit remains the pin of record.

## 0.6.0 — 2026-09-04

Feedback from the second adoption round (maelys-cli, three socle versions,
six tags): the two structural returns and the two small ones.

- `check-product.yml` no longer takes the declarations as inputs: its job
  fetches the socle pinned by `release.yml` (a depth-1 fetch by commit, no
  full clone), runs `maelys-release declarations` on the product and
  installs what it says. A `ci.yml` cannot drift from `release.yml` any
  more, and one created by 0.5.0 keeps working: the old inputs are
  accepted and ignored.
- `adopt` manages the socle line of a `ci.yml` the product owns
  (`uses: .../check-product.yml@SHA # TAG`), as it manages the block of
  `AGENTS.md`; `check` reports a `ci.yml` that calls no `check-product.yml`
  as a warning, counted as a violation (exit 2) without blocking `adopt`.
  maelys-cli's CI stayed on `scripts/adopt.sh` after adopting 0.5.0 and
  broke at the first check; this names it before the merge.
- `declarations DIR`: the product contract as data (dependencies, packages,
  formulas, checks), exit 2 when invalid.
- `tests/test_contract_conformance.py`: the agent-cli/v2 conformance of
  `bin/maelys-release`, checked by the kit of maelys-dev/agent-cli-spec at
  `adapter/AGENT_CLI_SPEC_PIN` (v2.0.0), the repository where the contract
  born in Hermes and made a framework by maelys-cli is now written once,
  with its schemas. No pin on maelys-cli: the socle and the framework both
  pin the specification.
- `adopt`, `check`, `preflight` and `rehearse` refuse a socle checkout with
  uncommitted changes in `share/`, `bin/` or `VERSION`: what it writes
  names a commit that does not produce it, and the product's CI then
  reports a drift it cannot explain (agent-cli-spec's first CI run).
- The sanitizers job of `check-product.yml` runs with `CC=clang
  CXX=clang++` and says so; it ran gcc's sanitizers under a clang name.
- README: how the socle itself is released, on one product first.

## 0.5.0 — 2026-09-03

The rest of the maelys-oci feedback, and the socle's scripts become one
command of the agent-cli/v2 contract.

- `bin/maelys-release`, in Python (standard library, 3.9 or later),
  replaces `scripts/adopt.sh`, `rehearse.sh`, `render-formula.sh`,
  `update-tap.sh` and `self-test.sh` with the commands `adopt`, `check`,
  `preflight`, `rehearse`, `render`, `tap` and `self-test`, plus `help`,
  `version`, `describe`, `completion` and `__complete` as maelys-cli
  defines them. One catalog drives the parser, the help, `describe` and the
  completion; `--format json` renders an envelope on stdout, failures an
  envelope on stderr with a stable code and a hint; exit 0, 1 or 2 (a
  validation that found violations: `check` on drift, `preflight` when the
  tag would be refused). `adopt` and `tap` plan by default and write with
  `--apply`; `--dry-run` is refused as the contract requires. Breaking for
  products: their CI steps call `bin/maelys-release check . --product NAME`
  instead of `scripts/adopt.sh . --check`.
- `check` refuses to run from a socle other than the one `release.yml`
  pins: upgrading is `adopt --apply` from the new socle, never an
  accidental regeneration by whichever checkout is at hand.
- `rehearse DIR TARGET` replays the build job of `release.yml` for
  `linux-x86_64` or `linux-arm64` in an `ubuntu:24.04` container: socle
  and declared packages, pinned checkouts through the managed
  `scripts/checkout-dependency.sh`, `package-release.sh TARGET`, on a copy
  of the working tree; `dist/` receives the artifacts. Verified on
  maelys-oci 0.2.0 (linux-arm64, native).
- `check-product.yml`, a reusable CI workflow from the same declarations:
  checkouts, packages, `make check` on the three release targets, the
  sanitizers on Linux x86_64 (`sanitizer_command`), and the socle drift
  check against the version `release.yml` pins. `adopt` creates
  `.github/workflows/ci.yml` calling it when the product has none; the
  file then belongs to the product.
- Conventions: a formula depending on a sibling formula is published after
  it, in the order of the `adapter/*_PIN` graph; until then the product
  ships without a template.
- `tests/test_maelys_release.py` (18 tests) covers the contract surface,
  the refusals, the generated files, the checkout against a local bare
  repository, the preflight with an SSH-signed tag, `render` and `tap`
  against a local tap. The socle's CI runs it on Linux and macOS.

## 0.3.0 — 2026-09-03

Feedback from the adoption by maelys-oci: everything that failed there was
upstream of the tag (declarations, checkouts, preconditions), not in the
release itself.

- `adapter/PACKAGES` declares the apt (`[linux]`) and brew (`[macos]`)
  packages a build needs; `adopt.sh` emits them as `linux_packages` (after
  the socle's packaging tools) and `macos_packages`, and `--check` covers
  them. maelys-oci had to install jansson, libarchive, e2fsprogs and Mbed
  TLS from a fake checkout script.
- Dependency checkouts are generated: `adopt.sh` reads `adapter/*_PIN`,
  installs the managed `scripts/checkout-dependency.sh NAME` and writes one
  `dependency_checkout` line per pin. A product-written
  `scripts/checkout-*.sh` is refused (four identical ones in maelys-oci,
  diverging already on tag versus commit checkout); a pin whose line 2 is
  not a commit is refused. Breaking: delete the product's checkout scripts,
  point the product's CI at `scripts/checkout-dependency.sh NAME`.
- `adopt.sh` requires a dated `## X.Y.Z` entry in `CHANGELOG.md` for
  `VERSION`, so `make check` fails before the tag would.
- `adopt.sh DIR --preflight`: `--check`, then `tag.gpgsign` and
  `user.signingkey`, the previous `v*` tag annotated and signed, `vX.Y.Z`
  free, the `release` environment limiting deployments to tags `v*` (with
  `gh`); exit 3. Until now each of these was discovered by a failed
  workflow. Presence of the environment is not enough: GitHub creates it
  without rules on first use, and the five adopted repositories had it
  that way or not at all.
- Documentation: `render_command` receives `TAG OUTPUT NAME` in the README
  as in the conventions; the README no longer mentions an
  `extra_placeholders` input that `tap.yml` does not have; the adoption
  snippet points at `vX.Y.Z`; the conventions say `package-release.sh` may
  rebuild from clean. The skill and the managed block describe the
  declarations and forbid installing packages from scripts.
- `scripts/self-test.sh` covers the refusals, the generated lines, the
  checkout against a local bare repository and the preflight against a
  fixture repository with an SSH-signed tag.

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
