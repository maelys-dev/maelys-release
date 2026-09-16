# Behavioral reference

A refactoring PR replays the reference; it never records a new one:

```sh
python3 tests/golden/capture.py /absolute/new/replay
diff -ru tests/golden/baseline /absolute/new/replay
```

The `diff` must produce no output and exit 0. Paste the command, output and exit
code into every refactoring PR. `check.yml` also runs this comparison on Linux
and macOS for PRs whose head branch starts with `refactor/`.

The manifest pins the original fixture builders and the eight fleet checkouts.
Changing the current tests cannot change these inputs. The capture compares the
candidate's CLI as a subprocess, with no `gh`, and never applies the candidate
to a product. See `capture.py` for the normalization and isolation rules.

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
   identity. It does not re-resolve the fleet's main branches or silently replace
   the fixture builders with the tests just edited. `--reference` can select a
   different existing reference explicitly.
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

## Coverage still to add

This reference covers `describe`, `declarations`, `check` and the `adopt` plan.
Before moving `protect`, record API responses through the host and add the
`protect` and `preflight` plans. The pure extraction approved after stage 1
precedes that additional coverage. Include the `public`
and `classic-protection` selectors, the adoption guard and `current` behavior.
Until then, the no-gh reference cannot prove those GitHub-dependent branches
unchanged; the stateful host tests exercise writes and their verification reads.
