# Releasing maelys-release

This repository publishes no package: its release is a signed tag that
products pin by commit. The socle does not release itself through its own
workflows, so `preflight` and `rehearse` do not apply here.

1. On `main`: `VERSION` = `X.Y.Z`, a dated `CHANGELOG.md` entry
   `## X.Y.Z — YYYY-MM-DD`, and `bin/maelys-release self-test` green.
2. **Try the candidate on one product**, in a scratch clone, never in the
   product's own checkout:

   ```sh
   git clone https://github.com/maelys-dev/<product> /tmp/<product>-trial
   bin/maelys-release adopt /tmp/<product>-trial --product <product> \
     --allow-untagged --apply
   bin/maelys-release check /tmp/<product>-trial --product <product>
   ```

   Push that as a branch of the product, wait for its CI, and read the
   verdict explicitly before going further. A trial PR is closed, never
   merged.
3. Merge the socle's own pull request with its CI green.
4. `git -C /abs/path/maelys-release tag -s vX.Y.Z -m "maelys-release X.Y.Z"`
   then push the tag. Never `cd`: a tag has been created in the wrong
   repository that way.
5. Tell the products. They re-adopt at their next release, never in a
   dedicated pull request; the changelog names what they must change by hand.

A published tag is never moved, deleted or force-pushed.
