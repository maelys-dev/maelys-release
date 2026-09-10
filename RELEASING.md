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
3. `bin/maelys-release cut . X.Y.Z --apply` on `main`, up to date, with the
   dated changelog entry written and `VERSION` left alone: it holds the gate
   (signing configuration, previous tag, free `vX.Y.Z`; the release
   environment does not apply here), writes `VERSION`, commits it signed on
   `release/vX.Y.Z`, opens the pull request and waits for its checks.
4. Merge that pull request with its CI green, then
   `bin/maelys-release cut . X.Y.Z --tag --apply`: it tags the merge commit
   whose checks passed and pushes the signed tag. The command takes the
   repository as an operand and never `cd`s: a tag has been created in the
   wrong repository that way.
5. Tell the products. They re-adopt at their next release, never in a
   dedicated pull request; the changelog names what they must change by hand.

A published tag is never moved, deleted or force-pushed.
