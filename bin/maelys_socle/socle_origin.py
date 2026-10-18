# SPDX-License-Identifier: MPL-2.0
"""The socle an entry point runs from: its identity, its allowed signers, and the pinned socle
a product names, fetched and run in its place.

Everything here reads the root of the running socle, which belongs to the entry point and not
to this package: two entry points loaded in one interpreter -- an installed copy and a
checkout -- share these modules and must each read their own root. So the root comes in the
context, never from this file's own location.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys

from maelys_cli import Failure, Invocation
from . import host, identity
from .checkouts import cache_root, git_base
from .constants import PROGRAM, SOCLE_REPOSITORY
from .context import Context
from .forge import forge
from .host import git, run
from .product_workflows import pinned_socle
from .project import pinned_product


def share_dir(context: Context) -> pathlib.Path:
    return identity.share_dir(context.socle_root())


def archived_identity(context: Context, root: pathlib.Path) -> "tuple[str, str] | None":
    """The tag and commit the archive was made from, or None outside one.

    A product pins the socle by commit, so a socle that cannot name its own
    commit cannot write a product's workflows: an installed copy used to
    refuse every write and hand the operator a `git ls-remote` to run by
    hand. It does not have to. `INSTALLED.pin` carries `$Format:` place
    holders that git expands when it builds the archive of a tag -- which is
    how GitHub builds the tarball a Homebrew formula downloads -- so the tag
    and its commit travel inside the archive itself, in the two-line shape
    every `dependencies/*.pin` already uses.

    Read closed: the commit must be forty hexadecimal characters and the tag
    must be the `v` of this copy's own VERSION. An unexpanded placeholder,
    a tarball built by something that ignores export-subst, or a file edited
    by hand all read as absent, and the refusal that follows names the
    commit the operator must pass. Nothing here trusts a file over the
    archive's own digest: what ties this copy to that commit is the sha256
    the formula verified before unpacking it.
    """
    pin = root / "INSTALLED.pin"
    if not pin.is_file():
        return None
    lines = pin.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return None
    described, commit = lines[0].strip(), lines[1].strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    expected = f"v{context.socle_version()}"
    if described != expected:
        raise Failure("PRECONDITION_FAILED",
                      f"this copy says VERSION {context.socle_version()} and was archived at {described or 'no tag'}:"
                      " the two disagree, so what it would write names a commit that does not produce it.",
                      "Install a copy of one tag, or run from a checkout at that tag.")
    return commit, described


def socle_identity(context: Context, sha: str, tag: str) -> tuple[str, str]:
    """The commit and tag written into the managed files.

    The files a product receives must be exactly what the pinned socle
    regenerates, so a checkout with uncommitted changes to what adopt
    writes (share/, bin/) is refused: its output would name a commit that
    does not produce it.
    """
    if sha:
        return sha, tag or "untagged"
    root = context.socle_root()
    if not (root / ".git").exists():
        recorded = archived_identity(context, root)
        if recorded:
            return recorded
        version = context.socle_version()
        raise Failure("PRECONDITION_FAILED", "This socle is not a git checkout and its archive carries no commit"
                      " (INSTALLED.pin was not expanded), so its commit is unknown, and the pin must name the commit.",
                      f"Pass the commit of the tag: --socle-sha \"$(git ls-remote {forge().clone_url(SOCLE_REPOSITORY)}"
                      f" 'v{version}^{{}}' | cut -f1)\" --socle-tag v{version}; or run from a checkout at that tag.")
    dirty = git("status", "--porcelain", "--", "share", "bin", "VERSION", cwd=root, check=False)
    if dirty:
        raise Failure("PRECONDITION_FAILED", "This socle checkout has uncommitted changes in share/, bin/ or VERSION;"
                      " what it would write names a commit that does not produce it.",
                      "Commit or stash the socle's changes, or run the socle at a tag.")
    return git("rev-parse", "HEAD", cwd=root), git("describe", "--tags", "--exact-match", cwd=root, check=False) or "untagged"


def socle_data(context: Context, sha: str, tag: str) -> dict:
    return identity.socle_data(sha, tag, context)


def discard_empty_checkout(cache: pathlib.Path) -> None:
    """Remove the directory a fetch that never landed left behind.

    `git init` makes the directory before the fetch can fail, so a commit
    the network, the remote or a typo puts out of reach leaves a repository
    with no commit in it. Nothing reads it -- the next call fetches again
    whatever is there -- and nothing removes it either: a cache of
    fifty-two checkouts held four such shells, one of them naming a commit
    GitHub no longer serves, which no later call could ever fill.

    What is removed is proved empty first, because this is the one place
    the socle deletes in a directory it made: nothing beside `.git`, and no
    commit to lose. A checkout that carries either stays, and the caller
    still degrades on None.
    """
    if not cache.is_dir() or [entry.name for entry in cache.iterdir()] != [".git"]:
        return
    if run(["git", "rev-parse", "--quiet", "--verify", "HEAD"], cwd=cache).returncode == 0:
        return
    shutil.rmtree(cache, ignore_errors=True)


def pinned_checkout(name: str, sha: str, witness: str) -> pathlib.Path | None:
    """A checkout of `name` at `sha`, fetched once into the user's cache.

    `witness` is the file whose presence means the cache already holds this
    commit. None when the fetch fails, so a caller can degrade instead of
    failing; a checkout that lacks the witness is still returned, because
    "fetched but old" and "not fetched" are different answers.
    """
    root = cache_root()
    cache = root / sha if name == "maelys-release" else root / f"{name}-{sha}"
    if (cache / witness).is_file():
        return cache
    base = git_base()
    cache.parent.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(cache))
    fetched = run(["git", "fetch", "-q", "--depth", "1", f"{base}/{name}.git", sha], cwd=cache)
    if fetched.returncode != 0:
        discard_empty_checkout(cache)
        return None
    git("checkout", "-q", "FETCH_HEAD", cwd=cache)
    return cache


def pinned_socle_checkout(sha: str) -> pathlib.Path:
    """A checkout of the socle at `sha`; the relocation cannot degrade."""
    cache = pinned_checkout("maelys-release", sha, "bin/maelys-release")
    if cache is None:
        raise Failure("NOT_FOUND", f"maelys-release {sha[:7]} could not be fetched.",
                      "Check the network, or run with MAELYS_RELEASE_NO_RELOCATE=1.")
    if (cache / "bin" / "maelys-release").is_file():
        (cache / "bin" / "maelys-release").chmod(0o755)
    return cache


def release_signers(context: Context, project: pathlib.Path) -> pathlib.Path:
    """The allowed signers this product's own release will be judged against.

    The release workflow reads them at the socle commit the product pinned,
    so `cut` reads them there too. Judging against the list of whichever
    socle happens to be running says ok to a key the workflow then refuses,
    after the tag is pushed -- and a published tag is never moved, so that
    costs a version. `check` and `preflight` relocate to the pinned socle
    and already judge the right list; `cut` never relocates, which is why it
    has to fetch that one file itself.

    A repository that pins nothing -- the socle cutting itself -- is judged
    by the list it carries.
    """
    pinned = pinned_socle(project)
    root = context.socle_root()
    if not pinned or os.environ.get("MAELYS_RELEASE_NO_RELOCATE") == "1":
        return identity.allowed_signers(root)
    if (root / ".git").exists() and git("rev-parse", "HEAD", cwd=root, check=False) == pinned["sha"]:
        return identity.allowed_signers(root)
    return identity.allowed_signers(pinned_socle_checkout(pinned["sha"]))


def run_pinned_socle(context: Context, invocation: Invocation, project: pathlib.Path) -> None:
    """Hand the invocation to the socle the product pins, when this is another one.

    A product's CI fetches the pinned socle itself; a developer's sibling
    checkout drifts as soon as anyone moves it. check, preflight and
    rehearse therefore re-execute themselves from a cached checkout at the
    pinned commit, so the answer is always the pinned socle's. adopt does
    not: moving the pin is exactly its purpose. MAELYS_RELEASE_NO_RELOCATE=1
    keeps the running socle, and --socle-sha names one explicitly.
    """
    if invocation.option("--socle-sha") or os.environ.get("MAELYS_RELEASE_NO_RELOCATE") == "1":
        return
    pinned = pinned_socle(project)
    if not pinned:
        return
    root = context.socle_root()
    if (root / ".git").exists() and git("rev-parse", "HEAD", cwd=root, check=False) == pinned["sha"]:
        return
    try:
        checkout = pinned_socle_checkout(pinned["sha"])
    except Failure as failure:
        raise Failure("PRECONDITION_FAILED",
                      f"{project.name} pins maelys-release {pinned['tag']} ({pinned['sha'][:7]}) but this is another"
                      f" socle, and the pinned one could not be fetched: {failure.message}",
                      "Check out the pinned tag, or set MAELYS_RELEASE_NO_RELOCATE=1 to run this socle anyway.") from failure
    if not (checkout / "bin" / "maelys-release").is_file():
        # before 0.5.0 the socle was scripts/adopt.sh: it cannot answer for itself
        raise Failure("PRECONDITION_FAILED",
                      f"{pinned_product(project) or project.name} pins maelys-release {pinned['tag']} ({pinned['sha'][:7]}), which predates the"
                      " maelys-release command (0.5.0) and cannot answer for itself.",
                      f"Upgrade it from a current socle at a tag: 'adopt {project} --apply', then check.")
    if invocation.format == "text":
        print(f"note     running the pinned socle {pinned['tag']} ({pinned['sha'][:7]}) from {checkout}", file=sys.stderr)
        sys.stderr.flush()
    sys.stdout.flush()
    environment = {**os.environ, "MAELYS_RELEASE_NO_RELOCATE": "1"}
    host.HOST.exec(str(checkout / "bin" / "maelys-release"), [PROGRAM, *sys.argv[1:]], environment)


def vendored_pin(context: Context) -> tuple[str, str, str]:
    """(tag, commit, sha256) of dependencies/maelys-cli.pin; the third line may be absent."""
    lines = (context.socle_root() / "dependencies" / "maelys-cli.pin").read_text(encoding="utf-8").split()
    digest = lines[2].partition(":")[2] if len(lines) > 2 else ""
    return lines[0], lines[1], digest
