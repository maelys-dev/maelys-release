# SPDX-License-Identifier: MPL-2.0
"""Derive required checks and preserve every other protection setting.

Adoption and protection share the readers of the contexts a product would
produce. The entry point supplies its socle root through the shared context;
API reads, writes and verification use the current host at call time.

`protect` writes what GitHub must carry before a publication: what the
default branch requires, and -- for a product the socle releases -- the
environment its publish job runs in, which `environment.py` plans and arms.
The two are halves that do not wait for each other: each has its reads, its
refusals, its writes and its read-back, a refusal of one is a line of the
result and never stops the other, and an error says what either had written.
"""
from __future__ import annotations

import json
import pathlib
import re
import tempfile

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from . import host
from .constants import CI_USES, DECLARATION_FILE, LEGS_JOB, SOCLE_MECHANISM
from .context import Context
from .declarations import parse_release
from .environment import Refusal, arm, environment_plan, record, text_environment, violations
from .forge import forge
from .github import (github_api, github_list, github_read, github_repository, observed_contexts, protection_settings, read_protection, read_repository, required_contexts, rule_contexts, ruleset_shape, verify_protection, withdrawn_for)
from .project import project_of
from .workflows import job_names, yaml_scalar


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
    lines = ci.read_text(encoding="utf-8").splitlines()
    for line, named in zip(lines, job_names(lines)):
        if named:
            caller = named
        elif CI_USES.search(line) and caller:
            break
    else:
        return "", []
    # And the product's own legs, which report under the legs job adopt
    # writes. Read from the declaration and not from ci.yml: what the guard
    # of adopt must compare a protection with is what the adoption is about
    # to produce, and a leg removed from [check] is still in ci.yml then.
    return caller, [f"{caller} / {name}" for name in socle_check_jobs(project, context)] \
        + [f"{LEGS_JOB} / {name}" for name in declared_legs(project)]


def socle_check_jobs(project: pathlib.Path, context: Context) -> list[str]:
    """The jobs of check-product.yml a product's call turns on, as each reports.

    Apart from the name of the job that calls them, so that `rehearse` lists
    the same jobs under a caller it reads itself.
    """
    ci = project / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        return []
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
    return names


def declared_legs(project: pathlib.Path) -> list[str]:
    """The names of the legs [check] declares, or none when nothing parses."""
    release = project / DECLARATION_FILE
    if not release.is_file():
        return []
    try:
        return [name for name, _, _ in parse_release(release.read_text(encoding="utf-8"))[-1]]
    except ValueError:
        return []


def declared_gate(project: pathlib.Path) -> str:
    """`[gate]` of the declaration file, through the parser `preflight` reads it with; '' when undeclared.

    One reader of a declaration, as of a protection: a second scan of the
    file would be the one a change of grammar forgets. A file that does not
    parse declares nothing here, and `check` is what says why.
    """
    release = project / DECLARATION_FILE
    if not release.is_file():
        return ""
    try:
        return parse_release(release.read_text(encoding="utf-8"))[9]
    except ValueError:
        return ""


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
    project, _product, mechanism = project_of(invocation)
    repository = github_repository(project)
    if not repository:
        raise Failure("PRECONDITION_FAILED", f"{project} has no GitHub origin.",
                      "A branch protection is a GitHub setting; this reads and writes one.")
    if not forge().available():
        raise Failure("PRECONDITION_FAILED", "gh is not installed.", "Install gh: this reads the GitHub API.")
    described = read_repository(repository)
    branch = described.default_branch
    caller, derived = socle_check_contexts(project, context)
    seen, from_tag, partial = observed_contexts(repository, branch)
    protection = read_protection(repository, branch)
    state, current = protection.classic_state, protection.classic_body
    # An answer that is no protection -- a 200 carrying nothing, or a list --
    # is not a reading of one. It was read as a protection that requires no
    # check, and the whole protection was put over whatever was there.
    if state == "ok" and not (isinstance(current, dict) and current):
        state = "empty"
    # Nor is every 404 an absence. GitHub says `Branch not protected` of a
    # branch that has no protection, and answers 404 as well to a token
    # that may not read protections and for a branch that does not exist:
    # read as one fact, the last two had the whole protection put on them.
    # Told apart here and nowhere else, since every other reader of a 404
    # reports and none of them writes on it.
    if state == "absent" and not protection.unprotected:
        state = "unsaid"
    # The environment a tag deploys into, for a product whose release is the
    # socle's: a repository that publishes its own way owns its environments
    # as it owns its release.yml, and this says nothing of them. Nor does
    # --without-legs, which is the narrowing half of a rename and nothing
    # else: it used to create and arm an environment on its way.
    gate = declared_gate(project)
    reading, environment = (environment_plan(repository, gate)
                            if mechanism == SOCLE_MECHANISM and not invocation.flag("--without-legs")
                            else (None, None))
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
    # GitHub answers `[]` for a branch no rule applies to. A 404 there is
    # not a reading of its rules, whatever it says, and neither is a body
    # that is not a list: nothing is said of the branch from either.
    if ruled == "absent" or ruled == "ok" and not isinstance(rules, list):
        ruled = "unreadable"
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
    # A 404 that does not say the branch is unprotected is told as it was
    # read. GitHub did answer -- `Branch not found` is an answer -- so the
    # note of a refusal to say would be false of it.
    elif state == "unsaid":
        words = getattr(protection.classic_state, "message", "")
        report["note"] = ("GitHub answered 404 with " + (f"the words '{words}'" if words else "no words")
                          + f" for the protection of {branch} of {repository}, which is not what it says of a"
                          " branch without protection: nothing is concluded from it.")
    # What a branch requires must be what a pull request produces. With no
    # merged pull request to read, this command proposes the socle's own
    # contexts and nothing else, and says why -- rather than reading the tip
    # of the default branch, which also carries what the tag pointing at it
    # ran. A product read a plan proposing `build (…)` and `publish` that
    # way, and wrote its protection by hand instead.
    if partial.get("noEvidence"):
        evidence = (f"no merged pull request of {repository} could be read, so none of this product's own"
                    " checks were observed: this plan proposes the socle's contexts alone. Merge a pull"
                    " request, then run this again to add what it produced.")
        report["note"] = f"{report['note']} {evidence}" if report.get("note") else evidence
    # A ruleset carrying the socle's own contexts is the one case --apply
    # must not touch. This command writes the classic protection; on a
    # branch already ruled, that is a second mechanism superimposed on the
    # first, two places to keep true, and the ruleset still naming the old
    # contexts after the next rename. Reported by agent-cli-spec, who
    # worked it out from the output before running it.
    ours = [name for name in by_rule if name in derived or socle_owned(name, caller)]
    # And of several rulesets, the one --apply writes is the one that
    # already carries a check of the socle, wherever GitHub lists it. The
    # first rule that required any check named it instead: with `lint` in
    # one ruleset and the three legs in the next, --apply gave the legs to
    # the first and failed its read-back on every run. `ruleset` is what
    # the plan says --apply writes, so it lists what those rulesets
    # require, and what every ruleset requires where none carries a check
    # of the socle; `before` keeps them all.
    requiring = rulesets_requiring(rules)
    carrying = [held for held in requiring if set(rule_contexts(held)) & set(ours)]
    if carrying:
        report["ruleset"] = sorted(rule_contexts([rule for held in carrying for rule in held]))
    # Where several rulesets require checks, one mechanism is written at
    # most, beside others this run does not write: the ruleset that alone
    # carries the socle's checks, or the classic protection where none
    # does. What that one requires, and what the others do.
    several = len(requiring) > 1
    alone = several and len(carrying) == 1
    own = (rule_contexts(carrying[0]) if alone else [] if carrying else protection.classic_contexts) if several else []
    elsewhere = ([name for held in requiring if not (alone and held is carrying[0]) for name in rule_contexts(held)]
                 + (protection.classic_contexts if carrying else [])) if several else []
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
    # What the mechanism written is given: the proposal, for one mechanism.
    # A ruleset written beside others is given what is its own business and
    # no more -- the checks it holds that the proposal keeps, and the
    # socle's. Given the whole proposal, it received `lint` from the ruleset
    # that requires it, bound to the app of its own checks, and then waited
    # on every pull request for a `lint` that app never reports. A check
    # another mechanism requires is neither added to it nor its to drop;
    # and one that pull requests produce and nothing requires is added to
    # no ruleset, which of them should require it not being read anywhere.
    written, beyond = proposed, []
    if alone and narrowing:
        # The legs leave the ruleset written, and nothing joins it.
        written = [name for name in own if not legs.match(name)]
        report["dropped"] = [name for name in own if legs.match(name)]
    elif several and not narrowing:
        # A check nothing produces, required where this run writes nothing,
        # is still required when it ends: no write of it settles that, and
        # no flag.
        beyond = [name for name in stale if name in elsewhere]
    if alone and not narrowing:
        unclaimed = [name for name in proposed if name not in required and name not in derived]
        report["proposed"] = proposed = [name for name in proposed if name not in unclaimed]
        written = [name for name in proposed if name in own or name in derived]
        if unclaimed:
            one = len(unclaimed) == 1
            aside = (f"{', '.join(unclaimed)} {'is' if one else 'are'} left out: pull requests produce"
                     f" {'it' if one else 'them'} and nothing requires {'it' if one else 'them'}, and of several"
                     " rulesets this command gives the one it writes the socle's checks and no other. Which"
                     f" ruleset should require {'it' if one else 'them'} is its owner's to settle.")
            report["note"] = f"{report['note']} {aside}" if report.get("note") else aside
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
    # Nor on a branch that was not read: when GitHub does not describe the
    # repository its default branch falls back to `main`, every reading
    # above is of that guess, and --apply created a protection on it.
    unread = [what for what, answered in (("the default branch", bool(described.data.get("default_branch"))),
                                          ("the branch protection", state in ("ok", "absent")),
                                          ("the rulesets", ruled == "ok"),
                                          ("the check runs of recent pull requests",
                                           not partial.get("unanswered")))
              if not answered]
    if environment:
        report["environment"] = environment
    report["unread"] = unread
    # Every other setting, in the plan: what an existing protection keeps,
    # and what a new one is created with. A new protection used to receive
    # the socle's shape -- linear history off among others -- without a line
    # saying so (maelys-datalog). Created where nothing protects the
    # branch, which the rules must have been read to say.
    if state == "ok":
        report["keeps"] = protection_settings(holds)
    elif (state == "absent" and ruled == "ok" and "a ruleset" not in mechanisms
          and "the default branch" not in unread):
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
    # --apply never narrows by itself. Retiring an alias for its leg is a
    # replacement, and a job the call turned off is kept; anything else the
    # branch requires and the proposal leaves out is named, and written only
    # when asked.
    # Judged on the checks of what is written: a check another mechanism
    # requires is not one this write stops requiring, and was said to be,
    # refused as a narrowing, and offered --allow-narrow to drop.
    dropping = [name for name in (own if several else required) if name not in written and name not in retiring]
    report["dropping"] = dropping
    # A name the branch already requires blocks nothing new by being written
    # again: the refusal is for names this write would add. agent-cli-spec
    # was refused a rename over a context its ruleset had required for a day.
    adding = [name for name in absent if name not in required]
    # What this half sent, for an error that hides the report: `before` is
    # promised to the terminal and the logs of every run that wrote.
    sent: list[str] = []
    # What --apply refuses with the flags of this run, each named once for
    # the plan and for the refusal itself: a plan that counted them another
    # way than --apply would end on a sentence its --apply contradicts.
    narrows = bool(dropping) and not narrowing and not invocation.flag("--allow-narrow")
    locks = bool(adding) and not narrowing and not invocation.flag("--allow-lock")
    standing = None
    if ours:
        # The plan has the rules of the branch and never reads the ruleset
        # they come from, which is what the body is made of: what the rules
        # list decides nothing here, and `held` is --apply's alone to find.
        write, listed, writable = ruleset_write(repository, carrying[0], written)
        # Nor does this command choose between rulesets that each carry
        # some of the socle's checks: it would have to move a check from
        # one to the other, or leave it required twice, and which ruleset
        # holds them is not a fact it can read. Whatever the flags, as a
        # narrowing has the same choice to make; and whatever else was not
        # read, since the rules of the branch were -- of the branch GitHub
        # named, a guess at its name being a reading of nothing.
        if len(carrying) > 1 and "the default branch" not in unread:
            named = [str(own[0].get("ruleset_id")) for own in carrying]
            standing = Failure(
                "PRECONDITION_FAILED",
                f"{SEVERAL} {branch} of {repository}: rulesets {', '.join(named[:-1])} and {named[-1]}.",
                "This command writes the one ruleset that carries the socle's checks, and does not choose"
                " between rulesets that each require some of them: nothing is written. Which of them should"
                " carry those checks is the repository's to settle, or the organisation's for a ruleset of"
                " its own; protect --apply then writes that one.")
        held, bare, refuses = False, False, bool(unread) or bool(standing) or narrows or locks or not writable
    else:
        write, held = classic_write(repository, branch, state, holds, proposed)
        # The whole protection is created only where nothing protects the
        # branch. Beside a ruleset that requires none of the socle's checks
        # it was put all the same, under a plan that printed no `create`
        # line and said --apply wrote the ruleset.
        beside = state == "absent" and "a ruleset" in mechanisms
        if beside:
            write = None
        # Nothing to narrow where no classic protection requires a check:
        # --without-legs is told so, in the words it always was where there
        # is no protection at all.
        bare = bool(narrowing) and (state != "ok" or write is None)
        # A protection that exists and requires no check has no write here
        # either, and both are said in the plan as under --apply. The whole
        # protection used to be put over such a protection with the socle's
        # shape, under a plan that printed `keeps every other setting as it
        # is`: reviews, linear history and the rule for administrators
        # replaced, and a read-back that passed because it compared with
        # the body just sent. No other write is guessed in its place: what
        # GitHub answers to a change of required checks where none are
        # required was not measured, and a protection rebuilt from its own
        # reading is a mapping between two shapes that nothing here verifies.
        if write is None and beside and not unread and not narrowing:
            standing = Failure(
                "PRECONDITION_FAILED",
                f"{branch} of {repository} is protected by a ruleset that requires none of the socle's checks.",
                "This command does not create a classic protection beside a ruleset, and does not choose which"
                " ruleset should require them. Requiring one of them in a ruleset is its owner's setting to"
                " change; protect --apply then writes the checks of that ruleset and of no other: it writes"
                " a ruleset of the repository, refuses one of the organisation, and refuses where more than"
                " one ruleset requires some of them.")
        elif write is None and not unread and not narrowing:
            standing = Failure(
                "PRECONDITION_FAILED",
                f"the protection of {branch} of {repository} requires no check yet.",
                "This command changes the required checks of a protection that already requires some, and"
                " creates a protection where none exists; it does not turn required status checks on in a"
                " protection that exists, and never replaces one. Turning that setting on is the repository's"
                " own to do; then protect --apply.")
        listed, refuses = False, bool(unread) or bool(standing) or narrows or locks or bare or not proposed
    if standing:
        report["refused"] = {"message": standing.message, "hint": standing.hint}
    # `writes` is what --apply with the flags of this run would send for
    # this half, in the plan and under --apply alike, as the environment
    # half says its own: nothing where the half refuses, nothing on a reading
    # that is not there, and nothing where what was read already holds it.
    # Every --apply used to send its write again: a second run reported a
    # write that had changed nothing, and touched the settings of a
    # repository for no reason. Three ways of sending nothing, and one of
    # them alone is `nothing to change`: sending nothing and refusing
    # nothing. For a ruleset only --apply can say it, on the ruleset's own
    # reading beside the rules of the branch; its plan names the write, and
    # --apply empties `writes` when both readings hold everything.
    report["writes"], report["written"] = [] if refuses or held else [write], sent
    report["nothingToChange"] = held and not refuses
    if listed and not refuses and not invocation.flag("--apply"):
        reading = (f"the rules of {branch} list these checks already. Whether the ruleset itself does is read"
                   " under --apply, which sends nothing when it does.")
        report["note"] = f"{report['note']} {reading}" if report.get("note") else reading

    def protection() -> None:
        """The protection half of --apply: its refusals, its write, its read-back."""
        # What the plan refused, --apply refuses in the same words, before
        # any refusal a flag could lift. And before a reading that is not
        # there, which the plan named beside its refusal: only rulesets it
        # does not choose between are refused with another reading missing,
        # the other two refusals standing on every one of them.
        if standing:
            raise standing
        # Nothing is written on what could not be read. `unreadable` is not
        # `absent`: a timeout on the protection endpoint made a protected
        # branch look open, and --apply would have written four contexts
        # over seventeen. Measured on maelys-system, the day 0.57.0 asked
        # five products to run this.
        if unread:
            raise Failure("PRECONDITION_FAILED",
                          f"GitHub did not answer for {', '.join(unread)} of {repository}: nothing is written on a"
                          " reading that is not there.",
                          "Run protect again once GitHub answers, and read the plan before --apply.")
        if narrows:
            raise Failure("PRECONDITION_FAILED",
                          f"this write would stop requiring {', '.join(dropping)} on {branch} of {repository}.",
                          "protect --apply does not narrow a protection by itself. If those checks are gone for"
                          " good, pass --allow-narrow; otherwise let them run on a pull request first.")
        if locks:
            waiting = [str(pull.get("number")) for pull in github_list(
                f"repos/{repository}/pulls?state=open&base={branch}&per_page=50") if isinstance(pull, dict)]
            raise Failure("PRECONDITION_FAILED",
                          f"no merged pull request of {repository} has run " + ", ".join(adding)
                          + ": requiring them now would block every pull request that does not produce them yet.",
                          "Merge the adoption first, then protect --apply."
                          + (f" Open pull requests built on the previous ci.yml (#{', #'.join(waiting)}) must"
                             f" merge {branch} to report the new names." if waiting else ""))
        if ours:
            # The ruleset carries the socle's contexts, so the ruleset is what
            # is written -- never a classic protection beside it, which would be
            # two mechanisms to keep true. Until 0.54.0 this refused, and a
            # repository protected by a ruleset alone had every step of a rename
            # to do by hand.
            report["applied"] = write_ruleset(repository, branch, rules, carrying[0], written, sent)
            # The ruleset as it was just read lists them already, as the
            # rules of the branch do: nothing was sent, and the result says
            # so instead of the write planned.
            if not report["applied"]:
                report["writes"], report["nothingToChange"] = [], True
            return
        if bare:
            raise Failure("PRECONDITION_FAILED",
                          f"{branch} of {repository} has no protection to narrow." if state != "ok" else
                          f"the protection of {branch} of {repository} requires no check: there is nothing to narrow.",
                          "Nothing requires the legs, so the adoption will not lock anything: adopt first.")
        if not proposed:
            raise Failure("PRECONDITION_FAILED",
                          f"no check run was seen on {branch} of {repository}.",
                          "A protection that requires nothing is not what this writes. Let the branch run"
                          " its checks once, then protect it.")
        # What the protection already holds is not sent again, and nothing
        # is read back of a write that was not made.
        if held:
            return
        # What the report showed, never what was merely observed. `seen` is
        # the intersection of recent pull requests; `proposed` is that plus
        # the socle's computed legs. Writing `seen` meant that right after an
        # adoption renaming a leg, --apply required the old name still
        # present in older pull requests -- the very lock this command was
        # written to prevent, in the command itself.
        with tempfile.TemporaryDirectory(prefix="maelys-release-protect.") as temp:
            payload = pathlib.Path(temp) / "protection.json"
            payload.write_text(json.dumps(write["body"]), encoding="utf-8")
            done = host.HOST.write(write["method"], write["endpoint"], payload, cwd=project)
        if done.returncode != 0:
            raise Failure("PROCESS_FAILED", f"the protection of {branch} was refused: {done.stderr.strip()}",
                          "A private repository needs a paid plan for branch protection; read the message above.")
        sent.append(f"{write['method']} {write['endpoint']}")
        # Every other setting reads back as it was under a protection that
        # exists, and as the body sets it under one this created.
        verify_protection(repository, branch,
                          protection_settings(holds if write["method"] == "PATCH" else write["body"]), proposed)
        report["applied"] = True

    # Two halves, and neither waits for the other. A half that will not
    # write says so in the result -- REFUSED, exit 2 -- and the other still
    # does its work: coupled, a repository whose protection this command
    # cannot write (a ruleset of the organisation, a check renamed and not
    # yet run) could never arm its environment with it. A half whose write
    # GitHub refused, or that did not read back, is an error; the other
    # half still runs, and the error says what both had written and what
    # each replaced, since an error replaces the report.
    failed: list[Failure] = []
    if invocation.flag("--apply"):
        try:
            protection()
        except Failure as failure:
            if failure.code == "PRECONDITION_FAILED" and not sent:
                report["refused"] = {"message": failure.message, "hint": failure.hint}
            else:
                failed.append(failure)
        if environment:
            try:
                arm(reading, environment, gate, project)
            except Refusal as refusal:
                environment["refused"].append(str(refusal))
            except Failure as failure:
                failed.append(failure)
    if failed:
        first = failed[0]
        wrote = ((" The protection half had written: " + "; ".join(sent) + ". Before, it was "
                  + json.dumps(report["before"], sort_keys=True) + ".") if sent else "")
        also = "".join(f" The other half failed too: {other.message} {other.hint}" for other in failed[1:])
        raise Failure(first.code, first.message, (first.hint + also + wrote + record(environment)).strip(),
                      first.issues)
    # One rule for the exit: 2 when something stands that --apply does not
    # settle -- a required check nothing produces, a check no pull request
    # has run, a policy beside the tag rule, a reviewer declared and not
    # there -- or when --apply refused a half; 0 otherwise, a plan whose
    # writes --apply would make included. A ruleset just written answers for
    # its own contexts, as it always did -- and so does one --apply found
    # holding them already: the exit of a second run is that of the first.
    # -- for the contexts it holds, that is: a check nothing produces that
    # another mechanism requires stands after any write of this one.
    settled = report["applied"] or report["nothingToChange"]
    left = (absent or stale) and not (settled and ours) or beyond
    return report, (EXIT_VIOLATIONS if left or report.get("refused") or violations(environment) else EXIT_OK)


def unordered(items: list) -> list[str]:
    """The members of a list as comparable text, whatever order they came in."""
    return sorted(json.dumps(item, sort_keys=True) for item in items)


def classic_write(repository: str, branch: str, state: str, holds: dict,
                  contexts: list[str]) -> tuple[dict | None, bool]:
    """The write that makes a classic protection require CONTEXTS, and whether it holds them already.

    Held is judged member by member of the body, on what GitHub answered and
    never on what an absence was read into: a write that would set a member
    the reading does not carry is sent as it always was. No write at all
    for a protection that exists and requires no check, nor for a reading
    that is not there: the whole protection is put only where none exists.
    """
    # An existing protection is changed in its required checks alone,
    # through their own endpoint, and keeps every other setting as it is.
    # The whole protection used to be PUT with the socle's shape, and
    # maelys-oci's "require branches to be up to date" went from true to
    # false with a plan that named three replacements and nothing else.
    existing = holds.get("required_status_checks") if state == "ok" else None
    if isinstance(existing, dict):
        method, endpoint = "PATCH", f"repos/{repository}/branches/{branch}/protection/required_status_checks"
        body = {"strict": bool(existing.get("strict")), "contexts": contexts}
        # `strict` goes back as it was read, so it is equal when GitHub
        # answered a boolean; the contexts are equal when the same names
        # are listed, each as many times, in any order. The app bound to
        # each context is neither compared nor verified on this half: the
        # body names contexts alone, and GitHub's reference says a context
        # sent that way may be bound to the app that recently set it. Not
        # sending keeps the bindings the repository has, and skips no write
        # that the list of names needs.
        listed = existing.get("contexts")
        held = (existing.get("strict") is body["strict"] and isinstance(listed, list)
                and unordered(listed) == unordered(contexts))
    elif state == "absent":
        method, endpoint = "PUT", f"repos/{repository}/branches/{branch}/protection"
        body = {**PROTECTION_SHAPE, "required_status_checks": {"strict": False, "contexts": contexts}}
        # Never held: this is sent only where no protection exists, and it
        # creates one, so there is no reading for it to equal.
        held = False
    else:
        return None, False
    return {"method": method, "endpoint": endpoint, "body": body}, held


def carrying_rules(rules: object) -> list[dict]:
    """The rules of a branch that require checks, in the order GitHub lists them."""
    return [rule for rule in (rules if isinstance(rules, list) else [])
            if isinstance(rule, dict) and rule.get("type") == "required_status_checks"]


SEVERAL = "more than one ruleset requires checks of the socle on"


def rulesets_requiring(rules: object) -> list[list[dict]]:
    """The rules that require checks, ruleset by ruleset, in the order GitHub lists them.

    One of them that carries a check of the socle is the one --apply
    writes, wherever it comes in that order; a ruleset that carries none
    is never written, and of several that carry some this command writes
    none.
    """
    found: list[tuple[object, list[dict]]] = []
    for rule in carrying_rules(rules):
        own = next((held for identifier, held in found if identifier == rule.get("ruleset_id")), None)
        if own is None:
            own = []
            found.append((rule.get("ruleset_id"), own))
        own.append(rule)
    return [own for _, own in found]


def ruleset_checks(rules: list, contexts: list[str]) -> list[dict]:
    """The required checks a write gives a ruleset: each context with the integration allowed to report it.

    A context the rules already carry keeps its own, and a new one takes the
    one its neighbours carry most often.
    """
    integration = {}
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                # A check that is not an object naming a context binds
                # nothing: the plan of a branch whose rules listed one
                # crashed here, where `rule_contexts` had read past it.
                if isinstance(check, dict) and isinstance(check.get("context"), str):
                    integration[check["context"]] = check.get("integration_id")
    known = [value for value in integration.values() if value]
    default = max(set(known), key=known.count) if known else None
    return [{"context": name, **({"integration_id": integration.get(name) or default}
                                 if (integration.get(name) or default) else {})}
            for name in contexts]


def checks_held(rules: list, checks: list[dict]) -> bool:
    """Whether every rule that requires checks lists CHECKS already, whatever the order.

    Each check is compared whole, the integration with the context: one that
    GitHub answered with a member the write would not send, or without one
    it would, is not held.
    """
    listed = [(rule.get("parameters") or {}).get("required_status_checks") for rule in rules
              if rule.get("type") == "required_status_checks"]
    return bool(listed) and all(isinstance(held, list) and unordered(held) == unordered(checks) for held in listed)


def ruleset_write(repository: str, own: list[dict], contexts: list[str]) -> tuple[dict, bool, bool]:
    """The write `write_ruleset` would send, whether the rules of the branch list its checks, and whether it may.

    OWN is the rules of the branch that require checks and come from the
    ruleset to write. For the plan, which reads the rules of a branch and
    never the ruleset they come from: the write is named without its body,
    which is that ruleset as it reads at the moment of writing. What the
    rules list is worth a note in a plan and is half of what --apply needs
    to send nothing, since the two readings can differ; and a ruleset of
    the organisation is one --apply refuses, whatever it lists.

    The rules list the checks when those of this ruleset hold what a write
    would give them, and name these contexts and no other: what the
    read-back after a write requires of the same reading. What another
    ruleset requires of its own is neither written nor read back here.
    """
    identifier = own[0].get("ruleset_id") if own else None
    named = [check.get("context") if isinstance(check, dict) else None
             for rule in own for check in (rule.get("parameters") or {}).get("required_status_checks") or []]
    return ({"method": "PUT", "endpoint": f"repos/{repository}/rulesets/{identifier}"},
            bool(own) and checks_held(own, ruleset_checks(own, contexts))
            and all(isinstance(name, str) for name in named) and sorted(named) == sorted(contexts),
            bool(own) and own[0].get("ruleset_source_type") in (None, "Repository"))


def write_ruleset(repository: str, branch: str, rules: object, own: list[dict], contexts: list[str],
                  sent: list[str] | None = None) -> bool:
    """Set the required checks of the ruleset OWN comes from, and say whether it was written.

    OWN is the rules of this branch that require checks and come from one
    ruleset, the one that carries the socle's checks: of several rulesets
    it is the only one written. Only a ruleset of this repository: one
    inherited from the organisation cannot be written with a repository's
    permissions, and saying so is better than a 403 halfway through a
    rename. Every other rule of the ruleset is sent back as it was read,
    and each kept context keeps the integration that was allowed to report
    it. Nothing is sent when the ruleset, as it is read here, and its
    rules of the branch both list these checks already.
    """
    if not own:
        raise Failure("PRECONDITION_FAILED", f"no ruleset of {repository} requires checks on {branch}.",
                      "Nothing to write.")
    if own[0].get("ruleset_source_type") not in (None, "Repository"):
        # Said of the socle's checks where another ruleset requires checks
        # of its own: those do not come from the organisation.
        what = "the checks" if len(own) == len(carrying_rules(rules)) else "the socle's checks"
        raise Failure("PRECONDITION_FAILED",
                      f"{what} {branch} of {repository} requires come from a ruleset of the"
                      " organisation, which a repository cannot write.",
                      "Change it in the organisation's settings, with the list 'protect' prints.")
    identifier = own[0].get("ruleset_id")
    state, ruleset = github_read(f"repos/{repository}/rulesets/{identifier}")
    if state != "ok" or not isinstance(ruleset, dict):
        raise Failure("PROCESS_FAILED", f"ruleset {identifier} of {repository} could not be read.",
                      "Read it with 'gh api repos/OWNER/REPO/rulesets/ID' and retry.")
    checks = ruleset_checks(ruleset.get("rules") or [], contexts)
    # Every other member of the body is the ruleset as it was just read,
    # sent back untouched: its required checks are all this write can
    # change, so a ruleset that lists them already is not written again --
    # when the rules of the branch list them too. A ruleset that holds its
    # checks while the branch does not show them is not one that protects
    # the branch with them: the write is sent and read back as it always
    # was, and fails as it always did when the rules do not follow. Not
    # sending on the ruleset's reading alone passed that state green.
    if ruleset_write(repository, own, contexts)[1] and checks_held(ruleset.get("rules") or [], checks):
        return False
    for rule in ruleset.get("rules") or []:
        if rule.get("type") != "required_status_checks":
            continue
        rule["parameters"]["required_status_checks"] = list(checks)
    body = {key: ruleset[key] for key in ("name", "target", "enforcement", "conditions", "rules",
                                          "bypass_actors") if key in ruleset}
    with tempfile.TemporaryDirectory(prefix="maelys-release-ruleset.") as temp:
        payload = pathlib.Path(temp) / "ruleset.json"
        payload.write_text(json.dumps(body), encoding="utf-8")
        done = host.HOST.write("PUT", f"repos/{repository}/rulesets/{identifier}", payload)
    if done.returncode != 0:
        raise Failure("PROCESS_FAILED", f"ruleset {identifier} of {repository} was refused: {done.stderr.strip()}",
                      "Writing a ruleset needs administration of the repository.")
    if sent is not None:
        sent.append(f"PUT repos/{repository}/rulesets/{identifier}")
    # Re-read, as for a classic protection: every rule but the required
    # contexts must read back as it was, and the contexts as written. The
    # contexts of this ruleset: read from every rule of the branch, a
    # second ruleset requiring a check of its own made a correct write fail
    # on every run. Every rule of every ruleset is still compared for the
    # rest.
    reread = read_protection(repository, branch, classic=False)
    state, after = reread.ruled_state, reread.rules
    if state != "ok" or not isinstance(after, list):
        raise Failure("PROCESS_FAILED",
                      f"ruleset {identifier} of {repository} was written, and GitHub did not answer its re-read.",
                      f"Read it: gh api repos/{repository}/rules/branches/{branch}")
    written = sorted(check.get("context") for rule in after if isinstance(rule, dict)
                     and rule.get("type") == "required_status_checks" and rule.get("ruleset_id") == identifier
                     for check in (rule.get("parameters") or {}).get("required_status_checks") or [])
    if ruleset_shape(after) != ruleset_shape(rules) or written != sorted(contexts):
        raise Failure("PROCESS_FAILED",
                      f"ruleset {identifier} of {repository} changed beyond its required checks, or they did not"
                      " read back as written.",
                      "Compare the ruleset with what protect printed; this is a defect of the socle, report it.")
    return True


def text_protect(data: dict) -> str:
    # A branch is said to be open on both readings of what protects it, and
    # on neither alone: with its rules unread, `is NOT protected` was said
    # of a branch a ruleset may protect.
    lines = [f"branch   {data['branch']} of {data['repository']}"
             + (" is protected by " + " and ".join(data["protectedBy"]) if data.get("protectedBy")
                else " could not be read" if {"the default branch", "the branch protection", "the rulesets"}
                & set(data.get("unread") or [])
                else "" if data["protected"] else " is NOT protected")]
    for what in data.get("unread") or []:
        lines.append(f"UNREAD   GitHub did not answer for {what}: this plan is not a reading, and --apply refuses")
    for name in data.get("dropping") or []:
        lines.append(f"DROP     {name} is required and this plan leaves it out: --apply refuses without --allow-narrow")
    if data.get("dropping"):
        # The flag alone is half an answer. A product renaming a check of
        # its own -- not a leg of the socle's matrix, which has aliases --
        # is stuck twice: the old name blocks every merge, and the new one
        # is proposed only once a *merged* pull request has produced it,
        # which cannot happen while the old one blocks. The way through is
        # two writes, and it is printed where the problem shows.
        lines.append("path     a required check nothing produces blocks every merge, and a name no merged pull"
                     " request has produced yet is never proposed: 'protect DIR --apply --allow-narrow' drops"
                     " those, then merge a pull request, then 'protect DIR --apply' requires what it produced")
    if data.get("ruleset"):
        # Said of a ruleset that carries a check of the socle, which is the
        # one --apply writes. Of any other it was said too, and --apply then
        # wrote the classic protection, or created one beside the ruleset.
        ours = [name for name in data["ruleset"] if name in data["socleContexts"] or socle_owned(name, data["caller"])]
        # Nor of several rulesets that each carry some: the refusal says
        # which, and this line says of them what --apply does.
        several = (data.get("refused") or {}).get("message", "").startswith(SEVERAL)
        lines.append(f"ruleset  requires " + ", ".join(data["ruleset"])
                     + (" — in more than one ruleset: --apply does not choose between them, and writes none"
                        if several else
                        " — --apply writes this ruleset, never a classic protection beside it" if ours else
                        " — none of them the socle's: --apply does not write this ruleset"))
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
        # Outside a narrowing, a required check nothing produces is one
        # this plan drops, unless a mechanism --apply does not write is what
        # requires it: said so, and not as a check --allow-narrow drops.
        other = "" if name in (data.get("dropping") or []) else (
            " by a ruleset this command does not write" if name in (data.get("before") or {}).get("rulesets", [])
            else " by the classic protection, which this command does not write here")
        lines.append(f"STALE    {name} is required{other}{',' if other else ''} and no run on {data['branch']}"
                     " produces it: a pull request would wait for it forever")
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
    refused = data.get("refused")
    if refused:
        lines.append(f"{'REFUSED':<8} {refused['message']} {refused['hint']}")
    lines.extend(text_environment(data.get("environment")))
    # As the environment says it: a plan whose write would change nothing
    # does not ask for --apply, and an --apply that sent nothing did not
    # apply. An empty `writes` is not enough to say so: it is empty too
    # where --apply would refuse, and on a reading that is not there.
    lines.append("protect: " + ("applied" if data["applied"] else "refused; the protection is not written"
                                if refused else "nothing to change" if data.get("nothingToChange")
                                else "nothing written; add --apply"))
    return "\n".join(lines) + "\n"
