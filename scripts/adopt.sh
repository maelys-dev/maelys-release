#!/bin/sh
# Adopt, upgrade or check the maelys-release socle in a product repository.
#
#   scripts/adopt.sh DIR [--product NAME] [--socle-sha SHA --socle-tag TAG]
#   scripts/adopt.sh DIR --apply     write the files
#   scripts/adopt.sh DIR --check     exit 2 when any managed file drifts
#
# Managed files: .github/workflows/release.yml (whole file), the
# maelys-release block of AGENTS.md and CLAUDE.md, the Claude skill
# .claude/skills/maelys-release/SKILL.md, and RELEASING.md (created only when
# absent). Without --apply nothing is written and every planned change is
# printed. The socle version is the checkout this script runs from.
set -eu

self=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
project=''
product=''
apply=0
check=0
socle_sha=''
socle_tag=''
while [ $# -gt 0 ]; do
    case $1 in
        --apply) apply=1 ;;
        --check) check=1 ;;
        --product) product=${2:?--product NAME}; shift ;;
        --socle-sha) socle_sha=${2:?--socle-sha SHA}; shift ;;
        --socle-tag) socle_tag=${2:?--socle-tag TAG}; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        -*) echo "adopt: unknown option $1" >&2; exit 64 ;;
        *) if [ -n "$project" ]; then echo "adopt: one directory only" >&2; exit 64; fi; project=$1 ;;
    esac
    shift
done
test -n "$project" || { echo "usage: adopt.sh DIR [--product NAME] [--apply|--check]" >&2; exit 64; }
project=$(CDPATH='' cd -- "$project" && pwd)
test -z "$product" && product=$(basename "$project")
case $product in *[!a-z0-9-]*|'') echo "adopt: product name must be [a-z0-9-]: $product" >&2; exit 64 ;; esac
if [ -z "$socle_sha" ]; then
    socle_sha=$(git -C "$self" rev-parse HEAD)
    socle_tag=$(git -C "$self" describe --tags --exact-match 2>/dev/null || printf 'untagged')
fi
test -n "$socle_tag" || socle_tag=untagged
socle_version=$(cat "$self/VERSION")

# ---- prerequisites ---------------------------------------------------------
fail=0
require_file() { if [ ! -f "$1" ]; then echo "MISSING  $2" >&2; fail=1; else echo "ok       $2"; fi; }
require_exec() { if [ ! -x "$1" ]; then echo "MISSING  $2" >&2; fail=1; else echo "ok       $2"; fi; }
require_file "$project/VERSION" "VERSION file"
if [ -f "$project/VERSION" ] && ! grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' "$project/VERSION"; then
    echo "MISSING  VERSION must be X.Y.Z" >&2; fail=1
fi
require_exec "$project/scripts/package-release.sh" "scripts/package-release.sh TARGET writing dist/"
require_file "$project/CHANGELOG.md" "CHANGELOG.md"
# Every packaging/homebrew/NAME.rb.in becomes one tap job publishing NAME:
# a repository may publish several formulas (a command and a library) and a
# formula is named after what it installs, not after the repository.
formulas=$(find "$project/packaging/homebrew" -maxdepth 1 -name '*.rb.in' 2>/dev/null | sed 's|.*/||; s|\.rb\.in$||' | sort)
if [ -n "$formulas" ]; then echo "ok       Homebrew formula templates: $(printf '%s' "$formulas" | tr '\n' ' ')"
else echo "note     no packaging/homebrew/*.rb.in: no tap job"; fi
# The agent block and skill name the real template files, not the product.
formula_paths=$(printf '%s\n' "$formulas" | sed '/^$/d; s|^|packaging/homebrew/|; s|$|.rb.in|' | paste -sd ',' - | sed 's|,|, |g')
test -n "$formula_paths" || formula_paths='packaging/homebrew/<name>.rb.in'
render_command=''
if [ -x "$project/scripts/render-homebrew-formula.sh" ]; then
    render_command="sh scripts/render-homebrew-formula.sh TAG OUTPUT"
    echo "ok       scripts/render-homebrew-formula.sh renders the formula"
fi
checkouts=$(find "$project/scripts" -maxdepth 1 -name 'checkout-*.sh' 2>/dev/null | sed "s|$project/||" | sort)
if [ -n "$checkouts" ]; then echo "ok       dependency checkouts: $(printf '%s' "$checkouts" | tr '\n' ' ')"; fi
test "$fail" -eq 0 || { echo "adopt: prerequisites missing in $project" >&2; exit 65; }

# ---- render the managed files into a staging tree ---------------------------
stage=$(mktemp -d "${TMPDIR:-/tmp}/maelys-release-adopt.XXXXXX")
stage=${stage%/}
trap 'rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$stage/.github/workflows" "$stage/.claude/skills/maelys-release"

{
    printf 'name: release\n\n'
    printf '# Managed by maelys-release %s (%s). Regenerate with\n' "$socle_tag" "$socle_version"
    printf '# scripts/adopt.sh of maelys-release; do not edit by hand.\n'
    # A job that calls a reusable workflow cannot be granted more than the
    # caller workflow declares at the top level, so the ceiling is set here
    # and each job narrows it.
    printf 'on:\n  push:\n    tags: ["v*"]\n'
    printf '  workflow_dispatch:\n    inputs:\n      tag:\n'
    printf '        description: Existing signed tag whose Homebrew publication is replayed\n'
    printf '        required: true\n        type: string\n'
    printf '\npermissions:\n  contents: write\n  id-token: write\n  attestations: write\n\njobs:\n'
    printf '  release:\n'
    printf "    if: github.event_name == 'push'\n"
    printf '    uses: maelys-dev/maelys-release/.github/workflows/release.yml@%s # %s\n' "$socle_sha" "$socle_tag"
    printf '    permissions:\n      contents: write\n      id-token: write\n      attestations: write\n'
    printf '    with:\n      product: %s\n' "$product"
    if [ -n "$checkouts" ]; then
        printf '      dependency_checkout: |\n'
        printf '%s\n' "$checkouts" | sed 's|^|        |'
    fi
    for formula in $formulas; do
        printf '\n  tap-%s:\n    needs: release\n' "$formula"
        printf "    if: always() && (needs.release.result == 'success' || needs.release.result == 'skipped')\n"
        printf '    uses: maelys-dev/maelys-release/.github/workflows/tap.yml@%s # %s\n' "$socle_sha" "$socle_tag"
        printf '    permissions:\n      contents: write\n      id-token: write\n      attestations: write\n'
        printf '    with:\n      product: %s\n' "$formula"
        # shellcheck disable=SC2016 # GitHub expression, literal here
        printf '      tag: ${{ inputs.tag }}\n'
        if [ -n "$render_command" ]; then printf '      render_command: %s %s\n' "$render_command" "$formula"; fi
        printf '      bottles: %s\n' "'[\"macos-15\",\"macos-26\"]'"
        # shellcheck disable=SC2016 # GitHub expressions are literal here
        printf '    secrets:\n      tap_token: ${{ secrets.HOMEBREW_TAP_TOKEN }}\n      tap_signing_key: ${{ secrets.HOMEBREW_TAP_SIGNING_KEY }}\n'
    done
} >"$stage/.github/workflows/release.yml"

render_text() { sed -e "s|@PRODUCT@|$product|g" -e "s|@FORMULAS@|$formula_paths|g" -e "s|@SOCLE_TAG@|$socle_tag|g" -e "s|@SOCLE_VERSION@|$socle_version|g" "$1"; }
render_text "$self/share/agents/claude-skill.md" >"$stage/.claude/skills/maelys-release/SKILL.md"
render_text "$self/share/agents/instructions-block.md" >"$stage/block.md"

# managed block: replace between markers, or append
managed() {
    file=$1
    out=$2
    if [ -f "$file" ] && grep -q '<!-- maelys-release:begin -->' "$file"; then
        awk -v blockfile="$stage/block.md" '
            /<!-- maelys-release:begin -->/ {
                print "<!-- maelys-release:begin -->"
                while ((getline line < blockfile) > 0) print line
                close(blockfile)
                print "<!-- maelys-release:end -->"
                skip = 1
                next
            }
            /<!-- maelys-release:end -->/ { skip = 0; next }
            !skip { print }' "$file" >"$out"
    else
        { if [ -f "$file" ]; then cat "$file"; printf '\n'; fi
          printf '<!-- maelys-release:begin -->\n'
          cat "$stage/block.md"
          printf '<!-- maelys-release:end -->\n'; } >"$out"
    fi
}
managed "$project/AGENTS.md" "$stage/AGENTS.md"
managed "$project/CLAUDE.md" "$stage/CLAUDE.md"
if [ ! -f "$project/RELEASING.md" ]; then render_text "$self/share/templates/RELEASING.md" >"$stage/RELEASING.md"; fi

# ---- plan, check or apply ----------------------------------------------------
changed=0
for relative in .github/workflows/release.yml AGENTS.md CLAUDE.md .claude/skills/maelys-release/SKILL.md RELEASING.md; do
    test -f "$stage/$relative" || continue
    if [ ! -f "$project/$relative" ]; then
        echo "create   $relative"; changed=1
    elif ! cmp -s "$stage/$relative" "$project/$relative"; then
        echo "update   $relative"; changed=1
        if [ "$apply" -eq 0 ]; then diff -u "$project/$relative" "$stage/$relative" | sed 's/^/         /' || true; fi
    else
        echo "same     $relative"
    fi
    if [ "$apply" -eq 1 ]; then
        mkdir -p "$(dirname "$project/$relative")"
        cp "$stage/$relative" "$project/$relative"
    fi
done
if [ "$check" -eq 1 ]; then
    if [ "$changed" -eq 1 ]; then echo "adopt: $product drifts from maelys-release $socle_tag" >&2; exit 2; fi
    echo "adopt: $product is on maelys-release $socle_tag"
    exit 0
fi
if [ "$apply" -eq 1 ]; then echo "adopt: wrote maelys-release $socle_tag into $project"
elif [ "$changed" -eq 1 ]; then echo "adopt: plan only; add --apply to write"
else echo "adopt: nothing to do"; fi
