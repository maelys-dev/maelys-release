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
from .constants import DECLARATION_FILE, DEFAULT_IMAGE, PLATFORMS, PROGRAM, SOCLE_LINUX_PACKAGES
from .context import Context
from .declarations import Declarations, require_valid
from .dependencies import CHECKOUT_SCRIPTS
from .github import cut_repository, github_read
from .host import run
from .project import project_of, require_socle_mechanism


REHEARSAL = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "== Install Linux packaging tools and declared packages"
apt-get update -q >/dev/null
# git and certificates are on the runner already; the image has neither.
apt-get install -y -q --no-install-recommends git ca-certificates $LINUX_PACKAGES >/dev/null
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
if [ "$REHEARSE_MODE" = check ]; then
    echo "== Check: $CHECK_COMMAND"
    eval "$CHECK_COMMAND"
    exit 0
fi
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


def handle_rehearse(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Replay the build job of release.yml for one Linux target in Docker."""
    project, product, mechanism = project_of(invocation)
    context.run_pinned_socle(invocation, project)
    require_socle_mechanism(project, mechanism, "rehearse")
    log = sys.stderr if invocation.format != "text" else sys.stdout
    if invocation.option("--channel"):
        return rehearse_channel(invocation, project, product, context.read_declarations(project, product, mechanism), log)
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
    if not host.HOST.which("docker"):
        raise Failure("NOT_FOUND", "docker is required to rehearse a Linux build.", "Install Docker and retry.")
    decl = context.read_declarations(project, product, mechanism)
    require_valid(decl, "rehearse")
    needed = CHECKOUT_SCRIPTS if decl.dependencies_apart else CHECKOUT_SCRIPTS[:1]
    absent = [name for name in needed if not os.access(project / name, os.X_OK)] if decl.dependencies else []
    if absent:
        raise Failure("PRECONDITION_FAILED", " and ".join(absent) + " is missing in the product.",
                      f"Run '{PROGRAM} adopt {project} --apply' first.")
    (project / "dist").mkdir(exist_ok=True)
    image = str(invocation.option("--image", os.environ.get("REHEARSE_IMAGE", DEFAULT_IMAGE)))
    package_command = str(invocation.option("--package-command", "bash scripts/package-release.sh TARGET"))
    mode = "check" if invocation.flag("--check") else "package"
    check_command = str(invocation.option("--check-command", "make check"))
    command = ["docker", "run", f"--name=rehearse-{target}" if invocation.flag("--keep") else "--rm",
               "--platform", PLATFORMS[target],
               "-v", f"{project}:/src:ro", "-v", f"{project / 'dist'}:/dist",
               "-e", f"TARGET={target}",
               "-e", f"LINUX_PACKAGES={SOCLE_LINUX_PACKAGES} {decl.linux_packages}".rstrip(),
               "-e", f"DEPENDENCIES={' '.join(decl.dependencies)}",
               "-e", f"DEPENDENCIES_APART={'1' if decl.dependencies_apart else ''}",
               "-e", f"PACKAGE_COMMAND={package_command}",
               "-e", f"REHEARSE_MODE={mode}", "-e", f"CHECK_COMMAND={check_command}",
               image, "bash", "-c", REHEARSAL]
    # The container's output is a log: it stays on stdout for a human and
    # moves to stderr when the envelope owns stdout.
    print(f"rehearse: {mode} {target} on {image} ({PLATFORMS[target]})", file=log)
    log.flush()
    completed = host.HOST.stream(command, log)
    if completed.returncode != 0:
        raise Failure("PROCESS_FAILED", f"The rehearsal of {target} failed with exit {completed.returncode}.",
                      "Read the container log above; fix the product and retry.")
    artifacts = [] if mode == "check" else sorted(path.name for path in (project / "dist").iterdir() if target in path.name)
    return {"product": product, "target": target, "platform": PLATFORMS[target], "image": image, "mode": mode,
            "artifacts": artifacts}, EXIT_OK



def text_rehearse(data: dict) -> str:
    if "channel" not in data:
        return (f"rehearse: {data['target']} built " + ", ".join(data["artifacts"]) + "\n"
                if data["mode"] == "package" else f"rehearse: check passed on {data['target']}\n")
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

