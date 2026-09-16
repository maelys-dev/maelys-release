# SPDX-License-Identifier: MPL-2.0
"""Locate and materialise pinned checkouts through the current host."""
from __future__ import annotations

import os
import pathlib

from maelys_cli import Failure
from .constants import PROGRAM
from .host import git, run


# The clone scripts/checkout-dependency.sh makes, so that what this command
# leaves in a directory is what CI leaves next to the product; a test holds
# the two together.
CLONE_FLAGS = ("--filter=blob:none", "--no-checkout")


# The key a materialised checkout carries in its own .git/config, so that the
# socle can tell what it cloned from what somebody works in.
MATERIALISED = "maelys-release.materialised"


def cache_root() -> pathlib.Path:
    """~/.cache/maelys-release: the one place the socle writes outside a product."""
    return pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") / "maelys-release"


def git_base() -> str:
    """Where a maelys-dev repository is cloned from, as the managed script reads it."""
    return os.environ.get("MAELYS_GIT_BASE", "https://github.com/maelys-dev")


def checkout_state(path: pathlib.Path, pin: dict, repository: str,
                   trusted: bool = False) -> tuple[str, str, str]:
    """(action, reason, head) for what sits at `path`: clone, same, refresh or blocked.

    Blocked is anything the socle did not put there, or that somebody has
    changed since: a symbolic link, a directory that is not a repository, a
    checkout on a branch, a tracked modification, a clone of another
    repository. The day maelys-egress reported this, one sibling carried
    another session's uncommitted work and another had been moved back to an
    older version by hand: both are working copies, and a command that reset
    either would have destroyed the first and silently corrected the second.
    What is detached and unmodified holds nothing a fetch and a checkout can
    lose -- local branches, stashes and the reflog all survive -- so it is
    refreshed. Nothing here, and nothing below, ever deletes a directory.
    """
    if path.is_symlink():
        # The obvious shortcut to "use my working copy instead": refreshing
        # through it is resetting the working copy.
        return "blocked", f"{path} is a symbolic link; the socle refreshes only what it cloned", ""
    if not path.exists():
        return "clone", "", ""
    if not (path / ".git").exists():
        return "blocked", f"{path} is not a git repository", ""
    if run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path).returncode == 0:
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False)
        return "blocked", f"{path} is on branch {branch}: a working copy, not a pinned checkout", ""
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=path).stdout.strip():
        return "blocked", f"{path} carries uncommitted changes to tracked files", ""
    origin = git("remote", "get-url", "origin", cwd=path, check=False)
    if origin != repository:
        return "blocked", (f"{path} was cloned from {origin or 'no remote'} and the pin names {repository};"
                           " move it aside by hand"), ""
    # Detached, clean and from the right remote is not proof the socle put it
    # there: a working copy left detached by a build script is all three, for
    # as long as nobody notices. The mark is what the socle wrote when it
    # cloned, and a checkout made before 0.53.0 carries none -- so it is
    # blocked once, with the one command that settles it.
    if git("config", "--local", "--get", MATERIALISED, cwd=path, check=False) != pin["name"]:
        if not trusted:
            return "blocked", (f"{path} is detached and clean, and the socle did not clone it: a working"
                               " copy left detached by a build script is all three for as long as nobody"
                               f" notices. Pass a --directory the socle owns, or move {path} aside"), ""
        # Under the socle's own root, the location is the proof: nothing else
        # writes there. Marking it in place is what keeps every checkout made
        # before 0.53.0 working instead of asking the fleet to delete five
        # directories per product for a key it can write itself.
        git("config", "--local", MATERIALISED, pin["name"], cwd=path)
    head = git("rev-parse", "HEAD", cwd=path, check=False)
    return ("same" if head == pin["commit"] else "refresh"), "", head


def materialise(path: pathlib.Path, pin: dict, repository: str, action: str) -> None:
    """Bring `path` to the pin, with the managed script's git in the socle's hands.

    The script refuses an existing destination and finds its pins from its
    own location, so it can neither refresh nor run from elsewhere; and a
    product the socle does not release carries no copy of it at all. The
    commands are the script's -- a partial clone, a detached checkout, a
    verified HEAD, submodules only when declared -- so a checkout made here
    and one CI makes next to the product are the same object.
    """
    if action == "clone":
        path.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", *CLONE_FLAGS, repository, str(path))
        # A mark in .git/config, invisible to the tree and surviving every
        # checkout: what the socle may move is what the socle put there.
        # Without it, a working copy that happens to be detached and clean
        # -- the state a build script leaves behind for an afternoon -- is
        # indistinguishable from a pinned checkout, and the next
        # 'dependencies --apply' would move it to another product's pin
        # without a word.
        git("config", "--local", MATERIALISED, pin["name"], cwd=path)
    if action in ("clone", "refresh"):
        # By commit first, from origin by name so a partial clone keeps its
        # promisor. A server that will not serve a bare commit is asked for
        # its refs instead, which reach any tagged pin.
        if action == "refresh" and run(["git", "fetch", "--quiet", "origin", pin["commit"]],
                                       cwd=path).returncode != 0:
            git("fetch", "--quiet", "--tags", "origin", cwd=path)
        git("checkout", "--quiet", "--detach", pin["commit"], cwd=path)
        if git("rev-parse", "HEAD", cwd=path) != pin["commit"]:
            raise Failure("FAILED", f"{path} is not at {pin['commit'][:7]} after checkout.",
                          "This is a defect of the socle or of git; inspect the checkout.")
    if pin["submodules"] is not None:
        # On a `same` too: a pin that gained its submodules line takes effect.
        arguments = ["submodule", "update", "--quiet", "--init"] + (["--recursive"] if pin["submodules"] else [])
        if run(["git", *arguments, "--depth", "1"], cwd=path).returncode != 0:
            git(*arguments, cwd=path)


def author_identity(path: pathlib.Path) -> None:
    """Leave the operator's identity alone, and name one only if git has none.

    A clone inherits the global configuration, which is the identity the
    operator commits and signs with. Setting a machine name and address over
    it produced commits nobody's account stands behind -- one of them reached
    the tap and was never verified -- and the branch a migration opens could
    not merge into a default branch that requires signed commits.

    The machine identity stays as a floor: a CI runner with no configured
    user must still be able to commit.
    """
    if os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_AUTHOR_EMAIL"):
        # git reads these itself; writing them into the clone's config would
        # only shadow what the operator asked for.
        return
    if run(["git", "var", "GIT_AUTHOR_IDENT"], cwd=path).returncode == 0:
        return
    git("config", "user.name", PROGRAM, cwd=path)
    git("config", "user.email", f"{PROGRAM}@users.noreply.github.com", cwd=path)
