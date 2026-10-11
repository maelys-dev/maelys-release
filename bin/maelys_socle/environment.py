# SPDX-License-Identifier: MPL-2.0
"""Arm the release environment: one tag rule written, nothing it carried erased.

The publish job of a release runs in an environment whose deployment policy
admits tags `v*` and nothing else. `preflight` has verified that since
0.61.0 and the socle wrote none of it: the two calls were in the conventions,
to be copied by hand, and the first release of a product ran the second one
twice, then both again when its repository was created anew.

`protect` writes it now, as a half of its own beside the branch's
protection: its own reads, its own refusals, its own writes and its own
read-back. What one half refuses does not stop the other -- a repository
whose protection this command cannot write still gets its environment.
The reading is the one `preflight` verifies, `github.read_release_environment`.

- Nothing is written on a reading that is not there, nor beside a policy
  somebody else put in the list: this adds the tag rule and removes nothing.
- An environment is created only when two endpoints say it is absent.
- An environment that exists is not PUT while its policy is already custom,
  and when a PUT is needed it carries back every protection that was read.
  GitHub's reference says what the endpoint sets and not what it leaves of a
  member it omits, so nothing here relies on an omission.
- The list is read again immediately before the rule is posted, and every
  write is read back; a protection that moved stops the command.

What this cannot do is hold GitHub still: a change somebody else makes
between the last read and the write is not prevented. It is detected by
the read-back, and said.

The reviewer is never this command's to choose: `[gate]` says whether one
is wanted, the repository says who, and what is printed is the gap.
"""
from __future__ import annotations

import json
import pathlib
import tempfile

from maelys_cli import Failure

from . import host
from .constants import PROGRAM
from .github import (CUSTOM_POLICY, RELEASE_ENVIRONMENT, TAG_RULE, ReleaseEnvironment, carried, comparable,
                     environment_gate, environments_named, read_release_environment, sent_back,
                     widening_verdicts, with_policies)
from .texts import LABELS


class Refusal(Exception):
    """This half will not write, or will write no further: a result, never an error."""


def named(entries: list[dict] | tuple) -> str:
    return ", ".join(f"the {entry.get('type') or 'policy'} {entry.get('name') or '?'}" for entry in entries)


def said(reading: ReleaseEnvironment) -> str:
    """What the environment admits today, in one sentence that is true of it."""
    repository = reading.repository
    if reading.unreadable:
        return f"environment release of {repository} could not be read ({reading.state})"
    if reading.missing:
        return f"environment release does not exist in {repository}"
    if reading.protected_only:
        return f"environment release of {repository} admits protected branches only, and no tag"
    if not reading.custom:
        return (f"environment release of {repository} has no deployment policy: every branch and every tag"
                " is admitted")
    if not reading.complete:
        return f"environment release of {repository} has custom policies, and GitHub did not give the whole list"
    if not reading.entries:
        return f"environment release of {repository} has custom policies and an empty list: nothing is admitted"
    if reading.tag_rule and not reading.widening:
        return f"environment release of {repository} limits deployments to tags v*"
    return f"environment release of {repository} admits " + named(reading.entries)


def describe(reading: ReleaseEnvironment, gate: str, witness: tuple[bool, list[str]] | None = None) -> dict:
    """What the environment is, and the writes that would make it what a release needs."""
    repository = reading.repository
    holds = carried(reading.environment)
    report: dict = {
        "name": RELEASE_ENVIRONMENT,
        "exists": False if reading.missing else None if reading.unreadable else True,
        "policy": (None if not reading.environment else "custom" if reading.custom
                   else "protected-branches" if reading.protected_only else "all"),
        "rules": [{"id": entry.get("id"), "type": entry.get("type"), "name": entry.get("name")}
                  for entry in reading.entries],
        "holds": holds,
        "state": said(reading),
        "writes": [], "refused": [], "unread": [], "gate": [], "applied": False, "written": []}
    put = {"method": "PUT", "endpoint": reading.endpoint, "body": {"deployment_branch_policy": dict(CUSTOM_POLICY)}}
    post = {"method": "POST", "endpoint": reading.listing, "body": dict(TAG_RULE)}
    if reading.unreadable:
        report["unread"].append("the release environment")
    elif reading.missing:
        # One 404 is one witness. An environment is created on two: its own
        # read, and the list of the repository's environments read whole.
        whole, names = witness if witness is not None else (False, [])
        if not whole:
            report["unread"].append("the list of the repository's environments, which must confirm that"
                                    " release is absent before it is created")
        elif RELEASE_ENVIRONMENT in names:
            report["refused"].append(
                f"GitHub answered 404 for environment release of {repository} and names it in the list of the"
                " repository's environments: one of the two readings is wrong, and creating over an"
                " environment that exists could erase what it carries")
        else:
            # Nothing exists, so nothing can be erased: the policy alone.
            report["writes"] = [put, post]
            report["becomes"] = "created, limited to tags v* and nothing else"
    elif not reading.custom:
        back, lost = sent_back(holds)
        beside = [entry for entry in reading.entries if entry not in reading.tag_rule]
        if lost:
            report["refused"].append(
                f"{said(reading)}, and it carries " + ", ".join(lost)
                + f": {PROGRAM} cannot send that back as it read it, and a PUT that omits it may erase it."
                " Limit the environment to tags v* in the repository's settings instead")
        elif reading.refused not in ("", "absent"):
            report["unread"].append("the deployment policies of the release environment")
        elif beside:
            # GitHub gave a list for an environment that does not use one:
            # the moment the policy is custom, that list is the rule.
            report["refused"].append(
                f"{said(reading)}, and GitHub keeps a list of policies for it that holds {named(beside)}:"
                " limiting the environment to its list would make that the rule. Remove "
                + " and ".join(f"'gh api -X DELETE {reading.listing}/{entry.get('id', '?')}'" for entry in beside)
                + " first")
        else:
            put["body"].update(back)
            report["writes"] = [put, post]
            report["becomes"] = ("limited to tags v* and nothing else: the PUT replaces "
                                 + ("the limit to protected branches" if reading.protected_only
                                    else "the absence of a limit") + ", it does not add to it")
            # Measured: the listing answers 404 while the policy is not
            # custom, so what GitHub kept there is known only after the PUT.
            report["note"] = ("GitHub does not list the policies of an environment that has no custom ones:"
                              " the list is read after the PUT. The tag rule is added unless the list already"
                              " holds it; if it holds anything else, the policy is put back as it was read"
                              " and nothing is added")
    else:
        if not reading.complete:
            report["unread"].append("the deployment policies of the release environment")
        report["refused"].extend(message for _, message in widening_verdicts(reading))
        if reading.complete and not reading.widening and not reading.tag_rule:
            report["writes"] = [post]
            report["becomes"] = "limited to tags v* and nothing else"
    # The gate, against the environment as --apply leaves it: one that does
    # not exist yet requires nobody once created, and only a gap is worth a
    # line before it exists.
    to_be = reading.environment
    if report["writes"]:
        to_be = {**(reading.environment or {"protection_rules": []}), "deployment_branch_policy": dict(CUSTOM_POLICY)}
    report["gate"] = [{"status": status, "message": message}
                      for status, message in environment_gate(repository, to_be, gate)
                      if reading.environment or status != "ok"]
    return report


def environment_plan(repository: str, gate: str) -> tuple[ReleaseEnvironment, dict]:
    """Read the release environment once, and say what `--apply` would write."""
    reading = read_release_environment(repository)
    witness = None
    if reading.missing:
        witness = environments_named(repository)
    elif reading.state == "ok" and not reading.custom:
        # The list, whatever the policy mode: see with_policies.
        reading = with_policies(reading)
    return reading, describe(reading, gate, witness)


def violations(report: dict | None) -> bool:
    """Whether the environment keeps something `--apply` does not settle."""
    return bool(report) and (bool(report["refused"]) or any(line["status"] == "fail" for line in report["gate"]))


def send(write: dict, project: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory(prefix="maelys-release-environment.") as temp:
        payload = pathlib.Path(temp) / "environment.json"
        payload.write_text(json.dumps(write["body"]), encoding="utf-8")
        done = host.HOST.write(write["method"], write["endpoint"], payload, cwd=project)
    if done.returncode != 0:
        raise Failure("PROCESS_FAILED",
                      f"{write['method']} {write['endpoint']} was refused: {done.stderr.strip()}",
                      "Writing an environment needs administration of the repository, and on a private"
                      " repository a plan that offers environments.")


def confirm(after: ReleaseEnvironment, baseline: dict | None, policy: dict | None = CUSTOM_POLICY) -> None:
    """Refuse unless the environment reads back with POLICY, whole, and carrying what it carried.

    Who reviews is compared as a set of (type, id): GitHub owes nobody the
    order of that list, and a display name is not an identity.
    """
    repository = after.repository
    read_it = f"Read it: gh api {after.endpoint}"
    if after.missing or after.unreadable:
        raise Failure("PROCESS_FAILED",
                      f"environment release of {repository} was written, and GitHub did not answer its"
                      " re-read: nothing confirms what it holds now.", read_it + ".")
    was, held = comparable(baseline), comparable(carried(after.environment))
    moved = [f"{key}: {json.dumps(was[key])} -> {json.dumps(held[key])}" for key in was or {}
             if was[key] != held[key]]
    if moved:
        raise Failure("PROCESS_FAILED",
                      f"environment release of {repository} changed beyond its deployment policy: "
                      + "; ".join(moved) + ".",
                      "Restore those settings by hand, from the values on the left. Either the write moved"
                      " them, which is a defect of the socle to report, or somebody changed the environment"
                      " between the read and the write, which this command detects and cannot prevent.")
    if after.policy != policy:
        raise Failure("PROCESS_FAILED",
                      f"environment release of {repository} was written and does not read back with the"
                      f" deployment policy that was sent: {json.dumps(after.policy)}.", read_it + ".")
    if after.custom and not after.complete:
        raise Failure("PROCESS_FAILED",
                      f"environment release of {repository} was written, and GitHub did not give the whole"
                      " list of its policies: nothing confirms what may publish.",
                      f"Read it: gh api {after.listing}.")


def put_back(reading: ReleaseEnvironment, report: dict, project: pathlib.Path) -> None:
    """Send the deployment policy back as READING held it, and require it to read back so.

    For the one refusal that can only come after a write: an environment
    made custom whose list turned out to hold something else. Leaving it
    custom would make that something the rule -- a branch admitted and no
    tag -- so the policy goes back to what it was, with everything the
    environment carried.
    """
    body = {"deployment_branch_policy": reading.policy, **sent_back(report["before"]["holds"])[0]}
    send({"method": "PUT", "endpoint": reading.endpoint, "body": body}, project)
    report["written"].append(f"PUT {reading.endpoint} (the deployment policy put back as it was read)")
    confirm(read_release_environment(reading.repository), report["before"]["holds"], reading.policy)


def arm(reading: ReleaseEnvironment, report: dict, gate: str, project: pathlib.Path) -> None:
    """Send the planned writes, each decided on a reading and each read back.

    Raises Refusal when this half will not write, or will write no further;
    Failure when a write was refused by GitHub or did not read back.

    The list is read again immediately before the tag rule is posted, on
    every path, and the rule is posted only on a list that holds nothing:
    the product this was written for ran that call twice by hand, and the
    plan's reading is older than the write by however long GitHub took.
    """
    if report["refused"]:
        return
    repository = reading.repository
    if report["unread"]:
        raise Refusal(f"GitHub did not answer for {', '.join(report['unread'])} of {repository}: nothing is"
                      " written on a reading that is not there")
    if not report["writes"]:
        return
    held = report["holds"]
    report["before"] = {key: report[key] for key in ("exists", "policy", "rules", "holds")}
    planned = {write["method"]: write for write in report["writes"]}
    changed = False
    if "PUT" in planned:
        if held is None:
            # The absence, asked again of both witnesses at the moment of
            # creating: nothing would be compared after a PUT over an
            # environment this run believed absent.
            again = read_release_environment(repository)
            whole, names = environments_named(repository)
            if not again.missing or not whole or RELEASE_ENVIRONMENT in names:
                raise Refusal(f"environment release of {repository} is no longer confirmed absent by both of"
                              " GitHub's answers: nothing is created over what may exist. Run protect again"
                              " and read the plan")
        send(planned["PUT"], project)
        report["written"].append(f"PUT {reading.endpoint}")
        confirm(read_release_environment(repository), held)
        changed = held is not None
    current = read_release_environment(repository)
    if current.missing or current.unreadable or not current.custom or not current.complete:
        raise Refusal(f"environment release of {repository} could not be read whole immediately before the"
                      f" tag rule was added ({said(current)}): the rule is not posted on a list that was not"
                      " read")
    if current.widening:
        if changed:
            put_back(reading, report, project)
        raise Refusal("; ".join(message for _, message in widening_verdicts(current))
                      + (". GitHub kept that list from an earlier custom policy and gives it only once the"
                         " policy is custom: the environment was put back as it was read, and read back so"
                         if changed else
                         ". That list is not the one the plan read: the tag rule is not added beside it"))
    if not current.tag_rule:
        baseline = carried(current.environment)
        send(planned["POST"], project)
        report["written"].append(f"POST {current.listing}")
        current = read_release_environment(repository)
        confirm(current, baseline)
    if len(current.entries) != 1 or len(current.tag_rule) != 1:
        raise Failure("PROCESS_FAILED",
                      f"environment release of {repository} was written and does not read back with the tag"
                      f" rule v* alone: it holds {len(current.entries)} policies.",
                      f"Read them: gh api {current.listing}. If one of them is not this command's, somebody"
                      " added it between the read and the write.")
    after = describe(current, gate)
    report.update({key: after[key] for key in ("exists", "policy", "rules", "holds", "state", "gate")})
    report.pop("becomes", None)
    report["applied"] = True


def record(report: dict | None) -> str:
    """What this half wrote and what it replaced, for an error that hides the report."""
    if not report or not report["written"]:
        return ""
    return (" The environment half had written: " + "; ".join(report["written"]) + ". Before, it was "
            + json.dumps(report.get("before"), sort_keys=True) + ".")


def text_environment(report: dict | None) -> list[str]:
    """The lines of the plan that are about the environment, after the branch's.

    No FAIL on what `--apply` settles: a line says what is, `then` what the
    writes make of it. FAIL and REFUSED are for what stands after them.
    """
    if not report:
        return []
    lines = [f"{'now':<8} {report['state']}"]
    for what in report["unread"]:
        lines.append(f"{'UNREAD':<8} GitHub did not answer for {what}: this plan is not a reading, and --apply"
                     " writes nothing to the environment")
    for message in report["refused"]:
        lines.append(f"{'REFUSED':<8} {message}")
    for line in report["written"]:
        lines.append(f"{'written':<8} {line}, and read back")
    if report["written"]:
        lines.append(f"{'before':<8} {json.dumps(report['before'], sort_keys=True)}")
    elif report["writes"]:
        lines.append(f"{'then':<8} {report.get('becomes', '')}")
        for write in report["writes"]:
            lines.append(f"{'write':<8} {write['method']} {write['endpoint']} {json.dumps(write['body'])}")
        if report.get("note"):
            lines.append(f"{'note':<8} {report['note']}")
    holds = report.get("holds")
    carries = [] if not holds else (
        ([f"reviewers {' '.join(entry['name'] for entry in holds['reviewers'])}"] if holds["reviewers"] else [])
        + ([f"a wait of {holds['waitTimer']} minutes"] if holds["waitTimer"] else [])
        + (["no bypass for administrators"] if holds["adminsBypass"] is False else []))
    if (report["writes"] or report["written"]) and carries:
        lines.append(f"{'keeps':<8} what the environment carries, sent back as read and read again after the"
                     " write: " + ", ".join(carries))
    lines.extend(f"{LABELS.get(line['status'], line['status']):<8} {line['message']}" for line in report["gate"])
    if report["applied"]:
        lines.append("environment: applied")
    elif report["refused"]:
        lines.append("environment: refused" + (", after the writes above" if report["written"]
                                               else "; nothing written"))
    elif report["unread"]:
        lines.append("environment: not read; nothing written")
    elif report["writes"]:
        lines.append("environment: nothing written; add --apply")
    else:
        lines.append("environment: nothing to change")
    return lines
