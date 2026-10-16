# SPDX-License-Identifier: MPL-2.0
"""Open a release, wait for its checks, and sign its tag through the current host."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import tempfile
import time

from typing import Callable

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation
from . import host
from .constants import DECLARATION_FILE, PROGRAM, UNPUBLISHED
from .context import Context
from .declarations import Declarations, version_tuple
from .forge import forge
from .github import (check_runs, cut_repository, default_branch, github_api, tag_deployments)
from .host import git, run
from .identity import socle_data
from .project import names_git_gave, project_of
from .release_checks import repository_checks, tag_checks
from .writes import confirm_ref, confirm_tag
from .texts import LABELS, unfenced


# ---- cutting a release -------------------------------------------------------
#
# Two stops, and the middle one is GitHub's. The conventions say a release
# is a signed annotated tag on a commit this repository's own checks passed
# on, and that a tag is never pushed before they do; no command applied
# either rule, and the failure mode was human and repeated. `cut` makes the
# two mechanical without taking the merge: it opens the pull request and
# waits, the repository merges it under its own rules, and `cut --tag` signs
# the tag on the merge commit those checks ran on -- not on the branch,
# which another merge can move between the two.

# What GitHub answers about a check run. `gh pr checks --watch` returns at
# once when no check has registered yet, which is how a release was tagged
# on a run that had not started: the socle waits for a check to exist
# before it waits for it to finish.
CHECK_PENDING = ("queued", "in_progress", "pending", "waiting", "requested")
# skipped and neutral are conclusions of a check that ran and refused
# nothing; a path filter or a matrix exclusion is not a red check.
CHECK_PASSED = ("success", "skipped", "neutral")
CHANGELOG_ENTRY = r"^## {version}[^\n]*?([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})[^\n]*$\n(.*?)(?=^## |\Z)"


def host_target() -> str:
    """The socle target name this machine is, for a verify command run here."""
    system, machine = os.uname().sysname, os.uname().machine
    architecture = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"{'macos' if system == 'Darwin' else 'linux'}-{architecture}"


def worktree_paths(project: pathlib.Path) -> dict:
    """path -> its two status columns, read unstripped.

    The two columns are what separates a tracked change from an untracked
    file, and the first of them is a space often enough that stripping the
    line eats a character of the path.
    """
    # A name that is not UTF-8 is written as the repository holds it where
    # core.quotePath is off, and ended `cut` here on a decoding error.
    try:
        answered = run(["git", "status", "--porcelain", "-uall"], cwd=project).stdout
    except UnicodeDecodeError as error:
        raise names_git_gave(error, skip=3) from None
    return {line[3:]: line[:2] for line in answered.split("\n") if line}


def entry_heading(project: pathlib.Path, version: str) -> str:
    """The heading to write for `version`, spelled as this changelog spells its entries.

    Any separator is accepted between the version and the date. The hint
    showed an em dash whatever the file used, and an agent on maelys-cli,
    whose ten entries say `## 0.5.29 - 2026-09-14`, would have copied the
    dash of the message rather than the dash of the file.
    """
    changelog = project / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8", errors="replace") if changelog.is_file() else ""
    found = re.search(r"^## [0-9]+\.[0-9]+\.[0-9]+(\s+\S+\s+)[0-9]{4}-[0-9]{2}-[0-9]{2}", text, re.MULTILINE)
    return f"## {version}{found.group(1) if found else ' — '}YYYY-MM-DD"


def changelog_entry(project: pathlib.Path, version: str, at: str = "") -> tuple[str, str]:
    """(date, body) of the ## VERSION entry, in the worktree or at a commit."""
    if at:
        source = git("show", f"{at}:CHANGELOG.md", cwd=project, check=False)
    else:
        path = project / "CHANGELOG.md"
        source = path.read_text(encoding="utf-8") if path.is_file() else ""
    match = re.search(CHANGELOG_ENTRY.format(version=re.escape(version)), source, re.MULTILINE | re.DOTALL)
    return (match.group(1), match.group(2).strip("\n")) if match else ("", "")


def check_summary(runs: list) -> list:
    return [{"name": run.get("name") or "?", "status": run.get("status") or "?",
             "conclusion": run.get("conclusion") or ""} for run in runs]


def failed_checks(runs: list) -> list:
    return [run for run in runs if (run.get("conclusion") or "") not in CHECK_PASSED]


def await_checks(repository: str, sha: str, timeout: int, poll: int, log) -> list:
    """Block until SHA has at least one check run and none is still running.

    A run that fails at startup produces no check at all, and waiting on
    nothing looks exactly like waiting on a queue: when the deadline passes
    with no check registered, the workflow runs GitHub does hold for that
    commit are named in the refusal.
    """
    deadline = time.monotonic() + timeout
    seen: list = []
    while True:
        runs = check_runs(repository, sha)
        if runs is None:
            raise Failure("PROCESS_FAILED", f"gh could not read the checks of {sha[:7]} in {repository}.",
                          "Check 'gh auth status' and the network, then run the command again.")
        seen = runs
        pending = [run for run in runs if (run.get("status") or "") in CHECK_PENDING]
        if runs and not pending:
            return runs
        if time.monotonic() >= deadline:
            break
        print(f"note     {sha[:7]}: "
              + (f"{len(pending)} of {len(runs)} checks still running" if runs else "no check registered yet"),
              file=log)
        log.flush()
        time.sleep(poll)
    if not seen:
        others = ", ".join(f"{run.get('name')} {run.get('status')}/{run.get('conclusion') or '-'}"
                           for run in (github_api(f"repos/{repository}/actions/runs?head_sha={sha}&per_page=100")
                                       or {}).get("workflow_runs") or []) or "none"
        raise Failure("PRECONDITION_FAILED",
                      f"No check registered on {sha[:7]} of {repository} within {timeout}s; workflow runs for that"
                      f" commit: {others}.",
                      "A tag is pushed only on a commit this repository has checked: give the commit a workflow,"
                      " or read the run that failed to start.")
    raise Failure("PRECONDITION_FAILED",
                  f"The checks of {sha[:7]} in {repository} were still running after {timeout}s: "
                  + ", ".join(f"{run.get('name')} {run.get('status')}" for run in seen
                              if (run.get("status") or "") in CHECK_PENDING) + ".",
                  "Wait for them and run the command again; nothing has been tagged.")


def version_pattern(version: str) -> "re.Pattern[str]":
    """VERSION as a version, not as a substring of one.

    On the left, a digit or a dot disqualifies: 0.1.1 must not match inside
    10.1.1. On the right the test is finer, because a dot means two
    different things. A dot followed by a digit continues the version, so
    0.1.1 must not match inside 0.1.10 or 0.1.1.2; a dot followed by
    anything else ends it, and 0.1.1.tar.gz carries 0.1.1. A letter or a
    dash never disqualifies: v0.19.9 and maelys-egress-node-sdk-0.19.9 are
    how versions are written, and an anchor that refused them lost real
    carriers of maelys-egress, whose README installs an archive by name.
    """
    return re.compile(r"(?<![0-9.])" + re.escape(version) + r"(?!\.?[0-9])")


def previous_bump(project: pathlib.Path) -> tuple[str, str, str]:
    """The last commit that moved VERSION, and the two versions it moved between.

    Empty when there is none to learn from: a first release, a shallow
    clone, or a repository whose first commit already carried VERSION.
    """
    commit = git("log", "-1", "--format=%H", "--", "VERSION", cwd=project, check=False)
    if not commit:
        return "", "", ""
    old = git("show", f"{commit}~1:VERSION", cwd=project, check=False).strip()
    new = git("show", f"{commit}:VERSION", cwd=project, check=False).strip()
    if not old or not new or old == new:
        return "", "", ""
    return commit, old, new


def version_carriers(project: pathlib.Path, commit: str, old: str, new: str) -> list[str]:
    """The files COMMIT moved the version in, VERSION excluded.

    A file qualifies when the same diff drops the old version and adds the
    new one. The conjunction is what makes this usable: CHANGELOG.md adds
    the new version without dropping the old, so it falls out on its own,
    and so does every file an adoption changed in the same commit.
    """
    patch = git("show", "--format=", "--no-color", commit, cwd=project, check=False)
    drops, adds = version_pattern(old), version_pattern(new)
    carriers, path, dropped, added = [], "", False, False
    def close() -> None:
        if path and path != "VERSION" and dropped and added:
            carriers.append(path)
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            close()
            _, _, tail = line.partition(" b/")
            path, dropped, added = tail, False, False
        elif line.startswith("-") and not line.startswith("---"):
            dropped = dropped or bool(drops.search(line))
        elif line.startswith("+") and not line.startswith("+++"):
            added = added or bool(adds.search(line))
    close()
    return sorted(set(carriers))


def bump_audit(project: pathlib.Path, old: str, new: str) -> tuple[list[tuple[str, str]], list[str]]:
    """What cut has just written, held against what the previous bump moved.

    cut writes VERSION and runs [cut] after-version, and nothing then reads
    the result until the pull request's own checks do -- after a branch is
    pushed and a pull request is opened, which is late, and only where the
    product happens to compare the two at all. This is cut auditing its own
    write rather than the socle judging the product a second time: the
    question is not what carries a version, which no socle can know, but
    whether this cut moved the version everywhere the last one did.

    It lags by one release. A place the version reached since the previous
    bump is invisible here, and so is the first release of a product.
    maelys-cli reported the gap, from a VERSION and a header compared by
    make check-version.
    """
    commit, was, became = previous_bump(project)
    if not commit:
        return [("note", "no previous bump to compare this one with: nothing says where else the version lives")], []
    carriers = version_carriers(project, commit, was, became)
    if not carriers:
        return [("ok", f"the bump {was} -> {became} moved the version in VERSION alone")], []
    found: list[tuple[str, str]] = []
    stale: list[str] = []
    for name in carriers:
        path = project / name
        if not path.is_file():
            found.append(("note", f"{name} carried the version at {was} -> {became} and is no longer in the tree"))
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            found.append(("note", f"{name} cannot be read: {error}"))
            continue
        if version_pattern(new).search(text):
            found.append(("ok", f"{name} carries {new}"))
        elif version_pattern(old).search(text):
            # The version is still there, one release behind. Nothing else
            # explains that, so this is the omission the audit exists for.
            stale.append(name)
            found.append(("fail", f"{name} carries the version and still holds {old}"))
        else:
            # Neither version is in the file, and that has two readings: a
            # carrier this cut forgot and whose contents moved on anyway, or
            # a file that has simply stopped carrying the version. They read
            # the same, and their consequences do not. The documented limit
            # -- a place the version reaches since the previous bump is
            # invisible -- costs a missed detection the product's own checks
            # still catch. Its twin, a place the version leaves, would cost a
            # refusal nothing downstream can lift: [cut] declares a command,
            # not a list, so the operator's only ways out would be to make a
            # file carry a version it should not, or to cut outside the tool.
            # maelys-cli found it in their own history: 0.1.0 -> 0.2.0 moved
            # the version in a generated CLI reference that no longer carries
            # it at all.
            found.append(("note", f"{name} moved the version at the previous bump and holds neither"
                                  f" {old} nor {new}: either this cut missed it, or it has stopped"
                                  " carrying the version. Refusing on that would block a release"
                                  " with nothing to unblock it"))
    return found, stale


# ---- said before the cut ------------------------------------------------------
#
# `cut` refuses what it must, and it is right to. It is also the last command
# of a release, run by whoever signs, alone at a terminal: twice in two days a
# new product was delivered with the entry of its release left undated, once
# with VERSION already moved to the version to cut, `check` answered "ok" to
# whoever prepared it, and the refusal arrived at the other end. So `check`
# says, as a note and in `cut`'s own words, what `cut` is going to refuse --
# to the reader who can still fix it.
#
# Never more than a note, and only what is read. Two of `cut_open`'s refusals
# are foretold here, the entry and the version; the others are a state of the
# moment, of GitHub or of the signer's machine, and docs/conventions.md lists
# them so that nobody reads "check: ok" as "cut will pass".

# The one title `cut` reads, and the titles that look like it to a person.
VERSION_HEADING = re.compile(r"^## ([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])[^\n]*$")
ENTRY_LIKE = re.compile(r"^#{2,3}[ \t]+\[?v?([0-9]+\.[0-9]+\.[0-9]+)\]?(?![0-9.])[ \t]*"
                        r"($|[-—–:(]|[0-9]{4}-[0-9]{2}-[0-9]{2})")


def unread_entries(decl: Declarations) -> list[str]:
    """One note per version after VERSION whose entry `cut` would not find.

    The entries above VERSION are the releases to come, and `cut` asks one
    thing of theirs: the title `## X.Y.Z` and a date on that line. Whether
    it finds one is asked of `cut`'s own reader, so that the note and the
    refusal cannot disagree. What is looked for is wider than what `cut`
    reads, on purpose: `## [1.3.0] - 2026-09-04`, `## v1.3.0`, `### 1.3.0`
    and a second space are entries to whoever wrote them, three products of
    the fleet title theirs with brackets, and `cut` answers "no dated entry"
    to each. A fenced block is skipped: an example is not an entry.
    """
    project = decl.project
    changelog = project / "CHANGELOG.md"
    if not changelog.is_file():
        return []
    said, seen = [], set()
    for line in unfenced(changelog.read_text(encoding="utf-8", errors="replace")).split("\n"):
        exact, like = VERSION_HEADING.match(line), ENTRY_LIKE.match(line)
        if not exact and not like:
            continue
        version = (exact or like).group(1)
        # A title present twice is `check`'s own refusal already: said once here.
        if version in seen or version_tuple(version) <= version_tuple(decl.version) \
                or changelog_entry(project, version)[0]:
            continue
        seen.add(version)
        refusal = f"`cut` will refuse {version} -- CHANGELOG.md has no dated entry '{entry_heading(project, version)}'"
        if exact:
            # One rule, in the note as in the managed texts: an entry takes
            # its version and its date together, the day the release is
            # handed over. Until then it is `## Unreleased`; a later date is
            # `cut`'s other refusal.
            said.append(f"CHANGELOG.md carries '{line.strip()}', without a date: {refusal}. A release is handed over"
                        " with its entry dated that day, never a later one, which `cut` refuses too; until then"
                        " its changes sit under '## Unreleased'")
        else:
            said.append(f"CHANGELOG.md carries '{line.strip()}', which `cut` does not read as the entry of"
                        f" {version}: {refusal}. Title it that way")
    return said


def in_a_job() -> bool:
    """Whether this runs in a job rather than on somebody's checkout.

    The note on VERSION is for whoever prepares a release, before the push.
    The shared CI checks out the whole history and runs `check` on every
    commit, the release commit included -- where VERSION is, by
    construction, a version no tag carries yet, and where nobody is there
    to do anything about a note. What a job is, the host says: the address
    note and the privacy guard ask it the same question.
    """
    return host.HOST.is_runner()


def release_in_progress(project: pathlib.Path, product: str, version: str) -> bool:
    """Whether what a release leaves behind is here: VERSION is then where it should be.

    From the first stop to the tag, VERSION is a version no tag carries --
    on the branch, on its pull request, and on the default branch once it
    is merged. Three things say so, measured on eight products of the fleet
    at their last release commit, the tag deleted:

    - a branch `release/vX.Y.Z` here or on origin, read where `cut` reads
      it to resume. One product names its own `release/X.Y.Z`, forty-four
      merges of them: both spellings.
    - the subject `cut` gives the commit that writes VERSION, `<product>
      X.Y.Z`. A squash keeps it and adds ` (#N)`; a merge keeps the commit.
      Six of the eight.
    - the subject GitHub gives the merge of that branch. Five of the eight,
      one of them the product whose bump commit is titled by hand.

    Together, seven. The eighth moved VERSION in the pull request of a
    feature and tagged eleven commits later: nothing separates that from a
    version moved before `cut`, because it is one, and the note says both.

    The two subjects are one search, anchored on the first line: a body
    that mentions the branch, or quotes a subject, is not a release.
    """
    for branch in (f"release/v{version}", f"release/{version}"):
        if any(git("rev-parse", "-q", "--verify", name, cwd=project, check=False)
               for name in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")):
            return True
    dotted = version.replace(".", "\\.")
    # ERE, as git compiles it, with no line mode: ^ is the start of the
    # message, which is its subject, and a newline is an ordinary character.
    # A product's name is [a-z0-9-], every command refuses another: it is
    # written as it is, since POSIX leaves an escaped hyphen undefined.
    subject = (f"^({product} {dotted}( \\(#[0-9]+\\))?"
               f"|Merge pull request #[0-9]+ from [^ \n]+/release/v?{dotted})(\n|$)")
    return bool(git("rev-parse", "-q", "--verify", "HEAD^{/" + subject + "}", cwd=project, check=False))


def unpublished_version(decl: Declarations) -> list[str]:
    """The note for a VERSION no tag has published, when that is neither normal nor unknowable.

    Normal: 0.0.0, which says so itself; a release in progress; a version
    this repository publishes candidates of under its own ceremony. Not
    knowable: a directory that is not a repository, a shallow clone, a clone
    told to fetch no tags -- `preflight` and `cut` refuse the same shallow
    clone for the same reason. And not said in a job at all, before any
    call is made for it.
    """
    project, version = decl.project, decl.version
    if version == UNPUBLISHED or in_a_job() or not (project / ".git").exists():
        return []
    if git("rev-parse", "--is-shallow-repository", cwd=project, check=False) != "false":
        return []
    tags = git("tag", "--list", "v*", "--sort=-v:refname", cwd=project, check=False).split()
    if f"v{version}" in tags or any(tag.startswith(f"v{version}-") for tag in tags):
        return []
    if not tags and git("config", "--get", "remote.origin.tagOpt", cwd=project, check=False) == "--no-tags":
        return []
    if release_in_progress(project, decl.product, version):
        return []
    # What was published last is what this history reaches, as `cut` reads
    # it for the pins: on a maintenance branch at 1.2.4, the highest tag of
    # the repository is a 1.3.0 that 1.2.4 does not come after.
    reached = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", cwd=project, check=False)
    back = reached[1:] if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", reached) \
        else (UNPUBLISHED if not reached else "the version last published")
    observed = (f"VERSION says {version} and this clone holds no tag v{version} ("
                + (f"the last tag this history reaches is {reached}" if reached else "this history reaches no tag")
                + ")")
    refuses = (f"for `cut`, VERSION says what was published last, and `cut` will refuse {version} as not coming"
               " after the current version")
    # A clone learns of a tag only when it fetches it, and the usual pull
    # does not: measured, 'git pull origin main' brings no new tag.
    stale = (" A clone may lack the newest tags, and 'git pull origin main' fetches none: 'git fetch --tags"
             " origin' first")
    if not decl.releases_here:
        # A repository that publishes by its own means moves VERSION by
        # hand: the RELEASING.md the socle seeds there says so, and tells it
        # to run `check` in exactly this state. Nothing to undo is named.
        return [f"{observed}. A release prepared by hand stands there between its merge and its tag, as"
                " RELEASING.md describes: nothing to change, the signed tag is what is left. `cut` does not"
                f" start from there: {refuses} -- a release made with `cut` leaves VERSION at {back} and lets"
                f" `cut` move it.{stale}"]
    # Observed first, then the two states that read the same, and what each
    # asks. The remedy of the second is wrong for the first, which is the
    # ordinary one, so it never opens the sentence.
    return [f"{observed}. Two states read that way. A release already merged and waiting for its tag: change"
            f" nothing; '{PROGRAM} cut DIR {version} --tag' signs it when `cut` opened it, a tag signed by hand"
            f" otherwise. Or a VERSION moved before `cut`: {refuses}; set VERSION back to {back}, commit, and"
            f" cut {version} with its dated entry.{stale}"]


def before_cut(decl: Declarations) -> list[str]:
    """What `cut` will refuse in this repository as it stands, for `check` to say as notes."""
    # A repository that publishes by nothing at all cuts nothing.
    if not decl.version or decl.mechanism == "none":
        return []
    return unread_entries(decl) + unpublished_version(decl)


def cut_gate(decl: Declarations, version: str, signers: pathlib.Path) -> list:
    """The gate cut holds before it writes anything.

    Only what the repository's own CI cannot see: the operator's signing
    configuration, the tags already published, and -- for a product the
    socle releases -- the environment a tag would deploy into. The product
    contract is judged by the checks cut then waits for, on the exact
    commit, rather than a second time here from a socle that may not be the
    one the product pins.
    """
    gate = tag_checks(decl.project, version, signers)
    if decl.releases_here:
        gate += repository_checks(decl)
    return gate


# Why GitHub reports a commit unverified, in the words that say what to fix.
# The reasons are GitHub's own (`commit.verification.reason`).
UNVERIFIED = {
    "unsigned": "it carries no signature",
    "no_user": "its committer address belongs to no GitHub account. user.email is the signer's own to set, to"
               " an address of the account that holds the signing key; anyone else leaves it",
    "unverified_email": "its committer address is not verified on the account that holds the signing key",
    "bad_email": "its committer address is not one of the account that holds the signing key",
    "unknown_key": "the key that signed it is not registered on GitHub as a signing key; one registered for"
                   " authentication alone does not count",
}
# The ones the commit itself carries: fixed by committing again, not on GitHub.
IN_THE_COMMIT = ("unsigned", "no_user", "unverified_email", "bad_email")


def release_commit_signature(project: pathlib.Path, repository: str, branch: str, sha: str,
                             version: str) -> tuple[str, str, str]:
    """(status, message, what to do) of the release commit, as GitHub reads its signature.

    `cut` signs that commit and had never asked whether the signature was
    worth anything where it lands. A new product cut its first release from a
    clone with no user.email: git signed as user@<hostname>, GitHub answered
    `no_user`, and the commit was merged signed and unverified -- found
    afterwards, by reading the history. The gate reads the key and cannot
    know this: only GitHub ties an address to an account, and only once the
    commit is there. So it is read back, like every other write.

    An answer GitHub did not give is a note and never a refusal: what stops
    a release is what was read, not what could not be.
    """
    answer = github_api(f"repos/{repository}/commits/{sha}") or {}
    verification = (answer.get("commit") or {}).get("verification")
    if not isinstance(verification, dict) or not isinstance(verification.get("verified"), bool):
        return "note", f"GitHub did not say whether it verifies the release commit {sha[:7]}", ""
    if verification["verified"]:
        return "ok", f"GitHub verifies the release commit {sha[:7]}", ""
    reason = str(verification.get("reason") or "")
    again = f"'{PROGRAM} cut {project} {version} --apply'"
    if reason in IN_THE_COMMIT:
        remedy = (f"fix it, then sign the commit again and replace it: git -C {project} commit --amend"
                  f" --reset-author -S --no-edit && git -C {project} push --force-with-lease origin {branch};"
                  f" then {again}")
    else:
        remedy = f"fix it on GitHub, then {again}: the commit itself need not change"
    return ("fail", f"GitHub does not verify the release commit {sha[:7]} ({reason or 'no reason given'}): "
                    + UNVERIFIED.get(reason, "read its verification on GitHub"), remedy)


def signature_gate(project: pathlib.Path, data: dict) -> bool:
    """Add the line above to the gate; False when it refuses, with what to do next."""
    status, message, remedy = release_commit_signature(project, data["repository"], data["branch"],
                                                       data["commit"], data["version"])
    data["gate"].append({"status": status, "message": message})
    if status != "fail":
        return True
    data["ready"] = False
    data["next"] = remedy
    return False


def one_pull(project: pathlib.Path, repository: str, branch: str) -> dict | None:
    """The single pull request coming from BRANCH, whatever its state."""
    listed = forge().pull_requests(project, repository, branch)
    if listed.returncode != 0:
        raise Failure("PROCESS_FAILED", f"gh pr list failed: {listed.stderr.strip()}",
                      "Check 'gh auth status', then run the command again.")
    # A pull request closed without a merge is not this release, whatever
    # its branch: the hint below asked to close it, and it already was --
    # 0.59.0's first attempt was closed after its checks refused, the branch
    # deleted and cut again, and `cut --tag` then counted the closed one and
    # refused to sign a release whose pull request was merged and green.
    pulls = [pull for pull in json.loads(listed.stdout or "[]") if pull.get("state") != "CLOSED"]
    if len(pulls) > 1:
        raise Failure("PRECONDITION_FAILED",
                      f"{len(pulls)} pull requests come from {branch} in {repository}: "
                      + ", ".join(str(pull["number"]) for pull in pulls) + ".",
                      "Close the ones that do not belong to this release, then run the command again.")
    return pulls[0] if pulls else None


def cut_report(data: dict, runs: list) -> tuple[dict, int]:
    """What the first stop answers once it has read the checks of its commit."""
    data["checks"] = check_summary(runs)
    failed = failed_checks(runs)
    data["ready"] = bool(runs) and not failed
    if not runs:
        data["gate"].append({"status": "note", "message": "no check is registered on this commit yet"})
    data["next"] = (f"merge {data['pullRequest']['url']} on GitHub, then"
                    f" '{PROGRAM} cut {data['project']} {data['version']} --tag --apply'" if data["ready"]
                    else (f"fix what the checks refused and push {data['branch']} again" if failed
                          else f"wait for the checks, then '{PROGRAM} cut {data['project']}"
                               f" {data['version']} --apply'"))
    return data, EXIT_OK if data["ready"] else EXIT_VIOLATIONS


def cut_resume(invocation: Invocation, decl: Declarations, data: dict, log, timeout: int,
               poll: int, signers: pathlib.Path) -> tuple[dict, int] | None:
    """The wait, re-entered; None when there is nothing to resume.

    A first stop whose wait timed out leaves a branch and an open pull
    request behind, and every later check of cut_open would refuse them:
    the version no longer follows, HEAD is no longer the default branch,
    the branch exists. Refusing a state cut itself created would leave the
    operator to finish by hand exactly where a command was wanted.
    """
    project, version, branch, repository = decl.project, data["version"], data["branch"], data["repository"]
    if not (git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", cwd=project, check=False)
            or git("ls-remote", "--heads", "origin", branch, cwd=project, check=False)):
        return None
    pull = one_pull(project, repository, branch)
    if pull is None:
        raise Failure("PRECONDITION_FAILED", f"branch {branch} exists and no pull request comes from it.",
                      "Open one, or delete the branch and cut again.")
    if pull["state"] == "MERGED":
        raise Failure("PRECONDITION_FAILED", f"{pull['url']} is already merged.",
                      f"Sign the tag: '{PROGRAM} cut {project} {version} --tag --apply'.")
    if pull["state"] != "OPEN":
        raise Failure("PRECONDITION_FAILED", f"{pull['url']} is {pull['state'].lower()}.",
                      "Reopen it, or delete the branch and cut again.")
    head = pull["headRefOid"]
    git("fetch", "-q", "origin", branch, cwd=project)
    carried = git("show", f"{head}:VERSION", cwd=project, check=False).strip()
    if carried != version:
        raise Failure("PRECONDITION_FAILED", f"{branch} carries VERSION {carried or 'nothing'}, not {version}.",
                      "That branch is not this release; delete it, or cut the version it carries.")
    data["pullRequest"] = {"number": pull["number"], "url": pull["url"], "state": pull["state"]}
    data["commit"] = head
    data["changelog"] = changelog_entry(project, version, at=head)[0]
    gate = cut_gate(decl, version, signers)
    data["gate"] = [{"status": status, "message": message} for status, message in gate]
    if any(status == "fail" for status, _ in gate):
        return data, EXIT_VIOLATIONS
    # The commit may have been replaced since the first stop refused it:
    # this is where the replacement is read.
    if not signature_gate(project, data):
        return data, EXIT_VIOLATIONS
    if invocation.flag("--apply"):
        print(f"cut      waiting for the checks of {head[:7]} in {repository}", file=log)
        log.flush()
        return cut_report(data, await_checks(repository, head, timeout, poll, log))
    return cut_report(data, check_runs(repository, head) or [])


def moved_pins_named(project: pathlib.Path, entry: str) -> list[tuple[str, str]]:
    """Every pin this release moves, named in the entry that describes it.

    A patch of maelys-cli moved maelys-json across an ABI, and a consumer
    found out by linking. The socle will not judge a product's versioning --
    `bump_audit` refuses to, on principle, and the fleet does cross a series
    in a patch -- but it can make the move impossible to miss: an entry that
    does not name the dependency it re-pins is refused, and since the tag's
    annotation is that entry, the name reaches the tag too.
    """
    last = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", cwd=project, check=False)
    if not last:
        return []
    try:
        answered = git("diff", "--name-only", f"{last}..HEAD", "--", "dependencies", cwd=project, check=False)
    except UnicodeDecodeError as error:
        raise names_git_gave(error) from None
    moved = [line for line in answered.splitlines()
             if line.startswith("dependencies/") and line.endswith(".pin")]
    found = []
    for path in moved:
        name = path[len("dependencies/"):-len(".pin")]
        if re.search(rf"(?<![A-Za-z0-9._-]){re.escape(name)}(?![A-Za-z0-9._-])", entry):
            found.append(("ok", f"{path} moved since {last}, and the entry names {name}"))
        else:
            found.append(("fail", f"{path} moved since {last} and the entry of this version does not name"
                                  f" {name}: a consumer of this release links against the new pin, and"
                                  " the only place it can learn so is the entry and the tag it becomes"))
    return found


def cut_open(invocation: Invocation, decl: Declarations, data: dict, log, timeout: int, poll: int, signers: pathlib.Path) -> tuple[dict, int]:
    """First stop: the signed bump commit, its pull request, and the wait."""
    project, product, version = decl.project, decl.product, data["version"]
    branch, base, repository = data["branch"], data["base"], data["repository"]
    resumed = cut_resume(invocation, decl, data, log, timeout, poll, signers)
    if resumed is not None:
        return resumed
    if not decl.version:
        raise Failure("PRECONDITION_FAILED", f"{project} carries no VERSION of the form X.Y.Z.",
                      "Write VERSION before cutting a release.")
    if version_tuple(version) <= version_tuple(decl.version):
        # A VERSION that no tag ever published is a scaffold's, or a bump
        # written by hand: saying which is what unblocks the first release.
        published = bool(git("tag", "--list", f"v{decl.version}", cwd=project, check=False))
        raise Failure("VALIDATION_FAILED", f"{version} does not come after the current version {decl.version}.",
                      "Pass the version this release carries; a published version is never cut twice." if published
                      else f"No tag v{decl.version} exists: VERSION says what was published last. Set it to the"
                           f" version last published (0.0.0 when none), commit, and cut {version} with its dated entry.")
    data["previousVersion"] = decl.version
    # The bump in progress is the only change the release commit may carry:
    # anything else in the worktree would ride into a tagged commit unread.
    unexpected = sorted(path for path, code in worktree_paths(project).items()
                        if code != "??" and path not in ("VERSION", "CHANGELOG.md"))
    if unexpected:
        raise Failure("PRECONDITION_FAILED",
                      f"{project} has uncommitted changes outside VERSION and CHANGELOG.md: "
                      + ", ".join(unexpected) + ".",
                      "Commit or stash them: the release commit carries the bump and nothing else.")
    date, body = changelog_entry(project, version)
    if not date:
        raise Failure("PRECONDITION_FAILED", f"CHANGELOG.md has no dated entry '{entry_heading(project, version)}'.",
                      "Write the entry first: the release workflow compares VERSION with the tag and never reads it,"
                      " so nothing downstream would catch its absence.")
    if date > datetime.date.today().isoformat():
        raise Failure("VALIDATION_FAILED", f"the CHANGELOG entry of {version} is dated {date}, in the future.",
                      "Date the entry the day the release is cut.")
    data["changelog"] = date
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=project, check=False)
    if current != base:
        raise Failure("PRECONDITION_FAILED", f"HEAD is on {current}, not {base}: a release is never cut from a branch.",
                      f"git -C {project} switch {base}")
    git("fetch", "-q", "origin", base, cwd=project)
    ahead = git("rev-list", "--count", "FETCH_HEAD..HEAD", cwd=project, check=False)
    behind = git("rev-list", "--count", "HEAD..FETCH_HEAD", cwd=project, check=False)
    if (ahead, behind) != ("0", "0"):
        raise Failure("PRECONDITION_FAILED",
                      f"{base} is {ahead} ahead and {behind} behind origin/{base}.",
                      f"Bring the two together before cutting: the pull request is opened against origin/{base}.")
    gate = cut_gate(decl, version, signers) + moved_pins_named(project, body)
    data["gate"] = [{"status": status, "message": message} for status, message in gate]
    refused = [message for status, message in gate if status == "fail"]
    data["ready"] = not refused
    if refused:
        return data, EXIT_VIOLATIONS
    if not invocation.flag("--apply"):
        if decl.verify_command:
            data["gate"].append({"status": "note",
                                 "message": f"--apply would run {decl.verify_command.replace('TARGET', host_target())}"
                                            " here before writing anything"})
        # Said before anything is written, so a product that needs
        # [cut] after-version learns it from the plan rather than from a
        # pull request its own checks refuse.
        commit, was, became = previous_bump(project)
        elsewhere = version_carriers(project, commit, was, became) if commit else []
        if elsewhere and not decl.after_version:
            data["gate"].append({"status": "note",
                                 "message": f"the bump {was} -> {became} moved the version in "
                                            + ", ".join(elsewhere)
                                            + " as well, and no [cut] after-version is declared:"
                                              " --apply would write VERSION alone and refuse"})
        data["next"] = f"{PROGRAM} cut {project} {version} --apply"
        return data, EXIT_OK
    # The gate a product declared for the release runners is held here too,
    # before a branch and a pull request exist. maelys-datalog found the gap
    # by migrating onto cut: their own ceremony ran make check and a second
    # compiler in a container before VERSION was written, and cut left the
    # branch's CI as the only gate. scripts/verify-release.sh is the socle's
    # own concept for exactly this, so it is declared once and held at both
    # ends rather than named twice.
    if decl.verify_command:
        command = decl.verify_command.replace("TARGET", host_target())
        print(f"cut      {command}", file=log)
        log.flush()
        verified = host.HOST.stream(["sh", "-c", command], log, cwd=project)
        if verified.returncode != 0:
            raise Failure("PROCESS_FAILED", f"{command} failed with exit {verified.returncode}.",
                          "Read the log above; nothing has been written, no branch and no pull request exist.")
    # VERSION is written by the socle rather than by a heredoc: four tags
    # once shipped a VERSION holding a literal backslash-n.
    before = worktree_paths(project)
    (project / "VERSION").write_text(version + "\n", encoding="utf-8")
    # A product whose version is also materialised elsewhere — a header, a
    # package.json, a formula — regenerates it here, and what the command
    # touched joins the bump commit. Without this the commit is red by
    # construction wherever a check compares the two, and the wait of the
    # first stop could never see it green.
    touched: list[str] = []
    if decl.after_version:
        print(f"cut      {decl.after_version}", file=log)
        log.flush()
        regenerated = run(["sh", "-c", decl.after_version], cwd=project)
        touched = sorted(path for path, code in worktree_paths(project).items() if before.get(path) != code)
        if regenerated.returncode != 0:
            (project / "VERSION").write_text(decl.version + "\n", encoding="utf-8")
            raise Failure("PROCESS_FAILED",
                          f"[cut] after-version failed with exit {regenerated.returncode}:"
                          f" {(regenerated.stderr or regenerated.stdout).strip() or 'no diagnostic'}",
                          "VERSION is restored"
                          + (f"; the command left {' '.join(path for path in touched if path != 'VERSION')}"
                             " changed" if len(touched) > 1 else "")
                          + ". Fix the command, then cut again.")
        data["regenerated"] = [path for path in touched if path != "VERSION"]
    # The write is now complete, and this is the last moment it costs
    # nothing to be wrong: no branch, no pull request, no run.
    audit, stale = bump_audit(project, decl.version, version)
    data["bump"] = [{"status": status, "message": message} for status, message in audit]
    for status, message in audit:
        print(f"{status:<8} {message}", file=log)
    log.flush()
    if stale:
        (project / "VERSION").write_text(decl.version + "\n", encoding="utf-8")
        left = [path for path in touched if path not in ("VERSION", "CHANGELOG.md")]
        raise Failure("PRECONDITION_FAILED",
                      f"VERSION was written and {', '.join(stale)} did not follow:"
                      f" the previous bump moved the version there too.",
                      "VERSION is restored"
                      + (f"; {' '.join(left)} is left changed" if left else "")
                      + ". Declare 'after-version COMMAND' in the [cut] section of "
                      + DECLARATION_FILE + " so the product regenerates them, then cut again."
                      " Without it the commit is red by construction wherever a check compares the two,"
                      " and silent wherever none does.")
    git("switch", "-q", "-c", branch, cwd=project)
    git("add", "--", "VERSION", "CHANGELOG.md", *touched, cwd=project)
    git("-c", "commit.gpgsign=true", "commit", "-q", "-S", "-m", f"{product} {version}", cwd=project)
    data["commit"] = git("rev-parse", "HEAD", cwd=project)
    git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=project)
    confirm_ref(project, "origin", f"refs/heads/{branch}", data["commit"],
                f"the release commit of {version}",
                f"Read it: git ls-remote origin refs/heads/{branch}. The commit is signed and local;"
                " nothing is lost by running the command again.")
    with tempfile.TemporaryDirectory(prefix="maelys-release-cut.") as temp:
        # The body reaches gh through a file: a changelog entry holds
        # backticks, and a shell once read five of them as commands.
        body_file = pathlib.Path(temp) / "body.md"
        body_file.write_text(f"## {version} — {date}\n\n{body}\n\n---\n\nOpened by `{PROGRAM} cut`."
                             f" The tag is signed on the merge commit of this pull request, once its checks are"
                             f" green, by `{PROGRAM} cut {project.name} {version} --tag --apply`.\n", encoding="utf-8")
        created = forge().open_pull_request(project, repository, base, branch, f"{product} {version}", body_file)
    if created.returncode != 0:
        raise Failure("PROCESS_FAILED", f"gh pr create failed: {created.stderr.strip()}",
                      f"The signed commit is pushed on {branch}: open the pull request by hand, then"
                      f" '{PROGRAM} cut {project} {version} --tag'.")
    viewed = forge().view_pull_request(project, repository, branch)
    pull = json.loads(viewed.stdout) if viewed.returncode == 0 else {"url": created.stdout.strip()}
    data["pullRequest"] = {"number": pull.get("number", 0), "url": pull.get("url", ""),
                           "state": pull.get("state", "OPEN")}
    # Read once the pull request exists, so that a refusal leaves a state
    # this command resumes from: the commit is replaced on its branch and
    # the same command reads the new one. Before the wait, because the
    # checks of a commit about to be replaced are nobody's.
    if not signature_gate(project, data):
        return data, EXIT_VIOLATIONS
    print(f"cut      waiting for the checks of {data['commit'][:7]} in {repository}", file=log)
    log.flush()
    return cut_report(data, await_checks(repository, data["commit"], timeout, poll, log))


def cut_tag(invocation: Invocation, decl: Declarations, data: dict, log, timeout: int, poll: int, signers: pathlib.Path) -> tuple[dict, int]:
    """Second stop: the tag, signed on the merge commit whose checks passed."""
    project, product, version = decl.project, decl.product, data["version"]
    tag, branch, base, repository = data["tag"], data["branch"], data["base"], data["repository"]
    apply = invocation.flag("--apply")
    pull = one_pull(project, repository, branch)
    if pull is None:
        raise Failure("NOT_FOUND", f"no pull request from {branch} in {repository}.",
                      f"Open the release first: '{PROGRAM} cut {project} {version} --apply'.")
    data["pullRequest"] = {"number": pull["number"], "url": pull["url"], "state": pull["state"]}
    if pull["state"] != "MERGED":
        raise Failure("PRECONDITION_FAILED", f"{pull['url']} is {pull['state'].lower()}, not merged.",
                      "Merge it on GitHub under this repository's own rules; cut never merges its own pull request,"
                      " which would work only where the default branch is unprotected.")
    merge = (pull.get("mergeCommit") or {}).get("oid") or ""
    if not merge:
        raise Failure("PRECONDITION_FAILED", f"{pull['url']} is merged but names no merge commit.",
                      "Read the pull request on GitHub; nothing is tagged.")
    data["commit"] = merge
    # The two commits usually hold the same tree: the pull request adds
    # VERSION and a header, and if the base has not moved the merge commit
    # carries exactly that. Said, and still waited on. The rule is "the
    # checks pass on that exact commit", and a check may legitimately differ
    # at equal trees -- the event is pull_request on one and push on the
    # other, the commit's own signature and its ancestry are read by some,
    # and the protection contexts are evaluated on the merge commit. What
    # the equality buys is a reader who knows the second wait is confirming
    # and not discovering.
    head_tree = git("rev-parse", f"{pull.get('headRefOid') or ''}^{{tree}}", cwd=project, check=False)
    merge_tree = git("rev-parse", f"{merge}^{{tree}}", cwd=project, check=False)
    data["sameTree"] = bool(head_tree) and head_tree == merge_tree
    git("fetch", "-q", "origin", base, cwd=project)
    head = git("rev-parse", "FETCH_HEAD", cwd=project)
    if run(["git", "cat-file", "-e", f"{merge}^{{commit}}"], cwd=project).returncode != 0:
        git("fetch", "-q", "origin", merge, cwd=project)
    # The tag names the commit whose checks were read, never the branch: a
    # merge landing between the two would move origin/BASE under the tag.
    if run(["git", "merge-base", "--is-ancestor", merge, head], cwd=project).returncode != 0:
        raise Failure("PRECONDITION_FAILED", f"the merge commit {merge[:7]} of {pull['url']} is not on origin/{base}.",
                      "The pull request was merged elsewhere, or the branch was moved; nothing is tagged.")
    carried = git("show", f"{merge}:VERSION", cwd=project, check=False).strip()
    if carried != version:
        raise Failure("PRECONDITION_FAILED", f"{merge[:7]} carries VERSION {carried or 'nothing'}, not {version}.",
                      "A tag names a commit that carries its version; read what was merged.")
    date, body = changelog_entry(project, version, at=merge)
    if not date:
        raise Failure("PRECONDITION_FAILED", f"{merge[:7]} has no dated CHANGELOG entry for {version}.",
                      "Read what was merged; nothing is tagged.")
    data["changelog"] = date
    gate = tag_checks(project, version, signers)
    if merge != head:
        gate.append(("note", f"origin/{base} has moved past {merge[:7]}: the tag names the commit whose checks"
                             " were read, not the head of the branch"))
    if git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", cwd=project, check=False):
        gate.append(("fail", f"{tag} already exists on origin; a published tag is never moved or recreated"))
    data["gate"] = [{"status": status, "message": message} for status, message in gate]
    if apply:
        print(f"cut      waiting for the checks of {merge[:7]} in {repository}", file=log)
        log.flush()
        runs = await_checks(repository, merge, timeout, poll, log)
    else:
        runs = check_runs(repository, merge) or []
    data["checks"] = check_summary(runs)
    failed = failed_checks(runs)
    if not runs:
        data["gate"].append({"status": "note", "message": f"no check is registered on {merge[:7]} yet"})
    refused = [message for status, message in gate if status == "fail"] \
        + [f"{run['name']}: {run.get('conclusion') or run.get('status')}" for run in failed]
    data["ready"] = bool(runs) and not refused
    if not data["ready"]:
        return data, EXIT_VIOLATIONS
    if not apply:
        data["next"] = f"{PROGRAM} cut {project} {version} --tag --apply"
        return data, EXIT_OK
    message = invocation.option("--message")
    annotation = pathlib.Path(str(message)).read_text(encoding="utf-8") if message \
        else f"{product} {version}\n\n{body}\n"
    git("tag", "-s", "-m", annotation, tag, merge, cwd=project)
    # Against the file the gate read and the release workflow will read, and
    # not whichever one this checkout's git names -- or none: a new clone
    # passed the whole gate, signed, and then could not verify its own tag,
    # because gpg.ssh.allowedSignersFile is one more setting nothing had
    # asked for. One product pointed it into a dependencies directory
    # that a clean removes.
    verified = run(["git", "-c", f"gpg.ssh.allowedSignersFile={signers}", "tag", "-v", tag], cwd=project)
    if verified.returncode != 0:
        git("tag", "-d", tag, cwd=project)
        raise Failure("PROCESS_FAILED", f"the signature of {tag} does not verify: {verified.stderr.strip()}",
                      "The signing key is the signer's own to fix before the command is run again; anyone"
                      " else leaves it. Nothing was pushed.")
    git("push", "-q", "origin", f"refs/tags/{tag}", cwd=project)
    confirm_tag(project, "origin", tag, merge, f"the tag {tag}",
                f"Read it: git ls-remote origin 'refs/tags/{tag}*'. A published tag is never moved:"
                " if it names another commit, stop and read its history before anything else.")
    data["pushed"] = True
    reference = github_api(f"repos/{repository}/git/ref/tags/{tag}") or {}
    object_sha = (reference.get("object") or {}).get("sha") or ""
    annotated = github_api(f"repos/{repository}/git/tags/{object_sha}") if object_sha else None
    data["verified"] = bool(((annotated or {}).get("verification") or {}).get("verified"))
    data["next"] = (f"{tag} is published on {merge[:7]}" if data["verified"]
                    else f"{tag} is pushed, and GitHub does not report it verified: check the signing key on GitHub")
    # Looked at once, not watched: a channel's approval appears only after
    # the release workflow finishes, which is hours on some products, and a
    # command that waited would hold a terminal for a decision it must not
    # take. What it can do is say that the decision exists and hand over the
    # line that answers it.
    # Looked at whatever the product declares. The guard used to be "[gate]
    # reviewer or a channel", and this repository declares neither -- so the
    # socle cutting its own releases never ran this line, which is how a
    # reader that could not work shipped and was tagged. A command that
    # exercises itself on every release of the socle is worth one request.
    for attempt in range(3):
        data["deployments"] = tag_deployments(repository, tag)
        if data["deployments"] or attempt == 2:
            break
        time.sleep(poll)
    return data, EXIT_OK


def handle_cut(invocation: Invocation, context: Context,
               signers: "Callable[[pathlib.Path], pathlib.Path]") -> tuple[dict, int]:
    """Open the release of a version, then sign its tag on the merged commit."""
    project, product, mechanism = project_of(invocation)
    version = str(invocation.operands[1])
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise Failure("VALIDATION_FAILED", f"VERSION is X.Y.Z: {version}.",
                      "Pass the version this release carries.")
    repository = cut_repository(project)
    decl = context.read_declarations(project, product, mechanism)
    data: dict = {"mode": "apply" if invocation.flag("--apply") else "plan",
                  "stage": "tag" if invocation.flag("--tag") else "open",
                  "product": product, "project": str(project), "repository": repository,
                  "version": version, "tag": f"v{version}", "branch": f"release/v{version}",
                  "base": default_branch(project, repository),
                  # Which socle is doing the cutting, said rather than
                  # assumed. `cut` is the one command that does NOT relocate
                  # to the socle a product pins -- it runs on an operator's
                  # machine and never in a workflow, so the pin governs what
                  # GitHub runs and nothing else. A product asked whether a
                  # fix to `cut` could only reach it through an adoption; it
                  # cannot, and now the command says so on its first line.
                  "socle": socle_data(*context.socle_identity(str(invocation.option("--socle-sha", "")),
                                                      str(invocation.option("--socle-tag", ""))), context=context),
                  "gate": [], "checks": [], "ready": False}
    log = sys.stderr if invocation.format != "text" else sys.stdout
    timeout = int(invocation.option("--timeout", 30)) * 60
    poll = int(invocation.option("--poll", 15))
    stage = cut_tag if invocation.flag("--tag") else cut_open
    # Resolved here and not before: it may fetch the pinned socle, and a
    # network failure must not stand in front of the refusals this command
    # owes first -- an origin that is not on GitHub, a dirty worktree.
    return stage(invocation, decl, data, log, timeout, poll, signers(project))


def text_cut(data: dict) -> str:
    stage = "opening" if data["stage"] == "open" else "tagging"
    lines = [f"cut      {stage} {data['product']} {data['version']} on {data['branch']}"
             f" -> {data['base']} of {data['repository']}"]
    if data.get("socle"):
        lines.append(f"{'socle':<8} {data['socle']['tag']} ({data['socle']['sha'][:7]}), this checkout:"
                     " cut is the one command that does not run as the socle a product pins")
    lines.extend(f"{LABELS.get(item['status'], item['status']):<8} {item['message']}" for item in data["gate"])
    for check in data["checks"]:
        state = check["conclusion"] or check["status"]
        lines.append(f"{('ok' if state in CHECK_PASSED else state.upper()):<8} {check['name']}")
    if "pullRequest" in data:
        lines.append(f"{'pull':<8} {data['pullRequest']['url']}")
    if data.get("regenerated"):
        lines.append(f"{'cut':<8} after-version also committed " + " ".join(data["regenerated"]))
    if data.get("commit"):
        lines.append(f"{'commit':<8} {data['commit']}")
    if data.get("sameTree"):
        lines.append(f"{'note':<8} the merge commit holds the same tree as the pull request:"
                     " these checks confirm what ran there, on the commit the tag will name")
    for deployment in data.get("deployments") or []:
        lines.append(f"{'approve':<8} {deployment['environment']} of {deployment['run']}")
        lines.append(f"{'':<8} {deployment['approve']}")
    if data.get("pushed") and "deployments" in data:
        # Said whether or not one is waiting now: the channel's deployment
        # does not exist until the release workflow has finished, so "none
        # pending" at this moment is not "nothing left to approve".
        lines.append(f"{'note':<8} a channel asks for its own approval, and it becomes pending only once"
                     " the release workflow has finished: look again then")
    lines.append(f"cut: {data.get('next') or ('ready' if data['ready'] else 'not ready')}")
    return "\n".join(lines) + "\n"
