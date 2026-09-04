# Release conventions of the Maelys repositories

Normative for every repository that adopts maelys-release. A product keeps
its own `RELEASING.md` for what is specific to it and links here for the
rest.

## Versions, tags, changelog

- `VERSION` holds `X.Y.Z` and nothing else. A release is the tag `vX.Y.Z`
  on the `main` commit whose `VERSION` says `X.Y.Z`.
- The tag is annotated and signed with a key registered on GitHub; the
  release workflow verifies it through the API and refuses lightweight,
  unsigned or unverified tags, and tags that do not name the workflow commit.
  `maelys-release preflight DIR` checks the signing configuration and the
  previous tag before the new one exists.
- `CHANGELOG.md` has one dated `## X.Y.Z — YYYY-MM-DD` entry per release;
  an `## Unreleased` section collects what the next one will carry. The
  entry for `VERSION` must exist: `maelys-release check` reports a product
  without it, so `make check` fails before the tag does.
- A published tag is never moved, deleted or force-pushed. A mistake is
  fixed by the next patch release.

## Dependencies and packages

- A dependency on another Maelys repository is pinned by commit in an
  `adapter/<NAME>_PIN` file, `NAME` being the repository name upper-cased
  with underscores (`MAELYS_SYSTEM_PIN`): the nearest tag on line 1 for
  humans, the pinned commit on line 2. The build verifies the checkout
  (`git rev-parse HEAD` equal to the pin, no local modification of the
  contract paths).
- The checkout is the managed `scripts/checkout-dependency.sh NAME`, written
  by `maelys-release adopt` from the pins; it clones `maelys-dev/NAME` next to the
  product at the pinned commit. The release workflow, the product's CI and
  developers run that one script. A product writes no `scripts/checkout-*.sh`
  of its own; `adopt` refuses them.
- The system packages the build needs on the runners are declared in
  `adapter/PACKAGES`, one per line under `[linux]` (apt names) or `[macos]`
  (brew names). `adopt` emits them into the release workflow after the
  socle's own packaging tools; nothing else installs packages during a
  release.
- The release workflow and any third-party action are pinned by full commit
  SHA with the tag in a trailing comment. `adopt` writes the socle pin;
  `check`, `preflight` and `rehearse` run as the pinned socle wherever they
  are started from, fetching it into the user's cache when the checkout at
  hand is another version.

## Packaging

- `scripts/package-release.sh TARGET` builds one target (`linux-x86_64`,
  `linux-arm64`, `macos-arm64`) and leaves every artifact with its
  `.sha256` in `dist/`. It must run on a developer machine without CI. It
  may rebuild from clean (`make clean`) to package exactly what the commit
  builds; a developer runs it expecting that rebuild.
- Every artifact gets a provenance attestation; the release lists them with
  a `SHA256SUMS`.

## Homebrew

- Formula names follow what they install: a command is named after its
  binary (`maelys`, `maelys-egress`), a library after its archive with a
  `lib` prefix (`libmaelys-sys`, `libmaelys-cli`, `libmaelys-json`). A name
  never describes a role (`-dev`, `-sdk`, `-tools`).
- A user-facing formula installs no build tools, libraries or headers it
  does not need at run time; a library formula installs the archive, headers
  and pkg-config file. Build-time dependencies are declared `=> :build`.
- A repository publishes one formula per `packaging/homebrew/<name>.rb.in`;
  `adopt` writes one tap job each, so a repository may publish a command
  and a library. A product renderer receives `TAG OUTPUT NAME`.
- The formula template `packaging/homebrew/<name>.rb.in` lives in the
  product repository and is rendered from the released tag's copy, so the
  formula and the released source cannot drift. Dependency pins are copied
  from the tag's `adapter/` files by the product's renderer.
- Open products keep a source URL with its sha256; bottles are an addition
  and `brew install --build-from-source` always works. Closed products ship
  bottles only, from a private repository, and say so in their formula.
- The tap is `maelys-dev/homebrew-tap`; it is updated only by the release
  workflows or by `maelys-release tap --apply`, with signed commits.
- A formula that depends on another Maelys formula (`depends_on
  "libmaelys-sys"`) is published after it: the bottle job resolves
  `depends_on` from the shared tap, so the dependency's formula must be in
  the tap before the dependent's first tap job. Until every sibling
  dependency has its formula, the product ships no
  `packaging/homebrew/*.rb.in`; its release still publishes the archives,
  and the formula follows in a later patch release. The order is the
  dependency graph of `adapter/*_PIN` (maelys-json, then maelys-system and
  maelys-cli, then maelys-http, then maelys-egress and maelys-oci).

## Continuous integration

- The product's `ci.yml` calls `check-product.yml` of the socle at the
  version pinned by `release.yml`; that line is the only one `adopt`
  manages in a `ci.yml` the product owns, and `check` warns when no job
  calls it. The job reads the declarations from `adapter/` through the
  pinned socle, so `ci.yml` repeats nothing: same checkouts, same packages,
  `make check` on the three release targets, sanitizers with clang, socle
  drift. Its other jobs are the product's own.
- Before the first tag of a product, and after any change to
  `adapter/PACKAGES` or `package-release.sh`, `maelys-release rehearse DIR
  TARGET` replays the Linux build job in Docker.

## Runners

- Runner inputs are JSON: a label string or a label array.
- Public repositories use GitHub-hosted runners only.
- A self-hosted runner is reserved for hardware gates, registered per
  repository, used only on signed tags or `workflow_dispatch`, behind the
  `release` environment, never on `pull_request`.

## Secrets

- `HOMEBREW_TAP_TOKEN`: write access to the tap, organization secret scoped
  to the repositories that publish formulas.
- `HOMEBREW_TAP_SIGNING_KEY`: SSH private key whose public half is registered
  on GitHub, used to sign tap commits.
- No repository holds a credential or a private key.
- The `publish` job runs in the `release` environment of the repository,
  whose deployment policy is limited to tags `v*`: a `workflow_dispatch`
  from a branch or a `release.yml` edited on a branch cannot publish. GitHub
  creates a missing environment without any rule, so `preflight` checks
  the tag rule, not the presence. Set it once per repository:

  ```bash
  gh api -X PUT repos/OWNER/REPO/environments/release \
    --input - <<<'{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
  gh api -X POST repos/OWNER/REPO/environments/release/deployment-branch-policies \
    -f name='v*' -f type=tag
  ```

## Agents

`adopt` installs a managed block in `AGENTS.md` and `CLAUDE.md` and a
Claude skill under `.claude/skills/maelys-release/`; they carry the rules
above in the form an agent needs. Products may add their own rules outside
the managed block.

## Adopting and upgrading

```sh
git clone https://github.com/maelys-dev/maelys-release && git -C maelys-release checkout vX.Y.Z
maelys-release/bin/maelys-release adopt /path/to/product            # plan
maelys-release/bin/maelys-release adopt /path/to/product --apply    # write
maelys-release/bin/maelys-release check /path/to/product            # exit 2 on any violation
maelys-release/bin/maelys-release preflight /path/to/product        # exit 2 when the tag would be refused
```

The command follows the agent-cli/v2 contract (maelys-dev/agent-cli-spec,
pinned in `adapter/AGENT_CLI_SPEC_PIN` for its conformance kit): `describe
--format json` returns its catalog, every command renders a JSON envelope
with `--format json`, failures are envelopes on stderr. `check` belongs in
the product's `make check` and in the fleet drift check of maelys-platform;
`preflight` is the first step of a release, on the machine that will sign
the tag.

## Replaying a tag's Homebrew publication

The generated caller workflow also accepts `workflow_dispatch` with a `tag`
input: it skips the release job and runs the tap jobs again for that
existing signed tag, from the workflow definition of the default branch. Use
it after adopting a corrected socle when a tag's release was published but
its formula or bottles were not; never re-tag for that.

```bash
gh workflow run release.yml --repo maelys-dev/PRODUCT -f tag=vX.Y.Z
```
