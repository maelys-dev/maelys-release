# Changelog

## 0.41.0 — 2026-09-12

- **A product declares the runner of the socle's macOS jobs.** `[runners]`
  holds one `macos LABEL...` line; a single label renders as a string and
  several as a label array, which is what `runs-on` takes. Until now the
  `render` and `publish` jobs of `tap.yml` were fixed on `macos-15` and the
  matrix of `check-product.yml` was written into the workflow with no input
  at all. The `build` job of `release.yml` was already a product's to
  declare, through a `[targets]` line naming labels, since 0.24.0.
- **`adopt` writes the line, and takes it away again.** The declaration
  reaches `release.yml`, which the socle owns, and `adopt` also maintains
  the one line it governs inside the `with:` of the job calling
  `check-product.yml` in `ci.yml` — the second thing the socle keeps current
  in a file that is otherwise the product's, after the `uses:` line.
  Removing the section removes the line, so the file follows the declaration
  in both directions; `check` reports the gap until the next adoption.
- **Honoured on a private repository only, and that is not a preference.** A
  self-hosted runner executes what the workflow checks out, on a machine
  somebody owns, and `check-product.yml`'s `check` job is reachable from a
  pull request. On a public repository a pull request carries anybody's
  code, so a public repository runs `macos-15` whatever is declared. The
  socle does not let a declaration put an outsider's code on a private
  machine.
- This resumes [#8](https://github.com/maelys-dev/maelys-release/pull/8),
  open since 6 September and 124 commits behind, whose gating condition had
  emptied under it: it moved the tag-triggered macOS legs to a self-hosted
  runner on a private repository, and named maelys-oci as the only private
  repository it would reach. maelys-oci is public now, and no private
  repository uses the socle's `release.yml` or `tap.yml` at all. What that
  pull request got right is kept whole — a self-hosted runner must never run
  a pull request's code — and what it hardcoded is declared instead, which
  is what was asked of the socle for branch protection, CI triggers and
  runners alike.

## 0.40.1 — 2026-09-12

- **A carrier that has stopped carrying the version is a note, not a
  refusal.** `bump_audit` wrote two messages and reached one verdict. The
  two readings do not weigh the same: a file that **still holds the old
  version** is an omission and nothing else explains it, so the release
  stops; a file that holds **neither** version may be a carrier this cut
  forgot, or one that has simply stopped naming a version, and the two read
  identically. Refusing there blocked a release with nothing downstream to
  lift it, because `[cut]` declares a command and not a list of files: the
  operator's only ways out were to make a file carry a version it should no
  longer carry, or to cut outside the tool.
- This is the twin of the limit 0.40.0 documented, and the dangerous half.
  A place the version *reaches* since the previous bump is invisible — a
  missed detection the product's own checks still catch. A place the version
  *leaves* was a false refusal. maelys-cli found it by measuring **all
  thirty bumps of their history**: twenty-nine were made by hand and every
  one of them is clean, including commits touching thirty, twenty-eight and
  twenty-five files, so the worry that a bump not made by `cut` would give a
  noisy baseline was the wrong worry. The one wide baseline came from a
  generated CLI reference that carried the version at `0.1.0 -> 0.2.0` and
  carries none today. They read the split out of the socle's own code before
  the socle did.
- `docs/conventions.md` states that one document per target is the rule
  rather than a limitation of `actions/attest`. SPDX repeats `DESCRIBES` and
  `documentDescribes` is an array, so a target that leaves a `tar.gz`, a
  `deb` and an `rpm` writes one document with three entries and the resolver
  already takes all three as subjects. Two tests cover it, one of which was
  a real gap: a wrong digest among several still stops the release. The
  matrix escape hatch for several documents is written in the conventions
  and deliberately not in the workflow, because no product needs it and a
  mechanism with no user is a mechanism nobody verifies. maelys-http made
  both points.

## 0.40.0 — 2026-09-12

- **`cut` audits its own write before it creates a branch.** It wrote
  `VERSION`, ran `[cut] after-version` when one was declared, and nothing
  read the result until the pull request's own checks did — after a branch
  was pushed and a pull request opened, and only where the product compares
  the two at all. `cut` now takes the last commit that moved `VERSION` and
  the files whose diff there dropped the old version and added the new one,
  and requires this cut to have moved the version in the same ones. The
  conjunction is what makes it usable: `CHANGELOG.md` adds the new version
  without dropping the old, so it falls out, and so does every file an
  adoption changed in the same commit — on a maelys-json release commit that
  touched eight files it leaves `VERSION` and one header.
- It is `cut` auditing itself, not the socle judging the product twice: the
  product contract stays with the checks `cut` waits for, on the exact
  commit. A product that has never released has nothing to compare with and
  the run says so; a carrier that has left the tree is a note. It lags by one
  release, and a place the version reached since the previous bump is
  invisible — the plan says what it found before anything is written, so the
  warning arrives before the branch.
- Reported by maelys-cli, from a `VERSION` and a header compared by `make
  check-version`. Two products were one cut away from it and not in the same
  way: **maelys-system** compares the two, so its next cut would have failed
  late; **maelys-egress** compares nothing, so its next cut would have gone
  green to the tag and published node and python SDKs whose `package.json`
  and `pyproject.toml` announced the previous version. Neither declares
  `[cut] after-version` today.
- The version is matched as a version and not as a substring of one. A digit
  or a dot on the left disqualifies, so `0.1.1` does not match inside
  `10.1.1`; on the right only a dot **followed by a digit** disqualifies, so
  it does not match inside `0.1.10` or `0.1.1.2` and does match in
  `0.1.1.tar.gz`. A first anchor that refused a dash and a trailing dot lost
  two real carriers of maelys-egress, whose README installs the archive by
  name.

## 0.39.0 — 2026-09-11

- **A product's SBOM is attested against the file it describes, and the socle
  never guesses which file that is.** `subject-path: dist/*` attests every
  file a build leaves, the SBOM included, so a verifier could confirm the
  document and the archive both came out of this workflow and nothing more.
  Nothing bound the one to the other. A product now declares `[sbom]` with a
  glob, and the release reads the subject **the document names itself** — an
  SPDX `DESCRIBES` relationship or the `documentDescribes` shorthand reaching
  an element with `packageFileName` or `fileName` and its `checksums`, or a
  CycloneDX `metadata.component` with its `hashes` — requires that file to
  exist in that target's `dist/`, and requires its SHA-256 to equal the one
  the document records. Then it attests that file with that document.
- Two ways of guessing were refused for the same reason. Pairing by file name
  imposes a convention and still guesses; attesting every package of a target
  gives one component list to a `tar.gz`, a `deb` and an `rpm` assembled by
  format, which is sometimes true and, when false, an attestation that is
  wrong rather than absent. 0.28.1 settled that rule for `migrate` and it
  holds here: naming a subject wrongly is worse than naming none. maelys-http
  proposed the reading that does not guess, against both of the socle's own
  options.
- The check runs even where nothing will be attested. A document that
  disagrees with the archive beside it is wrong on a private repository too,
  and there it is all that is left: the run then says the SBOM is published
  but not attested, and `preflight` says so before the tag exists, so a
  reader of the release knows which guarantee is missing.
- One document per target: `actions/attest` carries one predicate, so a glob
  matching two documents stops the release instead of attesting one and
  ignoring the other. A glob matching none stops it as well — a here-string
  built from no output still feeds one empty line, which would have left the
  attestation skipped without a word.
- **Both attestation bundles travel with the packages**, named after their
  target. A verifier offline needs the predicates, and one without the other
  says nothing: provenance alone does not bind the SBOM to the archive, and
  the SBOM predicate alone does not say where either came from. maelys-http
  keeps both and was right to say so.
- `docs/conventions.md` claimed a product needed no separate attestation for
  the files `package_command` leaves beside its packages. That confused the
  provenance of an SBOM *file* with a proof that it describes the archive
  beside it. Corrected, with what the socle actually does now.
- The 0.38.1 entry said the new workflow tests skip on the macOS runner.
  They do not: macOS carries `sha256sum` in `/sbin`, and that tag's own CI
  ran 167 tests with `skipped=10` on `macos-15` exactly as on Linux, against
  162 with `skipped=10` before. The tag is published with the sentence and
  cannot be moved; the entry now says what happened. The guard the SBOM step
  carries is real for another reason: the build runner is the product's to
  declare, and a self-hosted one carries whichever digest tool it was built
  with.

## 0.38.1 — 2026-09-11

- **A release no longer publishes one target's bytes under a name two
  targets built.** `publish` collected the per-target artifacts with
  `download-artifact`'s `merge-multiple`, which extracts them in parallel
  into one directory: a name two targets both produce is not resolved, it is
  raced. The survivor may be either file, or a mix of both, and `SHA256SUMS`
  is then computed on it — a manifest that agrees with itself and describes
  bytes a target never built, with the other target silently absent from the
  release. The artifacts now stay in their own `incoming/dist-TARGET/`, and
  the step compares the digests of every name the manifest publishes before
  assembling anything. Two targets that disagree stop the release, and the
  message names both artifacts. Equal bytes are not a collision: a package
  independent of the architecture may be built by every target. Reported by
  maelys-http, which builds a source archive its three targets all name the
  same; every product of the fleet already names its packages after the
  target, so none of them changes behaviour.
- `tap.yml` keeps its `merge-multiple`, and the reason is not that a
  collision would be harmless: bottle file names are composed by Homebrew
  from the platform tag, not by the product's packaging script, so the
  product cannot omit what distinguishes them. That is the defect above —
  a name that forgets what varies — and it cannot arise there.
- `tests/test_release_workflow.py` runs the assemble step's own shell
  against artifacts on disk, the way `test_tap_workflow.py` runs the tap's
  publish step. It skips where `sha256sum` is missing, which is a
  development machine and not a runner of this fleet: macOS carries
  `sha256sum` in `/sbin`, and the tag's own CI ran these five tests on
  `macos-15` as on Linux. The tag published with this sentence claiming the
  opposite; it was wrong, and the count of skipped tests said so.

## 0.38.0 — 2026-09-11

- **`rehearse DIR --channel NAME --tag vX.Y.Z` runs a product's publish
  script against a release that already exists.** `channel.yml` only ever
  runs on a signed tag, so the one contract it rests on — the publish command
  exits 0 without republishing when the registry already holds the version —
  had never been exercised anywhere, and a product could only discover its
  script was not idempotent by replaying a real release. It does what the job
  does: the release's own assets in `dist/`, the same substitutions and the
  same environment, `CHANNEL_RECORD` included.
- **And it compares the marker.** The object the `record` job would attach is
  composed here and set against the `channel-<name>.json` the release
  carries, ignoring `published` and `run`, which differ by construction. A
  disagreement anywhere else means the script records something the release
  does not carry, and the next publication would attach that instead.
- It refuses before touching anything: an undeclared channel, a tag that is
  not one, a release that does not exist, a non-empty `dist/` whose stale
  files would make the script publish what the release has not got. **The
  registry token is the operator's** — it passes through the environment, and
  the socle never reads one from a file, never supplies one and never prints
  one. What it downloaded is removed whether the run succeeded or failed.
- **`publish_command` is `bash` too.** 0.36.0 fixed `verify_command` and left
  this one on `sh`, which is dash on the runner and bash in POSIX mode on the
  operator's macOS: the same trap, in the one script that publishes
  irreversibly. A product regenerates the line at its next adoption.
- Proposed by maelys-datalog: *« C'est le test que je n'ai pas pu faire. »*

## 0.37.0 — 2026-09-11

- **A repository declares in `maelys-release.conf`, at its root.** It sat in
  `packaging/release`, and that was the wrong place: the eleven `packaging/`
  of the fleet hold **materials** — formula templates, systemd and launchd
  units, Containerfiles, a kernel config, a patch, a `.pc.in` — and never
  settings, while **eighteen repositories have no `packaging/` at all** and
  still want to declare a branch rule or a cut command. The name says the
  tool and the nature.
- **`adopt --apply` moves it with `git mv`**, in the same commit that moves
  the pin, so `git log --follow` still finds it. The old path is still read
  while a repository has not moved — losing its targets would be worse than
  the violation — and carrying both is refused, because the socle will not
  choose between two declarations. `check` exits 2 on the old path and
  `adopt` still runs: a status that blocked `adopt` would put the remedy out
  of reach.
- **The socle only ever reads this file, and that is now written down.** The
  comments a product puts beside each section are its reasoning, and they are
  the product's to keep. maelys-json's file explains in six lines why its
  version is materialised twice; nothing the socle does may cost that.
- **A section this socle does not know is named and skipped, never fatal.**
  Products pin the socle by commit, so a repository adding a section a newer
  socle understands would otherwise lose its targets, its channels and its
  gate in silence wherever an older socle reads the file — including in the
  fleet observer, which runs the socle each product pins.
- Two repositories carry the file already, both since this afternoon:
  maelys-datalog and maelys-json, the second having adopted `[cut]
  after-version` within an hour of 0.34.0 for the same reason as the first, a
  version materialised in a header its consumers assert on. The move costs
  them one `adopt --apply` each.
- Arbitrated against two other proposals, and both were mine or a peer's:
  `RELEASE` at the root would have been the thirteenth capitalised file of
  maelys-warden and would sit beside `RELEASING.md`, same radical, one prose
  owned by the product and one grammar read by the tool; a `release/`
  directory would need one file per section — seven of two lines for
  maelys-json, which needs one — and reads as a build output where two
  repositories already declare `build/release/bin`.

## 0.36.0 — 2026-09-11

- **The socle invoked its two product scripts with different shells.**
  `package_command` ran `bash scripts/package-release.sh`, and the generated
  `verify_command` ran `sh scripts/verify-release.sh`. `sh` is dash on Ubuntu
  and bash in POSIX mode on macOS, so a `set -o pipefail` in that script
  passes on the operator's machine and on the first stop of `cut`, and fails
  on the runner at the tag — the one moment nothing can be retried cheaply.
  Both are `bash` now. Reported by maelys-datalog, who nearly paid for it.
  A product regenerates the line at its next adoption.
- **The documented replay of a tag could not work, in either direction.** It
  said `gh workflow run release.yml -f tag=vX.Y.Z`, with no `--ref`. Without
  one the run starts from the default branch, and the `release` environment
  accepts tags `v*` alone — the socle requires that policy and `preflight`
  refuses a repository without it — so the run is refused at `publish` by the
  very guard the socle asks for. With `--ref vX.Y.Z` it runs, and it runs the
  workflow file at that tag, which pins the socle **that tag pinned**.
- So the page's claim that a replay applies a corrected socle was false both
  ways. A replay is for a run that failed for a reason outside the code — a
  cancelled job, an expired approval, a tap push lost to a race. **When the
  socle is at fault, the remedy is a new patch release of the product
  carrying the corrected pin**, never a replay of a tag that would repeat the
  fault. Corrected in the conventions, the managed instruction block and the
  seeded `RELEASING.md`. Reported by maelys-oci, who asked why the documented
  command had no `--ref`; the answer was worse than the question.

## 0.35.1 — 2026-09-11

- **`rehearse DIR linux-x86_64` works again on an Apple Silicon host.** The
  rehearsal copied the working tree with a `tar` pipe, and GNU tar 1.35
  extracting under an emulated `linux/amd64` fails every `mkdir` with ENOSYS,
  "Function not implemented" — so the x86_64 rehearsal was unusable on the
  machines the fleet develops on, which are all of them. `cp -a` followed by
  removing `dist/` does the same work and survives the emulation. Reported by
  maelys-oci, reproduced here, and verified by a full `make check` of
  maelys-oci under `linux-x86_64` on this host.
- **`rehearse` refuses a git worktree rather than letting it fail inside the
  container.** A worktree's `.git` — and a submodule checkout's — is a file
  naming a directory elsewhere on the host, which the container has not got,
  so git inside the rehearsal reads a path that is not there. Also reported by
  maelys-oci, who worked around it with a full clone; the socle now says that
  itself, in seconds, instead of surfacing as whatever the product's build
  makes of a broken repository. The refusal comes before the search for
  `docker`, so it holds on a machine that has none — which is where the first
  shape of it was caught, by the macOS runner rather than by this laptop.

## 0.35.0 — 2026-09-11

- **`--field NAME` reads one member of a result without a `jq` expression**,
  in every command of the socle. It comes from agent-cli/v2.4.0 through the
  vendored framework rather than from code written here: the socle asked the
  contract for it instead of inventing a local spelling that four other
  Maelys tools would then have spelled four other ways.
  `declarations DIR --field targets` prints one target per line;
  `--field workflows --format jsonl` prints one workflow per line with its
  members named. Refused with `--format json`, because a filtered envelope
  would no longer validate against the command's `outputSchema`; a name the
  result does not carry fails rather than printing nothing.
- `dependencies/agent-cli-spec.pin` moves to **v2.4.0** and
  `dependencies/maelys-cli.pin` to **v0.5.23**, with `bin/maelys_cli.py`
  re-vendored. The conformance kit the socle runs is that tag's, so the new
  rules are checked here first.
- The managed instruction blocks name `--field`, so an agent reading them
  knows the result can be read a member at a time. A product picks the text
  up at its next adoption.
- **`cut` names again the command that applies its plan.** 0.34.0 shipped a
  plan that printed `cut: ready` instead of the line to run next, and nothing
  caught it: a test now does. Found while cutting 0.34.0 itself.

## 0.34.0 — 2026-09-11

- **`declarations` says what a repository runs, and when.** A new `workflows`
  field holds, per file of `.github/workflows/`, the events that start it, the
  branch and tag filters of its `push`, its jobs, its runners, and whether one
  of its jobs calls the socle. A repository's strategy — pull request, push,
  signed tag, hand — was spread over those files, and reading it meant opening
  every one of them in every repository. It is read from the files alone, so
  the shared CI, which has no API access, gets it too.
- **The text rendering turns the table.** The JSON is per file, because that
  is where the facts are read; a reader asks what happens *when*, so
  `declarations` prints one line per moment — pull request, push, tag,
  manual, called — and nobody needs `jq` to see a repository's strategy.
- **A workflow that runs twice on every pull request is now named.** `push`
  with no branch filter beside a `pull_request` runs the same jobs for the
  push and for the pull request. A **note**, never a violation: a repository
  may want it. Two products of the fleet do it without having chosen to.
- **The socle stops calling a refusal an absence.** `github_api` turned every
  failed call into `None`, and `preflight` read `None` as "the default branch
  is not protected". A private repository on a free plan answers **403** to
  that endpoint — which is GitHub declining to say, not a branch left open —
  and seventeen repositories of the fleet were reported unprotected while
  that was true of none of them, measured. The reader now separates an
  answer, an absence (404), a refusal (403 or anything else) and a missing
  `gh`, and `preflight` reports each as what it is.
- **A branch protected by a ruleset is protected.** The classic protection
  endpoint answers "Branch not protected" for a repository whose default
  branch a ruleset guards, and `agent-cli-spec` is exactly that: reported
  open, permanently, not only while the organization was locked. Both
  endpoints are read now, and the note says which of the two protects.
- **`cut` runs what a product declares before it writes anything, and
  commits what a version bump regenerates.** maelys-datalog found both by
  migrating onto `cut`, which is the only way either could have been found.
- **`[cut] after-version COMMAND` in `packaging/release`.** A product whose
  version is materialised somewhere besides `VERSION` — a generated header, a
  `package.json`, a formula — names the command that regenerates it. `cut`
  runs it between writing `VERSION` and the bump commit, and whatever the
  command touched joins that commit. Without it the bump commit is **red by
  construction** wherever a check compares the two files, and the first stop,
  which waits for the checks of that very commit, could never see them green:
  the operator had to push a second commit onto the release branch by hand,
  which is the ceremony `cut` exists to remove. It is not datalog's alone — it
  holds for every product that materialises its version twice.
- **The first stop runs `scripts/verify-release.sh` when the repository
  carries one**, with the host's target substituted, and refuses on its
  failure — before a branch, a commit or a pull request exists. It is the
  socle's own concept, already rendered into `verify_command` for the release
  runners: declared once and held at both ends, rather than a gate that ran
  in a product's old ceremony and nowhere in the new one.
- **What is not in the manifest is not published, and two texts said
  otherwise.** Since 0.31.0 `publish` copies from the build's artifacts only
  what matches the manifest globs or `*.sha256`; a file `dist/` holds outside
  them is attested and never reaches the release. `docs/conventions.md` and
  the `manifest_patterns` input description both still claimed the manifest
  decided nothing about publication. Both corrected. Found by maelys-datalog,
  whose build receipt was attested and absent.
- The write path of the first stop is now tested end to end, against a real
  bare repository with a signed commit: it was not when 0.33.0 shipped it.
- **A branch is named after the change it carries, never after the tool that
  created it.** The rule enters the managed blocks of `AGENTS.md` and
  `CLAUDE.md`, so every session of every repository reads it: a branch takes
  the prefix its change would take in a commit — `fix/`, `docs/`,
  `release/` — and `claude/` or `codex/` says who typed rather than what
  changes.
- **No list is closed, deliberately.** maelys-datalog, which reported this,
  already uses ten commit prefixes of its own and no two products share a
  set. Enumerating change types would import one repository's vocabulary
  into twelve; the rule is the correspondence between the name and the
  nature of the change. The one list the socle does close is the other side:
  the agents it writes instructions for, `claude` and `codex`.
- `check` **notes** such a branch and never refuses one. A branch name is
  not a property of the working tree `check` reads, and in CI the checkout
  is detached, where the ref is GitHub's and not the author's: the note
  fires where it is useful, on the author's own checkout before the push.
- The fleet-wide count belongs to maelys-platform, which reads the remote
  refs of every repository. The socle declares, the observer counts — the
  same split as the release gate of 0.30.0.
- Reported by maelys-datalog, who measured that the tool exposes no
  branch-naming setting and argued against a `WorktreeCreate` hook: a faulty
  hook does not misname a branch, it breaks session creation, and the trade
  is bad against a prefix.

## 0.33.0 — 2026-09-11

- **`maelys-release cut DIR X.Y.Z` carries out a release in two stops.** Two
  conventions held only as prose — a tag comes after the checks of that exact
  commit, and a release is never published from a branch — and no command
  applied either. The first stop refuses before it writes anything (a version
  that does not follow the current one, a worktree carrying more than
  `VERSION` and `CHANGELOG.md`, a missing or future-dated entry, a `HEAD`
  that is not the default branch up to date with `origin`, and the gate
  `preflight` holds), then writes `VERSION`, commits it signed on
  `release/vX.Y.Z`, opens the pull request and waits.
- **It waits for the checks to exist before it waits for them to finish.**
  Polling `commits/SHA/check-runs` until `total_count` is non-zero and
  nothing is pending closes the race a fleet product paid for: a checks
  command that answers at once when no run has registered yet reports green
  on a release nobody has built. The reading is paged: one commit of
  maelys-egress carries forty-eight check runs and the endpoint's default
  page holds thirty.
- **`cut DIR X.Y.Z --tag` signs the tag on the merge commit, never on the
  branch.** It reads the merged pull request, takes its merge commit,
  verifies that commit is on the default branch and carries `VERSION` =
  `X.Y.Z` with its dated entry, waits for every one of its checks, signs and
  pushes — then reads back what GitHub says of the signature. `origin/main`
  moves; the commit whose checks were read does not.
- **It never merges its own pull request.** The middle stop is GitHub's,
  under the repository's own rules. A command that merged for itself would
  work only where the default branch is unprotected, which is today the case
  everywhere in the fleet, and would teach twelve repositories that the
  releaser approves themselves.
- The gate is what the repository's CI cannot see: the operator's signing
  configuration, the tags already published, and — for a product the socle
  releases — the `release` environment `preflight` reads. The product
  contract is judged by the checks `cut` then waits for, on the exact commit,
  rather than a second time from a socle that may not be the one the product
  pins.
- `preflight` keeps its behaviour and its output; its body is now the two
  readers `cut` shares, `tag_checks` and `repository_checks`.
- This changes the managed instruction blocks and the seeded `RELEASING.md`
  template, so a product picks the text up at its next adoption. Nothing in a
  product's release mechanism changes, and the ceremony by hand remains what
  it was: `cut` is not required anywhere.
- Proposed by maelys-datalog, whose own `cut-release.sh` this deliberately
  does not copy on two points: it tags `origin/main` rather than the merge
  commit, and it merges its own pull request.

## 0.32.0 — 2026-09-10

- A channel that published says so, in one asset of the release named
  `channel-<name>.json`. It is written by a second job of `channel.yml`
  that runs no code of the product: no checkout, no `dist/`, only what the
  socle composes. `needs: publish` is what makes the marker an observation
  and never an intention — the job does not start unless the publication
  succeeded, so the asset exists if and only if the channel published.
- **One asset per channel, written once and never rewritten.** maelys-datalog
  asked for a receipt reattached to the release after each channel; a
  rewritten asset cannot be covered by `SHA256SUMS`, which is computed
  before it, and two channels publishing in parallel would lose one
  another's entry on the clobber. An additive asset answers both.
- A publish script with something to record about its publication writes a
  JSON object to `$CHANNEL_RECORD`; its fields join the marker. It is
  optional, and the socle records the fact of publishing without it.
- The generated caller grants `contents: write` to the channel job, which
  only the marker job uses: the job running the product's publish script
  keeps `contents: read`. This changes a managed file, so a product picks it
  up at its next adoption.
- Reported by maelys-datalog, whose own receipt had once claimed an npm
  package whose publication had failed with a 404, and whose channel entry
  now waits for a success before it is written.

## 0.31.0 — 2026-09-10

- **The job that runs a product's own `package_command` no longer holds a
  write token.** `contents: write` on GitHub is repository-wide: the build
  could rewrite the assets of releases already published, push to any branch
  — no repository of the fleet protects its default branch — and create
  tags. A compromised toolchain in that job held the repository, not a
  draft. It now holds `contents: read` with OIDC and attestations, and
  nothing else.
- The bytes cross the run as workflow artifacts, kept one day, instead of
  through a draft release. `publish` verifies what `build` produced, not
  what a writing job could have edited between the two: the former
  `sha256sum -c` compared artifacts against `.sha256` files that came from
  the same draft and the same job, which proved consistency and not
  integrity.
- The `prepare` job disappears. `publish` creates the release, fills it and
  publishes it, and is the only job of this workflow that may write. A
  release therefore asks for one approval instead of two, in front of the
  only job that can change anything.
- Reported by maelys-datalog, whose own release separates these privileges.
  Their question was precise — where does `publish` get the hashes it
  verifies — and the answer was the draft.
- **This reverses 0.15.1**, which moved the transport onto a draft release so
  that publication would not depend on the Actions artifact quota. That trade
  bought storage with a token, and the token was repository-wide. The
  artifacts come back with a one-day retention, and the reversal is named
  rather than slipped in: `maelys-oci` is private, so its releases now
  consume billed artifact storage for up to a day. The four other products on
  this mechanism are public, where that storage is free.

## 0.30.0 — 2026-09-10

- A repository declares the gate it wants, and `preflight` verifies that
  answer instead of assuming one. `[gate]` of `packaging/release` says
  `reviewer` or `none`; the socle fails only on a promise unkept, and a
  repository that has declared nothing gets a note. The socle reports the
  gap and leaves the choice where it belongs.
- `preflight` reads the release environment's protection rules at all, which
  it did not. It checked that deployments are limited to tags `v*` — what
  may publish — and answered `ok` on an environment that required nobody.
  The conventions call that environment the human gate; the socle had never
  seen it closed.
- **Measured across the fleet**: of twelve repositories, one requires a
  reviewer, six have a deployment policy and no reviewer, two have an
  environment with no rule at all, three have no environment. No repository
  protects its default branch. Nothing here turns any of them red, because
  none of them has declared anything yet.
- A gate its approver can walk around is named for what it is: when an
  administrator may bypass the reviewer, or the reviewer may approve their
  own deployment, `preflight` says the gate is a pause and not a control.
- An unprotected default branch is reported as a note. The socle's contract
  is about tags; it matters because `commit_verification:
  signed-on-default-branch` relies on that branch meaning something.
- Reported by maelys-datalog, whose environment had published twice
  unattended while their document made the approval an invariant.

## 0.29.1 — 2026-09-10

- A runner label must have the shape of one. `read_runners` accepted
  whatever it read out of a `runs-on:`, and that had two consequences worse
  than a stray label. A jq line of this repository's own `release.yml`
  passed as a runner; and because it did, `runs-on: ${{ fromJSON(matrix.runner) }}`
  counted as resolved, so the `unresolved` entry that should have shown the
  hole never appeared. Anything that is not a label now goes to
  `unresolved`, where an observer blocks on it.
- The block form of `runs-on:`, where the labels sit on the following lines,
  no longer reads as the single label `- self-hosted`. The value never
  crosses a line, and a `runs-on:` introducing a block or a `group:` mapping
  is reported unresolved rather than invented. A `{ group: … }` mapping too.
  No repository of the fleet writes those forms today; the reader was wrong
  about them anyway, and a control that refuses self-hosted runners must not
  be wrong in that direction.
- `unresolved` names the line, not just the file, so an operator finds what
  the socle could not read.

## 0.29.0 — 2026-09-10

- The managed `AGENTS.md` and `CLAUDE.md` blocks say where a product's prose
  lives: `maelys-dev/maelys-docs`, directory `<product>/`, with a
  neighbouring checkout, and documenting means opening a pull request there.
  maelys-platform's documentation policy has claimed for a while that the
  block says this. It did not: neither block mentioned maelys-docs, the
  prose, or documentation at all.
- The same bullet carries the distinction 0.28.1 rests on: **that repository
  is private, so it is never named from a public README.** The reader of a
  managed block has access to it; the reader of a README may not. The two
  rules belong together, and separating them is what let the socle plant a
  private reference in a public README.
- This changes a managed text, so it is a minor release and a product sees
  the bullet appear at its next adoption.

## 0.28.1 — 2026-09-10

- `migrate` no longer writes a private repository's name into a public
  product's README. It replaced the links it moved with a pointer built by
  copying the destination, and the destination is `maelys-dev/maelys-docs`,
  which is private: the socle planted the private reference that
  maelys-platform's audit blocks a repository on before opening it to the
  public. maelys-json carries one such pointer today, written when its prose
  moved.
- The socle now asks GitHub for the destination's visibility. A public
  destination is still named; a private one is not, and the report says what
  a human must still do. A visibility the socle cannot check is read as
  private: naming a destination wrongly is worse than naming none.

## 0.28.0 — 2026-09-10

- Managed agent texts (`share/agents/`) use CC-BY-4.0 with attribution to
  David Bromberg. The installer preserves their copyright, source and license
  notices, and a test asserts that it does, for both mechanisms. Other
  templates and the installed dependency-checkout script remain CC0-1.0;
  the source code stays MPL-2.0.
- **This changes a managed text**, so a product sees the notice appear inside
  its `AGENTS.md` and `CLAUDE.md` blocks at its next adoption. That is the
  whole point: attribution that does not travel with the text is not
  attribution.
- The copyright holder is corrected in the licensing declaration and in the
  `LICENSING.md` template the socle seeds. The template is written once, when
  the file is missing, and belongs to the product afterwards: a repository
  already carrying the former holder keeps it until someone changes it there.

## 0.27.0 — 2026-09-10

- `declarations` answers what maelys-platform still read with a pattern.
  `pinned` gains the file the socle is named in, `managedBy` carries the
  version stamped in the generated `release.yml`, and `runners` holds the
  labels a repository's workflows select. The fleet asked for the first and
  the third; the second is what retires the last pattern.
- **`declared` separates what a product asked for from what it gets.**
  `targets` and `manifestPatterns` return the effective values, defaults
  included, which made a fleet survey read "the fourteen products target
  exactly these three" where the truth was "no product declared one". That
  reading was a defect of this command, not of the survey.
- The runner reader does not repeat the mistake it replaces. Searching the
  text of `runs-on` for a label misses a matrix, and misses a repository
  that names no runner because every job calls a reusable workflow.
  maelys-oci is that second case today. So a matrix reference is resolved
  inside the file, including the `include:` form maelys-datalog writes,
  everything still unnamed is reported under `unresolved`, and `delegated`
  says the repository chooses none of its own. An empty label list is not a
  clean bill and the conventions say so.
- Measured on the fleet: no repository names a self-hosted runner and none
  is unresolved. The blind spot is latent, and it becomes live the day a
  self-hosted runner arrives through a matrix.

## 0.26.0 — 2026-09-10

- The `Fuzzing` section of `docs/conventions.md` is rewritten against a
  survey of the fleet, because the version before it asserted a fleet that
  did not exist. It claimed three products fuzz: nine do. It claimed nothing
  schedules `make fuzz` here: the socle runs it for maelys-http and
  maelys-oci, through `sanitizer_command`. It framed `make fuzz` as a
  campaign: in this fleet it is a bounded run, `-runs=10000` in
  maelys-egress, `-max_total_time=30` in maelys-oci. maelys-egress asked for
  this text to be settled and was told it already was; it was not.
- The harnesses live in `tests/fuzz/` in five repositories and `fuzz/` in
  four. The convention names the first as the one to prefer and the socle
  reads either, rather than a single layout the fleet does not follow.
- `check` observes fuzzing and never refuses it. A repository carrying
  harnesses reports whether the socle's job runs them, a job of its own
  does, or none does; the last is a note. Nothing here rises above a note,
  because `check` counts anything but `ok` and `note` as a violation, so no
  conforming repository turns red.
- `declarations` carries a `fuzz` object, `harnesses` and `runs`. It answers
  "who fuzzes" only for whoever reads it: `maelys-platform` reads workflows
  by pattern today and calls this command for nothing.
- The description of `fuzz_command` in `check-product.yml` loses the same
  three false claims.

## 0.25.1 — 2026-09-10

- The fuzz job of `check-product.yml` installs `libclang-rt-18-dev`, as the
  sanitizers job beside it already did. Without it a harness linked with
  `-fsanitize=fuzzer` does not link, so the job could only host a smoke
  target written as a standalone driver. maelys-egress reported this as the
  campaign being impossible; the campaign is refused here on its own terms
  and stays refused, but the corpus replay is not, and a product that spells
  its smoke target as a libFuzzer binary was silently locked out.
- The comment beside that step now names `docs/conventions.md` as the text
  to read. Two of the three points in that report were already answered
  there: the campaign has had no place in CI since 0.21.0, and the
  conventions state that there is no fleet build layout to assume for
  `docs/cli.reference`, naming maelys-cli's `build/release/bin`.

## 0.25.0 — 2026-09-10

- A product publishes to a registry through the socle. `channel.yml` is a new
  reusable workflow, and a `[channels]` line of `packaging/release` plus an
  executable `scripts/publish-channel.sh TAG CHANNEL` render one job per
  channel. This closes the last third of `maelys-dev/maelys-release#19`;
  maelys-datalog and maelys-mcp both kept their whole mechanism local partly
  for want of it.
- A channel is its own reusable workflow rather than a step of `publish`,
  because the permissions of a called workflow are static: they cannot depend
  on what a product declares, and one caller job that under-grants fails the
  whole run at startup, the release job included. A product that declares no
  channel never calls the workflow and never receives its scopes.
- The channel runs after the release is published, never before. A release
  can be redrafted, a registry publication cannot be withdrawn, so
  `publish-channel.sh` must exit 0 without republishing when the registry
  already holds the version.
- `github-packages` is the only registry served, and refusing the others is
  measured rather than chosen: trusted publishing matches an OIDC claim
  naming the socle rather than the product once a reusable workflow
  publishes, and PyPI documents that a reusable workflow cannot be a trusted
  publisher.
- A test now asserts that the generated caller grants every scope the
  workflows it calls declare. Four throwaway runs established that property
  once; the test holds it at every change.

## 0.24.0 — 2026-09-10

- The release mechanism stops deciding alone what a product builds. Its
  three targets and the three archive kinds `SHA256SUMS` covers were
  written into the workflow; they are now the defaults of two inputs,
  `targets` and `manifest_patterns`, and a product declares what it needs in
  `packaging/release`. maelys-datalog's D7 recorded a wasm target and an npm
  channel as candidates to bring upstream, and nothing here tracked them
  (`maelys-dev/maelys-release#19`).
- `[targets]` replaces the matrix rather than adding to it, so a product
  that adds one names the ones it keeps. A target line that names no runner
  keeps the runner the workflow holds for it, so changing a default runner
  still reaches a product that only added a target.
- `[manifest]` adds archive kinds. It decides what the manifest vouches
  for, not what is published: the build job already uploads all of `dist/`
  and the attestation already covers `dist/*`, so an artifact of an unnamed
  kind was published and attested but absent from `SHA256SUMS`.
- A declaration the socle cannot honour is a violation of the release
  scope, so `check` exits 2 and `adopt` refuses rather than letting a tag
  build from a file nobody read.
- A product that declares neither section sees no change: the resolved
  matrix and the manifest are byte for byte what 0.23.0 produced, and
  `adopt` writes the same files.
- This opens no publication channel. The socle's `publish` job holds
  `contents: write` and nothing else, and a called workflow cannot widen
  the token beyond what its caller granted, so a registry channel remains a
  separate decision.

## 0.23.0 — 2026-09-09

- `migrate` finishes what it reports. It rewrites every Markdown file of the
  product, not the README alone: a product had to repoint
  `examples/README.md` by hand, and reporting a problem while fixing half of
  it is worse than either extreme. A link whose target left keeps its text
  and loses its link; a path named in prose becomes the path it now has; a
  document that is itself moving keeps its own relative links.
- What the socle will not rewrite, it names with its file and line, in the
  plan and in the commit that asks for the pull request: a header, a
  Makefile or a script says what it says for reasons the socle does not
  know.
- A rule naming `docs/` as a whole is reported as well, because the
  migration changes what it matches. maelys-egress installs `docs/*.md` at
  `Makefile:486`, and that glob was about to match one file instead of
  nineteen without a word.
- Migrating a prose that maelys-docs already carries is refused with its
  reason instead of failing on an empty commit and an opaque git error.

## 0.22.1 — 2026-09-09

- A broken third-party apt source of the runner image no longer fails a
  product's CI. `apt-get update` is best-effort and reports a warning; the
  install that follows decides, and still fails loudly when a package is
  missing. GitHub's Ubuntu images carry a Google Chrome source that served
  a mismatched index today, and `update && install` turned every Linux job
  of the fleet red for a reason no product controls, the socle's own
  workflows included.

## 0.22.0 — 2026-09-09

- A pin may carry a `submodules` line, or `submodules recursive`, and
  `scripts/checkout-dependency.sh` then initialises the clone's submodules.
  Without it nothing is fetched: a submodule's commit is pinned by its
  superproject, but its URL comes from that repository's `.gitmodules`,
  which the pin does not name, so initialising one reaches a repository the
  product never declared. The update is shallow first and falls back when
  the server refuses to serve a commit it does not advertise.
- This is what the `repository` line of 0.21.0 was built for and did not
  finish: Mbed TLS carries a `framework` submodule, and `cmake` refuses to
  configure without it even with the programs and the tests off. maelys-http
  worked around it with a line in its own CI. Reproduced on their pin, and
  `cmake -S mbedtls -B b -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF` now
  configures from what the managed script alone leaves on disk.

## 0.21.1 — 2026-09-09

- The drift job failed for every product that takes the shared CI while
  keeping its own release, which 0.20.0 had just made possible.
  `check-product.yml` knows how to find the socle in the `ci.yml` line that
  calls it, but `pinned_socle()` read the pin from `release.yml` alone: such
  a product had no pin at all, the socle fetched by commit stayed labelled
  `untagged`, and the regenerated `ci.yml` drifted against its own committed
  line. It now reads the `ci.yml` line too.
- `.github/workflows/ci.yml` belongs to the conventions verdict, not to the
  release mechanism: the shared CI is offered to every mechanism, so its
  drift is reported where the product can act on it.
- The conventions say what was practice: a first adoption is its own pull
  request, because it changes what a repository builds, checks and
  publishes; an upgrade is not, and rides the commit that prepares the next
  release. maelys-http asked, adopting for the first time.
- A verdict that does not apply can no longer carry a violation. It did, and
  the command exited 2 while printing `conventions: ok` and `release
  mechanism: not applicable`: an exit code contradicting what a human reads
  is worse than either answer. The socle now refuses that state outright.

## 0.21.0 — 2026-09-09

- A dependency outside `maelys-dev` declares where it lives, on a
  `repository <https URL>` line of its own pin;
  `scripts/checkout-dependency.sh` clones that URL and refuses anything but
  an `https://` one. maelys-http pins Mbed TLS from a commit of
  Mbed-TLS/mbedtls, its distribution's version being under the security
  floor of its own policy header, and had no way to say so.
- `release.yml` gains `verify_command`, run on each target runner before
  packaging; the socle renders it from an executable
  `scripts/verify-release.sh TARGET`. The release builds what the tag
  names and never replayed the product's tests on that exact commit.
- `release.yml` gains `commit_verification`: `none` as before, `signed` for
  the commit's own GitHub-verified signature, `signed-on-default-branch`
  to also require it to be an ancestor of the default branch, so a tag
  cannot publish a commit that never landed.
- `check-product.yml` gains `fuzz_command`, a Linux-and-clang job beside
  the sanitizers, empty by default. The convention is `make fuzz` for the
  campaign, `make fuzz-smoke` for replaying the committed corpus in
  seconds, everything under `tests/fuzz/`, the corpus read-only for the
  smoke run.
- The provenance attestation's subject is `dist/*`: every file
  `package_command` leaves there is attested, an SBOM included. Said in the
  workflow and in the conventions, since a product asked.
- The pinned dependencies and `dependencies/packages` are conventions, not
  release declarations: `check-product.yml` reads them for any product that
  takes the shared CI, so a product keeping its own release now gets them
  verified too.

## 0.20.0 — 2026-09-09

- The shared CI is separable from the release mechanism, which 0.16.0 left
  half done: `check` stopped reporting a foreign `release.yml` as drift, but
  `check-product.yml` still derived the socle's commit from that file and
  refused outright a product that did not carry the socle's one. It now
  falls back to the `ci.yml` line that calls the workflow itself, so a
  product publishing by its own means runs the shared CI: one job in its
  `ci.yml` gives it the declarations, the pinned checkouts, the three
  targets and the conventions verdict, while its release workflow is never
  read, regenerated or reported.
- `adopt` installs `ci.yml` for a `custom` mechanism too, created once and
  owned by the product afterwards, with its socle line kept current. A
  repository whose mechanism is `none` keeps its workflows to itself.

## 0.19.0 — 2026-09-09

- `maelys-release migrate DIR --product NAME --documents FILE [--apply]
  [--push]` moves a product's prose into maelys-docs with its history and
  out of the product, as one transaction: a migration has two sides, and
  either alone is a defect. The list of documents comes from
  `maelys-platform docs --prose --only NAME --format jsonl` and is not
  recomputed, so the socle and the platform cannot disagree about what is
  prose.
- maelys-docs receives the rewritten history (`git filter-repo` then a merge
  with `--allow-unrelated-histories`), so `git log -- <product>/<file>.md`
  reads there, plus `dependencies/<product>.pin` and `<product>/VERSION` at
  the product's latest tag. The pin is the fleet's one format: the socle has
  refused `adapter/<NAME>_PIN` in a product since 0.14.0 and writes none
  here either, though the documentation policy of maelys-platform still
  names it. A product with no tag is refused. The product side
  travels as a branch for a pull request, never a write into the operator's
  checkout, and nothing leaves the machine without `--push`.
- The plan says what moves, what stays and why, and names every file of the
  product that still points at a moved document. On maelys-json it reported
  that `include/maelys/json.h`, `CONTRIBUTING.md`, `README.md` and
  `tests/vectors/README.md` name `docs/canonical-json-v1.md`.
- `check` now names that command as the remedy for prose it finds in
  `docs/`.

## 0.18.0 — 2026-09-07

- `maelys-release new DIR --product NAME --depends NAME@vX.Y.Z ... [--apply]`
  creates a Maelys repository in an empty directory: the MPL-2.0 `LICENSE`,
  `VERSION` at `0.1.0`, a dated `CHANGELOG.md` entry, a `README.md`
  skeleton, a `scripts/package-release.sh` stub that fails until the product
  implements it, one `dependencies/<name>.pin` per dependency with the
  commit its tag names, and then everything `adopt` writes for the declared
  mechanism. Plan by default.
- The command replaces the manual procedure of maelys-platform's
  `docs/operations/new-product.md`, which describes it as "à venir" and
  still names the pre-0.14 `adapter/<DEP>_PIN` layout; the pins it writes
  are `dependencies/<name>.pin`.

## 0.17.0 — 2026-09-07

- `docs/` of a product holds what a machine writes and what `LICENSING.md`
  engages; its prose belongs in `maelys-docs/<product>/`. `check` sorts every
  file of `docs/` into generated, engaged, data and prose, and names each
  prose file with its destination.
- The generated command-line reference is `docs/cli.md`, one name and one
  place for every product. `docs/cli-reference.md` and
  `docs/generated/cli-reference.md`, the three spellings in use today, are
  named with the `git mv` that fixes them.
- A file counts as generated when its first ten lines carry both the word
  `generated` and a refusal to be edited. Reading only the first line missed
  `docs/generated/config-reference.md` of maelys-egress, whose mark sits on
  line 3 under its title, and counted it as prose; requiring the refusal
  keeps prose that merely says "generated" in a sentence out. The rule is
  the contract rather than one spelling, since the generators live in
  maelys-cli and in the products.
- **The socle generates the reference itself.** `adopt` writes
  `docs/cli.md` and `docs/cli-contract.json`, `check` compares them, and
  `check-product.yml` does that in every product's CI. The Markdown still
  comes from maelys-cli's generator, which the socle does not reimplement:
  it finds it at the commit `dependencies/maelys-cli.pin` names, using a
  checkout beside the product when there is one and its own cache otherwise.
  A product therefore carries no rule, no path, no variable and no freshness
  check of its own: the `cli-reference` and `contract-check` targets of
  maelys-cli, maelys-egress and maelys-oci, three spellings of one rule, are
  deleted at their next adoption.
- What cannot be guessed is declared in `docs/cli.reference`: `[build]`, the
  directory holding the programs (default `build/bin`); `[programs]`,
  defaulting to the product's commands with its `lib*` formulas aside; and
  `[flags]` for the generator. maelys-oci's `--neutral-availability
  unpack-rootfs` moves there from its Makefile, maelys-cli its
  `build/release/bin` and the second program it documents; the three
  products that generate a reference build into three different trees, so
  the directory is declared rather than assumed.
- A repository that publishes libraries alone is told nothing about a CLI
  reference: maelys-system and maelys-json ship `libmaelys-sys` and
  `libmaelys-json`, no command, so the rule does not apply to them and no
  note asks for a file they cannot have.
- maelys-cli does not pin itself, so the socle uses the
  `tools/generate_cli_reference.py` it carries: the framework is held to the
  rule it serves, and loses its own target like the others. Checked against
  both maelys-oci and maelys-cli: the socle alone reproduces what each
  Makefile produced, byte for byte.
- The refusal is opt-in: `check --docs-contract`, and the `docs_contract`
  input of `check-product.yml`, turn those notes into violations. It stays
  opt-in for as long as `maelys-docs` does not exist, because a product
  cannot move prose to a repository nobody has created yet. Without it,
  every product keeps the verdict it has today.

## 0.16.0 — 2026-09-07

- The conventions of a Maelys repository are separable from the mechanism
  that publishes it. `adopt` installs the conventions of any product and the
  release files only of a product the socle releases; `check` returns two
  verdicts, and a product with a mechanism of its own passes without a word
  about workflows it owns. A repository declares its mechanism by the
  release.yml it carries: the socle's generated one, its own (`custom`), or
  none at all, which also means `custom`. Installing the socle's mechanism
  is explicit, `--mechanism maelys-release`: maelys-warden publishes through
  its own qualify and publish workflows and carries no release.yml, and the
  socle must not claim a repository that never asked. The socle never
  overwrites a workflow it did not generate either.
- The conventions are `VERSION`, `CHANGELOG.md` with its dated entry, the
  managed `AGENTS.md` and `CLAUDE.md` blocks, and the seeded `RELEASING.md`,
  `LICENSING.md` and `SECURITY.md`. A seeded file is written once when
  missing and owned by the product afterwards: `check` reports a missing one
  as a note, never as a violation. `CONTRIBUTING.md` is deliberately not a
  convention (docs/conventions.md, "Conventions and mechanism").
- The `AGENTS.md` and `CLAUDE.md` block of a product the socle does not
  release states the conventions instead of the socle's release rules.
- `preflight` and `rehearse` refuse a product whose mechanism is not the
  socle's: they replay a release this socle does not run.
- `check`, `adopt` and `declarations` carry `mechanism`, every check and
  planned file carries its `scope`, and `check` carries the `conventions`
  and `release` verdicts. A command reports only the checks that apply to
  the product it was given.
- This repository now carries the conventions it installs: `AGENTS.md`,
  `CLAUDE.md`, `RELEASING.md`, `LICENSING.md` and `SECURITY.md`, written for
  a socle that publishes a signed tag and no artifact.
- For a product the socle releases nothing changes: the generated files are
  byte for byte what 0.15.3 wrote, and its `check` verdict is unchanged. At
  its next adoption, a product missing a seeded file receives that skeleton.

## 0.15.3 — 2026-09-06

- `maelys-cli` is pinned at v0.5.19 and the vendored Python framework is
  refreshed from that commit: the trunk options `--progress`, `--verbose` and
  `--pager` exist on every command and in `globalOptions`, `pattern` is
  enforced by the parser as POSIX ERE, and `describe --summary` no longer
  carries `globalOptions`, `invariants` and `output`.
- `agent-cli-spec` is pinned at v2.3.1, the contract the framework is held
  to; the conformance test runs that kit.

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
