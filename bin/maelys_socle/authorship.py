# SPDX-License-Identifier: MPL-2.0
"""The address a commit carries: an account's, or one made up on this machine.

A clone with neither `user.name` nor `user.email` commits anyway wherever
git can compose an address: the login, an at sign, and the name the machine
resolves to. It writes that into every commit, with the account's name as
the system holds it, and the first push publishes both. A clone with no
identity pushed twenty commits that way, long before any release; `cut`
reads back what GitHub says of one commit, at publication, which is correct
and too late. What is read here is read before the push, on the checkout
where the commits are made:

- the name and the address git would write here, role by role;
- the commits ahead of the branch's upstream -- the only ones this clone can
  say are not pushed -- each with the address of its author and committer;
- the annotated tags, each with the address of its tagger.

Two kinds of address are told apart, because they do not call for the same
thing. One git composed on this machine, or one with no domain at all, is
nobody's mailbox: the hook refuses to push it. One under a name private
networks use may be a machine's or a real mailbox at such a network, and a
contributor's commit may carry it: it is a note, never a refusal.

No message carries the address it is about. It holds the machine's name,
and what `check` prints ends up in logs: a message names the rule that
matched.

A line that rewrites history is printed only where everything it touches
was read, and is this machine's own: see `rewrite`. Everywhere else the
message says what was found and why no line is given.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import sys
from typing import Callable

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from . import host
from .constants import PROGRAM
from .texts import LABELS

# Names no public suffix ends with: what a home router, a zero-configuration
# network, a hypervisor or a company's private zone gives a machine. The last
# is what git itself writes when it finds no domain at all. The names RFC 2606
# reserves for documentation and tests are left out on purpose: nothing
# resolves them, a fixture that commits under one says so in every commit,
# and none of them is a machine's.
LOCAL_SUFFIXES = (".home", ".lan", ".local", ".localdomain", ".localhost", ".internal", ".intranet", ".corp",
                  ".home.arpa", ".fritz.box", ".mshome.net", ".(none)")

# A runner says that it is one, and with these words: a variable set to
# `false` or `0` on an author's machine is not a runner. Nothing in the
# checkout tells either: a job started by a push checks its branch out by
# name, has no identity and has nothing to push, exactly like a clone made a
# minute ago.
RUNNER_VARIABLES = ("CI", "GITHUB_ACTIONS")
RUNNER_VALUES = ("true", "1")

SEPARATOR = "\x1f"
COMMIT_FORMAT = "--format=" + "%x1f".join(("%m", "%h", "%p", "%an", "%ae", "%cn", "%ce"))
TAG_FORMAT = "--format=" + "%1f".join(("%(objecttype)", "%(refname)", "%(taggeremail)"))
IDENTITY_KEYS = r"^(user|author|committer)\.(name|email|useconfigonly)$"

# What git would write with nothing configured, asked of git itself: the
# kernel's name for the machine is not the one git resolves, and only git
# knows which it composes. Asked outside any repository, with the three
# levels of configuration and every variable that names an identity taken
# away by env(1), so that the question is in the command and the host
# records it like any other. `git var` writes nothing.
COMPOSED = ["env", "-u", "EMAIL", "-u", "GIT_AUTHOR_NAME", "-u", "GIT_AUTHOR_EMAIL", "-u", "GIT_COMMITTER_NAME",
            "-u", "GIT_COMMITTER_EMAIL", "-u", "GIT_DIR", "-u", "GIT_WORK_TREE", "-u", "GIT_CONFIG_COUNT",
            "-u", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null",
            "GIT_CONFIG_NOSYSTEM=1", "git", "-C", "/", "var", "GIT_COMMITTER_IDENT"]

SET_ADDRESS = ("'git config --global user.email ADDRESS' -- on GitHub, the account's noreply address"
               " (ID+LOGIN@users.noreply.github.com) names no machine")
SET_NAME = "'git config --global user.name NAME'"

AMEND = "--exec 'git commit --amend --no-edit --reset-author'"

HOOK_NAME = "pre-push"
HOOK_TEXT = f"""#!/bin/sh
# Written by '{PROGRAM} hook DIR --apply'. Optional, and not a managed file:
# delete it to remove it, 'git push --no-verify' to pass it once.
#
# Refuses a push that sends a commit or a tag carrying an address git made up
# on this machine, and says nothing of a push that sends none. It fails open:
# where {PROGRAM} is not installed it does nothing, and where the command
# does not complete it says so in one line and lets the push through. Only
# its verdict refuses.
command -v {PROGRAM} >/dev/null 2>&1 || exit 0
said=$({PROGRAM} outgoing . "--pre-push=$1" --format text --non-interactive 2>&1)
status=$?
case "$status" in
    0) [ -z "$said" ] || printf '%s\\n' "$said" >&2 ;;
    2) printf '%s\\n' "$said" >&2
       exit 1 ;;
    *) printf '%s\\n' "pre-push: {PROGRAM} did not read this push (exit $status), which goes through unread: $(printf '%s\\n' "$said" | head -n 1)" >&2 ;;
esac
exit 0
"""


def on_a_runner() -> bool:
    """Whether this process runs on a CI runner, which authors and pushes nothing."""
    return any(os.environ.get(name, "").strip().lower() in RUNNER_VALUES for name in RUNNER_VARIABLES)


def once(read: Callable[[], object]) -> Callable[[], object]:
    """READ, asked at most once: what git would compose is one host call, or none."""
    known: list = []

    def answer():
        if not known:
            known.append(read())
        return known[0]
    return answer


def composed_identity() -> tuple[str, str]:
    """The name and the address git would write here with nothing configured.

    ('', '') where nothing can say. Where git refuses to compose -- a machine
    whose name resolves to nothing with a dot -- it still says what it tried,
    and that is kept: a commit made on another network carries the same
    login and the same machine under another suffix.
    """
    if not host.HOST.which("env"):
        return "", ""
    completed = host.HOST.run(COMPOSED)
    written = re.match(r"(.*) <([^>]*)> ", completed.stdout) if completed.returncode == 0 else None
    if written:
        return written.group(1).strip(), written.group(2).strip()
    tried = re.search(r"got '([^']*@[^']*)'|for <([^>]*@[^>]*)>", completed.stderr)
    return "", ((tried.group(1) or tried.group(2)).strip() if tried else "")


def parts(address: str) -> tuple[str, str]:
    """The login and the domain of ADDRESS, lowercase; an address with no at sign has neither."""
    local, at, domain = address.strip().lower().rpartition("@")
    return (local, domain.rstrip(".")) if at else ("", "")


def local_rule(domain: str) -> str:
    """The rule that says DOMAIN is a name of a private network or of a machine, or ''."""
    if "." not in domain:
        return "its domain has no dot"
    for suffix in LOCAL_SUFFIXES:
        if domain.endswith(suffix) or domain == suffix[1:]:
            return f"its domain ends in {suffix}"
    return ""


def kind_of(address: str, composed: Callable[[], tuple[str, str]]) -> tuple[str, str]:
    """What ADDRESS is, and the rule that says so: ('', '') for an address nothing names.

    `bare`, an address with no domain, and `composed`, the one git composes
    on this machine, are nobody's mailbox. `composed` is the same login at
    the same domain, or at the same machine under another private suffix: a
    laptop is not on one network. Anything else under a private name is
    `local`, which may be a mailbox and may be somebody else's.
    """
    login, domain = parts(address)
    if not domain:
        return "bare", "it has no domain"
    rule = local_rule(domain)
    own_login, own_domain = parts(composed()[1])
    if own_domain and login == own_login and (
            domain == own_domain or (rule and domain.split(".")[0] == own_domain.split(".")[0])):
        return "composed", "it is the address git composes on this machine"
    return ("local", rule) if rule else ("", "")


def configuration(directory: pathlib.Path) -> dict:
    """The identity keys of git's configuration as git reads them here, the last value of each."""
    completed = host.HOST.run(["git", "config", "-z", "--get-regexp", IDENTITY_KEYS], cwd=directory)
    found: dict = {}
    for entry in completed.stdout.split("\0") if completed.returncode == 0 else []:
        key, given, value = entry.partition("\n")
        if key:
            # A key with no value at all is git's way of writing a true.
            found[key.lower()] = value if given else "true"
    return found


def identity(directory: pathlib.Path) -> dict:
    """What names the author and the committer here, role by role: git's own order.

    The environment first, one variable for each role; then the role's own
    key, `author.email` or `committer.email`; then `user.email`; then, for
    the address only, `EMAIL`. '' is a role nothing names: git composes it,
    or refuses to commit where `user.useConfigOnly` is set.
    """
    config = configuration(directory)
    roles = {}
    for role in ("author", "committer"):
        variable = "GIT_" + role.upper()
        roles[role] = {
            "name": os.environ.get(variable + "_NAME") or config.get(role + ".name") or config.get("user.name") or "",
            "address": (os.environ.get(variable + "_EMAIL") or config.get(role + ".email")
                        or config.get("user.email") or os.environ.get("EMAIL") or "")}
    return {"roles": roles,
            "configOnly": config.get("user.useconfigonly", "").strip().lower() in ("true", "yes", "on", "1")}


def identity_messages(read: dict, composed: Callable[[], tuple[str, str]]) -> list[str]:
    """What to say of the identity READ, before a commit is made with it."""
    found = []
    roles = read["roles"].values()
    # Where git is told to guess nothing, an unset key is a refusal to
    # commit, which git says itself: nothing leaves.
    unnamed = not read["configOnly"] and any(not role["name"] for role in roles)
    unaddressed = not read["configOnly"] and any(not role["address"] for role in roles)
    if unnamed and unaddressed:
        found.append("neither a name nor an address is set here: git writes into each commit this account's name"
                     " as the system holds it, and an address it composes from the login and this machine's"
                     " name where it can, and a push publishes both. Set them before the next commit: "
                     + SET_NAME + " and " + SET_ADDRESS)
    elif unaddressed:
        found.append("no address is set here: git composes one from the login and this machine's name where it"
                     " can, writes it into each commit, and a push publishes it. Set one before the next"
                     " commit: " + SET_ADDRESS)
    elif unnamed:
        found.append("no name is set here: git writes into each commit this account's name as the system holds"
                     " it, and a push publishes it. Set one before the next commit: " + SET_NAME)
    kinds: dict = {}
    for role in roles:
        if role["address"]:
            kind, rule = kind_of(role["address"], composed)
            if kind:
                kinds.setdefault("local" if kind == "local" else "made-up", []).append(rule)
    if "made-up" in kinds:
        found.append("the address set here is one made up on this machine, not an account's ("
                     + "; ".join(dict.fromkeys(kinds["made-up"])) + "): each commit carries it, and a push"
                     " publishes it. Set one before the next commit: " + SET_ADDRESS)
    elif "local" in kinds:
        found.append("the address set here is under a name private networks and machines use ("
                     + "; ".join(dict.fromkeys(kinds["local"])) + "): a mailbox there is nothing to change; a"
                     " machine's name is published by each push, and is replaced with " + SET_ADDRESS)
    return found


def existing(directory: pathlib.Path) -> pathlib.Path:
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    return directory


def identity_notes(directory: pathlib.Path) -> list[str]:
    """What `new` says before a repository's first commit, in a directory that may not exist yet."""
    if on_a_runner():
        return []
    return identity_messages(identity(existing(directory)), once(composed_identity))


def current_branch(project: pathlib.Path) -> tuple[str, bool]:
    """The branch HEAD is on, and whether PROJECT is a repository at all."""
    completed = host.HOST.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=project)
    # 1 is a detached HEAD, which is a repository; anything else is not one.
    return (completed.stdout.strip() if completed.returncode == 0 else ""), completed.returncode in (0, 1)


def upstream_of(project: pathlib.Path, branch: str) -> str:
    """The commit the upstream of BRANCH is at, as this clone last saw it; '' with none.

    A configured upstream whose remote-tracking ref is gone, or was never
    stored -- a shallow, single-branch clone -- is no upstream: nothing here
    says what the other side holds.
    """
    completed = host.HOST.run(["git", "rev-parse", "-q", "--verify", branch + "@{upstream}"], cwd=project)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def commits(project: pathlib.Path, tip: str, excluded: list[str]) -> dict | None:
    """The commits TIP reaches and EXCLUDED does not, with what they start from; None when git cannot say.

    TIP must resolve: a HEAD that names nothing is a reading that did not
    happen, not a range of zero commits. What is excluded may be missing --
    an object the other side holds and this clone never fetched -- and is
    then ignored, which only makes the range larger.
    """
    completed = host.HOST.run(["git", "log", "--boundary", COMMIT_FORMAT, tip, "--not", "--ignore-missing",
                               *excluded], cwd=project)
    if completed.returncode != 0:
        return None
    found, base = [], []
    for line in completed.stdout.splitlines():
        fields = line.split(SEPARATOR)
        if len(fields) != 7:
            continue
        if fields[0] == "-":
            base.append(fields[1])
            continue
        found.append({"commit": fields[1], "merge": len(fields[2].split()) > 1, "authorName": fields[3],
                      "author": fields[4], "committer": fields[6]})
    return {"commits": found, "base": base}


def tags(project: pathlib.Path, names: list[str]) -> list[dict]:
    """The annotated tags among NAMES -- every tag when it is refs/tags -- with their tagger's address."""
    completed = host.HOST.run(["git", "for-each-ref", TAG_FORMAT, *names], cwd=project)
    found = []
    for line in completed.stdout.splitlines() if completed.returncode == 0 else []:
        fields = line.split(SEPARATOR)
        if len(fields) == 3 and fields[0] == "tag":
            found.append({"tag": fields[1].removeprefix("refs/tags/"), "tagger": fields[2].strip().strip("<>")})
    return found


def judge(read: dict, composed: Callable[[], tuple[str, str]], author: dict) -> dict:
    """What the commits of READ carry that they should not.

    `fabricated` is nobody's mailbox, as author or committer. `local` is an
    address under a private name and nothing more. `own` counts the commits
    this machine's own address wrote as author *and* committer: only a range
    made of those can be given back to its author without taking anything
    from anyone. `renamed` is narrower than "another name": the author's
    address is this checkout's own and the name is not, so another person's
    commit and GitHub's merge committer are never in it.
    """
    fabricated, local, renamed, own = [], [], [], 0
    for commit in read["commits"]:
        kinds = [kind_of(commit[role], composed) for role in ("author", "committer")]
        made_up = list(dict.fromkeys(rule for kind, rule in kinds if kind in ("bare", "composed")))
        private = list(dict.fromkeys(rule for kind, rule in kinds if kind == "local"))
        if made_up:
            fabricated.append({"commit": commit["commit"], "rules": made_up})
        elif private:
            local.append({"commit": commit["commit"], "rules": private})
        own += all(kind == "composed" for kind, _ in kinds)
        if author["name"] and author["address"] and commit["author"].lower() == author["address"].lower() \
                and commit["authorName"] != author["name"]:
            renamed.append(commit["commit"])
    return {"commits": len(read["commits"]), "fabricated": fabricated, "local": local, "renamed": renamed,
            "own": own, "merges": len([commit for commit in read["commits"] if commit["merge"]]),
            "base": read["base"]}


def rewrite(judged: dict, branch: str, upstream: str, settled: bool) -> tuple[str, str]:
    """The line that writes a range again, or why none is given: exactly one of the two.

    A line is given only where everything it would touch was read, never
    assumed: HEAD is on a branch, the branch has an upstream this clone
    holds, the range is what the branch has ahead of it, no commit of it is
    a merge, every commit of it was written by this machine's own address as
    author and as committer, it starts from one commit, and the identity
    here is set -- the line would otherwise write the same address again.
    It names the branch and that commit, never HEAD and never a word to
    replace. A line that re-attributes one commit of somebody else, or
    replays a merge, is not a fix: it is the incident this note exists for.
    """
    if not branch:
        return "", "HEAD is detached, and a line here could only name HEAD"
    if not upstream:
        return "", (f"{branch} has no upstream this clone holds, so nothing here says where its own commits"
                    " start nor whether they were pushed")
    if judged["merges"]:
        return "", "the range holds a merge, which a line would replay with whatever it brought in"
    others = judged["commits"] - judged["own"]
    if others:
        return "", (f"{others} of them {'is' if others == 1 else 'are'} not this machine's own as author and"
                    " committer, and a line would give them to the identity set here")
    if len(judged["base"]) != 1:
        return "", "the range does not start from one commit of the upstream"
    if not settled:
        return "", "the identity here is not set yet, and a line would write the same address again"
    return f"git rebase {judged['base'][0]} {shlex.quote(branch)} {AMEND}", ""


def plural(count: int, one: str, several: str) -> str:
    return one if count == 1 else several


def listed(names: list[str]) -> str:
    return ", ".join(names[:5]) + (", …" if len(names) > 5 else "")


def rules_of(entries: list[dict]) -> str:
    return "; ".join(dict.fromkeys(rule for entry in entries for rule in entry["rules"]))


def fabricated_message(judged: dict, where: str, line: str, reason: str, branch: str = "") -> str:
    count, total = len(judged["fabricated"]), judged["commits"]
    said = (f"{count} of the {total} commit{plural(total, '', 's')} {where} carr{plural(count, 'ies', 'y')} an"
            f" address made up on this machine, as author or committer ({rules_of(judged['fabricated'])}): a"
            " push publishes it for good.")
    if not line:
        return said + f" No line is given to write them again: {reason}."
    return (said + f" This line checks out {branch} and commits the {total} again, all of them this machine's"
            " own, on the commit they start from: author and committer become the identity set here, author"
            " dates become now, signatures are dropped unless commit signing is configured, and it stops on"
            f" an empty commit or a failing pre-commit hook, where 'git rebase --abort' undoes it: {line}")


def local_message(judged: dict, where: str) -> str:
    count, total = len(judged["local"]), judged["commits"]
    return (f"{count} of the {total} commit{plural(total, '', 's')} {where} carr{plural(count, 'ies', 'y')} an"
            f" address under a name private networks and machines use ({rules_of(judged['local'])}): a mailbox"
            " there is nothing to change, and a machine's name is published by a push. Nothing here rewrites"
            " them: they may be somebody else's.")


def renamed_message(judged: dict, where: str) -> str:
    count = len(judged["renamed"])
    return (f"{count} commit{plural(count, '', 's')} {where} {plural(count, 'is', 'are')} authored under this"
            f" checkout's address and another name than its own ({listed(judged['renamed'])}): one address"
            " under two names, which is what a history rewrite leaves when it reuses the identity of another"
            " commit. Nothing here rewrites them: the socle cannot tell which name is the author's.")


def tags_message(names: list[str]) -> str:
    count = len(names)
    return (f"{count} annotated tag{plural(count, '', 's')} carr{plural(count, 'ies', 'y')} an address made up on"
            f" this machine as tagger ({listed(names)}): a push publishes it, and a published tag is never"
            " recreated. Nothing here says whether a tag was pushed.")


def made_up_tags(found: list[dict], composed: Callable[[], tuple[str, str]]) -> list[str]:
    return [tag["tag"] for tag in found if kind_of(tag["tagger"], composed)[0] in ("bare", "composed")]


def read_head(project: pathlib.Path) -> dict | None:
    """What HEAD has that the other side is not known to hold; None outside a repository.

    `upstream` is the one range this clone can call not pushed: what the
    branch has ahead of its upstream. Without one, what is read is what no
    remote-tracking ref of this clone reaches, and whether that was pushed
    -- by URL, to a branch since deleted, from a shallow clone, by whoever
    opened the pull request it was fetched from -- is not known here.
    """
    branch, repository = current_branch(project)
    if not repository:
        return None
    upstream = upstream_of(project, branch) if branch else ""
    read = commits(project, "HEAD", [upstream] if upstream else ["--remotes"])
    if read is None:
        return None
    where = (f"{branch} has not pushed to its upstream" if upstream
             else "that no remote-tracking ref of this clone reaches -- pushed or not, nothing here says --")
    return {**read, "branch": branch, "upstream": upstream, "where": where}


def notes(project: pathlib.Path) -> list[str]:
    """Everything `check` says of the identity here and of what already carries a made-up one."""
    if on_a_runner():
        return []
    composed = once(composed_identity)
    here = identity(project)
    found = identity_messages(here, composed)
    read = read_head(project)
    if read is None:
        return found
    if read["commits"]:
        judged = judge(read, composed, here["roles"]["author"])
        if judged["fabricated"]:
            line, reason = rewrite(judged, read["branch"], read["upstream"], settled=not found)
            found.append(fabricated_message(judged, read["where"], line, reason, read["branch"]))
        if judged["local"]:
            found.append(local_message(judged, read["where"]))
        if judged["renamed"]:
            found.append(renamed_message(judged, read["where"]))
    tagged = made_up_tags(tags(project, ["refs/tags"]), composed)
    if tagged:
        found.append(tags_message(tagged))
    return found


# ---- the hook ------------------------------------------------------------------

def directory_of(invocation: Invocation) -> pathlib.Path:
    """The directory a command reads. Neither `outgoing` nor `hook` needs a product, nor its name."""
    directory = pathlib.Path(invocation.operands[0])
    if not directory.is_dir():
        raise Failure("NOT_FOUND", f"DIR is not a directory: {directory}.", "Pass the repository.")
    return directory.resolve()


def hooks_elsewhere(project: pathlib.Path) -> str:
    completed = host.HOST.run(["git", "config", "--get", "core.hooksPath"], cwd=project)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def hook_path(project: pathlib.Path) -> pathlib.Path | None:
    """Where git reads this repository's pre-push hook, or None outside a repository."""
    completed = host.HOST.run(["git", "rev-parse", "--git-path", f"hooks/{HOOK_NAME}"], cwd=project)
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return project / completed.stdout.strip()


def hook_proposal(project: pathlib.Path) -> str:
    """The one line that enables the hook, only where running it would write the hook."""
    if on_a_runner() or hooks_elsewhere(project):
        return ""
    path = hook_path(project)
    if path is None or path.exists():
        return ""
    return f"{PROGRAM} hook {shlex.quote(str(project))} --apply"


def pushed_refs(stream: str) -> list[dict]:
    """What a push sends, from a pre-push hook's input: one line a ref.

    Local ref, local object, remote ref, remote object. An object of zeros
    is a ref that does not exist on that side: a deletion sends nothing, a
    new branch has nothing on the other side to start from.
    """
    found = []
    for line in stream.splitlines():
        fields = line.split()
        if len(fields) != 4 or not fields[1].strip("0"):
            continue
        found.append({"ref": fields[0], "object": fields[1], "remoteRef": fields[2],
                      "held": fields[3] if fields[3].strip("0") else ""})
    return found


def read_push(project: pathlib.Path, remote: str, stream: str) -> list[dict]:
    """Each ref a push sends to REMOTE, with the commits the other side is not known to hold.

    Exactly that remote: its own remote-tracking refs and the object git
    says it holds for the ref, never another remote's -- a commit already
    on a backup is still published by a push to the origin. A push by URL
    names no remote of this clone, and only the object git gives is taken
    off.
    """
    known = [f"--remotes={remote}"] if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) else []
    found = []
    for pushed in pushed_refs(stream):
        read = commits(project, pushed["object"], [*known, *([pushed["held"]] if pushed["held"] else [])])
        if read is None:
            raise Failure("PROCESS_FAILED", f"nothing was read: git could not list what {pushed['ref']} sends"
                          f" to {remote or 'the remote'} from {project}.",
                          "The hook lets such a push through unread; 'git log' on that ref says why.")
        found.append({**pushed, **read})
    return found


def handle_outgoing(invocation: Invocation) -> tuple[dict, int]:
    """Judge what is about to leave; exit 2 when it carries an address made up on this machine.

    The runner's silence is `check`'s, not this command's: whoever runs it,
    or pushes through the hook, is about to publish what it reads. A reading
    that did not happen is a failure, never a verdict.
    """
    project = directory_of(invocation)
    remote = invocation.option("--pre-push")
    composed = once(composed_identity)
    here = identity(project)
    author = here["roles"]["author"]
    data: dict = {"valid": True, "project": str(project), "source": "head" if remote is None else "pre-push",
                  "commits": 0, "fabricated": [], "local": [], "renamed": [], "tags": [], "violations": [],
                  "notes": [], "fix": ""}

    def say(judged: dict, where: str, refuses: bool, line: str = "", reason: str = "", branch: str = "") -> None:
        data["commits"] += judged["commits"]
        for member in ("fabricated", "local", "renamed"):
            data[member].extend(judged[member])
        if judged["fabricated"]:
            (data["violations"] if refuses else data["notes"]).append(
                fabricated_message(judged, where, line, reason, branch))
        if judged["local"]:
            data["notes"].append(local_message(judged, where))
        if judged["renamed"]:
            data["notes"].append(renamed_message(judged, where))

    if remote is None:
        read = read_head(project)
        if read is None:
            raise Failure("PRECONDITION_FAILED", f"nothing was read: {project} is not a git repository, or has"
                          " no commit yet.", "Pass a repository with at least one commit.")
        data.update(scope="upstream" if read["upstream"] else "untracked")
        judged = judge(read, composed, author)
        line, reason = rewrite(judged, read["branch"], read["upstream"],
                               settled=not identity_messages(here, composed)) if judged["fabricated"] else ("", "")
        # Only what the branch has ahead of its upstream is known not to be
        # pushed, so only that is refused; the rest is said, as what it is.
        say(judged, read["where"], bool(read["upstream"]), line, reason, read["branch"])
        data["fix"] = line
    else:
        remote = str(remote)
        data.update(scope="push", remote=remote)
        sent = read_push(project, remote, sys.stdin.read())
        for pushed in sent:
            say(judge(pushed, composed, author), f"{pushed['ref']} sends to {pushed['remoteRef']} of {remote}", True,
                reason=f"'{PROGRAM} check' on the branch gives one where it can be given safely")
        names = [pushed["ref"] for pushed in sent if pushed["ref"].startswith("refs/tags/")]
        data["tags"] = made_up_tags(tags(project, names), composed) if names else []
        if data["tags"]:
            data["violations"].append(tags_message(data["tags"]))
    data["valid"] = not data["violations"]
    return data, EXIT_OK if data["valid"] else EXIT_VIOLATIONS


def text_outgoing(data: dict) -> str:
    lines = [f"{LABELS['refused']:<8} {message}" for message in data["violations"]]
    lines.extend(f"{LABELS['note']:<8} {message}" for message in data["notes"])
    if not lines and data["source"] == "pre-push":
        # A hook that speaks at every push is one nobody reads the day it refuses.
        return ""
    count, total = len(data["fabricated"]) + len(data["tags"]), data["commits"]
    read = {"upstream": "ahead of the upstream", "untracked": "that no remote-tracking ref reaches",
            "push": f"sent to {data.get('remote', '')}"}[data["scope"]]
    lines.append(f"outgoing: {total} commit{plural(total, '', 's')} {read}, "
                 + (f"{count} carrying an address made up on this machine"
                    + ("" if data["violations"] else ", which nothing here says were not pushed already") if count
                    else "none carrying an address made up on this machine"))
    return "\n".join(lines) + "\n"


def handle_hook(invocation: Invocation) -> tuple[dict, int]:
    """Write the optional pre-push hook, and never over one this repository wrote."""
    project = directory_of(invocation)
    apply = invocation.flag("--apply")
    path = hook_path(project)
    if path is None:
        raise Failure("PRECONDITION_FAILED", f"{project} is not a git repository: it has no hook to write.",
                      "Run 'git init' first, or pass the repository.")
    data = {"mode": "apply" if apply else "plan", "project": str(project), "path": str(path), "wrote": False,
            "hook": HOOK_TEXT}
    shared = hooks_elsewhere(project)
    if shared:
        # A directory several repositories read, or one this repository
        # tracks: neither is the socle's to write into.
        data.update(action="kept", reason=f"core.hooksPath is set, so the hooks of this repository live in {shared},"
                                          " which the socle does not write: add the lines below there by hand")
    elif path.exists():
        same = path.is_file() and path.read_text(encoding="utf-8", errors="replace") == HOOK_TEXT
        data.update(action="same" if same else "kept")
        if not same:
            data["reason"] = (f"{path} exists and is this repository's own, which the socle does not overwrite:"
                              " add the lines below to it by hand")
    else:
        data["action"] = "create"
        if apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(HOOK_TEXT, encoding="utf-8")
            path.chmod(0o755)
            data["wrote"] = True
    return data, EXIT_OK


def text_hook(data: dict) -> str:
    lines = [f"{data['action']:<8} {data['path']}" + (f" ({data['reason']})" if data.get("reason") else "")]
    if data["action"] == "kept":
        lines.extend("         " + line for line in data["hook"].rstrip("\n").split("\n"))
        lines.append("hook: nothing written, and nothing refuses a push here until the lines above are added")
    elif data["action"] == "same":
        lines.append("hook: nothing written: it is already there")
    elif not data["wrote"]:
        lines.append("hook: nothing written, plan only; add --apply to write. Optional, and not a managed file: it"
                     " refuses a push that sends an address made up on this machine, and 'git push --no-verify'"
                     " passes it once")
    else:
        lines.append("hook: written. A push that sends an address made up on this machine is refused;"
                     " 'git push --no-verify' passes it once, and deleting the file removes it")
    return "\n".join(lines) + "\n"
