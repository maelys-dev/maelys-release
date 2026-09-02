#!/usr/bin/env bash
# usage: update-tap.sh PRODUCT TAG FORMULA_PATH
# Publishes one rendered formula into the Maelys Homebrew tap with a signed
# commit. Environment: TAP_REPOSITORY (default maelys-dev/homebrew-tap),
# TAP_TOKEN (push credential), TAP_SIGNING_KEY (SSH private key text, optional),
# GIT_AUTHOR_NAME/EMAIL, DRY_RUN=1 to print the diff without pushing.
set -euo pipefail
product=${1:?PRODUCT}
tag=${2:?TAG}
formula=${3:?FORMULA_PATH}
tap_repository=${TAP_REPOSITORY:-maelys-dev/homebrew-tap}
version=${tag#v}
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 PRODUCT vX.Y.Z FORMULA" >&2; exit 64; }
test -f "$formula" || { echo "missing rendered formula: $formula" >&2; exit 66; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if [ -n "${TAP_TOKEN:-}" ]; then
    git clone -q "https://x-access-token:${TAP_TOKEN}@github.com/${tap_repository}.git" "$tmp/tap"
else
    git clone -q "https://github.com/${tap_repository}.git" "$tmp/tap"
fi
mkdir -p "$tmp/tap/Formula"
cp "$formula" "$tmp/tap/Formula/${product}.rb"
if command -v brew >/dev/null 2>&1; then brew style "$tmp/tap/Formula/${product}.rb"; fi
git -C "$tmp/tap" add "Formula/${product}.rb"
if git -C "$tmp/tap" diff --cached --quiet; then
    echo "tap already carries ${product} ${version}"
    exit 0
fi
if [ -n "${DRY_RUN:-}" ]; then
    git -C "$tmp/tap" diff --cached
    exit 0
fi
sign=()
if [ -n "${TAP_SIGNING_KEY:-}" ]; then
    key="$tmp/signing-key"
    printf '%s\n' "$TAP_SIGNING_KEY" >"$key"
    chmod 0600 "$key"
    sign=(-c gpg.format=ssh -c "user.signingkey=$key" -c commit.gpgsign=true)
fi
git -C "$tmp/tap" "${sign[@]}" \
    -c "user.name=${GIT_AUTHOR_NAME:-maelys-release}" \
    -c "user.email=${GIT_AUTHOR_EMAIL:-maelys-release@users.noreply.github.com}" \
    commit -q -m "Update ${product} to ${version}"
git -C "$tmp/tap" push -q origin HEAD:main
echo "brew install ${tap_repository%/homebrew-*}/${tap_repository#*/homebrew-}/${product}"
