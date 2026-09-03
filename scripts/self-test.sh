#!/bin/sh
# Exercises adopt.sh on a throwaway product: prerequisites, plan, apply,
# idempotence, check, and the managed-block replacement.
set -eu
self=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/maelys-release-selftest.XXXXXX")
work=${work%/}
trap 'rm -rf "$work"' EXIT HUP INT TERM
product="$work/maelys-fixture"
mkdir -p "$product/scripts" "$product/packaging/homebrew"
printf '1.2.3\n' >"$product/VERSION"
printf '# Changelog\n' >"$product/CHANGELOG.md"
printf '#!/bin/sh\nexit 0\n' >"$product/scripts/package-release.sh"
chmod +x "$product/scripts/package-release.sh"
printf '#!/bin/sh\nexit 0\n' >"$product/scripts/checkout-system.sh"
printf 'class MaelysFixture < Formula\nend\n' >"$product/packaging/homebrew/maelys-fixture.rb.in"
printf 'class LibmaelysFixture < Formula\nend\n' >"$product/packaging/homebrew/libmaelys-fixture.rb.in"
printf '# Agent instructions\n\nKeep me.\n' >"$product/AGENTS.md"

# missing prerequisite is refused
rm "$product/VERSION"
if "$self/scripts/adopt.sh" "$product" >/dev/null 2>&1; then echo "self-test: missing VERSION accepted" >&2; exit 1; fi
printf '1.2.3\n' >"$product/VERSION"

"$self/scripts/adopt.sh" "$product" | grep -q '^create   .github/workflows/release.yml'
if "$self/scripts/adopt.sh" "$product" --check >/dev/null 2>&1; then echo "self-test: check passed before apply" >&2; exit 1; fi
"$self/scripts/adopt.sh" "$product" --apply >/dev/null
test -f "$product/.github/workflows/release.yml"
grep -q 'product: maelys-fixture' "$product/.github/workflows/release.yml"
grep -q '^  id-token: write' "$product/.github/workflows/release.yml"
test "$(grep -c '^      id-token: write' "$product/.github/workflows/release.yml")" -ge 2
grep -q 'scripts/checkout-system.sh' "$product/.github/workflows/release.yml"
grep -q 'tap-maelys-fixture:' "$product/.github/workflows/release.yml"
grep -q 'tap-libmaelys-fixture:' "$product/.github/workflows/release.yml"
grep -q 'product: libmaelys-fixture' "$product/.github/workflows/release.yml"
grep -q 'Keep me.' "$product/AGENTS.md"
grep -q 'maelys-release:begin' "$product/AGENTS.md"
test -f "$product/CLAUDE.md" && test -f "$product/RELEASING.md"
test -f "$product/.claude/skills/maelys-release/SKILL.md"
"$self/scripts/adopt.sh" "$product" --check >/dev/null
"$self/scripts/adopt.sh" "$product" --apply >/dev/null
test "$(grep -c 'maelys-release:begin' "$product/AGENTS.md")" = 1
# drift is detected
printf '\n# edited\n' >>"$product/.github/workflows/release.yml"
status=0
"$self/scripts/adopt.sh" "$product" --check >/dev/null 2>&1 || status=$?
test "$status" = 2
# a product without a formula template gets no tap job
rm "$product/packaging/homebrew/maelys-fixture.rb.in" "$product/packaging/homebrew/libmaelys-fixture.rb.in"
"$self/scripts/adopt.sh" "$product" --apply >/dev/null
if grep -q 'tap.yml@' "$product/.github/workflows/release.yml"; then echo "self-test: tap job kept without template" >&2; exit 1; fi
echo "self-test: adopt.sh ok"
