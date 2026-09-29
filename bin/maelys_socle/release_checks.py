# SPDX-License-Identifier: MPL-2.0
"""The signing and repository gates shared by preflight and cut."""
from __future__ import annotations

import datetime
import pathlib
import re
import tempfile

from . import host
from .constants import DECLARATION_FILE
from .declarations import Declarations
from .checkouts import git_base
from .github import (branch_protection, channel_visibility, deployment_policies, environment_gate,
                     github_read, github_repository, publication_record, read_protection,
                     read_repository, tap_drift, tap_secrets)
from .host import git, run


SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")


def allowed_signer_keys(text: str) -> list[tuple[str, str, str, str]]:
    """(principal, options, type, material) of each line of an allowed_signers file.

    The options field sits between the principal and the key and may hold
    anything, `valid-before` included, so the key is found by its type rather
    than by its position -- and the options are kept, because they decide as
    much as the key does. Reading the material alone answered `ok` for four
    lines ssh-keygen refuses: a retired key, one not yet valid, a
    certificate authority, and a key allowed in another namespace.
    """
    found: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        for index, token in enumerate(parts[1:], start=1):
            if token.startswith(SSH_KEY_TYPES) and index + 1 < len(parts):
                found.append((parts[0], " ".join(parts[1:index]), token, parts[index + 1]))
                break
    return found


def signer_options(options: str) -> dict[str, str]:
    """The options of one line, lowercased, quotes removed; a flag maps to "".

    Commas separate them and a value may be quoted, so a comma inside quotes
    belongs to the value.
    """
    found: dict[str, str] = {}
    name, value, quoted, key = "", "", False, True
    for character in options + ",":
        if character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            if name.strip():
                found[name.strip().lower()] = value
            name, value, key = "", "", True
        elif character == "=" and key and not quoted:
            key = False
        elif key:
            name += character
        else:
            value += character
    return found


def signer_line_refuses(options: dict[str, str], today: str) -> str:
    """Why ssh-keygen would refuse this line for a git signature today, or ""."""
    if "cert-authority" in options:
        return "the line names a certificate authority, not this key"
    namespaces = options.get("namespaces", "")
    if namespaces and "git" not in [name.strip() for name in namespaces.split(",")]:
        return f"the line allows the namespaces {namespaces} and a tag is signed in git"
    # A signature carries no timestamp: ssh-keygen judges these against the
    # clock of whoever runs it, and the release workflow gives it the tag's
    # own tagger date. Here the signature does not exist yet, so the moment
    # is now -- the moment this checkout would sign.
    after, before = options.get("valid-after", ""), options.get("valid-before", "")
    for name, value in (("valid-after", after), ("valid-before", before)):
        if value and not signer_date(value):
            return (f'the line carries {name}="{value}", which ssh-keygen reads as an invalid time:'
                    " the shape is YYYYMMDD, or YYYYMMDDHHMM[SS], with an optional Z and no separator")
    if after and today < signer_date(after):
        return f"the line allows this key only after {after}"
    if before and today >= signer_date(before):
        return f"the line retired this key on {before}"
    return ""


SIGNER_TIME = re.compile(r"[0-9]{8}([0-9]{4}([0-9]{2})?)?[Zz]?")


def signer_date(value: str) -> str:
    """YYYYMMDD[HHMM[SS]][Z] padded to the comparable YYYYMMDDHHMMSS, or "".

    The shape ssh-keygen accepts and nothing else: it reads no separator,
    and answers `invalid "valid-before" time` to a line carrying one --
    measured. A reader more permissive than the tool that decides is a
    reader that says ok to a line the release will refuse.
    """
    if not SIGNER_TIME.fullmatch(value):
        return ""
    return (value.rstrip("Zz") + "0" * 14)[:14]


def public_key(project: pathlib.Path, declared: str) -> str:
    """The key text `user.signingkey` names: a literal, a .pub, or a private key beside one.

    git accepts all three, so all three are read; the private key beside a
    .pub is the shape a signing configuration takes most often, and reading
    it back as if it were a public key is how a checker reports a key it
    never understood. The content decides, not the name.
    """
    # git's own literal form, documented under user.signingKey.
    declared = declared[5:] if declared.startswith("key::") else declared
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
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    refusals = []
    for principal, options, kind, material in entries:
        if (kind, material) != (parts[0], parts[1]):
            continue
        refused = signer_line_refuses(signer_options(options), today)
        if not refused:
            return [("ok", f"the signing key is named in {allowed_signers.name}, as {principal}")]
        refusals.append(f"{principal}: {refused}")
    if refusals:
        return [("fail", f"the signing key is in {allowed_signers} on a line that would not sign this tag"
                         " today -- " + "; ".join(refusals) + ". ssh-keygen reads those options and the"
                         " release workflow runs it, so the tag would be refused after the push")]
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



# What a template leaves for the tag to fill, and what a rendered formula
# must not carry any more.
FORMULA_PLACEHOLDER = re.compile(r"@[A-Z0-9_]+@")

# One line of brew style: path, position, severity, cop, message.
STYLE_OFFENSE = re.compile(r"^(?P<path>\S+?):\d+:\d+: \w: (?P<cop>[\w/]+): (?P<message>.*)$")


def style_offenses(output: str, linted: pathlib.Path) -> tuple[list[str], list[str]]:
    """(offenses of this file, offenses it has only on this machine).

    Measured on the socle's own formula: with the fleet's tap tapped,
    `brew style` reports `Lint/DuplicateMethods` on `install`, naming the
    tap's installed copy of the same formula -- the class is defined twice
    in one RuboCop run. The runner that lints at the tag has no tap
    installed, so that offense exists on a maintainer's machine and nowhere
    else, which is the worst possible place for a false refusal: it is where
    `preflight` runs.

    Only a duplicate whose other definition is in another file is set aside,
    and it is reported rather than hidden. Two methods of the same name in
    the template itself name that file twice, and stay a refusal.
    """
    kept, elsewhere = [], []
    for line in output.splitlines():
        found = STYLE_OFFENSE.match(line.strip())
        if not found or found.group("path") != str(linted):
            continue
        # The paths a message names, each one possibly followed by :LINE.
        others = [named for named in re.findall(r"(/\S+?\.rb)(?::\d+)?", found.group("message"))
                  if named != str(linted)]
        if found.group("cop") == "Lint/DuplicateMethods" and others:
            elsewhere.append(f"{found.group('cop')}: {found.group('message')}")
        else:
            kept.append(f"{found.group('cop')}: {found.group('message')}")
    return kept, elsewhere


def formula_checks(decl: Declarations) -> list[tuple[str, str]]:
    """Whether each declared formula renders, and passes brew style, before the tag.

    A product published a version with no formula in the tap and had to cut
    the next patch for that alone: nothing between `preflight` and the tag
    ever looked at the template, and the first thing that did was the
    release itself, at the one moment nothing can be retried cheaply.

    A full render cannot happen here -- it hashes the archive of a tag that
    does not exist yet -- but everything else can. The template is
    substituted with the values that tag will produce, an unrendered
    placeholder is a refusal, and brew reads the result if brew is
    installed. What is left untested is the digest, which is the one part
    the workflow computes rather than the product writes.

    Every formula of a product goes to brew in one call: `brew style` boots
    Ruby and RuboCop, which costs seconds, and a product with two formulas
    paid it twice for nothing -- each offense names the file it is in, so
    one call answers for all of them.
    """
    found: list[tuple[str, str]] = []
    # The archive URL of the tag to come, built from the base and the
    # product rather than read from the remote: what is measured here is the
    # template and the style of what it produces, and asking git for the
    # origin would add a call to a gate that the recorded API replays hold
    # call by call. The real URL has this shape; brew reads its shape.
    base = git_base().rstrip("/")
    tag = f"v{decl.version}"
    rendered: dict[str, str] = {}
    for name in sorted(decl.formulas):
        template = decl.project / "packaging" / "homebrew" / f"{name}.rb.in"
        if not template.is_file():
            continue
        if decl.render_command:
            found.append(("note", f"{name}: rendered by the product's own script, which hashes the archive of a"
                                  " tag that does not exist yet: not rendered here"))
            continue
        text = template.read_text(encoding="utf-8") \
            .replace("@URL@", f"{base}/{decl.product}/archive/refs/tags/{tag}.tar.gz") \
            .replace("@VERSION@", decl.version) \
            .replace("@SHA256@", "0" * 64)
        left = sorted(set(FORMULA_PLACEHOLDER.findall(text)))
        if left:
            found.append(("fail", f"packaging/homebrew/{name}.rb.in leaves {', '.join(left)} unrendered:"
                                  " the tap job refuses a formula that still carries a placeholder"))
            continue
        if not host.HOST.which("brew"):
            found.append(("note", f"{name}: the formula renders; brew is not installed here, so its style is"
                                  " read at the tag and not before"))
            continue
        rendered[name] = text
    if not rendered:
        return found
    with tempfile.TemporaryDirectory(prefix="maelys-release-formula.") as temp:
        # Each under its own name: brew reads the class name against the
        # file name, and a formula linted as something else is not linted.
        paths = {name: pathlib.Path(temp) / f"{name}.rb" for name in rendered}
        for name, path in paths.items():
            path.write_text(rendered[name], encoding="utf-8")
        styled = run(["brew", "style", *(str(path) for path in paths.values())])
        output = styled.stdout + styled.stderr
        read: list[tuple[str, str]] = []
        attributed = False
        for name, path in paths.items():
            offenses, elsewhere = style_offenses(output, path)
            attributed = attributed or bool(offenses) or bool(elsewhere)
            if offenses:
                read.append(("fail", f"brew style refuses the rendered {name}: {offenses[0]}"))
            else:
                read.append(("ok", f"{name} renders from its template and passes brew style"))
            for offense in elsewhere:
                read.append(("note", f"{name}: brew reports {offense.split(':')[0]} because this formula is also"
                                     " installed in a tap on this machine; the runner that lints at the tag"
                                     " has no tap"))
        if styled.returncode != 0 and not attributed:
            # brew refused and no line of its output named one of these
            # files: whatever it is, none of them may be called passing.
            detail = output.strip().splitlines()
            found.append(("fail", "brew style refuses the rendered formulas: "
                                  + (detail[-1] if detail else "no diagnostic")))
        else:
            found.extend(read)
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
