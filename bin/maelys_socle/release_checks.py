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


SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")


def allowed_signer_keys(text: str) -> list[tuple[str, str, str]]:
    """(principal, type, material) of each line of an ssh allowed_signers file.

    The options field sits between the principal and the key and may hold
    anything, `valid-before` included, so the key is found by its type rather
    than by its position.
    """
    found: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        for index, token in enumerate(parts[1:], start=1):
            if token.startswith(SSH_KEY_TYPES) and index + 1 < len(parts):
                found.append((parts[0], token, parts[index + 1]))
                break
    return found


def public_key(project: pathlib.Path, declared: str) -> str:
    """The key text `user.signingkey` names: a literal, a .pub, or a private key beside one.

    git accepts all three, so all three are read; the private key beside a
    .pub is the shape a signing configuration takes most often, and reading
    it back as if it were a public key is how a checker reports a key it
    never understood. The content decides, not the name.
    """
    if declared.startswith(SSH_KEY_TYPES):
        return declared
    path = pathlib.Path(declared).expanduser()
    if not path.is_absolute():
        path = project / path
    for candidate in (path.with_name(path.name + ".pub"), path):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text.startswith(SSH_KEY_TYPES):
                return text
    return ""


def signing_key_named(project: pathlib.Path, declared: str, fmt: str,
                      allowed_signers: pathlib.Path) -> list[tuple[str, str]]:
    """Whether the key this checkout would sign with is one the fleet names.

    GitHub's `verified` says the key belongs to some account, and every
    account that may push a tag has one: it answers a different question from
    "may this key sign a Maelys release". The release workflow refuses a tag
    whose signature does not verify against the socle's allowed signers, at
    the commit the product pinned. This reads the same file before there is a
    tag, because a refusal after the tag is pushed costs a version: a
    published tag is never moved.
    """
    if not declared:
        return []
    if fmt != "ssh":
        return [("fail", f"gpg.format = {fmt}: a release is signed with an ssh key named in"
                         f" {allowed_signers.name}, and the release workflow verifies the tag against that file")]
    if not allowed_signers.is_file():
        return [("fail", f"{allowed_signers} is missing: nothing says which key may sign a release,"
                         " and the release workflow refuses a tag it cannot match")]
    entries = allowed_signer_keys(allowed_signers.read_text(encoding="utf-8"))
    if not entries:
        return [("fail", f"{allowed_signers} names no key: the release workflow would refuse every tag")]
    text = public_key(project, declared)
    if not text:
        return [("note", f"user.signingkey names {declared}, which is not a key this command can read:"
                         " whether the release workflow will accept it is unknown here")]
    parts = text.split()
    if len(parts) < 2 or not parts[0].startswith(SSH_KEY_TYPES):
        return [("note", f"user.signingkey names {declared}, which does not read as an ssh public key:"
                         " whether the release workflow will accept it is unknown here")]
    for principal, kind, material in entries:
        if (kind, material) == (parts[0], parts[1]):
            return [("ok", f"the signing key is named in {allowed_signers.name}, as {principal}")]
    return [("fail", f"the signing key is not named in {allowed_signers}: the release workflow verifies the"
                     " tag's signature against that file and would refuse this one. Add the key there, or sign"
                     " with one it names")]


def tag_checks(project: pathlib.Path, version: str, allowed_signers: pathlib.Path,
               between_releases: bool = False) -> list[tuple[str, str]]:
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
    declared = config("user.signingkey")
    if declared:
        found.append(("ok", f"gpg.format = {fmt}, user.signingkey set"))
    else:
        found.append(("fail", f"user.signingkey is not set (gpg.format = {fmt}); the key must be registered on GitHub"))
    found.extend(signing_key_named(project, declared, fmt, allowed_signers))
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
