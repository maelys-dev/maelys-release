# SPDX-License-Identifier: MPL-2.0
"""The signing and repository gates shared by preflight and cut."""
from __future__ import annotations

import pathlib
import re

from . import host
from .constants import DECLARATION_FILE
from .declarations import Declarations
from .github import (branch_protection, channel_visibility, deployment_policies, environment_gate,
                     github_read, github_repository, publication_record, read_protection,
                     read_repository, tap_drift, tap_secrets)
from .host import git, run


def tag_checks(project: pathlib.Path, version: str, between_releases: bool = False) -> list[tuple[str, str]]:
    """What the signed tag vVERSION needs of this checkout, whatever publishes it.

    These hold for every Maelys repository, the ones the socle does not
    release included: a release is a signed annotated tag whose commit
    carries that VERSION, and nothing but the operator's own checkout can
    answer for the signing key.
    """
    found: list[tuple[str, str]] = []

    def config(key: str) -> str:
        return git("config", "--get", key, cwd=project, check=False)

    signs = config("tag.gpgsign") == "true"
    found.append(("ok", "tag.gpgsign = true") if signs
                 else ("fail", "tag.gpgsign is not true: git config tag.gpgsign true"))
    fmt = config("gpg.format") or "openpgp"
    if config("user.signingkey"):
        found.append(("ok", f"gpg.format = {fmt}, user.signingkey set"))
    else:
        found.append(("fail", f"user.signingkey is not set (gpg.format = {fmt}); the key must be registered on GitHub"))
    if git("rev-parse", "--is-shallow-repository", cwd=project, check=False) == "true":
        found.append(("fail", "shallow clone: previous tags are not visible; run from a full clone"))
    else:
        tags = git("tag", "--list", "v*", "--sort=-v:refname", cwd=project, check=False).split()
        if not tags:
            found.append(("note", "no v* tag yet"))
        else:
            last = tags[0]
            if git("cat-file", "-t", last, cwd=project, check=False) != "tag":
                found.append(("fail", f"{last} is a lightweight tag; the workflow requires annotated signed tags"))
            elif not re.search(r"-----BEGIN .*SIGNATURE-----", git("cat-file", "-p", last, cwd=project, check=False)):
                found.append(("fail", f"{last} is not signed"))
            else:
                found.append(("ok", f"{last} is annotated and signed"))
    if git("rev-parse", "-q", "--verify", f"refs/tags/v{version}", cwd=project, check=False):
        latest = (git("tag", "--list", "v*", "--sort=-v:refname", cwd=project, check=False).split() or [""])[0]
        # Released means what the workflow would have published: the latest
        # tag, annotated, on this commit's history. A lightweight tag at the
        # version is not a release but a collision waiting for the next cut,
        # and keeps its failure.
        released = latest == f"v{version}" \
            and git("cat-file", "-t", f"v{version}", cwd=project, check=False) == "tag" \
            and run(["git", "merge-base", "--is-ancestor", f"v{version}", "HEAD"], cwd=project).returncode == 0
        if between_releases and released:
            # The state of every repository between two releases, not a
            # defect: VERSION is what was published last. preflight is also
            # the one command that reads the environment, the gate and the tap
            # credentials, so it is run as a health check between releases --
            # and a FAIL here made every other verdict of that run read as a
            # failure too. maelys-json ran it that way three times.
            found.append(("note", f"v{version} is the release this commit already carries; `cut` moves"
                                  " VERSION when there is something to publish"))
        else:
            found.append(("fail", f"tag v{version} already exists; a published tag is never moved or recreated"))
    else:
        found.append(("ok", f"tag v{version} is free"))
    return found



def repository_checks(decl: Declarations) -> list[tuple[str, str]]:
    """What the release workflow needs of the GitHub repository behind origin.

    GitHub creates a missing environment on first use, without rules, so
    presence proves nothing: the environment must limit deployments to tags
    v*, which keeps a workflow_dispatch from a branch, or a release.yml
    edited on a branch, from publishing.

    It takes the declaration and not a handful of its fields. It took four
    of them, three with a default, and cut passed two: every product with a
    formula in the tap was told under `cut` that nothing declared it, with
    the remedy inverted, at the moment the operator was cutting a release --
    and the sbom note went missing there too. An optional parameter is a way
    for a caller to lose something quietly; a declaration cannot be passed
    by halves. maelys-http reported it.
    """
    project, gate = decl.project, decl.gate
    decl_sbom, decl_formulas = decl.sbom_pattern, decl.formulas
    found: list[tuple[str, str]] = []
    repository = github_repository(project)
    if not repository:
        return [("note", "origin is not on GitHub: release environment not checked")]
    if not host.HOST.which("gh"):
        return [("note", f"gh is not installed: release environment of {repository} not checked")]
    repository_data = read_repository(repository)
    if repository_data.visibility == "public" and decl.docs_named:
        found.append(("fail", f"{repository} is public and {DECLARATION_FILE} declares [docs] named: AGENTS.md and"
                              " CLAUDE.md name the private documentation repository to anyone. Remove the"
                              " declaration and adopt"))
    if repository_data.private:
        found.append(("note", f"{repository} is private: the release ships no provenance attestation (GitHub reserves"
                              " them to paid plans); the signed tag and SHA256SUMS remain"))
        if decl_sbom:
            # The check that the document names its subject still runs; what
            # is missing is the signature over that link, and a reader of the
            # release has to be told which of the two they are holding.
            found.append(("note", f"{repository} is private: the SBOM {decl_sbom} is published but not attested."
                                  " The release still refuses a document that names no subject or records a"
                                  " digest that disagrees; nothing signs the link"))
    # Read once, with its state: an environment GitHub refused to describe
    # was reported as an environment that does not exist, which is the
    # collapse github_read exists to prevent.
    state, body = github_read(f"repos/{repository}/environments/release")
    environment = body if state == "ok" and isinstance(body, dict) else None
    found.extend(deployment_policies(repository, environment, state))
    # A deployment policy says *what* may publish; it says nothing about
    # *who* approves. The conventions call this environment the human gate,
    # and the socle used to describe a gate it never saw closed: a product
    # published unattended and preflight said ok.
    found.extend(environment_gate(repository, environment, gate))
    # The default branch is what a tag is expected to come from, and what
    # commit_verification: signed-on-default-branch relies on. It is
    # protected in two ways that do not answer on the same endpoint: the
    # classic branch protection, and a ruleset. agent-cli-spec is protected
    # by a ruleset alone, and reading only the first reported it open.
    # What the tap serves for this repository, against what it declares.
    # preflight and not check: this reads one file per formula of the tap,
    # and check runs on every pull request of every product.
    found.extend(tap_drift(repository, decl_formulas))
    found.extend(tap_secrets(repository, decl_formulas))
    found.extend(channel_visibility(repository, decl.channels))
    branch = repository_data.default_branch
    protection = read_protection(repository, branch)
    found.append(branch_protection(repository, branch, protection.classic_state, protection.ruled_state,
                                   protection.rules, decl.commit_verification))
    # Last, and a note whatever it says: what the repository has already
    # published. Everything above is configuration, and a configuration that
    # conforms is not a publication that worked.
    found.extend(publication_record(repository))
    return found
