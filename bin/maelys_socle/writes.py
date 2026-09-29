# SPDX-License-Identifier: MPL-2.0
"""Read back what was just written, and refuse what the remote does not hold.

`protect` learned this in 0.58.0, after two products lost a setting under a
write whose plan named something else: it re-reads what it wrote and fails
on anything that moved. A product review of 2026-09-29 asked the obvious
next question -- whether `cut`, `tap` and `migrate` do the same -- and the
answer was no. They pushed, read the exit status of `git push`, and said
"published".

An exit status is not a reading. It says the transport succeeded, not that
the remote holds what was meant: a server-side hook, a ref rewritten by an
ancestor of the same second, a credential that writes into a fork, all
leave zero behind. So every write to a remote in this package goes through
one of these, and the report that follows is a report of what was read.
"""
from __future__ import annotations

import pathlib

from maelys_cli import Failure
from .host import run


def remote_refs(clone: pathlib.Path, remote: str, pattern: str) -> dict | None:
    """{ref: sha} the remote answers for `pattern`, or None when it cannot be read.

    Unreadable and empty are kept apart: a remote that refuses to answer
    says nothing about what it holds, and reporting that as "the ref is
    missing" is the mistake this package made once in its reading of the
    deployment policies.
    """
    listed = run(["git", "ls-remote", remote, pattern], cwd=clone)
    if listed.returncode != 0:
        return None
    found = {}
    for line in listed.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip():
            found[ref.strip()] = sha.strip()
    return found


def confirm_ref(clone: pathlib.Path, remote: str, ref: str, expected: str, what: str, hint: str) -> None:
    """Refuse unless the remote answers `expected` for `ref`."""
    held = remote_refs(clone, remote, ref)
    if held is None:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {remote} could not be read back: nothing confirms what it holds.",
                      hint)
    if ref not in held:
        raise Failure("PROCESS_FAILED", f"{what} was pushed and {remote} does not hold {ref}.", hint)
    if held[ref] != expected:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {remote} holds {held[ref][:12]} at {ref}, not {expected[:12]}.", hint)


def confirm_tag(clone: pathlib.Path, remote: str, tag: str, commit: str, what: str, hint: str) -> None:
    """Refuse unless the remote's `tag` is an annotated tag of `commit`.

    The peeled ref is what says which commit a tag names; the tag object's
    own sha says nothing about it. A published tag is never moved, so a tag
    that names another commit is not something to correct -- it is
    something to stop on.
    """
    ref = f"refs/tags/{tag}"
    held = remote_refs(clone, remote, ref + "*")
    if held is None:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {remote} could not be read back: nothing confirms what it holds.",
                      hint)
    if ref not in held:
        raise Failure("PROCESS_FAILED", f"{what} was pushed and {remote} does not hold {ref}.", hint)
    peeled = held.get(ref + "^{}")
    if peeled is None:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {remote} holds {ref} as a lightweight tag: a release is annotated"
                      " and signed.", hint)
    if peeled != commit:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {remote} has {ref} on {peeled[:12]}, not {commit[:12]}.", hint)


def confirm_contains(clone: pathlib.Path, remote: str, branch: str, commit: str, what: str, hint: str) -> None:
    """Refuse unless `branch` of the remote now descends from `commit`.

    Containment and not equality, because this branch is shared: every
    product of the fleet publishes its formula on the tap's default branch,
    and one that lands a second later leaves the head somewhere else
    without losing anything. What must be true is that the commit arrived.
    """
    fetched = run(["git", "fetch", "-q", remote, branch], cwd=clone)
    if fetched.returncode != 0:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {branch} of {remote} could not be read back:"
                      " nothing confirms what it holds.", hint)
    if run(["git", "merge-base", "--is-ancestor", commit, "FETCH_HEAD"], cwd=clone).returncode != 0:
        raise Failure("PROCESS_FAILED",
                      f"{what} was pushed and {branch} of {remote} does not carry {commit[:12]}.", hint)
