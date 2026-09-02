#!/bin/sh
# usage: render-formula.sh TEMPLATE OUTPUT KEY=VALUE...
# Replaces every @KEY@ in TEMPLATE and refuses to leave a placeholder behind.
set -eu
template=${1:?TEMPLATE}
output=${2:?OUTPUT}
shift 2
expression=''
for pair in "$@"; do
    key=${pair%%=*}
    value=${pair#*=}
    case $key in
        *[!A-Z0-9_]*|'') echo "invalid placeholder name: $key" >&2; exit 64 ;;
    esac
    escaped=$(printf '%s' "$value" | sed 's/[|&]/\\&/g')
    expression="$expression -e s|@$key@|$escaped|g"
done
mkdir -p "$(dirname "$output")"
# shellcheck disable=SC2086
sed $expression "$template" >"$output"
if grep -q '@[A-Z0-9_]*@' "$output"; then
    echo "unrendered placeholder in $output:" >&2
    grep -n '@[A-Z0-9_]*@' "$output" >&2
    exit 65
fi
printf '%s\n' "rendered $output"
