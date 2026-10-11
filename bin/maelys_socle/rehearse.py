# SPDX-License-Identifier: MPL-2.0
"""Rehearse a Linux build or an existing publication through the current host."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import tempfile

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation
from . import host
from .constants import (
    CHECK_JOB_MINUTES, CHECK_JOB_PACKAGES, CHECK_LEG_OF, DECLARATION_FILE, DEFAULT_IMAGE, LEGS_JOB, PLATFORMS,
    PROGRAM, SOCLE_LINUX_PACKAGES, SOCLE_MECHANISM, SOCLE_REPOSITORY,
)
from .context import Context
from .declarations import Declarations, require_valid
from .dependencies import CHECKOUT_SCRIPTS
from .github import cut_repository, github_read
from .host import run
from .project import project_of, require_socle_mechanism
from .protect import check_product_default, declared_legs, socle_check_contexts, socle_check_jobs


# The check leg, as the step of check-product.yml runs it and not as the
# script around it runs. `shell: bash` there writes the line to a file and
# runs `bash --noprofile --norc -eo pipefail FILE`, without the -u that
# script has; the step's environment carries CI=true and LANG=C.UTF-8 and
# none of the rehearsal's own variables. TARGET is the one that matters:
# make reads the environment, and a Makefile whose `TARGET ?=` names its
# binary would build another file here than in the job. The job is bounded,
# so the step is, by the same number.
#
# What the runner has that this cannot have is said rather than hidden. Its
# image carries hundreds of tools and this one what was named: a check that
# stops on a command the container does not have exits 127 with the names,
# so that the result can say where such a tool is declared.
#
# Apart from the script, so that a test runs these very lines.
CHECK_STEP = r"""
if [ "$REHEARSE_MODE" = check ]; then
    work="${REHEARSE_WORK:-/work}"
    echo "== The image's cc: ${image_cc:-absent}; the environment names no compiler, as in the check job"
    echo "== Check: $CHECK_SHOWN"
    printf '%s\n' "$CHECK_COMMAND" >"$work/check-step.sh"
    status=0
    timeout -k 10 "${CHECK_MINUTES}m" \
        env -u TARGET -u LINUX_PACKAGES -u DEPENDENCIES -u DEPENDENCIES_APART -u PACKAGE_COMMAND \
        -u REHEARSE_MODE -u REHEARSE_WORK -u CHECK_COMMAND -u CHECK_SHOWN -u CHECK_MINUTES \
        -u DEBIAN_FRONTEND CI=true LANG=C.UTF-8 \
        bash --noprofile --norc -eo pipefail "$work/check-step.sh" 2>&1 | tee "$work/check-step.log" || status=$?
    if [ "$status" -eq 124 ]; then
        echo "== Cut after $CHECK_MINUTES minutes, the bound of the job" >&2
    elif [ "$status" -ne 0 ]; then
        named="$(sed -n -E \
            -e 's/.*: ([A-Za-z0-9_.+-]+): command not found$/\1/p' \
            -e 's/.*sh: [0-9]+: ([A-Za-z0-9_.+-]+): not found$/\1/p' \
            -e 's/^make(\[[0-9]+\])?: ([A-Za-z0-9_.+-]+): No such file or directory$/\2/p' \
            -e 's/^(\/usr\/bin\/)?env: [^A-Za-z0-9_.+-]*([A-Za-z0-9_.+-]+)[^A-Za-z0-9_.+-]*: No such file or directory$/\2/p' \
            "$work/check-step.log" | sort -u)"
        missing=""
        for name in $named; do
            command -v "$name" >/dev/null 2>&1 || missing="$missing $name"
        done
        if [ -n "$missing" ]; then
            echo "== Not in the image:$missing" >&2
            status=127
        fi
    fi
    # The one way out of a check: it never goes on to package.
    exit "$status"
fi
"""


REHEARSAL = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "== Install Linux packaging tools and declared packages"
# As both jobs do: the update reports, the install decides. A mirror that
# does not answer was fatal here and a warning there.
timeout -k 10 120 apt-get update -q >/dev/null \
    || echo "::warning::apt-get update failed or was cut after two minutes; the install decides"
# git and certificates are on the runner already; the image has neither. A
# check leg installs with the recommended packages, which is where clang
# takes its runtime libraries from, so the check does too.
recommended="--no-install-recommends"
[ "$REHEARSE_MODE" != check ] || recommended=""
apt-get install -y -q $recommended git ca-certificates $LINUX_PACKAGES >/dev/null
echo "== Take the tree CI would check out"
mkdir -p /work/product
git config --global --add safe.directory "*"
# A clone, plus the uncommitted changes to tracked files. What .gitignore
# excludes never travels, which is the point: a rehearsal replays the build
# job of release.yml, and that job checks out a tag -- it does not inherit
# build/ from the last twenty builds on this machine, nor a .git carrying
# the host's hooks and signing configuration.
#
# Not a tar pipe: GNU tar 1.35 extracting under an emulated linux/amd64 on
# an Apple Silicon host fails every mkdir with ENOSYS, "Function not
# implemented", so the x86_64 rehearsal was unusable on the machines the
# fleet develops on. Reported by maelys-oci, reproduced here. git runs under
# the same emulation and the checkout steps below prove it.
if git clone -q --no-hardlinks /src /work/product; then
    git -C /src diff --binary HEAD >/work/uncommitted.patch || true
    if [ -s /work/uncommitted.patch ]; then
        git -C /work/product apply /work/uncommitted.patch
    fi
    modified="$(git -C /src diff --name-only HEAD | wc -l | tr -d ' ')"
    # Said rather than carried: CI has no untracked file either, and a
    # rehearsal that silently added one would answer about a tree nobody
    # else will ever build.
    untracked="$(git -C /src ls-files --others --exclude-standard | wc -l | tr -d ' ')"
    echo "== Rehearsing $(git -C /src rev-parse --short HEAD) plus $modified modified file(s); $untracked untracked file(s) left behind, as CI would"
else
    echo "::warning::/src is not a git repository; copying it whole, build output included"
    cp -a /src/. /work/product/
fi
cd /work/product
rm -rf /work/product/dist
echo "== Pinned dependency checkouts"
# Beside the product, or apart and the root given: the third place the socle
# clones, and the one that decides whether a rehearsal is the same object as
# the run it rehearses.
if [ -n "${DEPENDENCIES_APART:-}" ]; then
    eval "$(sh scripts/checkout-dependencies.sh /work/dependencies)"
    export MAELYS_DEPENDENCIES_DIR
    # The third place, and the one that would have been forgotten: a
    # rehearsal that did not export what CI exports answers about a tree
    # nobody else builds. That is the class 0.45.0 to 0.48.0 chased out.
    [ -z "${MAELYS_RELEASE_DIR:-}" ] || export MAELYS_RELEASE_DIR
else
    for name in $DEPENDENCIES; do sh scripts/checkout-dependency.sh "$name"; done
fi
# Said, because it is what a green rehearsal was read without: two products
# packaged with the compiler their script exports, passed, and failed the
# check legs of their CI, which name no compiler and so build with this one.
image_cc="$(cc --version 2>/dev/null | head -n 1 || true)"
""" + CHECK_STEP.strip("\n") + r"""
echo "== The image's cc: ${image_cc:-absent}; the package command chooses its own compiler, and the check legs of CI do not follow it"
echo "== Package: ${PACKAGE_COMMAND//TARGET/$TARGET}"
eval "${PACKAGE_COMMAND//TARGET/$TARGET}"
test -n "$(ls dist 2>/dev/null)" || { echo "rehearse: dist/ is empty" >&2; exit 1; }
cp dist/* /dist/
echo "== Artifacts in dist/"
ls -l /dist
"""


PUBLISH_COMMAND = "bash scripts/publish-channel.sh TAG CHANNEL"


# What a publication marker says about a registry, and nothing else. Without
# a fixed list each product invents its own field, and `rehearse --channel`
# then compares what the script chose to write with what the release carries
# -- a comparison of a product with itself, which proves nothing common. One
# product dropped a field of its own to make its rehearsal agree, which is
# the shape of the problem: the product decides what is comparable.
CHANNEL_RECORD_FIELDS = ("registry", "package", "version", "dist_tag")


def kept_record(recorded: object) -> tuple[dict, list[str]]:
    """The fields of a script's record the marker keeps, and the names it drops.

    Dropped and not refused: the publication has already happened when this
    is read, so failing here would leave a registry holding a version and a
    release saying nothing. The names are reported instead, in the run and in
    the rehearsal.
    """
    if not isinstance(recorded, dict):
        return {}, []
    kept = {name: recorded[name] for name in CHANNEL_RECORD_FIELDS
            if isinstance(recorded.get(name), str)}
    return kept, sorted(set(recorded) - set(kept))


def channel_marker(product: str, tag: str, channel: str, recorded: dict, run: str) -> dict:
    """The marker channel.yml attaches, composed the way its jq expression does.

    The socle's own fields first and the record's four after, which cannot
    collide with them: the jq expression used to be `{socle} + ($recorded[0]
    // {})`, where a record naming `tag` would have overwritten the tag of
    the release it was attached to.
    """
    marker = {"product": product, "tag": tag, "channel": channel,
              "published": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "run": run}
    marker.update(kept_record(recorded)[0])
    return marker


def marker_differences(marker: dict, attached: dict) -> list:
    """The fields on which a composed marker and an attached one disagree.

    `published` and `run` differ by construction — one names this rehearsal,
    the other the run that published — so they are not a disagreement. Any
    other field is: it means the script records something different from what
    the release carries, and the next publication would attach that instead.
    """
    volatile = ("published", "run")
    return sorted({key for key in set(marker) | set(attached)
                   if key not in volatile and marker.get(key) != attached.get(key)})


def rehearse_channel(invocation: Invocation, project: pathlib.Path, product: str,
                     decl: Declarations, log) -> tuple[dict, int]:
    """Run a product's publish script against a release that already exists.

    channel.yml only ever runs on a signed tag, so the one contract it rests
    on — `publish_command` exits 0 without republishing when the registry
    already holds the version — had never been exercised anywhere. This runs
    it the way the workflow does: the release's own assets in dist/, the same
    environment, the same substitutions, and then the marker composed here is
    compared with the one attached to that release.

    It publishes nothing by design. If the script is not idempotent it either
    fails, which is the finding, or publishes, which the registry would have
    refused anyway for a version it already holds. Proposed by maelys-datalog,
    who could not test this any other way.
    """
    channel = str(invocation.option("--channel"))
    tag = str(invocation.option("--tag"))
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?", tag):
        raise Failure("VALIDATION_FAILED", f"--tag is vX.Y.Z: {tag}.", "Pass the published tag to rehearse.")
    declared = [name for name, _ in decl.channels]
    if channel not in declared:
        raise Failure("PRECONDITION_FAILED",
                      f"{product} declares no {channel} channel"
                      + (f"; it declares {' '.join(declared)}" if declared else "") + ".",
                      f"Declare it under [channels] of {DECLARATION_FILE}, or rehearse a channel it has.")
    repository = cut_repository(project)
    # The registry token is the operator's and never the socle's: it passes
    # through the environment, is never read from a file and never printed.
    if not any(os.environ.get(name) for name in ("NODE_AUTH_TOKEN", "GH_TOKEN")):
        raise Failure("PRECONDITION_FAILED",
                      "No registry token in the environment: the publish script needs one, and the socle"
                      " never supplies it.",
                      "Export NODE_AUTH_TOKEN (or GH_TOKEN) for this run; nothing is written to the registry"
                      " when the script keeps its side of the contract.")
    dist = project / "dist"
    if dist.is_dir() and any(dist.iterdir()):
        raise Failure("PRECONDITION_FAILED", f"{dist} is not empty, and the rehearsal fills it with the"
                      " release's own assets.",
                      "Empty dist/ first: a stale file would make the script publish something the release"
                      " does not carry.")
    if github_read(f"repos/{repository}/releases/tags/{tag}")[0] != "ok":
        raise Failure("NOT_FOUND", f"{repository} has no published release for {tag}.",
                      "A channel carries a release that exists; rehearse a tag that has one.")
    dist.mkdir(exist_ok=True)
    data: dict = {"product": product, "channel": channel, "tag": tag, "repository": repository,
                  "command": "", "assets": [], "recorded": [], "dropped": [], "matches": None}
    try:
        downloaded = run(["gh", "release", "download", tag, "--repo", repository, "--dir", str(dist)], cwd=project)
        if downloaded.returncode != 0:
            raise Failure("PROCESS_FAILED", f"gh release download {tag} failed: {downloaded.stderr.strip()}",
                          "Check the release and the credentials, then rehearse again.")
        data["assets"] = sorted(path.name for path in dist.iterdir())
        with tempfile.TemporaryDirectory(prefix="maelys-release-channel.") as temp:
            record = pathlib.Path(temp) / "channel-record.json"
            command = PUBLISH_COMMAND.replace("TAG", tag).replace("CHANNEL", channel)
            data["command"] = command
            print(f"rehearse: {command}", file=log)
            log.flush()
            published = host.HOST.stream(["bash", "-c", command], log, cwd=project,
                                       env={**os.environ, "TAG": tag, "CHANNEL": channel,
                                            "PRODUCT": product, "CHANNEL_RECORD": str(record),
                                            "CHANNEL_DRY_RUN": "1"})
            if published.returncode != 0:
                raise Failure("PROCESS_FAILED",
                              f"{command} exited {published.returncode} on a version {channel} already holds.",
                              "channel.yml requires a publish command that succeeds without republishing:"
                              " a replay of this tag would fail the same way. Read the log above.")
            recorded = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        kept, dropped = kept_record(recorded)
        data["recorded"] = sorted(kept)
        data["dropped"] = dropped
        marker = channel_marker(product, tag, channel, recorded, "(rehearsal)")
        attached_file = dist / f"channel-{channel}.json"
        attached = json.loads(attached_file.read_text(encoding="utf-8")) if attached_file.is_file() else None
        if attached is not None:
            data["differs"] = marker_differences(marker, attached)
            data["matches"] = not data["differs"]
        data["marker"] = marker
    finally:
        for path in sorted(dist.iterdir()) if dist.is_dir() else []:
            path.unlink()
    return data, EXIT_OK if data["matches"] is not False else EXIT_VIOLATIONS


CI_FILE = ".github/workflows/ci.yml"
CALLED = f"{SOCLE_REPOSITORY}/.github/workflows/check-product.yml@"
PACKAGE_DEFAULT = "bash scripts/package-release.sh TARGET"
PLAIN_KEY = r"[A-Za-z_][A-Za-z0-9_-]*"

# Every named step of the two jobs a rehearsal stands for, and what the
# rehearsal makes of it: `replayed` is the one step it runs as the job does,
# `own` a step it performs in its own way -- the tree, the pins, the
# packages -- `macos` a step no Linux run has, and `left` a step it does not
# run at all. A result names the first and lists the last; a test holds both
# tables against the workflows, so a step added there fails here until
# somebody has said which of the four it is.
CHECK_JOB_STEPS = {
    "Fetch the socle this workflow comes from": "own",
    "Carried dependencies": "left",
    "Point the pinned checkouts at the carried bundles": "left",
    "Pinned dependency checkouts, beside the product": "own",
    "Pinned dependency checkouts, apart": "own",
    "Install Linux packages": "own",
    "Install macOS packages": "macos",
    "Require Apple Silicon": "macos",
    "Check": "replayed",
    "Release socle drift": "left",
}
BUILD_JOB_STEPS = {
    "Pinned dependency checkouts": "own",
    "Install Linux packaging tools": "own",
    "Install macOS packages": "macos",
    "Require Apple Silicon": "macos",
    "Verify before packaging": "left",
    "Package": "replayed",
    "Read the subject the SBOM names": "left",
    "Provenance attestation": "left",
    "SBOM attestation": "left",
    "Keep both attestation bundles with the artifacts": "left",
    "No provenance attestation": "left",
    "Hand the artifacts to publish": "left",
}

# What a check in a container is not, whatever was made equal: said in every
# result of the check mode, because a reader of a green one is owed it.
UNLIKE_THE_RUNNER = (
    "user: root here, an unprivileged user on the runner",
    "tools: the image's packages and those named here, where the runner image carries hundreds;"
    " a product declares what its check needs under [linux] of dependencies/packages",
    "tree: the local HEAD and its uncommitted changes, where a pull request checks out its merge commit",
    "versions: the archive's packages of today, where the runner has those of the day its image was built",
)


class Unread(Exception):
    """A shape of ci.yml the line reader will not guess at: (line, why)."""

    def __init__(self, line: int, why: str) -> None:
        super().__init__(why)
        self.line, self.why = line, why


def indent(line: str, number: int) -> int:
    leading = line[:len(line) - len(line.lstrip())]
    if "\t" in leading:
        raise Unread(number, "a tab indents this line")
    return len(leading)


def significant(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


class Call:
    """The job of ci.yml that calls check-product.yml, and what its `with:` passes.

    `inputs` maps a name to (the rest of its line, the line's number, the
    lines below that belong to it, the indentation of the names).
    """

    def __init__(self, job: str, line: int, inputs: dict) -> None:
        self.job, self.line, self.inputs = job, line, inputs


def shared_ci_call(text: str) -> Call | None:
    """The call of check-product.yml in a ci.yml, or None when no line makes one.

    The socle carries no YAML parser and this reads lines, so it reads the
    shapes it can read exactly and raises `Unread` on every other: a call
    written in flow style, a quoted key, a `with:` that is not a block of
    `name: value` lines. A reader that guessed ran an empty line and called
    the leg replayed.
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    calls = [index for index, line in enumerate(lines) if CALLED in line and significant(line)]
    if not calls:
        return None
    if len(calls) > 1:
        raise Unread(calls[1] + 1, "a second job calls check-product.yml, and one leg has one line")
    at = calls[0]
    uses = re.fullmatch(r"( +)uses: +(['\"]?)" + re.escape(CALLED) + r"[^\s'\"#]+\2( +#.*)?", lines[at])
    if not uses:
        raise Unread(at + 1, "the call of check-product.yml is not a `uses:` line of its own")
    level = len(uses.group(1))
    header = next((index for index in range(at - 1, -1, -1)
                   if significant(lines[index]) and indent(lines[index], index + 1) < level), None)
    named = re.fullmatch(rf"( +)({PLAIN_KEY}): *(#.*)?", lines[header]) if header is not None else None
    if not named:
        raise Unread((header if header is not None else at) + 1, "the job that makes the call is not a plain `name:` line")
    depth = len(named.group(1))
    above = next((lines[index] for index in range(header - 1, -1, -1)
                  if significant(lines[index]) and indent(lines[index], index + 1) < depth), "")
    if not re.fullmatch(r"jobs: *(#.*)?", above):
        raise Unread(header + 1, "the job that makes the call is not directly under `jobs:`")
    end = next((index for index in range(header + 1, len(lines))
                if significant(lines[index]) and indent(lines[index], index + 1) <= depth), len(lines))
    passed = None
    for index in range(header + 1, end):
        line = lines[index]
        if not significant(line) or indent(line, index + 1) != level:
            continue
        key = re.match(rf" *(['\"]?)({PLAIN_KEY})\1 *:(.*)$", line)
        if not key:
            raise Unread(index + 1, "a key of the calling job is not a plain `name:`")
        if key.group(2) != "with":
            continue
        if key.group(1) or not re.fullmatch(r" *(#.*)?", key.group(3)) or passed is not None:
            raise Unread(index + 1, "`with:` is quoted, written on one line, or given twice")
        passed = index
    inputs: dict = {}
    if passed is None:
        return Call(named.group(2), at + 1, inputs)
    width, current, closed = None, None, False
    for index in range(passed + 1, end):
        line = lines[index]
        if not line.strip():
            if current:
                inputs[current][2].append("")
            continue
        here = indent(line, index + 1)
        if here <= level:
            if significant(line):
                break
            closed = True
            continue
        if width is None:
            if not significant(line):
                continue
            width = here
        if here > width:
            if current is None or closed:
                raise Unread(index + 1, "a line under `with:` belongs to no input")
            inputs[current][2].append(line)
            continue
        if not significant(line):
            closed = True
            continue
        entry = re.fullmatch(rf" {{{width}}}({PLAIN_KEY}):( .*|)", line) if here == width else None
        if not entry:
            raise Unread(index + 1, "a line under `with:` is not a plain `name: value`")
        if entry.group(1) in inputs:
            raise Unread(index + 1, f"{entry.group(1)} is passed twice")
        current, closed = entry.group(1), False
        inputs[current] = (entry.group(2), index + 1, [], width)
    return Call(named.group(2), at + 1, inputs)


def input_string(rest: str, number: int, below: list[str], width: int) -> str:
    """The string YAML makes of one input, for the shapes read exactly.

    A plain value on its line, a quoted one whose escapes JSON also has, a
    literal block, a folded block of lines at one indentation. Everything
    else is `Unread`: a plain value carried over several lines, a quoted
    one that does not close on its line, an alias, a flow collection, an
    indentation indicator -- shapes YAML reads and this does not pretend to.
    The string is the one the job runs up to its final line break, which a
    shell does not read.
    """
    value = rest.strip()
    while below and not below[-1].strip():
        below = below[:-1]
    block = re.fullmatch(r"([|>])[+-]?( +#.*)?", value)
    if block:
        first = next((line for line in below if line.strip()), "")
        margin = indent(first, number)
        if any(line.strip() and indent(line, number) < margin for line in below):
            raise Unread(number, "a line of this block is indented less than its first")
        content = [line[margin:] if line.strip() else "" for line in below]
        if block.group(1) == "|":
            return "\n".join(content)
        if any(line[:1] in (" ", "\t") for line in content):
            raise Unread(number, "a folded block with lines indented further, which YAML does not fold")
        folded, blanks = "", 0
        for line in content:
            if not line:
                blanks += 1
                continue
            folded += ("\n" * blanks if blanks or not folded else " ") + line
            blanks = 0
        return folded
    if any(line.strip() for line in below):
        raise Unread(number, "a value carried over the lines below it")
    if not value or value.startswith("#"):
        raise Unread(number, "no value on this line")
    if value[0] == "'":
        quoted = re.fullmatch(r"'((?:[^']|'')*)'( +#.*)?", value)
        if not quoted:
            raise Unread(number, "a single-quoted value that does not close on its line")
        return quoted.group(1).replace("''", "'")
    if value[0] == '"':
        quoted = re.fullmatch(r'("(?:[^"\\]|\\.)*")( +#.*)?', value)
        if not quoted:
            raise Unread(number, "a double-quoted value that does not close on its line")
        try:
            return str(json.loads(quoted.group(1)))
        except ValueError:
            raise Unread(number, "a double-quoted value with an escape this reader does not decode") from None
    plain = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if plain[0] in "[{&*!|>%@`" or plain[:2] in ("- ", "? ", ": ") or ": " in plain or plain.endswith(":") \
            or plain in ("~", "null", "Null", "NULL"):
        raise Unread(number, "a value YAML does not read as one plain string")
    return plain


class Leg:
    """What the check leg of a product runs, as far as its ci.yml was read."""

    def __init__(self, caller: str = "", command: str = "", where: str = "", carried: bool = False) -> None:
        self.caller, self.command, self.where, self.carried = caller, command, where, carried


def refused(line: int, why: str) -> Failure:
    return Failure("PRECONDITION_FAILED",
                   f"{CI_FILE} line {line}: {why}. The line the check leg runs is not read for certain, and"
                   " nothing was run.",
                   "Pass --check-command with the line to run; the result then names no step of the leg.")


def check_leg(project: pathlib.Path, context: Context | None, with_line: bool = True) -> Leg:
    """The check leg of this product: who calls it, and the line it runs.

    Three products of the fleet pass a `check_command` of their own -- one of
    them a target that has no `make check` in it -- so a rehearsal that ran
    `make check` answered for a line their CI never runs. Read from the
    call; the workflow's own default is named only when the call was read
    and passes none, and never typed here, where it would drift from the
    one the job has. Certain or refused: an empty line passes `bash -e`, and
    a rehearsal that ran one was green for having run nothing.
    """
    ci = project / CI_FILE
    try:
        call = shared_ci_call(ci.read_text(encoding="utf-8")) if ci.is_file() else None
    except Unread as unread:
        raise refused(unread.line, unread.why) from None
    if call is None:
        return Leg()
    leg = Leg(call.job, carried="carried_dependencies" in call.inputs)
    if not with_line or context is None:
        return leg
    if "check_command" in call.inputs:
        rest, number, below, width = call.inputs["check_command"]
        try:
            leg.command = input_string(rest, number, below, width)
        except Unread as unread:
            raise refused(unread.line, f"check_command is {unread.why}") from None
        leg.where = f"check_command of {CI_FILE}, line {number}"
        if "${{" in leg.command:
            raise refused(number, "check_command carries an expression only GitHub evaluates")
        if not leg.command.strip():
            raise refused(number, "check_command is empty")
        return leg
    try:
        leg.command = input_string(check_product_default("check_command", context), call.line, [], 0)
    except Unread:
        raise refused(call.line, "the call passes no check_command, and this socle carries no check-product.yml"
                                 " to read the default from") from None
    leg.where = "the default of check-product.yml, the call passing none"
    return leg


def left_steps(steps: dict) -> list[str]:
    return [name for name, kind in steps.items() if kind == "left"]


def rehearsal_coverage(project: pathlib.Path, context: Context, decl: Declarations, target: str, mode: str,
                       leg: Leg, as_the_job: bool) -> tuple[str, list[str], list[str]]:
    """(the step a rehearsal replays, the other steps of its job, the jobs of the shared CI it does not run).

    The second and third are the reason this exists. A package that builds
    was read as "CI will be green", twice in one cycle, by products whose
    packaging exports clang while their check legs build with the runner's
    cc: a gcc diagnostic passed the rehearsal on both architectures and
    stopped the pull request.

    A step, and not a job: the first version of this named the job, and
    the job does more. The build job verifies before it packages when a
    product carries a verify script, and does not package at all on a
    target `[package]` leaves out; the check job of the linux leg ends on
    the socle's own drift check. A line given by hand is no step of
    anybody's: `--check-command` and `--package-command` are for looking at
    one thing, and what they pass is not what the workflow runs.

    The jobs come from the product's call and the workflow this socle
    carries, as `protect` reads them, under the names a pull request shows.
    """
    jobs = [f"{leg.caller} / {name}" for name in socle_check_jobs(project, context)] if leg.caller else []
    jobs += [f"{LEGS_JOB} / {name}" for name in declared_legs(project)] if leg.caller else []
    if mode == "package":
        packages = not decl.package or target in decl.package
        step = f"the Package step of release.yml's build job for {target}" if as_the_job and packages else ""
        steps = [name for name in left_steps(BUILD_JOB_STEPS)
                 if name != "Verify before packaging" or decl.verify_command]
        return step, steps, jobs
    name = f"{leg.caller} / check ({CHECK_LEG_OF[target]})" if leg.caller else ""
    steps = [step for step in left_steps(CHECK_JOB_STEPS)
             if (step == "Release socle drift" and CHECK_LEG_OF[target] == "linux")
             or (step != "Release socle drift" and leg.carried)] if name else []
    if not as_the_job:
        return "", steps, jobs
    return (f"the Check step of {name}" if name else ""), steps, [job for job in jobs if job != name]


def handle_rehearse(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Replay one step of one Linux job in Docker: the release's packaging, or the check of a leg."""
    project, product, mechanism = project_of(invocation)
    context.run_pinned_socle(invocation, project)
    log = sys.stderr if invocation.format != "text" else sys.stdout
    if invocation.option("--channel"):
        require_socle_mechanism(project, mechanism, "rehearse")
        return rehearse_channel(invocation, project, product, context.read_declarations(project, product, mechanism), log)
    mode = "check" if invocation.flag("--check") else "package"
    # The check leg is not the release mechanism: a product that publishes
    # by its own means and calls check-product.yml has that leg like any
    # other, and it was refused here in the name of a release it never
    # asked the socle for. Packaging keeps the requirement, which is its.
    if mode == "package":
        require_socle_mechanism(project, mechanism, "rehearse")
    if not invocation.operands[1:]:
        raise Failure("VALIDATION_FAILED", "rehearse takes a TARGET, or --channel NAME --tag vX.Y.Z.",
                      "Name the Linux target to replay, or the channel to run against a published release.")
    # The copy carries .git as it stands. In a worktree — and in a submodule
    # checkout — .git is a file naming a directory elsewhere on the host,
    # which the container does not have, so git inside the rehearsal reads a
    # path that is not there. Reported by maelys-oci, who worked around it
    # with a full clone; the socle says so rather than letting it surface as
    # whatever the product's build makes of a broken repository.
    if (project / ".git").is_file():
        raise Failure("PRECONDITION_FAILED",
                      f"{project} is a git worktree or a submodule checkout: its .git is a file naming a"
                      " directory outside it, and rehearse clones the tree inside a container where that"
                      " directory does not exist.",
                      "Run the rehearsal from a full clone.")
    target = invocation.operands[1]
    package_command = str(invocation.option("--package-command", PACKAGE_DEFAULT))
    by_hand = "--check-command" in invocation.options
    # Everything that can refuse, before anything is written or started:
    # the first version read ci.yml after it had created dist/.
    leg, check_command, where, as_the_job = Leg(), "", "", True
    if mode == "check":
        if by_hand:
            check_command, where = str(invocation.option("--check-command")), "--check-command"
            if not check_command.strip():
                raise Failure("VALIDATION_FAILED", "--check-command is empty, and an empty line passes.",
                              "Give the line to run.")
            try:
                leg = check_leg(project, context)
            except Failure:
                # The leg's own line does not read: the one given by hand
                # runs, and names no step.
                leg = lenient_leg(project, context)
            as_the_job = bool(leg.command) and check_command == leg.command
        else:
            leg = check_leg(project, context)
            check_command, where = leg.command, leg.where
            if not leg.caller:
                check_command, where = "make check", f"no call of check-product.yml in {CI_FILE}"
        if not leg.caller and mechanism != SOCLE_MECHANISM:
            raise Failure("PRECONDITION_FAILED",
                          f"{project} publishes through a {mechanism} mechanism and its {CI_FILE} does not call"
                          " check-product.yml: there is no leg of the shared CI to rehearse.",
                          f"'{PROGRAM} check {project}' verifies the conventions of such a product.")
    else:
        leg = lenient_leg(project, context)
        as_the_job = package_command == PACKAGE_DEFAULT
    if not host.HOST.which("docker"):
        raise Failure("NOT_FOUND", "docker is required to rehearse a Linux build.", "Install Docker and retry.")
    decl = context.read_declarations(project, product, mechanism)
    require_valid(decl, "rehearse")
    needed = CHECKOUT_SCRIPTS if decl.dependencies_apart else CHECKOUT_SCRIPTS[:1]
    absent = [name for name in needed if not os.access(project / name, os.X_OK)] if decl.dependencies else []
    if absent:
        raise Failure("PRECONDITION_FAILED", " and ".join(absent) + " is missing in the product.",
                      f"Run '{PROGRAM} adopt {project} --apply' first.")
    image = str(invocation.option("--image", os.environ.get("REHEARSE_IMAGE", DEFAULT_IMAGE)))
    # What the job installs, on top of what its machine already carries. The
    # build job of the release installs the socle's packaging tools; a check
    # leg installs the two of CHECK_JOB_PACKAGES, on a runner image that
    # carries those packaging tools already. Neither is the runner: the
    # image has what is named here and nothing else.
    packages = SOCLE_LINUX_PACKAGES.split()
    if mode == "check":
        packages += [name for name in CHECK_JOB_PACKAGES.split() if name not in packages]
    replayed, steps, jobs = rehearsal_coverage(project, context, decl, target, mode, leg, as_the_job)
    ran = check_command if mode == "check" else package_command.replace("TARGET", target)
    command = ["docker", "run", f"--name=rehearse-{target}" if invocation.flag("--keep") else "--rm",
               "--platform", PLATFORMS[target], "-v", f"{project}:/src:ro"]
    if mode == "package":
        # The one write on the host, after every refusal, and none for a
        # check: it leaves dist/ alone, so it is not given dist/.
        (project / "dist").mkdir(exist_ok=True)
        command += ["-v", f"{project / 'dist'}:/dist"]
    command += ["-e", f"TARGET={target}",
                "-e", f"LINUX_PACKAGES={' '.join(packages)} {decl.linux_packages}".rstrip(),
                "-e", f"DEPENDENCIES={' '.join(decl.dependencies)}",
                "-e", f"DEPENDENCIES_APART={'1' if decl.dependencies_apart else ''}",
                "-e", f"PACKAGE_COMMAND={package_command}", "-e", f"REHEARSE_MODE={mode}"]
    if mode == "check":
        command += ["-e", f"CHECK_COMMAND={check_command}", "-e", f"CHECK_SHOWN={shown(check_command)}",
                    "-e", f"CHECK_MINUTES={CHECK_JOB_MINUTES}"]
    command += [image, "bash", "-c", REHEARSAL]
    # The container's output is a log: it stays on stdout for a human and
    # moves to stderr when the envelope owns stdout.
    print(f"rehearse: {mode} {target} on {image} ({PLATFORMS[target]})", file=log)
    if mode == "check":
        print(f"rehearse: {replayed or 'no step of the shared CI'}: {shown(check_command)} ({where})", file=log)
    log.flush()
    completed = host.HOST.stream(command, log)
    if completed.returncode == 127 and mode == "check":
        # The one failure a container adds to those of the leg, and so the
        # one the result explains: two products call tools from their
        # Makefile that the runner image carries and they never declared.
        raise Failure("PROCESS_FAILED",
                      f"The check of {target} stopped on a command the image does not have (exit 127); the log"
                      " above names it.",
                      "The runner image carries tools this container does not, so the leg may pass where this"
                      " stopped: declare what the check needs under [linux] of dependencies/packages, which the"
                      " legs and the rehearsal both install.")
    if completed.returncode == 124 and mode == "check":
        raise Failure("PROCESS_FAILED",
                      f"The check of {target} was cut after {CHECK_JOB_MINUTES} minutes, the bound of the job"
                      " (exit 124).",
                      "A leg that runs this long is cut on the runner too; an emulated target is slower than"
                      " the runner, and --check-command narrows what is run.")
    if completed.returncode != 0:
        raise Failure("PROCESS_FAILED", f"The rehearsal of {target} failed with exit {completed.returncode}.",
                      "Read the container log above; fix the product and retry.")
    artifacts = [] if mode == "check" else sorted(path.name for path in (project / "dist").iterdir() if target in path.name)
    data = {"product": product, "target": target, "platform": PLATFORMS[target], "image": image, "mode": mode,
            "command": ran, "replayed": replayed, "notReplayedSteps": steps, "notReplayed": jobs,
            "artifacts": artifacts}
    if mode == "check":
        data["unlikeTheRunner"] = list(UNLIKE_THE_RUNNER)
    return data, EXIT_OK


def lenient_leg(project: pathlib.Path, context: Context) -> Leg:
    """Who calls the shared CI, for a run that only lists its jobs and claims none.

    The exact reader first; where it refuses, the caller `protect` reads.
    Nothing is run on this answer: it names what was not replayed.
    """
    try:
        return check_leg(project, None, with_line=False)
    except Failure:
        return Leg(socle_check_contexts(project, context)[0])


def shown(command: str) -> str:
    """A command on one line with nothing a terminal would obey.

    The line comes from a file of the repository, which a pull request
    writes: printed raw, an escape sequence in it reached the terminal of
    whoever rehearsed that branch.
    """
    joined = " && ".join(line.strip() for line in command.splitlines() if line.strip())
    return "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in joined)


def text_container(data: dict) -> str:
    """What a container rehearsal proved, and in the same breath what it did not."""
    target, jobs, steps = data["target"], data["notReplayed"], data["notReplayedSteps"]
    command = shown(data["command"])
    leg = next((name for name in jobs if name.endswith(f" / check ({CHECK_LEG_OF[target]})")), "")
    rest = [("of that job", ", ".join(steps))] if steps else []
    rest.append(("of the shared CI", ", ".join(jobs) + ", and whatever jobs this repository's workflows add of"
                 " their own") if jobs else ("of this repository", "its own jobs, which the socle does not read"))
    left = [f"{'' if index else 'not replayed':<13} {where}: {what}" for index, (where, what) in enumerate(rest)]
    if data["mode"] == "package":
        lines = [f"rehearse: {target} built " + ", ".join(data["artifacts"]),
                 f"{'replayed':<13} " + (f"{data['replayed']}: {command}" if data["replayed"] else
                                         f"no step: the build job of the release does not run this for {target}:"
                                         f" {command}"),
                 *left,
                 f"{'':<13} A package that builds says nothing of them: the package command chooses its own"
                 " compiler and flags, and those jobs do not follow it."]
        if leg:
            lines.append(f"{'':<13} '{PROGRAM} rehearse DIR {target} --check' replays the Check step of {leg}.")
        return "\n".join(lines) + "\n"
    if data["replayed"]:
        said = f"{data['replayed']}: {command}"
    elif leg:
        said = f"no step: --check-command ran another line than {leg} runs: {command}"
    else:
        said = f"no step of the shared CI, which this repository's ci.yml does not call: {command}"
    return "\n".join([f"rehearse: check passed on {target}",
                      f"{'replayed':<13} {said}",
                      *left,
                      *(f"{'' if index else 'unlike':<13} {line}"
                        for index, line in enumerate(data["unlikeTheRunner"]))]) + "\n"


def text_rehearse(data: dict) -> str:
    if "channel" not in data:
        return text_container(data)
    lines = [f"{'assets':<10} {len(data['assets'])} from {data['tag']}",
             f"{'command':<10} {data['command']} (exited 0 without republishing)"]
    if data["recorded"]:
        lines.append(f"{'recorded':<10} " + " ".join(data["recorded"]))
    if data.get("dropped"):
        lines.append(f"{'dropped':<10} " + " ".join(data["dropped"])
                     + f"; a marker carries {', '.join(CHANNEL_RECORD_FIELDS)} and nothing else,"
                     " so what the release will hold is the line above")
    if data["matches"] is None:
        lines.append(f"{'marker':<10} {data['tag']} carries no channel-{data['channel']}.json to compare with")
    elif data["matches"]:
        lines.append(f"{'marker':<10} matches channel-{data['channel']}.json of {data['tag']}")
    else:
        lines.append(f"{'marker':<10} differs from channel-{data['channel']}.json on "
                     + " ".join(data["differs"]))
    verdict = "ok" if data["matches"] is not False else "the marker differs"
    return "\n".join(lines) + f"\nrehearse: {data['channel']} of {data['product']} {data['tag']}: {verdict}\n"

