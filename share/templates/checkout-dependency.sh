#!/bin/sh
# Managed by maelys-release @SOCLE_TAG@ (@SOCLE_VERSION@). Regenerate with
# scripts/adopt.sh of maelys-release; do not edit by hand.
#
# usage: scripts/checkout-dependency.sh NAME [DESTINATION]
#
# Clones the Maelys repository NAME (maelys-cli, maelys-system, ...) next to
# this one at the commit on line 2 of adapter/<NAME>_PIN, where <NAME> is
# the name upper-cased with hyphens turned to underscores; line 1 holds the
# nearest tag for humans. The release socle runs it before packaging, the
# product's CI before `make check`, a developer once by hand. It refuses to
# replace an existing DESTINATION. MAELYS_GIT_BASE (default
# https://github.com/maelys-dev) prefixes the clone URL.
set -eu
name=${1:?NAME}
case $name in *[!a-z0-9-]*|'') echo "checkout-dependency: NAME must be [a-z0-9-]: $name" >&2; exit 64 ;; esac
root=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
destination=${2:-$root/../$name}
pin_file="$root/adapter/$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')_PIN"
test -f "$pin_file" || { echo "checkout-dependency: no pin for $name: $pin_file" >&2; exit 66; }
tag=$(sed -n '1p' "$pin_file")
pin=$(sed -n '2p' "$pin_file")
printf '%s' "$pin" | grep -Eq '^[0-9a-f]{40}$' || { echo "checkout-dependency: line 2 of $pin_file must be the pinned commit" >&2; exit 65; }
if [ -e "$destination" ]; then
    echo "checkout-dependency: refusing to replace existing path: $destination" >&2
    exit 1
fi
git clone --quiet --filter=blob:none --no-checkout "${MAELYS_GIT_BASE:-https://github.com/maelys-dev}/$name.git" "$destination"
git -C "$destination" checkout --quiet --detach "$pin"
test "$(git -C "$destination" rev-parse HEAD)" = "$pin"
echo "$name $tag ($pin) in $destination"
