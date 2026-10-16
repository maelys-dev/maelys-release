# SPDX-License-Identifier: MPL-2.0
"""The forge: what the socle asks of where a repository is hosted, and GitHub as the one it has.

The socle reads and writes a forge through `host.HOST` -- `read` and
`write` for its API, `run` for its command line -- and nothing here starts
a process: a fake host, a recording host and a replay host see the same
calls, in the same order, as before this object existed. What this module
holds is what names the forge rather than the process: which remotes are
its, how a repository is called from one, where the fleet's repositories
are cloned from, and the few commands of its CLI that are not a GET of
its API. Nothing else in the socle names github.com, and a test holds it.

`github.py` stays GitHub's: the readings of a protection, a ruleset, an
environment, the check runs of a commit, a release, each by its REST path.
They are reached through the same host, and they are the next thing to put
behind this interface -- a forge that keeps its policy in a manifest read at
push time answers those questions in another vocabulary. One forge today,
and `forge()` answers it for every remote: choosing one by the remote is
for the day there are two.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

from . import host
from .constants import SOCLE_REPOSITORY

FLEET_OWNER = SOCLE_REPOSITORY.split("/")[0]


class Forge:
    """What the socle asks of a forge.

    Every answer comes from the host, so that what a command does is read
    from the host's calls alone, whatever forge answered them.
    """

    #: The forge's name, as a message would spell it.
    name = ""
    #: The hosts a repository URL of this forge carries, lower case.
    hosts: tuple[str, ...] = ()

    def repository(self, remote: str) -> str:
        """`owner/name` of a remote URL that is this forge's, else ''."""
        raise NotImplementedError

    def available(self) -> bool:
        """Whether this machine can ask the forge: its CLI is installed."""
        raise NotImplementedError

    def clone_base(self) -> str:
        """Where a repository of the fleet is cloned from, as the managed script reads it."""
        raise NotImplementedError

    def clone_url(self, repository: str, token: str = "") -> str:
        """The https URL a clone of `owner/name` reads, with a token where a push needs one."""
        raise NotImplementedError

    def pull_requests(self, project: pathlib.Path, repository: str, branch: str) -> subprocess.CompletedProcess:
        """The pull requests coming from BRANCH, in every state, as JSON on stdout."""
        raise NotImplementedError

    def open_pull_request(self, project: pathlib.Path, repository: str, base: str, branch: str, title: str,
                          body: pathlib.Path) -> subprocess.CompletedProcess:
        """Open BRANCH -> BASE with TITLE and the body held in a file: the URL on stdout."""
        raise NotImplementedError

    def view_pull_request(self, project: pathlib.Path, repository: str, branch: str) -> subprocess.CompletedProcess:
        """The pull request of BRANCH: its number, URL and state, as JSON on stdout."""
        raise NotImplementedError

    def download_release(self, project: pathlib.Path, repository: str, tag: str,
                         directory: pathlib.Path) -> subprocess.CompletedProcess:
        """Every asset of the release TAG into DIRECTORY."""
        raise NotImplementedError


class GitHub(Forge):
    """GitHub, through `gh`: the one forge the socle has."""

    name = "GitHub"
    hosts = ("github.com", "www.github.com")
    # A remote of this forge, https or ssh, with or without .git: what
    # stands after the host is the repository.
    REMOTE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$")

    def repository(self, remote: str) -> str:
        match = self.REMOTE.search(remote)
        return match.group(1) if match else ""

    def available(self) -> bool:
        return bool(host.HOST.which("gh"))

    def clone_base(self) -> str:
        # MAELYS_GIT_BASE is what a test and a mirror point at another place:
        # the managed script reads the same variable.
        return os.environ.get("MAELYS_GIT_BASE", f"https://github.com/{FLEET_OWNER}")

    def clone_url(self, repository: str, token: str = "") -> str:
        return (f"https://x-access-token:{token}@github.com/{repository}.git" if token
                else f"https://github.com/{repository}.git")

    def pull_requests(self, project: pathlib.Path, repository: str, branch: str) -> subprocess.CompletedProcess:
        return host.run(["gh", "pr", "list", "--repo", repository, "--head", branch, "--state", "all",
                         "--limit", "10", "--json", "number,url,state,mergeCommit,headRefOid"], cwd=project)

    def open_pull_request(self, project: pathlib.Path, repository: str, base: str, branch: str, title: str,
                          body: pathlib.Path) -> subprocess.CompletedProcess:
        return host.run(["gh", "pr", "create", "--repo", repository, "--base", base, "--head", branch,
                         "--title", title, "--body-file", str(body)], cwd=project)

    def view_pull_request(self, project: pathlib.Path, repository: str, branch: str) -> subprocess.CompletedProcess:
        return host.run(["gh", "pr", "view", branch, "--repo", repository, "--json", "number,url,state"],
                        cwd=project)

    def download_release(self, project: pathlib.Path, repository: str, tag: str,
                         directory: pathlib.Path) -> subprocess.CompletedProcess:
        return host.run(["gh", "release", "download", tag, "--repo", repository, "--dir", str(directory)],
                        cwd=project)


FORGE: Forge = GitHub()


def forge() -> Forge:
    """The forge of the fleet's repositories."""
    return FORGE
