#!/bin/sh
# Adopt, upgrade or check the maelys-release socle in a product repository.
#
#   scripts/adopt.sh DIR [--product NAME] [--socle-sha SHA --socle-tag TAG]
#   scripts/adopt.sh DIR --apply      write the files
#   scripts/adopt.sh DIR --check      exit 2 when any managed file drifts
#   scripts/adopt.sh DIR --preflight  --check, then the release preconditions
#                                     of this repository and machine; exit 3
#
# Managed files: .github/workflows/release.yml (whole file),
# scripts/checkout-dependency.sh (when adapter/*_PIN files exist), the
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
preflight=0
socle_sha=''
socle_tag=''
while [ $# -gt 0 ]; do
    case $1 in
        --apply) apply=1 ;;
        --check) check=1 ;;
        --preflight) check=1; preflight=1 ;;
        --product) product=${2:?--product NAME}; shift ;;
        --socle-sha) socle_sha=${2:?--socle-sha SHA}; shift ;;
        --socle-tag) socle_tag=${2:?--socle-tag TAG}; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        -*) echo "adopt: unknown option $1" >&2; exit 64 ;;
        *) if [ -n "$project" ]; then echo "adopt: one directory only" >&2; exit 64; fi; project=$1 ;;
    esac
    shift
done
test -n "$project" || { echo "usage: adopt.sh DIR [--product NAME] [--apply|--check|--preflight]" >&2; exit 64; }
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
version=''
require_file "$project/VERSION" "VERSION file"
if [ -f "$project/VERSION" ]; then
    if grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' "$project/VERSION"; then version=$(cat "$project/VERSION")
    else echo "MISSING  VERSION must be X.Y.Z" >&2; fail=1; fi
fi
require_exec "$project/scripts/package-release.sh" "scripts/package-release.sh TARGET writing dist/"
require_file "$project/CHANGELOG.md" "CHANGELOG.md"
# The release of VERSION needs its dated entry; the workflow only compares
# VERSION with the tag, so the entry is checked here, before the tag exists.
if [ -n "$version" ] && [ -f "$project/CHANGELOG.md" ]; then
    if grep -Eq "^## $(printf '%s' "$version" | sed 's/\./\\./g')[[:space:]].*[0-9]{4}-[0-9]{2}-[0-9]{2}" "$project/CHANGELOG.md"; then
        echo "ok       CHANGELOG.md has a dated ## $version entry"
    else
        echo "MISSING  CHANGELOG.md needs a dated entry: ## $version — YYYY-MM-DD" >&2; fail=1
    fi
fi
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
# Dependencies: one adapter/<NAME>_PIN per Maelys repository, nearest tag on
# line 1 for humans and the pinned commit on line 2. The managed
# scripts/checkout-dependency.sh clones each of them from the pin, so the
# product writes no checkout script of its own.
dependencies=''
for pin in "$project"/adapter/*_PIN; do
    test -f "$pin" || continue
    name=$(basename "$pin" _PIN | tr '[:upper:]_' '[:lower:]-')
    if sed -n '2p' "$pin" | grep -Eq '^[0-9a-f]{40}$'; then
        dependencies="$dependencies $name"
    else
        echo "MISSING  adapter/$(basename "$pin"): line 2 must be the pinned commit (line 1 its tag)" >&2; fail=1
    fi
done
dependencies=${dependencies# }
if [ -n "$dependencies" ]; then echo "ok       pinned dependencies: $dependencies"; fi
stray=$(find "$project/scripts" -maxdepth 1 -name 'checkout-*.sh' ! -name 'checkout-dependency.sh' 2>/dev/null | sed "s|$project/||" | sort | tr '\n' ' ')
if [ -n "$stray" ]; then
    echo "REFUSED  ${stray}: dependencies are cloned from adapter/*_PIN by the managed scripts/checkout-dependency.sh NAME; delete these" >&2; fail=1
fi
# Packages: adapter/PACKAGES lists what the build needs on the runners, one
# package per line under a [linux] (apt) or [macos] (brew) heading. The
# socle's own Linux packaging tools are added in front of the product's.
socle_linux_packages='build-essential dpkg-dev file rpm' # the release.yml default
linux_packages=''
macos_packages=''
if [ -f "$project/adapter/PACKAGES" ]; then
    if packages=$(awk '
        /^[[:space:]]*(#|$)/ { next }
        /^\[(linux|macos)\][[:space:]]*$/ { section = substr($1, 2, length($1) - 2); next }
        /^\[/ { printf "unknown section %s at line %d\n", $0, NR > "/dev/stderr"; bad = 1; next }
        section == "" { printf "package outside a [linux] or [macos] section at line %d\n", NR > "/dev/stderr"; bad = 1; next }
        NF != 1 || $1 !~ /^[A-Za-z0-9][A-Za-z0-9+._@:-]*$/ { printf "one package per line at line %d: %s\n", NR, $0 > "/dev/stderr"; bad = 1; next }
        { print section, $1 }
        END { exit bad }' "$project/adapter/PACKAGES"); then
        linux_packages=$(printf '%s\n' "$packages" | awk '$1 == "linux" { printf "%s%s", sep, $2; sep = " " }')
        macos_packages=$(printf '%s\n' "$packages" | awk '$1 == "macos" { printf "%s%s", sep, $2; sep = " " }')
        echo "ok       adapter/PACKAGES: linux [$linux_packages] macos [$macos_packages]"
    else
        echo "MISSING  adapter/PACKAGES is malformed" >&2; fail=1
    fi
fi
test "$fail" -eq 0 || { echo "adopt: prerequisites missing in $project" >&2; exit 65; }

# ---- render the managed files into a staging tree ---------------------------
stage=$(mktemp -d "${TMPDIR:-/tmp}/maelys-release-adopt.XXXXXX")
stage=${stage%/}
trap 'rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$stage/.github/workflows" "$stage/.claude/skills/maelys-release" "$stage/scripts"

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
    if [ -n "$dependencies" ]; then
        printf '      dependency_checkout: |\n'
        for name in $dependencies; do printf '        sh scripts/checkout-dependency.sh %s\n' "$name"; done
    fi
    if [ -n "$linux_packages" ]; then printf '      linux_packages: %s %s\n' "$socle_linux_packages" "$linux_packages"; fi
    if [ -n "$macos_packages" ]; then printf '      macos_packages: %s\n' "$macos_packages"; fi
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
if [ -n "$dependencies" ]; then render_text "$self/share/templates/checkout-dependency.sh" >"$stage/scripts/checkout-dependency.sh"; fi

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
for relative in .github/workflows/release.yml scripts/checkout-dependency.sh AGENTS.md CLAUDE.md .claude/skills/maelys-release/SKILL.md RELEASING.md; do
    test -f "$stage/$relative" || continue
    if [ ! -f "$project/$relative" ]; then
        echo "create   $relative"; changed=1
    elif ! cmp -s "$stage/$relative" "$project/$relative"; then
        echo "update   $relative"; changed=1
        if [ "$apply" -eq 0 ]; then diff -u "$project/$relative" "$stage/$relative" | sed 's/^/         /' || true; fi
    elif [ "$relative" = scripts/checkout-dependency.sh ] && [ ! -x "$project/$relative" ]; then
        echo "update   $relative (not executable)"; changed=1
    else
        echo "same     $relative"
    fi
    if [ "$apply" -eq 1 ]; then
        mkdir -p "$(dirname "$project/$relative")"
        cp "$stage/$relative" "$project/$relative"
        case $relative in scripts/*.sh) chmod +x "$project/$relative" ;; esac
    fi
done

# ---- preflight: what the release workflow will demand of the tag -------------
# Run on the developer machine before tagging: the signing configuration,
# the previous tag, the next tag, and the GitHub environment the publish job
# runs in. Everything here is otherwise discovered by a failed workflow.
preflight() {
    status=0
    pf_ok() { echo "ok       $1"; }
    pf_fail() { echo "FAIL     $1" >&2; status=1; }
    pf_note() { echo "note     $1"; }
    if [ "$(git -C "$project" config --get tag.gpgsign || true)" = true ]; then pf_ok "tag.gpgsign = true"
    else pf_fail "tag.gpgsign is not true: git config tag.gpgsign true"; fi
    format=$(git -C "$project" config --get gpg.format || printf 'openpgp')
    if [ -n "$(git -C "$project" config --get user.signingkey || true)" ]; then pf_ok "gpg.format = $format, user.signingkey set"
    else pf_fail "user.signingkey is not set (gpg.format = $format); the key must be registered on GitHub"; fi
    last=$(git -C "$project" tag --list 'v*' --sort=-v:refname | head -n 1)
    if [ "$(git -C "$project" rev-parse --is-shallow-repository)" = true ]; then pf_fail "shallow clone: previous tags are not visible; run from a full clone"
    elif [ -z "$last" ]; then pf_note "no v* tag yet"
    elif [ "$(git -C "$project" cat-file -t "$last")" != tag ]; then pf_fail "$last is a lightweight tag; the workflow requires annotated signed tags"
    elif ! git -C "$project" cat-file -p "$last" | grep -q -- '-----BEGIN .*SIGNATURE-----'; then pf_fail "$last is not signed"
    else pf_ok "$last is annotated and signed"; fi
    if git -C "$project" rev-parse -q --verify "refs/tags/v$version" >/dev/null; then pf_fail "tag v$version already exists; bump VERSION"
    else pf_ok "tag v$version is free"; fi
    remote=$(git -C "$project" remote get-url origin 2>/dev/null | sed 's/\.git$//' || true)
    repository=$(printf '%s' "$remote" | sed -n 's|.*github\.com[:/]\([^/]*/[^/]*\)$|\1|p')
    # GitHub creates a missing environment on first use, without rules, so
    # presence proves nothing: the environment must limit deployments to
    # tags v*, which keeps a workflow_dispatch from a branch or a release.yml
    # edited on a branch from publishing.
    if [ -z "$repository" ]; then pf_note "origin is not on GitHub: release environment not checked"
    elif ! command -v gh >/dev/null 2>&1; then pf_note "gh is not installed: release environment of $repository not checked"
    else
        policy=$(gh api "repos/$repository/environments/release" --jq '.deployment_branch_policy.custom_branch_policies // false' 2>/dev/null) || policy=absent
        case $policy in
            absent) pf_fail "environment release is missing in $repository; create it and limit its deployments to tags v*" ;;
            true)
                if gh api "repos/$repository/environments/release/deployment-branch-policies" \
                    --jq '.branch_policies[] | select(.type == "tag" and .name == "v*") | .name' 2>/dev/null | grep -q .; then
                    pf_ok "environment release of $repository limits deployments to tags v*"
                else pf_fail "environment release of $repository has a deployment policy without the tag rule v*"; fi ;;
            *) pf_fail "environment release of $repository has no deployment policy: any branch, workflow_dispatch or edited release.yml can publish; limit it to tags v*" ;;
        esac
    fi
    return $status
}

if [ "$check" -eq 1 ]; then
    if [ "$changed" -eq 1 ]; then echo "adopt: $product drifts from maelys-release $socle_tag" >&2; exit 2; fi
    echo "adopt: $product is on maelys-release $socle_tag"
    if [ "$preflight" -eq 1 ]; then
        if preflight; then echo "adopt: $product $version is ready to tag"
        else echo "adopt: $product $version is not ready to tag" >&2; exit 3; fi
    fi
    exit 0
fi
if [ "$apply" -eq 1 ]; then echo "adopt: wrote maelys-release $socle_tag into $project"
elif [ "$changed" -eq 1 ]; then echo "adopt: plan only; add --apply to write"
else echo "adopt: nothing to do"; fi
