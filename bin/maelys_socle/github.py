# SPDX-License-Identifier: MPL-2.0
"""GitHub readings and protection shapes through the single current host."""
from __future__ import annotations

from dataclasses import dataclass

import base64
import json
import pathlib
import re

from maelys_cli import Failure
from . import host
from .context import Context
from .host import git
from .constants import DECLARATION_FILE, DEFAULT_TAP, SOCLE_REPOSITORY


# A formula names its repository in its url, and the socle renders two
# shapes of it: the source archive of a tag, and the bottle root. Reading
# only the second missed every formula without bottles, which is most of
# them.
FORMULA_REPOSITORY = re.compile(r"github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?:archive|releases)")


FORMULA_VERSIONS = (re.compile(r"/archive/refs/tags/(v?[0-9][A-Za-z0-9.+-]*?)\.tar\.gz"),
                    re.compile(r"/releases/download/(v?[0-9][A-Za-z0-9.+-]*)/"))


TAP_SECRETS = ("HOMEBREW_TAP_TOKEN", "HOMEBREW_TAP_SIGNING_KEY")


TAG_WORKFLOWS = ("release", "tap", "channel")


WITHDRAWN_FILE = "WITHDRAWN"


def github_read(path: str) -> tuple[str, object]:
    """(state, body) of one GET through gh, the state saying why there is no body.

    `absent` is an answer: a branch that is not protected and an environment
    that does not exist both say something. `unreadable` is not: GitHub
    refused to tell, which a 403 does on a private repository of a free plan
    or with a token that lacks the scope.

    Collapsing the two is how the socle told seventeen private repositories
    that their default branch was unprotected, at the very moment GitHub was
    refusing to say — measured while the organization was locked for an
    Actions billing overage, which rebates it to the free plan's abilities.
    """
    return host.HOST.read(path)


def github_api(path: str) -> dict | None:
    """The body of a GET, or None whatever kept it from arriving.

    For a reader that acts the same way on every absence. Anything that
    reports on what it could not read uses github_read instead.
    """
    body = github_read(path)[1]
    return body if isinstance(body, dict) else None


def github_list(path: str) -> list:
    """The body of a GET that answers a list, or an empty one.

    github_api keeps its shape -- a mapping or nothing -- because everything
    that reports on an absence relies on it. A listing endpoint answers an
    array, and widening that reader to carry both would have made every
    caller ask which it got.
    """
    body = github_read(path)[1]
    return body if isinstance(body, list) else []


def github_repository(project: pathlib.Path) -> str:
    """owner/name of the origin remote when it is on GitHub, else ''."""
    remote = git("remote", "get-url", "origin", cwd=project, check=False)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    return match.group(1) if match else ""


def cut_repository(project: pathlib.Path) -> str:
    repository = github_repository(project)
    if not repository:
        raise Failure("PRECONDITION_FAILED", f"origin of {project} is not on GitHub.",
                      "cut opens a pull request and reads its checks through gh; publish by hand elsewhere.")
    if not host.HOST.which("gh"):
        raise Failure("NOT_FOUND", "gh is required to open the pull request and to read its checks.",
                      "Install the GitHub CLI, or run the ceremony by hand.")
    return repository



def repository_visibility(project: pathlib.Path, visibility: str) -> bool | None:
    """Whether origin's repository has this visibility; None when GitHub cannot say."""
    repository = github_repository(project)
    if not repository or not host.HOST.which("gh"):
        return None
    state, body = github_read(f"repos/{repository}")
    if state != "ok" or not isinstance(body, dict) or not body.get("visibility"):
        return None
    return body["visibility"] == visibility


@dataclass(frozen=True)
class Protection:
    """What protects a branch, read once, both mechanisms side by side.

    A branch is protected by the classic protection, by a ruleset, or by
    both, and GitHub applies them in union; the two answer on different
    endpoints, and each has its own way of not answering. This socle learned
    that three times over -- preflight in 0.46.x, the adoption guard in
    0.51.1, protect in 0.51.2 -- each time in a reader of its own, and the
    fourth reader was what a fix could miss. One reader now, and one parser
    of the contexts a ruleset requires; each command still combines the two
    lists the way it always did.
    """

    classic_state: str
    classic_body: object
    ruled_state: str
    rules: object

    @property
    def classic(self) -> dict:
        return self.classic_body if isinstance(self.classic_body, dict) else {}

    @property
    def classic_contexts(self) -> list[str]:
        return list(((self.classic.get("required_status_checks") or {}).get("contexts")) or [])

    @property
    def rule_contexts(self) -> list[str]:
        return rule_contexts(self.rules) if self.ruled_state == "ok" else []

    @property
    def unread(self) -> bool:
        """Neither endpoint answered: not the same fact as a branch that requires nothing."""
        return self.classic_state not in ("ok", "absent") and self.ruled_state not in ("ok", "absent")


def rule_contexts(rules: object) -> list[str]:
    """The contexts every required_status_checks rule of a branch names."""
    found: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                found.append(str(check["context"]))
    return found


def read_protection(repository: str, branch: str, classic: bool = True, rulesets: bool = True) -> Protection:
    """Read what protects BRANCH, the classic endpoint first, then the rulesets.

    The two flags exist for the readers that need one side only -- the
    selector that asks whether a classic protection exists, the re-reads
    after a write -- so that no site reads more than it did.
    """
    state, body = github_read(f"repos/{repository}/branches/{branch}/protection") if classic else ("skipped", None)
    ruled, rules = github_read(f"repos/{repository}/rules/branches/{branch}") if rulesets else ("skipped", None)
    return Protection(state, body, ruled, rules)


def classic_protection(project: pathlib.Path) -> bool | None:
    """Whether the default branch carries a classic protection; None when GitHub cannot say."""
    repository = github_repository(project)
    if not repository or not host.HOST.which("gh"):
        return None
    branch = (github_api(f"repos/{repository}") or {}).get("default_branch") or "main"
    state = read_protection(repository, branch, rulesets=False).classic_state
    return True if state == "ok" else False if state == "absent" else None


def destination_is_public(repository: str) -> bool | None:
    """Whether the prose's destination can be read by a stranger; None if unknown."""
    data = github_api(f"repos/{repository}")
    return None if data is None else data.get("visibility") == "public"


def environment_gate(repository: str, environment: dict | None, declared: str = "") -> list[tuple[str, str]]:
    """Whether the release environment holds the gate this repository asked for.

    A deployment policy says *what* may publish and says nothing about *who*
    approves. The conventions called this environment the human gate, and the
    socle answered ok on an environment that required nobody: it described a
    gate it had never seen closed.

    What it does not do is choose for a repository. `[gate]` of
    The declaration file says `reviewer` or `none`; the socle compares that
    answer with the environment and fails only on a promise unkept. A
    repository that has not answered gets a note, never a failure: the socle
    reports the gap and leaves the decision where it belongs.
    """
    if environment is None:
        return []
    rules = {rule.get("type"): rule for rule in environment.get("protection_rules") or []}
    reviewers = rules.get("required_reviewers")
    if not reviewers:
        if declared == "none":
            return [("ok", f"environment release of {repository} requires no reviewer, as [gate] none declares")]
        unattended = (f"environment release of {repository} requires no reviewer: the publication runs"
                      " unattended. Arm it with 'gh api -X PUT repos/" + repository + "/environments/release"
                      " -f reviewers[][type]=User -F reviewers[][id]=ID', or declare the choice"
                      f" with [gate] none in {DECLARATION_FILE}")
        return [("fail" if declared == "reviewer" else "note", unattended)]
    names = " ".join((entry.get("reviewer") or {}).get("login") or "?"
                     for entry in reviewers.get("reviewers") or [])
    found = [("ok", f"environment release of {repository} requires a reviewer: {names}"
                    + (", as [gate] reviewer declares" if declared == "reviewer" else ""))]
    if declared == "none":
        found.append(("note", f"{repository} declares [gate] none but its environment requires {names}:"
                              " the environment is stricter than the declaration"))
    # A gate the approver can walk around is a pause, not a control, and the
    # socle says which of the two this is rather than implying the second.
    if environment.get("can_admins_bypass"):
        found.append(("note", f"an administrator of {repository} may bypass that reviewer"
                              " (can_admins_bypass): the gate is a pause, not a control"))
    if reviewers.get("prevent_self_review") is False:
        found.append(("note", f"the reviewer of {repository} may approve their own deployment"
                              " (prevent_self_review false): the gate is a pause, not a control"))
    return found


def branch_protection(repository: str, branch: str, classic: str,
                      ruled: str, rules: object, promised: str = "") -> tuple[str, str]:
    """What protects a default branch, told apart from what cannot be read.

    Two endpoints answer, and not for the same thing: the classic branch
    protection, and a ruleset. agent-cli-spec is protected by a ruleset
    alone, so reading the first alone reported it open — permanently, not
    only while the organization was locked.

    `promised` is the product's `[commit]` declaration. An unprotected
    branch is a note for a repository that promised nothing and a violation
    for one that declared `signed-on-default-branch`: that declaration asks
    the release to check the commit landed on a branch whose rules apply,
    and on an open branch there are none. The pattern is `[gate] reviewer`
    against an environment that requires nobody — a promise unkept, which
    `preflight` already fails on. A refusal to say stays a note either way:
    GitHub declining to answer is not an open branch.
    """
    protected = [name for name, state, present in (("branch protection", classic, True),
                                                   ("a ruleset", ruled, bool(rules)))
                 if state == "ok" and present]
    if protected:
        return "ok", f"the default branch {branch} of {repository} is protected by " + " and ".join(protected)
    if "unreadable" in (classic, ruled):
        return "note", (f"the socle cannot read what protects {branch} of {repository}: GitHub refused to say,"
                        " which a private repository of a free plan does. That is not the same fact as an"
                        " unprotected branch, and the socle will not report it as one")
    if promised == "signed-on-default-branch":
        return "fail", (f"{DECLARATION_FILE} declares '[commit] {promised}' and the default branch {branch}"
                        f" of {repository} is not protected: the release would check that the commit is an"
                        " ancestor of a branch anyone who can push can move, which proves nothing it does"
                        " not already prove. Protect the branch -- 'maelys-release protect . --apply'"
                        " derives the contexts -- or declare '[commit] signed'")
    return "note", (f"the default branch {branch} of {repository} is not protected: anyone who can push can"
                    " move it, and a tag proves only what the tag proves")


def tap_drift(repository: str, formulas: list[str], tap: str = "") -> list[tuple[str, str]]:
    """Formulas the tap serves for this repository that it no longer declares.

    Nothing else in the fleet can see this. The tap is one repository for
    every product, so a product only ever reads its own packaging; the socle
    is the one that pushes to the tap, and the only place where what is
    served and what is declared sit side by side.

    A formula is tied to its product by the repository its url names, never
    by its own name: the case this exists for is maelys-datalog, whose
    formula the tap still serves at v0.1.0-alpha.3 while the product stopped
    declaring a template at alpha.4 and would carry a different name today.
    Matching on the name would have missed exactly that.
    """
    tap = tap or DEFAULT_TAP
    listing = github_read(f"repos/{tap}/contents/Formula")[1]
    if not isinstance(listing, list):
        return [("note", f"the formulas of {tap} could not be read: drift against what this product"
                         " declares is not checked")]
    served: list[tuple[str, str]] = []
    for entry in listing:
        name = str(entry.get("name", ""))
        if not name.endswith(".rb"):
            continue
        body = github_read(f"repos/{tap}/contents/Formula/{name}")[1]
        text = ""
        if isinstance(body, dict) and body.get("encoding") == "base64":
            try:
                text = base64.b64decode(body.get("content", "")).decode("utf-8", "replace")
            except (ValueError, TypeError):
                continue
        named = FORMULA_REPOSITORY.search(text)
        if named and named.group(1) == repository:
            version = next((found.group(1) for found in
                            (pattern.search(text) for pattern in FORMULA_VERSIONS) if found),
                           "an unreadable version")
            served.append((name[:-3], version))
    found: list[tuple[str, str]] = []
    for name, version in sorted(served):
        if name in formulas:
            continue
        remedy = (f"declare packaging/homebrew/{name}.rb.in again"
                  if not formulas else
                  f"this product declares {' '.join(formulas)} instead")
        found.append(("note", f"{tap} serves Formula/{name}.rb at {version} for this repository and"
                              f" nothing here declares it: {remedy}, or remove the formula from the"
                              f" tap. Until then that is what 'brew install {tap.split('/')[0]}/"
                              f"{tap.split('/')[-1].replace('homebrew-', '')}/{name}' installs"))
    return found


def tap_secrets(repository: str, formulas: list[str]) -> list[tuple[str, str]]:
    """Whether the tap jobs of this repository will hold their credentials.

    The tap pushes only when tap_token reaches the job, and the socle has
    always said so -- `renders, lints and reports instead of failing`. What
    it did not say was *before the tag*. A product read two green publish
    jobs of its own release, pushed nothing, and paid a full replay on four
    macOS runners to find out: the organisation secret was limited to
    selected repositories, and the list still named the archived repository
    it had been renamed from. 0.46.0 made the job say it afterwards, on the
    release; this says it before there is a tag to say it about.

    Read from the repository's side and never the organisation's. Listing
    the repositories a secret is shared with needs an organisation
    administrator; listing the secrets *this repository can see* needs what
    preflight already has. The two answer the same question from opposite
    ends, and only one end is always available -- which is also the end the
    job will be standing at.
    """
    if not formulas:
        return []
    visible: set[str] = set()
    answered = False
    for path in ("actions/secrets", "actions/organization-secrets"):
        state, body = github_read(f"repos/{repository}/{path}")
        answered = answered or state == "ok"
        if isinstance(body, dict):
            visible.update(str(entry.get("name", "")) for entry in body.get("secrets") or [])
    if not answered:
        return [("note", f"the secrets {repository} can see could not be read: whether its tap jobs"
                         f" will hold {TAP_SECRETS[0]} is not checked")]
    found: list[tuple[str, str]] = []
    if TAP_SECRETS[0] in visible:
        found.append(("ok", f"{repository} sees {TAP_SECRETS[0]}: its tap jobs will push"))
    else:
        found.append(("fail", f"{repository} declares {' '.join(formulas)} and does not see"
                              f" {TAP_SECRETS[0]}: the tap jobs would render, lint and push nothing,"
                              " and the release would say so only afterwards. An organisation secret"
                              " limited to selected repositories has to name this one"))
    if TAP_SECRETS[1] not in visible:
        found.append(("note", f"{repository} does not see {TAP_SECRETS[1]}: the tap commit would be"
                              " pushed unsigned"))
    return found


def channel_visibility(repository: str, channels: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Whether a declared channel publishes into a package anyone can install.

    GitHub gives a package its own visibility, which starts private and does
    not follow the repository: a product whose repository went public kept
    publishing into a package nobody outside the organisation could install,
    for eleven versions, with every job green. The release says published,
    the registry holds it, and `npm install` from outside answers 404 --
    there is no red anywhere in that story.

    A note and never a violation: a private package is a legitimate choice.
    What was missing is that nobody was told which one they had.
    """
    owner = repository.split("/")[0]
    found: list[tuple[str, str]] = []
    for name, kind in channels:
        if kind != "github-packages":
            continue
        state, body = github_read(f"orgs/{owner}/packages?package_type={name}&per_page=100")
        if state != "ok" or not isinstance(body, list):
            found.append(("note", f"the {name} packages of {owner} could not be read"
                                  f" (a token without read:packages answers this too):"
                                  f" whether the {name} channel publishes publicly is not checked"))
            continue
        mine = [entry for entry in body
                if isinstance(entry, dict)
                and (entry.get("repository") or {}).get("full_name") == repository]
        if not mine:
            found.append(("note", f"{owner} holds no {name} package for {repository} yet:"
                                  " the first publication creates it, private, whatever this"
                                  " repository is"))
            continue
        for entry in mine:
            package, visibility = entry.get("name", "?"), entry.get("visibility", "?")
            if visibility == "public":
                found.append(("ok", f"the {name} channel publishes {package}, which is public"))
            else:
                found.append(("note", f"the {name} channel publishes {package}, which is {visibility}:"
                                      " the release will say published and a consumer outside"
                                      f" {owner} cannot install it. A package's visibility is its"
                                      " own and does not follow the repository's"))
    return found


def check_runs(repository: str, sha: str) -> list | None:
    """Every check run of a commit, paged; None when gh cannot answer.

    The endpoint's default page holds thirty, and one commit of
    maelys-egress carries forty-eight check runs (measured on f797cf7): an
    unpaged read would call a release green on two thirds of its checks.
    """
    runs: list = []
    page = 1
    while True:
        answer = github_api(f"repos/{repository}/commits/{sha}/check-runs?per_page=100&page={page}")
        if answer is None:
            return None
        batch = answer.get("check_runs") or []
        runs.extend(batch)
        if not batch or len(runs) >= int(answer.get("total_count") or 0):
            return runs
        page += 1


def default_branch(project: pathlib.Path, repository: str) -> str:
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=project, check=False)
    if head:
        return head.rpartition("/")[2]
    return (github_api(f"repos/{repository}") or {}).get("default_branch") or "main"


def tag_deployments(repository: str, tag: str) -> list[dict]:
    """The approvals the run of this tag is waiting on, and how to answer them.

    A release under `[gate] reviewer` asks for one approval, and a product
    with a channel asks for another -- which does not exist until the
    release workflow has finished, so an operator who approves "the"
    deployment and walks away leaves the channel waiting. Measured on one
    release: the release job ran at 10:05 and the channel job at 14:04, on
    the same tag, four hours apart. Nothing said so; it read as an approval
    GitHub had lost.
    """
    waiting = []
    # An object, not an array: this endpoint answers {total_count,
    # workflow_runs}, and github_list hands back [] for anything that is not
    # a list -- so this loop never ran once, on any repository, while the
    # changelog of 0.50.0 said the command named the approval. The test was
    # green because it stubbed the shape I had imagined; `cut` reads the
    # same endpoint correctly ten lines above, which is where I should have
    # looked.
    runs = github_api(f"repos/{repository}/actions/runs?event=push&per_page=20") or {}
    for run_data in runs.get("workflow_runs") or []:
        if not isinstance(run_data, dict) or run_data.get("head_branch") != tag:
            continue
        identifier = run_data.get("id")
        for deployment in github_list(f"repos/{repository}/actions/runs/{identifier}/pending_deployments"):
            if not isinstance(deployment, dict) or not deployment.get("current_user_can_approve"):
                continue
            environment = (deployment.get("environment") or {}).get("name", "")
            waiting.append({"run": f"https://github.com/{repository}/actions/runs/{identifier}",
                            "environment": environment,
                            "approve": f"gh api -X POST repos/{repository}/actions/runs/{identifier}"
                                       f"/pending_deployments -f state=approved -f comment=''"
                                       f" -F 'environment_ids[]={(deployment.get('environment') or {}).get('id', 0)}'"})
    return waiting


def observed_contexts(repository: str, branch: str, samples: int = 3) -> tuple[list[str], list[str], dict]:
    """Check names that every recent pull request of this repository produced.

    A branch protection can only require what a pull request produces, so
    the evidence is pull requests and never the tip of the default branch:
    that tip also carries what the tag pointing at it ran, and the push-only
    runs besides.

    And it is several pull requests, intersected, not one. A name seen on
    one head and not the next is either new or intermittent, and requiring
    either blocks a pull request at random -- maelys-cli carries a run named
    `ci` on some heads and not others. What appears on some but not all is
    reported and left out: a protection should require what reliably runs,
    and the operator can add the rest by hand once it does.
    """
    # Every read that did not answer is counted. An observation missing a
    # pull request, or one of its check runs, is an intersection of fewer
    # sets or of empty ones, and what it leaves out reads as "not produced".
    # maelys-system watched `protect` call its main unprotected and propose
    # four contexts of seventeen while GitHub was timing out.
    unanswered = 0
    listed, pulls = github_read(f"repos/{repository}/pulls?state=closed&per_page=30")
    if listed != "ok" or not isinstance(pulls, list):
        unanswered += 1
        pulls = []
    heads = [pull.get("head", {}).get("sha", "") for pull in pulls
             if isinstance(pull, dict) and pull.get("merged_at")][:samples]
    if not heads:
        read, head = github_read(f"repos/{repository}/commits/{branch}")
        unanswered += read != "ok"
        head = head if isinstance(head, dict) else {}
        heads = [head.get("sha", "")] if head.get("sha") else []
    if not heads:
        return [], [], {"unanswered": unanswered}
    per_head: list[set] = []
    from_tag: set = set()
    for sha in heads:
        read, body = github_read(f"repos/{repository}/commits/{sha}/check-runs?per_page=100")
        if read != "ok" or not isinstance(body, dict):
            unanswered += 1
            continue
        runs = body.get("check_runs", [])
        names = set()
        for run in runs:
            name = str(run.get("name", ""))
            if not name:
                continue
            if name.split(" / ")[0] in TAG_WORKFLOWS:
                from_tag.add(name)
                continue
            if run.get("conclusion") in ("skipped", "neutral", None):
                continue
            names.add(name)
        per_head.append(names)
    every = set.intersection(*per_head) if per_head else set()
    some = set().union(*per_head) - every if per_head else set()
    counted = {name: sum(1 for names in per_head if name in names) for name in sorted(some)}
    return sorted(every), sorted(from_tag), {"heads": len(per_head), "partial": counted, "unanswered": unanswered,
                                             "ever": sorted(set().union(*per_head)) if per_head else []}


def required_contexts(project: pathlib.Path) -> list[str] | None:
    """The checks the default branch requires, by the classic protection and by rulesets.

    None when GitHub cannot be asked -- no origin, no gh, or neither endpoint
    answering -- which is not the same as a branch that requires nothing.
    """
    repository = github_repository(project)
    if not repository or not host.HOST.which("gh"):
        return None
    branch = (github_api(f"repos/{repository}") or {}).get("default_branch") or "main"
    # Both endpoints, as `branch_protection` reads them: a repository of the
    # fleet is protected by a ruleset alone, and reading the classic
    # protection by itself called it open -- the very mistake this socle
    # corrected in 0.46.x and made again in the guard written to keep a rename
    # from locking a branch. agent-cli-spec requires the socle legs through a
    # ruleset and nothing else.
    protection = read_protection(repository, branch)
    if protection.unread:
        return None
    required = (protection.classic_contexts if protection.classic_state == "ok" else []) + protection.rule_contexts
    return [name for name in required if isinstance(name, str)]


def protection_settings(body: object) -> dict:
    """Every setting of a classic protection except its required contexts, normalised.

    Read from what GitHub answers (settings as `{"enabled": bool}`) and from
    what this command sends (plain booleans) alike, so that the plan, the
    write and the re-read compare as one shape.
    """
    if not isinstance(body, dict):
        return {}

    def on(key: str) -> bool:
        value = body.get(key)
        return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)

    reviews = body.get("required_pull_request_reviews")
    return {"strict": bool((body.get("required_status_checks") or {}).get("strict")),
            "required_pull_request_reviews": None if not isinstance(reviews, dict) else {
                "required_approving_review_count": int(reviews.get("required_approving_review_count") or 0),
                "dismiss_stale_reviews": bool(reviews.get("dismiss_stale_reviews")),
                "require_code_owner_reviews": bool(reviews.get("require_code_owner_reviews")),
                "require_last_push_approval": bool(reviews.get("require_last_push_approval"))},
            "enforce_admins": on("enforce_admins"),
            "required_linear_history": on("required_linear_history"),
            "required_conversation_resolution": on("required_conversation_resolution"),
            "required_signatures": on("required_signatures"),
            "allow_force_pushes": on("allow_force_pushes"),
            "allow_deletions": on("allow_deletions"),
            "lock_branch": on("lock_branch"),
            "allow_fork_syncing": on("allow_fork_syncing"),
            "block_creations": on("block_creations"),
            "restrictions": bool(body.get("restrictions"))}


def verify_protection(repository: str, branch: str, expected: dict, contexts: list[str]) -> None:
    """Re-read the protection just written, and fail on any setting that moved.

    From 0.43.0 to 0.57.0 this command PUT a whole protection to change its
    checks, and nothing compared what GitHub held afterwards with what the
    plan said: maelys-oci lost "up to date before merging", maelys-datalog
    its linear history, both under plans that named checks alone. The write
    is narrower since 0.57.1; this is the check that would have caught it,
    and that catches whatever the next write gets wrong.
    """
    reread = read_protection(repository, branch, rulesets=False)
    state, after = reread.classic_state, reread.classic_body
    if state != "ok" or not isinstance(after, dict):
        raise Failure("PROCESS_FAILED",
                      f"the protection of {branch} of {repository} was written, and GitHub did not answer its"
                      " re-read: nothing confirms what it holds now.",
                      f"Read it: gh api repos/{repository}/branches/{branch}/protection")
    held = protection_settings(after)
    moved = [f"{key}: {expected.get(key)!r} -> {held.get(key)!r}" for key in expected
             if expected.get(key) != held.get(key)]
    required = ((after.get("required_status_checks") or {}).get("contexts")) or []
    if sorted(required) != sorted(contexts):
        moved.append(f"contexts: {sorted(contexts)} -> {sorted(required)}")
    if moved:
        raise Failure("PROCESS_FAILED",
                      f"the protection of {branch} of {repository} changed beyond what the plan said: "
                      + "; ".join(moved) + ".",
                      "Restore those settings by hand; this is a defect of the socle, report it.")


def ruleset_shape(rules: object) -> list[str]:
    """The rules of a branch as comparable text, required contexts set apart."""
    shaped = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        parameters = dict(rule.get("parameters") or {})
        parameters.pop("required_status_checks", None)
        shaped.append(json.dumps({"type": rule.get("type"), "ruleset_id": rule.get("ruleset_id"),
                                  "parameters": parameters}, sort_keys=True))
    return sorted(shaped)


def withdrawn_for(command: str, context: Context) -> tuple[str, str] | None:
    """Whether this socle's version is withdrawn for a command, read from the socle's main.

    ("withdrawn", reason), ("unread", why), or None when it may run. `protect`
    runs at the checkout at hand, so a fix to it needs no adoption -- and the
    reverse holds: a checkout at a version whose writes were wrong keeps
    writing them. The list lives on the default branch of maelys-release and
    not in this file, so that a version found wrong after its tag can be
    withdrawn without the old code knowing anything new. Suggested by
    maelys-cli. The versions before 0.58.0 do not read it; that limit is
    stated, not hidden.
    """
    state, body = github_read(f"repos/{SOCLE_REPOSITORY}/contents/{WITHDRAWN_FILE}")
    if state != "ok" or not isinstance(body, dict) or body.get("encoding") != "base64":
        return "unread", f"{SOCLE_REPOSITORY}/{WITHDRAWN_FILE} could not be read ({state})"
    version = context.socle_version()
    for raw in base64.b64decode(body.get("content", "")).decode("utf-8", "replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        listed, _, rest = line.partition(" ")
        commands, _, reason = rest.strip().partition(" ")
        if listed == version and command in commands.split(","):
            return "withdrawn", reason.strip() or "withdrawn"
    return None
