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
- `CHANGELOG.md` has one dated `## X.Y.Z — YYYY-MM-DD` entry per release;
  an `## Unreleased` section collects what the next one will carry.
- A published tag is never moved, deleted or force-pushed. A mistake is
  fixed by the next patch release.

## Dependencies

- A dependency on another Maelys repository is pinned by commit in an
  `adapter/<NAME>_PIN` file, verified by the build (`git rev-parse HEAD`
  equal to the pin, no local modification of the contract paths), and
  fetched in CI by a `scripts/checkout-<name>.sh` that clones and checks out
  the pin. Tags are recorded next to the commit for humans.
- The release workflow and any third-party action are pinned by full commit
  SHA with the tag in a trailing comment. `adopt.sh` writes the socle pin.

## Packaging

- `scripts/package-release.sh TARGET` builds one target (`linux-x86_64`,
  `linux-arm64`, `macos-arm64`) and leaves every artifact with its
  `.sha256` in `dist/`. It must run on a developer machine without CI.
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
  `adopt.sh` writes one tap job each, so a repository may publish a command
  and a library. A product renderer receives `TAG OUTPUT NAME`.
- The formula template `packaging/homebrew/<name>.rb.in` lives in the
  product repository and is rendered from the released tag's copy, so the
  formula and the released source cannot drift. Dependency pins are copied
  from the tag's `adapter/` files by the product's renderer.
- Open products keep a source URL with its sha256; bottles are an addition
  and `brew install --build-from-source` always works. Closed products ship
  bottles only, from a private repository, and say so in their formula.
- The tap is `maelys-dev/homebrew-tap`; it is updated only by the release
  workflows or by `scripts/update-tap.sh`, with signed commits.

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

## Agents

`adopt.sh` installs a managed block in `AGENTS.md` and `CLAUDE.md` and a
Claude skill under `.claude/skills/maelys-release/`; they carry the rules
above in the form an agent needs. Products may add their own rules outside
the managed block.

## Adopting and upgrading

```sh
git clone https://github.com/maelys-dev/maelys-release && git -C maelys-release checkout vX.Y.Z
maelys-release/scripts/adopt.sh /path/to/product            # plan
maelys-release/scripts/adopt.sh /path/to/product --apply    # write
maelys-release/scripts/adopt.sh /path/to/product --check    # exit 2 on drift
```

`--check` belongs in the product's `make check` and in the fleet drift
check of maelys-platform.
