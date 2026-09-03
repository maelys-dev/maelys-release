#!/bin/sh
# Exercises adopt.sh on a throwaway product: prerequisites, plan, apply,
# idempotence, check, the managed-block replacement, the generated dependency
# checkouts, the package declaration and the preflight.
set -eu
self=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/maelys-release-selftest.XXXXXX")
work=${work%/}
trap 'rm -rf "$work"' EXIT HUP INT TERM
# The host's git configuration must not make the preflight pass or fail.
GIT_CONFIG_GLOBAL=/dev/null; GIT_CONFIG_SYSTEM=/dev/null; GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_NOSYSTEM
git() { command git -c user.name=self-test -c user.email=self-test@example.invalid -c init.defaultBranch=main "$@"; }
adopt="$self/scripts/adopt.sh"
refused() { # refused MESSAGE COMMAND...: the command must fail
    message=$1; shift
    if "$@" >/dev/null 2>&1; then echo "self-test: $message" >&2; exit 1; fi
}
expect_status() { # expect_status STATUS COMMAND...
    wanted=$1; shift
    status=0
    "$@" >/dev/null 2>&1 || status=$?
    test "$status" = "$wanted" || { echo "self-test: expected exit $wanted, got $status: $*" >&2; exit 1; }
}

# a dependency repository the fixture pins: one tag, one commit past it
dependency="$work/src/maelys-system"
mkdir -p "$dependency" "$work/remotes"
git -C "$dependency" init -q
printf 'one\n' >"$dependency/file" && git -C "$dependency" add file && git -C "$dependency" commit -q -m one
git -C "$dependency" tag v0.0.1
printf 'two\n' >"$dependency/file" && git -C "$dependency" commit -q -am two
pinned=$(git -C "$dependency" rev-parse HEAD)
git clone -q --bare "$dependency" "$work/remotes/maelys-system.git"
git -C "$work/remotes/maelys-system.git" config uploadpack.allowFilter true

product="$work/maelys-fixture"
mkdir -p "$product/scripts" "$product/packaging/homebrew" "$product/adapter"
printf '1.2.3\n' >"$product/VERSION"
printf '# Changelog\n\n## Unreleased\n\n## 1.2.3 — 2026-09-03\n\n- Something.\n' >"$product/CHANGELOG.md"
printf '#!/bin/sh\nexit 0\n' >"$product/scripts/package-release.sh"
chmod +x "$product/scripts/package-release.sh"
printf 'v0.0.1-1-g%s\n%s\n' "$(printf '%s' "$pinned" | cut -c1-7)" "$pinned" >"$product/adapter/MAELYS_SYSTEM_PIN"
printf '# build inputs\n[linux]\npkg-config\nlibjansson-dev\n\n[macos]\njansson\n' >"$product/adapter/PACKAGES"
printf 'class MaelysFixture < Formula\nend\n' >"$product/packaging/homebrew/maelys-fixture.rb.in"
printf 'class LibmaelysFixture < Formula\nend\n' >"$product/packaging/homebrew/libmaelys-fixture.rb.in"
printf '# Agent instructions\n\nKeep me.\n' >"$product/AGENTS.md"

# ---- prerequisites are refused one by one ------------------------------------
rm "$product/VERSION"
refused "missing VERSION accepted" "$adopt" "$product"
printf '1.2.3\n' >"$product/VERSION"
cp "$product/CHANGELOG.md" "$work/changelog"
printf '# Changelog\n\n## Unreleased\n\n- Something.\n' >"$product/CHANGELOG.md"
refused "undated changelog entry accepted" "$adopt" "$product"
cp "$work/changelog" "$product/CHANGELOG.md"
printf '#!/bin/sh\nexit 0\n' >"$product/scripts/checkout-system.sh"
refused "product checkout script accepted" "$adopt" "$product"
rm "$product/scripts/checkout-system.sh"
cp "$product/adapter/MAELYS_SYSTEM_PIN" "$work/pin"
printf 'v0.0.1\nnot-a-commit\n' >"$product/adapter/MAELYS_SYSTEM_PIN"
refused "pin without a commit accepted" "$adopt" "$product"
cp "$work/pin" "$product/adapter/MAELYS_SYSTEM_PIN"
cp "$product/adapter/PACKAGES" "$work/packages"
printf 'pkg-config\n[linux]\nlibjansson-dev\n' >"$product/adapter/PACKAGES"
refused "package outside a section accepted" "$adopt" "$product"
printf '[windows]\nfoo\n' >"$product/adapter/PACKAGES"
refused "unknown package section accepted" "$adopt" "$product"
printf '[linux]\nfoo bar\n' >"$product/adapter/PACKAGES"
refused "two packages on one line accepted" "$adopt" "$product"
cp "$work/packages" "$product/adapter/PACKAGES"

# ---- plan, apply, generated workflow ----------------------------------------
"$adopt" "$product" | grep '^create   .github/workflows/release.yml' >/dev/null
expect_status 2 "$adopt" "$product" --check
"$adopt" "$product" --apply >/dev/null
workflow="$product/.github/workflows/release.yml"
test -f "$workflow"
grep -q 'product: maelys-fixture' "$workflow"
grep -q '^  id-token: write' "$workflow"
grep -q '^  workflow_dispatch:' "$workflow"
grep -q "^    if: github.event_name == 'push'" "$workflow"
# shellcheck disable=SC2016 # GitHub expression, literal here
grep -q '^      tag: ${{ inputs.tag }}' "$workflow"
test "$(grep -c '^      id-token: write' "$workflow")" -ge 2
grep -q '^        sh scripts/checkout-dependency.sh maelys-system$' "$workflow"
grep -q '^      linux_packages: build-essential dpkg-dev file rpm pkg-config libjansson-dev$' "$workflow"
grep -q '^      macos_packages: jansson$' "$workflow"
grep -q 'tap-maelys-fixture:' "$workflow"
grep -q 'tap-libmaelys-fixture:' "$workflow"
grep -q 'product: libmaelys-fixture' "$workflow"
grep -q 'Keep me.' "$product/AGENTS.md"
grep -q 'maelys-release:begin' "$product/AGENTS.md"
# the agent block and the skill name the real formula templates
grep -q 'packaging/homebrew/libmaelys-fixture.rb.in, packaging/homebrew/maelys-fixture.rb.in' "$product/AGENTS.md"
grep -q 'packaging/homebrew/libmaelys-fixture.rb.in' "$product/.claude/skills/maelys-release/SKILL.md"
if grep -q '@FORMULAS@\|@PRODUCT@' "$product/AGENTS.md" "$product/.claude/skills/maelys-release/SKILL.md"; then echo "self-test: placeholder unrendered" >&2; exit 1; fi
test -f "$product/CLAUDE.md" && test -f "$product/RELEASING.md"
test -f "$product/.claude/skills/maelys-release/SKILL.md"
test -x "$product/scripts/checkout-dependency.sh"

# ---- the managed checkout clones the pin -------------------------------------
checkout="$product/scripts/checkout-dependency.sh"
MAELYS_GIT_BASE="file://$work/remotes" "$checkout" maelys-system "$work/maelys-system" >/dev/null
test "$(git -C "$work/maelys-system" rev-parse HEAD)" = "$pinned"
expect_status 1 env MAELYS_GIT_BASE="file://$work/remotes" "$checkout" maelys-system "$work/maelys-system"
expect_status 66 env MAELYS_GIT_BASE="file://$work/remotes" "$checkout" maelys-json "$work/maelys-json"
expect_status 64 "$checkout" 'Bad Name'

# ---- idempotence and drift ---------------------------------------------------
"$adopt" "$product" --check >/dev/null
"$adopt" "$product" --apply >/dev/null
test "$(grep -c 'maelys-release:begin' "$product/AGENTS.md")" = 1
printf '\n# edited\n' >>"$workflow"
expect_status 2 "$adopt" "$product" --check
"$adopt" "$product" --apply >/dev/null
chmod -x "$checkout"
expect_status 2 "$adopt" "$product" --check
"$adopt" "$product" --apply >/dev/null
test -x "$checkout"
"$adopt" "$product" --check >/dev/null

# ---- preflight ----------------------------------------------------------------
git -C "$product" init -q
git -C "$product" add -A && git -C "$product" commit -q -m fixture
expect_status 3 "$adopt" "$product" --preflight            # no signing configuration
git -C "$product" config tag.gpgsign true
git -C "$product" config gpg.format ssh
git -C "$product" config user.signingkey "$work/signing-key"
"$adopt" "$product" --preflight >/dev/null                 # no tag yet, no origin
git -C "$product" -c tag.gpgsign=false tag v1.0.0
expect_status 3 "$adopt" "$product" --preflight            # lightweight tag
git -C "$product" tag -d v1.0.0 >/dev/null
git -C "$product" -c tag.gpgsign=false tag -a v1.0.0 -m unsigned
expect_status 3 "$adopt" "$product" --preflight            # unsigned tag
git -C "$product" tag -d v1.0.0 >/dev/null
if command -v ssh-keygen >/dev/null 2>&1; then
    ssh-keygen -q -t ed25519 -N '' -f "$work/signing-key"
    git -C "$product" tag -s v1.0.0 -m signed
    "$adopt" "$product" --preflight >/dev/null             # signed previous tag
    git -C "$product" -c tag.gpgsign=false tag v1.2.3
    expect_status 3 "$adopt" "$product" --preflight        # VERSION already tagged
    git -C "$product" tag -d v1.2.3 >/dev/null
fi

# ---- a product without a formula template gets no tap job ---------------------
rm "$product/packaging/homebrew/maelys-fixture.rb.in" "$product/packaging/homebrew/libmaelys-fixture.rb.in"
"$adopt" "$product" --apply >/dev/null
if grep -q 'tap.yml@' "$workflow"; then echo "self-test: tap job kept without template" >&2; exit 1; fi
grep -q 'packaging/homebrew/<name>.rb.in' "$product/AGENTS.md"
# a product without pins gets no checkout script line
rm "$product/adapter/MAELYS_SYSTEM_PIN"
"$adopt" "$product" --apply >/dev/null
if grep -q 'dependency_checkout' "$workflow"; then echo "self-test: checkout kept without pin" >&2; exit 1; fi
echo "self-test: adopt.sh ok"
