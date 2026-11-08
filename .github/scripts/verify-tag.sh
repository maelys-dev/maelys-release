#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# The tag gate of a Maelys release: what must hold of a tag before anything
# is built from it or deployed from it.
#
# Two workflows run it, and neither carries a copy. release.yml runs it in
# its verify job, before it builds; verify-tag.yml runs it for a product
# that deploys its own way and calls the gate before its deploy job. Both
# fetch this file from the socle at the commit their job runs,
# job.workflow_sha, exactly as they fetch share/allowed-signers: one text,
# read at the commit the product pinned, so a correction made here reaches
# both at the product's next adoption and never diverges between them.
#
#   verify-tag.sh tag     VERSION, an annotated tag GitHub verifies, naming the checkout
#   verify-tag.sh signer  the tag's signature, by a key the socle names, and the socle pinned
#   verify-tag.sh commit  the tag's commit, as commit_verification asks
#   verify-tag.sh caller  the workflow file that calls verify-tag.yml, for `signer` to read
#
# Run from the product's checkout at the tag. Every refusal says why on
# stderr and exits 1, and none of them falls back to GitHub's verdict.
set -euo pipefail

refuse() { echo "$1" >&2; exit 1; }

# VERSION, the annotated tag, GitHub's verdict on it, and the commit it names.
# env: TAG GH_TOKEN GITHUB_REPOSITORY GITHUB_OUTPUT
check_tag() {
  local ref object head
  test "v$(cat VERSION)" = "$TAG"
  ref="$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}")"
  test "$(jq -r .object.type <<<"$ref")" = tag || { echo "$TAG is not an annotated tag" >&2; exit 1; }
  object="$(gh api "repos/${GITHUB_REPOSITORY}/git/tags/$(jq -r .object.sha <<<"$ref")")"
  test "$(jq -r .verification.verified <<<"$object")" = true || { echo "$TAG is not verified by GitHub" >&2; exit 1; }
  head="$(git rev-parse HEAD)"
  test "$(jq -r .object.sha <<<"$object")" = "$head" || { echo "$TAG does not name $head" >&2; exit 1; }
  {
    echo "tag=$TAG"
    echo "version=${TAG#v}"
    echo "commit=$head"
  } >>"$GITHUB_OUTPUT"
}

# GitHub's `verified` says the key belongs to some account, and every
# account that may push a tag to this repository has such a key. This says
# *which* key: the allowed signers travel with the socle commit the product
# pinned, are read at that commit, and a tag signed by anything else stops
# here -- before a single artifact is built or anything is deployed.
#
# Fail closed at every step: no socle commit, a socle this tag did not pin,
# no file, an empty file, no matching principal, a signature that does not
# verify. None of them falls back to GitHub's verdict.
#
# env: TAG GH_TOKEN GITHUB_REPOSITORY RUNNER_TEMP
#      SOCLE SOCLE_SHA  the workflow that defines this job, as job.workflow_* names it. The first
#                       candidate read github.job_workflow_sha, which GitHub calls a documentation
#                       bug -- it exists as an OIDC claim and never in this context -- and the pilot
#                       measured it empty on its v0.2.0, run 36116807658, which cost that repository
#                       a version. job.workflow_* is what GitHub fills.
#      PIN_FILE         the product's workflow that names the socle commit: its release.yml for
#                       release.yml, the caller of the gate for verify-tag.yml
#      PIN_WORKFLOW     the socle workflow that line calls: release.yml or verify-tag.yml
check_signer() {
  local pattern pinned signers ref seen signed_at principal
  test -n "$SOCLE_SHA" || refuse "the socle judging ${TAG} is unknown: job.workflow_sha is empty"
  test -n "$SOCLE" || refuse "the socle judging ${TAG} is unknown: job.workflow_repository is empty"
  case "$SOCLE_SHA" in
    *[!0-9a-f]*|"") refuse "the socle judging ${TAG} is not a commit: ${SOCLE_SHA}" ;;
  esac
  test "${#SOCLE_SHA}" = 40 || refuse "the socle judging ${TAG} is not a commit: ${SOCLE_SHA}"
  case "$PIN_WORKFLOW" in
    ""|*[!a-z0-9.-]*) refuse "the gate was given no socle workflow to read the pin of: '${PIN_WORKFLOW}'" ;;
  esac
  # The socle this run executes must be the one this tag pinned. On a tag
  # push they are one file; on a replay they agree only when the replay
  # started from the tag, which is the only replay the conventions allow --
  # and one started from a branch is refused here, in seconds, rather than
  # by the environment after three builds.
  pattern="${SOCLE}/\.github/workflows/${PIN_WORKFLOW//./\\.}@[0-9a-f]{40}"
  pinned="$( { grep -Eo "$pattern" "$PIN_FILE" || true; } | sed -E 's/.*@//' | sort -u)"
  case "$pinned" in
    "$SOCLE_SHA") ;;
    "") refuse "${TAG} names no ${SOCLE} commit in ${PIN_FILE}: the socle judging it is not the one it pinned" ;;
    *) refuse "this run executes ${SOCLE} at ${SOCLE_SHA} but ${TAG} pinned $(tr '\n' ' ' <<<"$pinned"): replay it with --ref ${TAG}" ;;
  esac
  signers="$RUNNER_TEMP/allowed-signers"
  gh api "repos/${SOCLE}/contents/share/allowed-signers?ref=${SOCLE_SHA}" --jq .content \
    >"$RUNNER_TEMP/signers.b64" \
    || refuse "${SOCLE} at ${SOCLE_SHA} has no share/allowed-signers, or this token cannot read it"
  base64 -d <"$RUNNER_TEMP/signers.b64" >"$signers"
  test -s "$signers" || refuse "${SOCLE} at ${SOCLE_SHA} names no allowed signer"
  ref="$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}" --jq .object.sha)"
  gh api "repos/${GITHUB_REPOSITORY}/git/tags/${ref}" >"$RUNNER_TEMP/tag.json"
  # -j: the payload is signed byte for byte, and a trailing newline jq would
  # add of its own is one byte too many.
  jq -rj .verification.payload "$RUNNER_TEMP/tag.json" >"$RUNNER_TEMP/payload"
  jq -rj .verification.signature "$RUNNER_TEMP/tag.json" >"$RUNNER_TEMP/signature"
  test -s "$RUNNER_TEMP/signature" || refuse "${TAG} carries no signature GitHub could read"
  grep -q '^-----BEGIN SSH SIGNATURE-----' "$RUNNER_TEMP/signature" \
    || refuse "${TAG} carries no ssh signature: a Maelys release is signed with an ssh key"
  # An sshsig carries no timestamp of its own, so ssh-keygen judges
  # valid-after and valid-before against the clock of whoever runs it.
  # Judged at the clock, a key retired with valid-before stops verifying
  # every tag it ever signed, and a replay -- which is how a failed
  # publication is repeated, since a published tag is never moved -- fails
  # on the day of the retirement.
  #
  # The tag's own tagger date fixes that and opens a worse hole: it sits in
  # the payload the signer writes, so a stolen key backdates its tag and
  # walks through its own retirement. Measured.
  #
  # GitHub's verified_at is neither: it is the moment GitHub saw the tag,
  # recorded once, and the signer does not write it. A replay reads the same
  # tag object and gets the same moment, so an old release still verifies;
  # a tag pushed after the retirement cannot claim to be old. Absent, this
  # stops: a verdict without its date is not a verdict.
  seen="$(jq -r '.verification.verified_at // empty' "$RUNNER_TEMP/tag.json")"
  test -n "$seen" || refuse "${TAG} carries no verified_at: GitHub did not say when it saw this tag"
  signed_at="$(printf '%s' "$seen" | tr -d ':-' | tr 'T' ' ' | cut -d' ' -f1,2 | tr -d ' ')"
  case "$signed_at" in ''|*[!0-9Zz]*) refuse "${TAG} has an unreadable verified_at: ${seen}" ;; esac
  principal="$(ssh-keygen -Y find-principals -O "verify-time=${signed_at}" \
    -f "$signers" -s "$RUNNER_TEMP/signature" | head -n 1)" \
    || refuse "${TAG} is signed by a key ${SOCLE} did not name on ${signed_at}: it may not publish here"
  test -n "$principal" || refuse "${TAG} is signed by a key ${SOCLE} does not name"
  ssh-keygen -Y verify -O "verify-time=${signed_at}" -f "$signers" -I "$principal" -n git \
    -s "$RUNNER_TEMP/signature" <"$RUNNER_TEMP/payload"
  echo "${TAG} is signed by ${principal}, named by ${SOCLE} at ${SOCLE_SHA}, allowed on ${signed_at}"
}

# A signed tag says who published; this says what was published: the commit
# carries its own verified signature, and optionally landed on the default
# branch through the branch's own rules.
# env: MODE GH_TOKEN GITHUB_REPOSITORY
check_commit() {
  local head commit branch
  case "$MODE" in
    signed|signed-on-default-branch) ;;
    *) echo "commit_verification must be none, signed or signed-on-default-branch, not $MODE" >&2; exit 1 ;;
  esac
  head="$(git rev-parse HEAD)"
  commit="$(gh api "repos/${GITHUB_REPOSITORY}/commits/${head}")"
  test "$(jq -r .commit.verification.verified <<<"$commit")" = true \
    || { echo "the commit $head is not signed and verified by GitHub" >&2; exit 1; }
  if [ "$MODE" = signed-on-default-branch ]; then
    branch="$(gh api "repos/${GITHUB_REPOSITORY}" --jq .default_branch)"
    git fetch -q origin "$branch"
    git merge-base --is-ancestor "$head" "origin/$branch" \
      || { echo "the commit $head is not an ancestor of $branch: it never landed there" >&2; exit 1; }
  fi
}

# The workflow file that calls verify-tag.yml, which is where the product
# names the socle commit the gate must be. GitHub gives it as
# github.workflow_ref, OWNER/REPO/PATH@REF, for the top-level workflow of
# the run: a gate called through an intermediate workflow of the product's
# own is read in the top one, which does not pin it, and is refused.
# env: WORKFLOW_REF GITHUB_REPOSITORY GITHUB_OUTPUT
check_caller() {
  local path file
  path="${WORKFLOW_REF%%@*}"
  file="${path#"${GITHUB_REPOSITORY}/"}"
  test -n "$WORKFLOW_REF" || refuse "GitHub did not name the workflow calling this gate: github.workflow_ref is empty"
  test "$path" = "${GITHUB_REPOSITORY}/${file}" \
    || refuse "the workflow calling this gate is not one of ${GITHUB_REPOSITORY}: ${WORKFLOW_REF}"
  case "$file" in
    */../*|*/./*) refuse "the workflow calling this gate is not a plain path: ${WORKFLOW_REF}" ;;
    .github/workflows/*.yml|.github/workflows/*.yaml) ;;
    *) refuse "the workflow calling this gate is not under .github/workflows: ${WORKFLOW_REF}" ;;
  esac
  echo "file=$file" >>"$GITHUB_OUTPUT"
}

case "${1:-}" in
  tag) check_tag ;;
  signer) check_signer ;;
  commit) check_commit ;;
  caller) check_caller ;;
  *) refuse "usage: verify-tag.sh tag|signer|commit|caller" ;;
esac
