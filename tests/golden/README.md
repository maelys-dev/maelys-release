# Behavioral reference

A refactoring PR replays the reference; it never records a new one:

```sh
python3 tests/golden/capture.py /absolute/new/replay
diff -ru tests/golden/baseline /absolute/new/replay
python3 tests/golden/capture_api.py /absolute/new/api-replay
diff -ru tests/golden/api/baseline /absolute/new/api-replay
```

Both diffs must produce no output and exit 0. Paste the commands, output and exit
code into every refactoring PR. `check.yml` also runs this comparison on Linux
and macOS for PRs whose head branch starts with `refactor/`.

The manifest pins the original fixture builders and the eight fleet checkouts.
Changing the current tests cannot change these inputs. The capture compares the
candidate's CLI as a subprocess, with no `gh`, and never applies the candidate
to a product. See `capture.py` for the normalization and isolation rules.

`baseline/socle/VERSION` and `baseline/socle/CHANGELOG.md` are inputs of the
reference, written by `--record` and restored into a copy of the candidate at
replay: the version is in every generated header and every `socle.version`,
the changelog is what `adopt` and `check` read. A candidate at any version
replays the same reference; a changelog edit is a behavior change and follows
the fix ritual below.

## An intentional behavior fix

A fix takes precedence over the next refactoring stage. Give it its own fix PR:
the golden diff is part of that PR's description of the product behavior that
changes. Never update the reference to make a pure refactor pass.

1. Reproduce the defect, add its regression test, and implement the fix. Replay
   against the current reference first. Keep the failing diff and explain each
   changed message, value or exit code in the PR.
2. Commit the implementation and tests on the fix branch. The source checkout
   must be clean, including workflows and dependency pins. Read its full commit with
   `git -C /absolute/socle rev-parse HEAD`.
3. Explicitly record that candidate, into a new directory:

   ```sh
   python3 tests/golden/capture.py /absolute/new/fixed-golden \
     --source /absolute/socle --record --candidate FULL_FIX_COMMIT
   diff -ru /absolute/socle/tests/golden/baseline /absolute/new/fixed-golden
   ```

   `--candidate` requires `--record`, the exact full HEAD hash, and clean source.
   It preserves the old manifest's fixture commit, fleet commits and comparison
   identity, and the frozen `socle/VERSION` and `socle/CHANGELOG.md` of the
   reference: the fixtures were built under that version, and their managed
   headers carry it. It does not re-resolve the fleet's main branches or silently
   replace the fixture builders with the tests just edited. `--reference` can
   select a different existing reference explicitly. Only the plain `--record`
   bootstrap freezes the source's own VERSION and CHANGELOG.
4. Review the complete diff. Copy the accepted capture into `tests/golden/baseline`
   as a separate commit in the **same fix PR**, removing obsolete snapshot files
   if necessary. Include both the behavior explanation and the reviewed diff.
5. Replay without `--record` into another new directory and require an empty
   diff. The reviewer approves the intentional behavior change together with
   its reference. Resume refactoring only after this fix is accepted.

Plain `--record` remains the bootstrap operation: it requires unchanged
`origin/main` and resolves new fleet inputs. Do not use it for a fix or refactor:
that would conflate changing the implementation with changing the inputs.
Refreshing fixture/fleet inputs is a separate, explicitly reviewed operation.

## API and host reference

`api/inputs/socle/VERSION` and `api/inputs/socle/CHANGELOG.md` are inputs of
this reference, frozen at recording and restored into a copy of the candidate at
replay: the version is in the header of every generated `release.yml`, and the
changelog is what `adopt` and `check` read to say what a product is behind on.
Without them, the first `cut` after a recording turned every recorded fixture
into a drift. A candidate at any version replays the same reference.


The first reference holds 715 observations of 216 fixture tests (17 more are
skipped), 164 distinct command triples and the eight pinned fleet
repositories, without `gh`: counted from `baseline/manifest.json`, where the
figures written here before had stayed at those of the first recording. Its
inputs are the ones it was first recorded with. The additional `api/` reference
records the approved stage 4d implementation at
`a6a308b67c26ddd2a16dfffee25f7476f02a06d1` (PR #117), before moving any further
command. It contains 32 scenarios, each in JSON and text (64 CLI invocations);
the count had stayed at the original 27 through two additions and is counted
from the manifest here:

| Command | Recorded branches |
| --- | --- |
| `protect` plan | Classic settings, rulesets, open branch, unreadable protection, unreadable check runs, `--without-legs`; in each but the last, the release environment and its policies, read through the reader `preflight` verifies with and found with nothing to change |
| `preflight` | Classic protection, ruleset, private visibility, open branch, absent release environment, an unreadable one, a branch policy beside the tag rule, a repository that has never published; signing configuration and tap drift reads |
| `check` and `preflight` on a seeded name | The prose line naming the documentation repository: refused, and `preflight` stopping before its GitHub reads |
| `adopt` plan and `check` | Old pin with public/classic-protection selectors; `current` false, true and unknown, and the resulting `check` verdicts |
| `adopt --apply` on throwaway files only | Classic and ruleset guards: allowed or refused due to a retired required context; unreadable protection |
| `migrate` plan, without `--push` | A moving document and its references, generated/public/data/unlisted documents staying, foreign product and wrong destination refusals |

The inputs contain 34 real GET responses read through `Host.read`, 26 of them
on 2026-09-16 and the protections and branch rules of both repositories the
day after; the release listings and the deployment policies of both
repositories were read on 2026-09-23, when both readers started asking for a
page at a time: `maelys-dev/maelys-system` (classic protection),
`maelys-dev/agent-cli-spec` (ruleset), and the public `homebrew-tap` formula
listing/files read by preflight. Each response retains its decoded body and
state, endpoint and reading timestamp. The cache keeps the first reading of
each endpoint; these independent GETs are not an atomic GitHub snapshot.
The 20 derived responses explicitly name their original response and changes:
private visibility, absent/unreadable protection, missing environment, an
unreadable one, unreadable check runs, an added retired context, a branch
policy sitting beside the tag rule, or a repository with no release. The last two have no
end on the fleet to record live — every repository carries the tag rule
alone, and every one of them has published. These are constructed
branches, **not** claims about the repositories' actual settings. No real
settings were changed to manufacture a failure.

One member of GitHub's answers is in no stored response: `security_and_analysis`,
which a repository document carries for a reader who administers the
repository and which says whether secret scanning and push protection are
on. `preflight` reads it to note a public repository that has them off, and
a reference holding it would say that of two real repositories, by name, on
the day they were read. The state of a real repository's security settings
is not this reference's to publish: `record_api.py` drops the member from
every answer, read live or taken from the cache, before the recorded program
sees it, so every recorded `preflight` says nothing of those settings and
`tests/test_record_api.py` holds both halves. The note itself is proven on
constructed answers — the member present and off, on, and absent — in
`tests/test_privacy.py`.

`inputs/` freezes the product files (including executable bits), command line,
and ordered host calls. Git reads run only when recording inputs, in temporary
local repositories; their stdout, stderr and exit codes become inputs too.
What git would compose on the machine is the one exception: a reading
compares an address with it, and the recorder never asks the real machine. It
answers with an account and a machine that do not exist, to the command it
records as to the reference, so no input holds the login, the account's name
or the name of the machine it was recorded on.
The manifest hashes every input file. Current test fixture builders never run
during replay. The local fixture commit seen by `migrate` is recorded, not
recreated on another platform.

`capture_api.py` launches each case in a fresh isolated interpreter with an
empty PATH and no inherited credentials or private self-test switches. It runs
the real CLI and replaces only `host.HOST`. Every `read`, `which` and `run` must
match the next recorded call, and all calls must be consumed. There is no
fallback to real GitHub or Git; `write`, `stream` and `exec` always fail. Plans
must leave their files unchanged. Successful local adoptions capture the
changed files as well as stdout, stderr and exit code. Only absolute product
and socle paths are normalized; API metadata, messages and JSON fields remain.

`test_api_golden.py` checks this boundary, fails a deliberately corrupted
transcript, and checks that the reference actually reaches the branches above.
Both full replays run in `check.yml` on Linux and macOS for `refactor/**` PRs.
To inspect a single case, add `--case protect-classic-json` to `capture_api.py`.

### Recording and intentional fixes

Three situations, and only the second and third touch `record_api.py`. Five
changes in a row took the third command for the second, from this page as it
stood: each froze the version of the day into the reference. Find yours
first.

**1. A refactor, or a fix that changes what is printed and not what is asked
of the host.** Replay; never record. The inputs stay as they are:

```sh
python3 tests/golden/capture_api.py /absolute/new/api-baseline --source /absolute/socle
diff -ru /absolute/socle/tests/golden/api/baseline /absolute/new/api-baseline
```

A refactor needs that diff empty. A fix explains every line of it, with the
diff of the first reference, and copies the reviewed outputs into
`api/baseline` in a separate commit of its PR.

**2. A fix that changes the calls a recorded command makes** — one more git
read, one more GitHub read, a lookup, another order. It shows itself: the
replay above stops on `unrecorded host call`, and `tests/test_api_golden.py`
is red. Such a change cannot replay the inputs it changes, so it records them
again **under the version the reference is frozen at, and offline**:

```sh
cp /absolute/socle/tests/golden/api/inputs/responses.json /absolute/new/responses.json
python3 tests/golden/record_api.py /absolute/new/api-inputs \
  --source /absolute/socle --commit FULL_FIX_COMMIT \
  --reference /absolute/socle/tests/golden/api/inputs \
  --responses-cache /absolute/new/responses.json --offline
python3 tests/golden/capture_api.py /absolute/new/api-baseline \
  --source /absolute/socle --inputs /absolute/new/api-inputs
```

Both options, always, for this case:

- `--reference` runs the candidate's code under the `socle/VERSION` and
  `socle/CHANGELOG.md` of the existing inputs, as `capture.py --candidate`
  does for the other reference; those inputs are verified digest by digest
  before they are used, as a replay verifies the ones it reads. Without it
  the recording freezes the VERSION and the changelog of the commit at hand:
  every fixture pins another version, every list of versions a product is
  behind on grows, and the diff holds the releases cut since the last
  recording instead of the calls that moved.
- `--offline` refuses any read the cache does not answer, so that "nothing is
  read live" is held by the tool and not by whoever ran it. A cache copied
  from `responses.json` answers every read already recorded; a fix that
  needs a new one fails naming the endpoint, and that endpoint is then read
  in a recording of its own, reviewed as a new input.

Then check what was written before reviewing anything else:
`git diff --stat` shows nothing under `api/inputs/socle/`; `manifest.json`
says the `frozenVersion` it said before, with `sourceCommit` the fix and
`sourceVersion` the VERSION that commit carries; and `responses.json` holds
the answers it held, possibly in another order — the same count, read and
derived. If the frozen version moved, the recording was made without
`--reference`: discard it and record again. Copy `api-inputs` over
`api/inputs` and `api-baseline` over `api/baseline`, replay once more into
a new directory and require an empty diff, and say in the commit which calls
each case gained or lost. The first reference is recorded in the same PR, by
`capture.py --record --candidate` above, when `check`, `adopt`,
`declarations` or the catalog say anything else than they did: it is
replayed in CI on `refactor/**` branches only, and a reference left behind
is found by whoever comes next.

**3. The bootstrap, or a deliberate refresh of what GitHub answered.** The
only recording that reads GitHub, for reads only, and the only one that
freezes a new version. `record_api.py` refuses a different HEAD, a dirty
source, an existing destination or a self-test environment:

```sh
python3 tests/golden/record_api.py /absolute/new/api-inputs \
  --source /absolute/clean/approved-socle --commit FULL_APPROVED_COMMIT \
  --responses-cache /absolute/new/first-read-responses.json
python3 tests/golden/capture_api.py /absolute/new/api-baseline \
  --source /absolute/clean/approved-socle --inputs /absolute/new/api-inputs
```

The explicit cache path preserves first reads across interrupted recordings.
A new cache means a deliberate live refresh, requiring a separately reviewed
input diff. Review the complete provenance, input and output diff before
copying the new directories into `api/inputs` and `api/baseline`. Not for a
fix, and never to make a refactor pass: a changed host call sequence is
itself a reviewable difference, and updating inputs needs an explicit
explanation, not a fallback or an automatic recording.

In every case `record_api.py` answers for the machine and withholds what the
reference does not publish, as said above, whatever the cache holds.

The replay manifest's `sourceCommit` identifies the implementation that
recorded the **inputs**; it remains fixed while comparing candidate outputs.

### Limits

The new reference covers plans and local adoption, not the remote writes of
`protect`, `cut`, `tap` or `migrate`. Stateful host tests remain responsible for
writes and their verification reads; property tests still verify preservation
of non-check protection settings. Neither reference covers every command or
every API branch (pagination and all channel/gate combinations, for example).

Neither reference knows the machine it was recorded on. The privacy guard of
`check` asks the host for `hostname` and `scutil`, and a reference is
replayed on every machine and belongs to none: `record_api.py` answers that
neither tool exists, and the initial reference gets the same answer from its
allowlisted PATH, which holds neither. The name of the recording machine is
therefore never asked for and never enters a recorded file, and every
recorded `check` says the machine's name was not looked for. The author's
private list is read under HOME, which both captures replace; and neither
capture is a CI runner's, whatever it is replayed on: the replay answers so,
and the initial capture builds its environment from nothing.

The guard asks git which tracked files hold a shape: two `git grep` calls
whose patterns are arguments, recorded as they are. With a local name to
look for the patterns go to git on its standard input instead, through
`Host.feed`, which the recorder and the replay refuse — so a reference can
only be recorded where no local name is known, which is how it is recorded.
`check --history` is outside both references for the same reason: it reads
a history through `Host.feed`, and it is held by `tests/test_privacy.py` on
repositories built by the tests.

The recorded implementation allows local adoption when protection cannot be
read: no lock was detected. This is captured explicitly, not endorsed by the
reference. Changing it requires a behavior fix and an explained golden diff.
Likewise, `protect` can return 0 with `unread` diagnostics; absence and unknown
remain distinct recorded results. Approval of this extension is required before
the deferred command extractions resume.
