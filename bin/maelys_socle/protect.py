# SPDX-License-Identifier: MPL-2.0
"""Derive required checks and preserve every other protection setting.

Adoption and protection share the readers of the contexts a product would
produce. The entry point supplies its socle root through the shared context;
API reads, writes and verification use the current host at call time.
"""
from __future__ import annotations

import json
import pathlib
import re
import tempfile

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from . import host
from .constants import CI_JOB, CI_USES, DECLARATION_FILE, LEGS_JOB
from .context import Context
from .declarations import parse_release
from .github import (github_api, github_list, github_read, github_repository, observed_contexts, protection_settings, read_protection, read_repository, required_contexts, ruleset_shape, verify_protection, withdrawn_for)
from .project import project_of
from .workflows import yaml_scalar


def socle_check_contexts(project: pathlib.Path, context: Context) -> tuple[str, list[str]]:
    """The contexts the socle's check job produces, and the job that calls it.

    Read from the product's own ci.yml and the check-product.yml this socle
    carries, never typed. A branch protection requires a check by its name,
    and that name is '<calling job> / <called job>': the calling job is the
    product's to name -- some call it check, maelys-egress calls it socle --
    and the called ones are the socle's matrix legs plus the jobs that run
    beside it.
    """
    ci = project / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        return "", []
    caller = ""
    for line in ci.read_text(encoding="utf-8").splitlines():
        named = CI_JOB.match(line)
        if named:
            caller = named.group(1)
        elif CI_USES.search(line) and caller:
            break
    else:
        return "", []
    # Every job of check-product.yml that this product's call turns on, not
    # the matrix alone: fuzz and sanitizers are jobs of the socle too, and
    # leaving them out of what is computed made them look like product jobs
    # that had stopped running.
    text = ci.read_text(encoding="utf-8")
    names = [f"check ({leg})" for leg in check_product_legs(context)]
    # Each of these jobs runs when its input is not empty, and each input
    # has a default: sanitizer_command defaults to a command and fuzz_command
    # to nothing, so one is opt-out and the other opt-in. Reading only the
    # call missed every product that takes the default -- which is every
    # product, for sanitizers.
    for job, gate in (("fuzz", "fuzz_command"), ("sanitizers", "sanitizer_command")):
        called = re.search(rf"^\s+{gate}:(.*)$", text, re.MULTILINE)
        value = yaml_scalar(called.group(1) if called else check_product_default(gate, context))
        # A block indicator is not an empty value: it says the value is on
        # the lines below. Reading `sanitizer_command: |` as empty made the
        # socle announce that two protected repositories required a job that
        # never runs, when it runs and passes on every pull request they have.
        if value in ("|", "|-", ">", ">-") or value.strip("'\"") != "":
            names.append(job)
    # And the product's own legs, which report under the legs job adopt
    # writes. Read from the declaration and not from ci.yml: what the guard
    # of adopt must compare a protection with is what the adoption is about
    # to produce, and a leg removed from [check] is still in ci.yml then.
    return caller, [f"{caller} / {name}" for name in names] + [f"{LEGS_JOB} / {name}"
                                                               for name in declared_legs(project)]


def declared_legs(project: pathlib.Path) -> list[str]:
    """The names of the legs [check] declares, or none when nothing parses."""
    release = project / DECLARATION_FILE
    if not release.is_file():
        return []
    try:
        return [name for name, _, _ in parse_release(release.read_text(encoding="utf-8"))[-1]]
    except ValueError:
        return []


def socle_owned(name: str, caller: str) -> bool:
    """Whether a context is one the socle's workflows report, by its prefix."""
    return name.startswith((f"{caller} / ", f"{LEGS_JOB} / "))


def vanishing_contexts(project: pathlib.Path, context: Context) -> list[str]:
    """Required checks of the default branch that this socle would stop producing.

    The socle generates the workflow whose jobs carry those names, so it is
    the only thing that can see this coming: a leg renamed by an adoption
    leaves a branch requiring a context nothing will ever report again, and
    every pull request of that repository blocks -- the adoption's own
    first. 0.41.0 came within one trial of doing it to ten repositories.

    Silent when GitHub cannot be asked. A refusal to answer is not a list of
    contexts, and an adoption must not depend on the network to be possible.
    """
    caller, produced = socle_check_contexts(project, context)
    # An alias is produced too: the old name keeps reporting while the new
    # one starts, and a branch that requires it loses nothing at adoption.
    produced = produced + [f"{caller} / check ({old})" for old, _ in check_product_aliases(context)]
    # And the jobs a call turns off: they report skipped, which passes a
    # required check, so a branch requiring one loses nothing. 0.57.1 stopped
    # counting agent-cli-spec's sanitizers as run -- rightly -- and this guard,
    # which read the same list, then refused its adoption over a context that
    # reports on every pull request. protect had learned it in the same
    # version; the guard had not.
    produced = produced + [f"{caller} / {job}" for job in ("fuzz", "sanitizers")]
    if not caller:
        return []
    required = required_contexts(project) or []
    if not required:
        return []
    # Only the socle's own contexts: what a product requires of its own jobs
    # is the product's business, and this socle has no idea what produces it.
    return sorted(name for name in required
                  if isinstance(name, str) and socle_owned(name, caller) and name not in produced)


def check_product_legs(context: Context) -> list[str]:
    """The legs of check-product.yml's matrix, from the workflow itself."""
    workflow = context.socle_root() / ".github" / "workflows" / "check-product.yml"
    if not workflow.is_file():
        return []
    legs = []
    for line in workflow.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- leg:"):
            legs.append(stripped.split(":", 1)[1].strip())
    return legs


def check_product_aliases(context: Context) -> list[tuple[str, str]]:
    """(old name, leg) for each alias check-product.yml keeps, from the workflow.

    Read from its `# alias: OLD -> LEG` lines: an alias reports the legs under
    a name they no longer carry, so a branch requiring it moves to the leg in
    one write rather than through a narrowed protection.
    """
    workflow = context.socle_root() / ".github" / "workflows" / "check-product.yml"
    if not workflow.is_file():
        return []
    return [(found.group(1), found.group(2)) for found in
            re.finditer(r"^\s*# alias: (\S+) -> (\S+)\s*$", workflow.read_text(encoding="utf-8"), re.MULTILINE)]


def check_product_default(name: str, context: Context) -> str:
    """The default of one input of check-product.yml, from the workflow."""
    workflow = context.socle_root() / ".github" / "workflows" / "check-product.yml"
    if not workflow.is_file():
        return ""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"{name}:":
            # To the next input, and not a fixed dozen lines: the window was
            # twelve, and a description that grew by a paragraph pushed
            # `default:` past it -- silently, since the reader returns "" for
            # "no default" as well as for "did not look far enough", and the
            # sanitizers job is chosen from that answer.
            for following in lines[index + 1:]:
                if re.match(r"^      [a-z_]+:", following):
                    break
                if following.strip().startswith("default:"):
                    return following.split(":", 1)[1]
            return ""
    return ""


PROTECTION_SHAPE = {"enforce_admins": True, "restrictions": None,
                    "required_pull_request_reviews": {"required_approving_review_count": 0,
                                                      "dismiss_stale_reviews": False,
                                                      "require_code_owner_reviews": False},
                    "allow_force_pushes": False, "allow_deletions": False,
                    "required_linear_history": False, "required_conversation_resolution": True}


def handle_protect(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """What the default branch should require, derived rather than typed.

    A branch protection requires a check by name, and those names are the
    socle's matrix legs prefixed by the job that calls it -- so a protection
    posted by hand is a copy of what some pin produced on the day somebody
    looked. When the socle's legs move, every context that named the old
    ones waits for a run that will never start, and the branch locks with no
    warning until a pull request refuses to merge. This reads the names from
    the workflows and from the runs the default branch actually carries, and
    says which of them the protection is missing or naming wrongly.
    """
    project, _product, _mechanism = project_of(invocation)
    repository = github_repository(project)
    if not repository:
        raise Failure("PRECONDITION_FAILED", f"{project} has no GitHub origin.",
                      "A branch protection is a GitHub setting; this reads and writes one.")
    if not host.HOST.which("gh"):
        raise Failure("PRECONDITION_FAILED", "gh is not installed.", "Install gh: this reads the GitHub API.")
    branch = read_repository(repository).default_branch
    caller, derived = socle_check_contexts(project, context)
    seen, from_tag, partial = observed_contexts(repository, branch)
    protection = read_protection(repository, branch)
    state, current = protection.classic_state, protection.classic_body
    holds = protection.classic
    required = protection.classic_contexts
    # The other endpoint, and the third place this socle has had to learn
    # it. A branch is protected by the classic protection, by a ruleset, or
    # by both, and GitHub applies them in union. `preflight` learned it in
    # 0.46.x after calling seventeen repositories open; the adoption guard
    # learned it in 0.51.1; `protect` -- the command whose whole subject is
    # what a branch requires -- was still reading one, and told a
    # ruleset-protected repository it was not protected while proposing to
    # add three contexts its ruleset already required.
    ruled, rules = protection.ruled_state, protection.rules
    by_rule = protection.rule_contexts
    required += [name for name in by_rule if name not in required]
    # The socle's legs are computed, never observed: they come from the
    # check-product.yml this socle carries, so an adoption that renames them
    # is known here before any run exists. The product's own jobs are the
    # opposite -- nothing here can predict their names, so they are taken
    # from what its pull requests produced, intersected to keep an
    # intermittent one out. Observing the socle's legs too would have
    # confused the two: right after an adoption the older pull requests
    # still carry the previous names, and an intersection would have called
    # a renamed leg intermittent and dropped it.
    others = [name for name in seen if name not in derived and not socle_owned(name, caller)]
    # A job of the socle this call turns off -- fuzz or sanitizers with an
    # empty command -- reports as skipped, and a skipped job passes a required
    # check. A branch that already requires one keeps it: dropping it would be
    # this command narrowing a protection outside a rename, and turning the
    # command back on would then run a check nobody requires.
    turned_off = [f"{caller} / {job}" for job in ("fuzz", "sanitizers") if f"{caller} / {job}" not in derived]
    kept = [name for name in required if name in turned_off]
    proposed = derived + kept + others
    # "never seen" is judged on every pull request sampled, not on their
    # intersection: a leg renamed by a recent adoption runs on the newest
    # head alone, and calling that absent would have been the same mistake
    # as calling it intermittent.
    ever = partial.get("ever", [])
    absent = [name for name in derived if ever and name not in ever]
    # A required alias whose leg this proposal requires is not stale: the
    # write replaces one by the other, and the protection never holds less
    # than both. Named pair by pair, so that what a reader -- or a guard --
    # sees removed is a duplicate with its successor beside it.
    replaced = [{"required": f"{caller} / check ({old})", "by": f"{caller} / check ({leg})"}
                for old, leg in check_product_aliases(context)
                if f"{caller} / check ({old})" in required and f"{caller} / check ({leg})" in proposed]
    retiring = [pair["required"] for pair in replaced]
    stale = [name for name in required if name not in proposed and name not in retiring]
    mechanisms = [name for name, present in (("branch protection", state == "ok"),
                                             ("a ruleset", bool(by_rule) or ruled == "ok" and bool(rules)))
                  if present]
    report = {"repository": repository, "branch": branch, "protected": bool(mechanisms),
              "protectedBy": mechanisms, "ruleset": sorted(by_rule),
              "caller": caller, "socleContexts": derived, "observed": seen,
              "fromTag": from_tag, "required": sorted(required),
              "seenOnSome": {name: count for name, count in (partial.get("partial") or {}).items()
                             if name not in derived and not socle_owned(name, caller)},
              "pullRequests": partial.get("heads", 0),
              "missingFromRuns": absent, "requiredButNeverRun": stale, "replaced": replaced, "kept": kept,
              "proposed": proposed, "applied": False,
              # What protected the branch before this command touched it,
              # kept in the output of every run: the settings a write must
              # keep, the contexts each mechanism required. What 0.43.0 to
              # 0.57.0 overwrote is gone because nothing had read it first
              # (maelys-datalog); a write from here on leaves its before
              # state in the terminal and the logs, for a hand to restore.
              # This is a record, not a restore command: a second writing
              # path is not what the command that produced six defects in
              # two days needs.
              "before": {"classic": protection_settings(holds) if state == "ok" else None,
                         "required": sorted(required), "rulesets": sorted(by_rule)}}
    # A job seen on some pull requests is intermittent only if a workflow
    # still defines it. maelys-json removed `fuzz` from its ci.yml in 0.2.0,
    # and protect called it "1 of 3 recent pull requests": the conclusion
    # right, the reason wrong, with the file that says why in hand.
    defined = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                        for path in sorted((project / ".github" / "workflows").glob("*.y*ml")))
    report["gone"] = [name for name in report["seenOnSome"]
                      if not re.search(r"^\s*(?:name:\s*['\"]?|)" + re.escape(name.split(" (")[0])
                                       + r"['\"]?\s*:?\s*$", defined, re.MULTILINE)]
    if state == "unreadable":
        report["note"] = (f"GitHub refused to say what protects {branch} of {repository}, which a private"
                          " repository of a free plan does. That is not the same fact as an open branch.")
    # A ruleset carrying the socle's own contexts is the one case --apply
    # must not touch. This command writes the classic protection; on a
    # branch already ruled, that is a second mechanism superimposed on the
    # first, two places to keep true, and the ruleset still naming the old
    # contexts after the next rename. Reported by agent-cli-spec, who
    # worked it out from the output before running it.
    ours = [name for name in by_rule if name in derived or socle_owned(name, caller)]
    # --without-legs: the first half of a rename. What the branch requires,
    # minus every leg of the socle's matrix -- the jobs named `check (…)` under
    # the calling job -- and nothing else. A required context that nothing
    # produces blocks every merge, so a rename can only narrow ahead of the
    # adoption and widen after it; this is the narrowing, and `--apply`
    # without it is the widening.
    legs = re.compile(re.escape(f"{caller} / ") + r"check \(")
    narrowing = invocation.flag("--without-legs")
    if narrowing:
        report["proposed"] = proposed = [name for name in required if not legs.match(name)]
        report["narrowing"] = True
        # What a narrowing drops is dropped on purpose: reporting it as stale,
        # or the new legs as not yet run, would describe the rename as a
        # fault in the middle of doing it.
        report["dropped"] = [name for name in required if legs.match(name)]
        report["requiredButNeverRun"], report["missingFromRuns"] = [], []
        absent, stale = [], []
    # Widening before the adoption is merged requires names that only the
    # adoption's own pull request produces, and every other open pull request
    # -- built on the previous ci.yml -- would wait for them forever. The
    # order printed in 0.54.0 said "narrow, adopt, widen" and did not say
    # "merged"; maelys-json followed it as written, had no other pull request
    # open, and said so. Widen now waits for a merged pull request to have
    # run under every name it is about to require.
    # Nothing is written on what could not be read. `unreadable` is not
    # `absent`: a timeout on the protection endpoint made a protected branch
    # look open, and --apply would have written four contexts over
    # seventeen. Measured on maelys-system, the day 0.57.0 asked five
    # products to run this.
    unread = [what for what, answered in (("the branch protection", state in ("ok", "absent")),
                                          ("the rulesets", ruled in ("ok", "absent")),
                                          ("the check runs of recent pull requests",
                                           not partial.get("unanswered")))
              if not answered]
    report["unread"] = unread
    # Every other setting, in the plan: what an existing protection keeps,
    # and what a new one is created with. A new protection used to receive
    # the socle's shape -- linear history off among others -- without a line
    # saying so (maelys-datalog).
    if state == "ok":
        report["keeps"] = protection_settings(holds)
    elif state == "absent" and not by_rule:
        report["creates"] = protection_settings(dict(PROTECTION_SHAPE, required_status_checks={"strict": False}))
    if invocation.flag("--apply"):
        withdrawn = withdrawn_for("protect", context)
        if withdrawn and withdrawn[0] == "withdrawn":
            raise Failure("PRECONDITION_FAILED",
                          f"maelys-release {context.socle_version()} is withdrawn for protect --apply: {withdrawn[1]}.",
                          "Run protect from a checkout of the latest tag of maelys-release.")
        if withdrawn:
            raise Failure("PRECONDITION_FAILED",
                          f"whether maelys-release {context.socle_version()} may write a protection is unknown: {withdrawn[1]}.",
                          "Nothing is written on a reading that is not there; run again once GitHub answers.")
    if invocation.flag("--apply") and unread:
        raise Failure("PRECONDITION_FAILED",
                      f"GitHub did not answer for {', '.join(unread)} of {repository}: nothing is written on a"
                      " reading that is not there.",
                      "Run protect again once GitHub answers, and read the plan before --apply.")
    # --apply never narrows by itself. Retiring an alias for its leg is a
    # replacement, and a job the call turned off is kept; anything else the
    # branch requires and the proposal leaves out is named, and written only
    # when asked.
    dropping = [name for name in required if name not in proposed and name not in retiring]
    report["dropping"] = dropping
    if invocation.flag("--apply") and dropping and not narrowing and not invocation.flag("--allow-narrow"):
        raise Failure("PRECONDITION_FAILED",
                      f"this write would stop requiring {', '.join(dropping)} on {branch} of {repository}.",
                      "protect --apply does not narrow a protection by itself. If those checks are gone for"
                      " good, pass --allow-narrow; otherwise let them run on a pull request first.")
    # A name the branch already requires blocks nothing new by being written
    # again: the refusal is for names this write would add. agent-cli-spec
    # was refused a rename over a context its ruleset had required for a day.
    adding = [name for name in absent if name not in required]
    if invocation.flag("--apply") and not narrowing and adding and not invocation.flag("--allow-lock"):
        waiting = [str(pull.get("number")) for pull in github_list(
            f"repos/{repository}/pulls?state=open&base={branch}&per_page=50") if isinstance(pull, dict)]
        raise Failure("PRECONDITION_FAILED",
                      f"no merged pull request of {repository} has run " + ", ".join(adding)
                      + ": requiring them now would block every pull request that does not produce them yet.",
                      "Merge the adoption first, then protect --apply."
                      + (f" Open pull requests built on the previous ci.yml (#{', #'.join(waiting)}) must"
                         f" merge {branch} to report the new names." if waiting else ""))
    if invocation.flag("--apply") and ours:
        # The ruleset carries the socle's contexts, so the ruleset is what
        # is written -- never a classic protection beside it, which would be
        # two mechanisms to keep true. Until 0.54.0 this refused, and a
        # repository protected by a ruleset alone had every step of a rename
        # to do by hand.
        report["applied"] = write_ruleset(repository, branch, rules, proposed)
        return report, EXIT_OK
    if invocation.flag("--apply") and narrowing and state != "ok":
        raise Failure("PRECONDITION_FAILED", f"{branch} of {repository} has no protection to narrow.",
                      "Nothing requires the legs, so the adoption will not lock anything: adopt first.")
    if invocation.flag("--apply"):
        if not proposed:
            raise Failure("PRECONDITION_FAILED",
                          f"no check run was seen on {branch} of {repository}.",
                          "A protection that requires nothing is not what this writes. Let the branch run"
                          " its checks once, then protect it.")
        # What the report showed, never what was merely observed. `seen` is
        # the intersection of recent pull requests; `proposed` is that plus
        # the socle's computed legs. Writing `seen` meant that right after an
        # adoption renaming a leg, --apply required the old name still
        # present in older pull requests -- the very lock this command was
        # written to prevent, in the command itself.
        # An existing protection is changed in its required checks alone,
        # through their own endpoint, and keeps every other setting as it is.
        # The whole protection used to be PUT with the socle's shape, and
        # maelys-oci's "require branches to be up to date" went from true to
        # false with a plan that named three replacements and nothing else.
        existing = holds.get("required_status_checks") if state == "ok" else None
        if isinstance(existing, dict):
            method, endpoint = "PATCH", f"repos/{repository}/branches/{branch}/protection/required_status_checks"
            body = {"strict": bool(existing.get("strict")), "contexts": proposed}
            expected = protection_settings(holds)
        else:
            method, endpoint = "PUT", f"repos/{repository}/branches/{branch}/protection"
            body = {**PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": proposed}}
            expected = protection_settings(body)
        with tempfile.TemporaryDirectory(prefix="maelys-release-protect.") as temp:
            payload = pathlib.Path(temp) / "protection.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            done = host.HOST.write(method, endpoint, payload, cwd=project)
        if done.returncode != 0:
            raise Failure("PROCESS_FAILED", f"the protection of {branch} was refused: {done.stderr.strip()}",
                          "A private repository needs a paid plan for branch protection; read the message above.")
        verify_protection(repository, branch, expected, proposed)
        report["applied"] = True
    return report, EXIT_OK if not (absent or stale) else EXIT_VIOLATIONS


def write_ruleset(repository: str, branch: str, rules: object, contexts: list[str]) -> bool:
    """Set the required checks of the ruleset that protects this branch.

    Only a ruleset of this repository: one inherited from the organisation
    cannot be written with a repository's permissions, and saying so is
    better than a 403 halfway through a rename. Every other rule of the
    ruleset is sent back as it was read, and each kept context keeps the
    integration that was allowed to report it.
    """
    carrying = [rule for rule in (rules if isinstance(rules, list) else [])
                if isinstance(rule, dict) and rule.get("type") == "required_status_checks"]
    if not carrying:
        raise Failure("PRECONDITION_FAILED", f"no ruleset of {repository} requires checks on {branch}.",
                      "Nothing to write.")
    if carrying[0].get("ruleset_source_type") not in (None, "Repository"):
        raise Failure("PRECONDITION_FAILED",
                      f"the checks {branch} of {repository} requires come from a ruleset of the"
                      " organisation, which a repository cannot write.",
                      "Change it in the organisation's settings, with the list 'protect' prints.")
    identifier = carrying[0].get("ruleset_id")
    state, ruleset = github_read(f"repos/{repository}/rulesets/{identifier}")
    if state != "ok" or not isinstance(ruleset, dict):
        raise Failure("PROCESS_FAILED", f"ruleset {identifier} of {repository} could not be read.",
                      "Read it with 'gh api repos/OWNER/REPO/rulesets/ID' and retry.")
    integration = {}
    for rule in ruleset.get("rules") or []:
        if rule.get("type") == "required_status_checks":
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                integration[check.get("context")] = check.get("integration_id")
    known = [value for value in integration.values() if value]
    default = max(set(known), key=known.count) if known else None
    for rule in ruleset.get("rules") or []:
        if rule.get("type") != "required_status_checks":
            continue
        rule["parameters"]["required_status_checks"] = [
            {"context": name, **({"integration_id": integration.get(name) or default}
                                 if (integration.get(name) or default) else {})}
            for name in contexts]
    body = {key: ruleset[key] for key in ("name", "target", "enforcement", "conditions", "rules",
                                          "bypass_actors") if key in ruleset}
    with tempfile.TemporaryDirectory(prefix="maelys-release-ruleset.") as temp:
        payload = pathlib.Path(temp) / "ruleset.json"
        payload.write_text(json.dumps(body), encoding="utf-8")
        done = host.HOST.write("PUT", f"repos/{repository}/rulesets/{identifier}", payload)
    if done.returncode != 0:
        raise Failure("PROCESS_FAILED", f"ruleset {identifier} of {repository} was refused: {done.stderr.strip()}",
                      "Writing a ruleset needs administration of the repository.")
    # Re-read, as for a classic protection: every rule but the required
    # contexts must read back as it was, and the contexts as written.
    reread = read_protection(repository, branch, classic=False)
    state, after = reread.ruled_state, reread.rules
    if state != "ok" or not isinstance(after, list):
        raise Failure("PROCESS_FAILED",
                      f"ruleset {identifier} of {repository} was written, and GitHub did not answer its re-read.",
                      f"Read it: gh api repos/{repository}/rules/branches/{branch}")
    written = sorted(check.get("context") for rule in after if isinstance(rule, dict)
                     and rule.get("type") == "required_status_checks"
                     for check in (rule.get("parameters") or {}).get("required_status_checks") or [])
    if ruleset_shape(after) != ruleset_shape(rules) or written != sorted(contexts):
        raise Failure("PROCESS_FAILED",
                      f"ruleset {identifier} of {repository} changed beyond its required checks, or they did not"
                      " read back as written.",
                      "Compare the ruleset with what protect printed; this is a defect of the socle, report it.")
    return True


def text_protect(data: dict) -> str:
    lines = [f"branch   {data['branch']} of {data['repository']}"
             + (" is protected by " + " and ".join(data["protectedBy"]) if data.get("protectedBy")
                else " could not be read" if "the branch protection" in (data.get("unread") or [])
                else "" if data["protected"] else " is NOT protected")]
    for what in data.get("unread") or []:
        lines.append(f"UNREAD   GitHub did not answer for {what}: this plan is not a reading, and --apply refuses")
    for name in data.get("dropping") or []:
        lines.append(f"DROP     {name} is required and this plan leaves it out: --apply refuses without --allow-narrow")
    if data.get("ruleset"):
        lines.append(f"ruleset  requires " + ", ".join(data["ruleset"])
                     + " — --apply writes this ruleset, never a classic protection beside it")
    if data.get("narrowing"):
        lines.extend(f"{'drop':<8} {name}" for name in data.get("dropped", []))
        lines.append(f"{'narrow':<8} the legs of the socle's matrix leave the required list; everything"
                     " else stays. Adopt the socle that renames them, merge that adoption, then"
                     " 'protect --apply' again")
    if data.get("note"):
        lines.append(f"note     {data['note']}")
    if data["caller"]:
        lines.append(f"socle    called by the job '{data['caller']}': "
                     + ", ".join(data["socleContexts"]))
    for name in data["proposed"]:
        mark = "ok" if name in data["required"] else "add"
        lines.append(f"{mark:<8} {name}")
    for name in data.get("kept") or []:
        lines.append(f"{'keep':<8} {name}: this call turns the job off, so it reports skipped, which passes;"
                     " the branch already requires it and protect does not narrow it")
    if data.get("keeps"):
        lines.append(f"{'keeps':<8} every other setting as it is: "
                     + ", ".join(f"{key}={json.dumps(value)}" for key, value in data["keeps"].items()))
    for key, value in (data.get("creates") or {}).items():
        lines.append(f"{'create':<8} {key}={json.dumps(value)} (no protection exists; --apply creates it with this)")
    for pair in data.get("replaced") or []:
        lines.append(f"{'replace':<8} {pair['required']} -> {pair['by']}: the alias leaves in the same write"
                     " that requires the leg it mirrored, so main never requires less")
    for name in data["requiredButNeverRun"]:
        lines.append(f"STALE    {name} is required and no run on {data['branch']} produces it:"
                     " a pull request would wait for it forever")
    for name in data["missingFromRuns"]:
        lines.append(f"NOT YET  {name} is what this socle's check job produces and no recent pull request"
                     " carries it: adopt this socle first, or its next pull request will be the first to"
                     " run under that name")
    for name, count in (data.get("seenOnSome") or {}).items():
        if name in (data.get("gone") or []):
            lines.append(f"note     {name} is left out: no workflow of this repository defines it any more")
            continue
        lines.append(f"note     {name} is left out: {count} of {data.get('pullRequests', 0)} recent pull"
                     " requests produced it, and a protection should require what reliably runs")
    for name in data["fromTag"]:
        lines.append(f"note     {name} is left out: a tag produces it, a pull request never does")
    if data["applied"] and data.get("before"):
        # Printed on a write only: the plan already lists what it keeps.
        lines.append(f"{'before':<8} {json.dumps(data['before'], sort_keys=True)}")
    lines.append(f"protect: {'applied' if data['applied'] else 'nothing written; add --apply'}")
    return "\n".join(lines) + "\n"
