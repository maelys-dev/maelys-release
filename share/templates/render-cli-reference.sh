#!/bin/sh
# Managed by maelys-release; regenerate with 'maelys-release adopt --apply'.
# Do not edit by hand.
#
# Writes the generated command-line reference of @PRODUCT@ into $1 (docs/cli.md
# by convention) and its machine-readable contract next to it. The Markdown
# comes from maelys-cli's generator, which asks each program for its own
# catalogue through `describe`; this repository only says which programs and
# where their binaries are.
#
# Every value below is a fleet convention with an environment override:
#   BIN or BUILD                the built binaries (default build/bin)
#   MAELYS_CLI_DIR              the pinned maelys-cli checkout (default ../maelys-cli,
#                               where scripts/checkout-dependency.sh puts it)
#   CLI_REFERENCE_PROGRAMS      the programs to describe (default @PROGRAMS@)
#   CLI_REFERENCE_FLAGS         extra generator flags, for example
#                               --neutral-availability IDS
#   PYTHON                      the interpreter (default python3)
set -eu

output=${1:?usage: sh scripts/render-cli-reference.sh OUTPUT.md}
build=${BIN:-${BUILD:-build}/bin}
generator=${MAELYS_CLI_DIR:-../maelys-cli}/tools/generate_cli_reference.py
programs=${CLI_REFERENCE_PROGRAMS:-@PROGRAMS@}

if [ ! -f "$generator" ]; then
    echo "$generator is missing: run 'sh scripts/checkout-dependency.sh maelys-cli'" >&2
    exit 1
fi

# The contract keeps the reference's name, so docs/cli.md pairs with
# docs/cli-contract.json; docs/ holds it as data, never as prose.
contract=${output%.md}-contract.json

# shellcheck disable=SC2086
exec "${PYTHON:-python3}" "$generator" \
    --build "$build" \
    --markdown "$output" \
    --json "$contract" \
    ${CLI_REFERENCE_FLAGS:-} \
    $programs
