# SPDX-License-Identifier: MPL-2.0
"""The catalog: every command of the socle, its operands, options and output schema, and the handler
that answers it.

One catalog drives the parser, help, describe and completion. It is built for one entry point: each
handler is given that entry point's context, the socle it runs from, and nothing else of it.
"""
from __future__ import annotations

import functools
from typing import Callable

import maelys_cli as cli
from maelys_cli import Invocation

from .. import adoption, authorship, cut, dependencies, inspection, migration, protect, rehearse, signers
from .. import socle_origin
from ..commands import new, preflight, render, self_test, vendor
from ..constants import DEFAULT_IMAGE, PLATFORMS, PRODUCT, PROGRAM
from ..context import Context
from ..tap import handle_tap
from .program import SocleProgram
from .schemas import (
    CHECKS_SCHEMA, CHECK_SCHEMA, CUT_SCHEMA, DIR_OPERAND, DOCS_CONTRACT_OPTION, FILES_SCHEMA, MECHANISM_OPTION,
    MECHANISM_SCHEMA, PRIVACY_SCHEMA, SOCLE_OPTIONS, SOCLE_SCHEMA, STATUS_LINE, STRINGS,
)
from .texts import TEXT


def build(context: Context) -> SocleProgram:
    """The program of one entry point: every handler answers with that entry point's context."""

    def bound(handler: Callable) -> Callable[[Invocation], tuple]:
        @functools.wraps(handler)
        def answer(invocation: Invocation) -> tuple:
            return handler(invocation, context)
        return answer

    handle_migrate = bound(migration.handle_migrate)
    handle_new = bound(new.handle_new)
    handle_adopt = bound(adoption.handle_adopt)
    handle_dependencies = bound(dependencies.handle_dependencies)
    handle_check = bound(inspection.handle_check)
    handle_declarations = bound(inspection.handle_declarations)
    handle_preflight = bound(preflight.handle_preflight)
    handle_signers = bound(signers.handle_signers)
    handle_protect = bound(protect.handle_protect)
    handle_rehearse = bound(rehearse.handle_rehearse)
    handle_self_test = bound(self_test.handle_self_test)
    handle_vendor = bound(vendor.handle_vendor)
    handle_render = render.handle_render

    def handle_cut(invocation: Invocation) -> tuple:
        # cut judges a tag against the signers of the socle the product pins.
        return cut.handle_cut(invocation, context, lambda project: socle_origin.release_signers(context, project))

    return SocleProgram(PROGRAM, PRODUCT, context.socle_version(), [
        cli.transaction("migrate", "migrate",
                        "Move a product's prose into the documentation repository with its history and out of the product;"
                        " plan without --apply.",
                        handle_migrate, operands=[DIR_OPERAND],
                        options=[SOCLE_OPTIONS[0],
                                 cli.option("--documents", "JSONL of the documents to move, '-' for stdin"
                                            " (maelys-platform docs --prose --only PRODUCT --format jsonl).",
                                            cli.argument("FILE"), default="-"),
                                 cli.option("--documents-repository", "Repository the prose moves to.",
                                            cli.argument("REPOSITORY")),
                                 cli.option("--site", "Public site of the product the README points at"
                                            " (an https URL); without it the README says only that the"
                                            " prose left.", cli.argument("URL")),
                                 cli.flag("--push", "Send both branches; without it nothing leaves this machine.")],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "product", "project", "repository", "version", "tag", "commit",
                                             "moving", "staying", "documents"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "product": {"type": "string"},
                                               "project": {"type": "string"}, "repository": {"type": "string"},
                                               "version": {"type": "string"}, "tag": {"type": "string"},
                                               "commit": {"type": "string"}, "documents": {"type": "integer"},
                                               "site": {"type": "string"},
                                               "moving": {"type": "array", "items": {
                                                   "type": "object", "required": ["path", "destination", "referencedBy"],
                                                   "properties": {"path": {"type": "string"},
                                                                  "destination": {"type": "string"},
                                                                  "referencedBy": STRINGS}}},
                                               "staying": {"type": "array", "items": {
                                                   "type": "object", "required": ["path", "reason"],
                                                   "properties": {"path": {"type": "string"},
                                                                  "reason": {"type": "string"}}}},
                                               "readme": {"type": "string"},
                                               "rewritten": STRINGS,
                                               "remaining": {"type": "array", "items": {
                                                   "type": "object", "required": ["path", "line", "names"],
                                                   "properties": {"path": {"type": "string"},
                                                                  "line": {"type": "integer"},
                                                                  "names": {"type": "string"}}}},
                                               "globs": {"type": "array", "items": {
                                                   "type": "object", "required": ["path", "line", "names"],
                                                   "properties": {"path": {"type": "string"},
                                                                  "line": {"type": "integer"},
                                                                  "names": {"type": "string"}}}},
                                               "outward": {"type": "integer"},
                                               "outwardLeft": {"type": "array", "items": {
                                                   "type": "object", "required": ["path", "names"],
                                                   "properties": {"path": {"type": "string"},
                                                                  "names": {"type": "string"}}}},
                                               "documentsBranch": {"type": "string"}, "documentsCommit": {"type": "string"},
                                               "productBranch": {"type": "string"}, "productCommit": {"type": "string"},
                                               "pushed": {"type": "boolean"}, "kept": {"type": "string"}}}),
        cli.transaction("new", "new",
                        "Create a Maelys repository in an empty directory: licence, version, changelog, pins,"
                        " and the files of the socle; plan without --apply.",
                        handle_new,
                        operands=[cli.operand("DIR", "Directory to create the repository in; empty, or absent.")],
                        options=[*SOCLE_OPTIONS, MECHANISM_OPTION,
                                 cli.option("--depends", "A pinned Maelys dependency, NAME@vX.Y.Z; repeatable.",
                                            cli.argument("DEPENDENCY"), repeatable=True)],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "product", "project", "mechanism", "version", "dependencies",
                                             "files", "adopted"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "product": {"type": "string"},
                                               "project": {"type": "string"}, "mechanism": MECHANISM_SCHEMA,
                                               "version": {"type": "string"}, "socle": SOCLE_SCHEMA,
                                               "dependencies": {"type": "array", "items": {
                                                   "type": "object", "required": ["name", "tag", "commit"],
                                                   "properties": {"name": {"type": "string"}, "tag": {"type": "string"},
                                                                  "commit": {"type": "string"}}}},
                                               "files": FILES_SCHEMA, "adopted": FILES_SCHEMA,
                                               "notes": STRINGS}}),
        cli.transaction("adopt", "adopt",
                        "Write the conventions of the socle into a product, and its release files when it publishes"
                        " through the socle; plan without --apply.",
                        handle_adopt, operands=[DIR_OPERAND],
                        options=[*SOCLE_OPTIONS, MECHANISM_OPTION,
                                 cli.flag("--allow-untagged", "Accept a socle commit without a tag (a trial of a candidate)."),
                                 cli.flag("--allow-lock", "Adopt even when a required check would stop being produced.")],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "product", "project", "mechanism", "socle", "changed", "checks", "files"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "product": {"type": "string"},
                                               "project": {"type": "string"}, "mechanism": MECHANISM_SCHEMA,
                                               "socle": SOCLE_SCHEMA,
                                               "changed": {"type": "boolean"}, "checks": CHECKS_SCHEMA,
                                               "moved": {"type": "string"}, "files": FILES_SCHEMA,
                                               "hook": {"type": "string"},
                                               "impactFrom": {"type": "string"},
                                               "impact": {"type": "array", "items": {
                                                   "type": "object", "required": ["version", "says", "asks", "asksThis"],
                                                   "properties": {"version": {"type": "string"},
                                                                  "says": {"type": "string"},
                                                                  "asks": {"type": ["array", "null"],
                                                                           "items": {"type": "string"}},
                                                                  "asksThis": {"type": ["boolean", "null"]},
                                                                  "supersededBy": {"type": ["string", "null"]},
                                                                  "writes": {"type": "array",
                                                                             "items": {"type": "string"}}}}},
                                               "current": {"type": ["boolean", "null"]}}}),
        cli.read("check", "check",
                 "Report the conventions of a product and, when it publishes through the socle, its release mechanism;"
                 " exit 2 on any violation.",
                 handle_check, operands=[DIR_OPERAND],
                 options=[*SOCLE_OPTIONS, MECHANISM_OPTION, DOCS_CONTRACT_OPTION,
                          cli.flag("--history", "Read every ref of the clone as well -- each blob once, commit messages,"
                                   " author and committer lines -- for what the tracked files are read for; says what"
                                   " it could not read.")],
                 schema=CHECK_SCHEMA),
        cli.read("outgoing", "outgoing",
                 "Report the commits about to leave whose author or committer address git made up on this machine;"
                 " exit 2 when there is one.",
                 authorship.handle_outgoing, operands=[DIR_OPERAND],
                 options=[cli.option("--pre-push", "Judge what a push sends to REMOTE, from the refs git hands a"
                                     " pre-push hook on stdin, instead of what HEAD has ahead of its upstream.",
                                     cli.argument("REMOTE"))],
                 schema={"type": "object", "additionalProperties": False,
                         "required": ["valid", "project", "source", "scope", "commits", "fabricated", "local",
                                      "renamed", "tags", "violations", "notes", "fix"],
                         "properties": {"valid": {"type": "boolean"}, "project": {"type": "string"},
                                        "source": {"enum": ["head", "pre-push"]},
                                        "scope": {"enum": ["upstream", "untracked", "push"]},
                                        "remote": {"type": "string"},
                                        "commits": {"type": "integer"},
                                        "fabricated": {"type": "array", "items": {
                                            "type": "object", "additionalProperties": False,
                                            "required": ["commit", "rules"],
                                            "properties": {"commit": {"type": "string"}, "rules": STRINGS}}},
                                        "local": {"type": "array", "items": {
                                            "type": "object", "additionalProperties": False,
                                            "required": ["commit", "rules"],
                                            "properties": {"commit": {"type": "string"}, "rules": STRINGS}}},
                                        "renamed": STRINGS, "tags": STRINGS, "violations": STRINGS,
                                        "notes": STRINGS, "fix": {"type": "string"}}}),
        cli.transaction("hook", "hook",
                        "Write the optional pre-push hook that refuses a push sending an address git made up on this"
                        " machine; plan without --apply.",
                        authorship.handle_hook, operands=[DIR_OPERAND],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "project", "path", "action", "wrote", "hook"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "project": {"type": "string"},
                                               "path": {"type": "string"}, "wrote": {"type": "boolean"},
                                               "action": {"enum": ["create", "same", "kept"]},
                                               "reason": {"type": "string"}, "hook": {"type": "string"}}}),
        cli.read("declarations", "declarations",
                 "Return the product contract as data: pinned dependencies, packages, formulas; exit 2 when invalid.",
                 handle_declarations, operands=[DIR_OPERAND], options=[SOCLE_OPTIONS[0]],
                 schema={"type": "object", "additionalProperties": False,
                         "required": ["valid", "product", "project", "version", "mechanism", "dependencies",
                                      "linuxPackages", "macosPackages", "formulas", "targets",
                                      "manifestPatterns", "channels", "gate", "afterVersion", "fuzz",
                                      "declared", "pinned", "managedBy", "runners", "workflows", "carried",
                                      "unreleased", "checks"],
                         "properties": {"valid": {"type": "boolean"}, "product": {"type": "string"},
                                        "project": {"type": "string"}, "version": {"type": "string"},
                                        "mechanism": MECHANISM_SCHEMA,
                                        "dependencies": STRINGS,
                                        "unreleased": {"type": "array", "items": {
                                            "type": "object", "additionalProperties": False,
                                            "required": ["name", "repository", "branch", "tag", "commit"],
                                            "properties": {"name": {"type": "string"},
                                                           "repository": {"type": "string"},
                                                           "branch": {"type": "string"},
                                                           "tag": {"type": "string"},
                                                           "commit": {"type": "string"}}}},
                                        "linuxPackages": {"type": "string"},
                                        "macosPackages": {"type": "string"}, "formulas": STRINGS,
                                        "targets": STRINGS, "manifestPatterns": {"type": "string"},
                                        "channels": {"type": "array", "items": {
                                            "type": "object", "additionalProperties": False,
                                            "required": ["name", "registry"],
                                            "properties": {"name": {"type": "string"},
                                                           "registry": {"type": "string"}}}},
                                        "gate": {"enum": ["", "reviewer", "none"]},
                                        "afterVersion": {"type": "string"},
                                        "declared": {"type": "object", "additionalProperties": False,
                                                     "required": ["targets", "manifestPatterns"],
                                                     "properties": {"targets": STRINGS,
                                                                    "manifestPatterns": STRINGS}},
                                        "pinned": {"type": ["object", "null"]},
                                        "managedBy": {"type": "string"},
                                        "workflows": {"type": "array", "items": {
                                            "type": "object", "additionalProperties": False,
                                            "required": ["file", "events", "branches", "tags", "jobs",
                                                         "runners", "unresolved", "delegates"],
                                            "properties": {"file": {"type": "string"}, "events": STRINGS,
                                                           "branches": STRINGS, "tags": STRINGS,
                                                           "jobs": STRINGS, "runners": STRINGS,
                                                           "unresolved": STRINGS,
                                                           "delegates": {"type": "boolean"}}}},
                                        "carried": {"type": "object", "additionalProperties": False,
                                                    "required": ["dependencies", "calls", "unresolved"],
                                                    "properties": {
                                                        "dependencies": STRINGS,
                                                        "calls": {"type": "array", "items": {
                                                            "type": "object", "additionalProperties": False,
                                                            "required": ["file", "job", "dependencies", "runner"],
                                                            "properties": {"file": {"type": "string"},
                                                                           "job": {"type": "string"},
                                                                           "dependencies": STRINGS,
                                                                           "runner": STRINGS}}},
                                                        "unresolved": STRINGS}},
                                        "runners": {"type": "object", "additionalProperties": False,
                                                    "required": ["labels", "unresolved", "delegated"],
                                                    "properties": {"labels": STRINGS,
                                                                   "unresolved": STRINGS,
                                                                   "delegated": {"type": "boolean"}}},
                                        "fuzz": {"type": "object", "additionalProperties": False,
                                                 "required": ["harnesses", "runs"],
                                                 "properties": {
                                                     "harnesses": {"type": "string"},
                                                     "runs": {"enum": ["socle", "own", "none"]}}},
                                        "checks": CHECKS_SCHEMA}}),
        cli.read("preflight", "preflight",
                 "Check, then report what the release workflow would refuse of the next tag; exit 2 when not ready."
                 " It reads the configuration and the publication record, and builds nothing itself.",
                 handle_preflight, operands=[DIR_OPERAND], options=[*SOCLE_OPTIONS, DOCS_CONTRACT_OPTION],
                 schema={"type": "object", "additionalProperties": False,
                         "required": ["ready", "valid", "product", "project", "socle", "pinned", "checks", "files",
                                      "preflight", "violations"],
                         "properties": {"ready": {"type": "boolean"}, "valid": {"type": "boolean"},
                                        "product": {"type": "string"}, "project": {"type": "string"},
                                        "socle": SOCLE_SCHEMA, "pinned": {"type": ["object", "null"]},
                                        "checks": CHECKS_SCHEMA, "files": FILES_SCHEMA,
                                        "preflight": CHECKS_SCHEMA, "violations": STRINGS,
                                        "privacy": PRIVACY_SCHEMA}}),
        cli.execute("signers", "signers",
                    "List the keys that may sign a release of this fleet, or prepare one line of that file;"
                    " --apply writes it.",
                    handle_signers, operands=[DIR_OPERAND],
                    options=[cli.option("--add", "Public key file to name, with --principal.",
                                        cli.argument("FILE", "path"), conflicts_with=("--retire",)),
                             cli.option("--principal", "The identity that key signs as, as in an email.",
                                        cli.argument("NAME", "string"), requires=("--add",)),
                             cli.option("--retire", "Principal or SHA256 fingerprint to stop from signing new tags.",
                                        cli.argument("WHO", "string"), conflicts_with=("--add",)),
                             cli.option("--at", "When it stopped being trusted, YYYYMMDD[HHMM[SS]]Z; now by default.",
                                        cli.argument("TIME", "string"), requires=("--retire",)),
                             cli.flag("--apply", "Write the file instead of only reporting what it would become.")],
                    schema={"type": "object", "additionalProperties": False,
                            "required": ["file", "action", "signers", "wrote", "next"],
                            "properties": {"file": {"type": "string"},
                                           "action": {"enum": ["list", "add", "retire"]},
                                           "wrote": {"type": "boolean"}, "diff": {"type": "boolean"},
                                           "line": {"type": "string"}, "retired": STRINGS, "next": STRINGS,
                                           "signers": {"type": "array", "items": {
                                               "type": "object", "additionalProperties": False,
                                               "required": ["principal", "options", "type", "fingerprint",
                                                            "signsToday", "refusal"],
                                               "properties": {"principal": {"type": "string"},
                                                              "options": {"type": "string"},
                                                              "type": {"type": "string"},
                                                              "fingerprint": {"type": "string"},
                                                              "signsToday": {"type": "boolean"},
                                                              "refusal": {"type": "string"}}}}}}),
        cli.transaction("cut", "cut",
                        "Open the release of a version, then sign its tag on the merge commit whose checks passed;"
                        " plan without --apply.",
                        handle_cut, commit=True,
                        operands=[DIR_OPERAND, cli.operand("VERSION", "The version this release carries, X.Y.Z.")],
                        options=[SOCLE_OPTIONS[0], MECHANISM_OPTION,
                                 cli.flag("--tag", "The second stop: read the merged pull request and sign the tag."),
                                 cli.option("--message", "File holding the tag's annotation; the CHANGELOG entry of the"
                                            " version by default.", cli.argument("FILE"), requires=("--tag",)),
                                 cli.option("--timeout", "Minutes to wait for the checks of a commit.",
                                            cli.argument("MINUTES", "integer", minimum=1, maximum=240), default="30"),
                                 cli.option("--poll", "Seconds between two readings of the checks.",
                                            cli.argument("SECONDS", "integer", minimum=5, maximum=300), default="15",
                                            hidden=True)],
                        schema=CUT_SCHEMA),
        cli.transaction("dependencies", "dependencies",
                        "Materialise the product's pinned dependencies at their commits in one directory apart"
                        " from any working copy, refreshing what is already there; plan without --apply.",
                        handle_dependencies, operands=[DIR_OPERAND],
                        options=[SOCLE_OPTIONS[0],
                                 cli.option("--directory", "Where the checkouts go;"
                                            " ~/.cache/maelys-release/dependencies/PRODUCT by default.",
                                            cli.argument("DIR")),
                                 cli.option("--all", "Also the pins marked on-request, which a job clones"
                                            " by name and this command skips otherwise.")],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "product", "project", "directory", "variable",
                                             "declared", "dependencies", "notes", "blocked"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "product": {"type": "string"},
                                               "project": {"type": "string"}, "directory": {"type": "string"},
                                               "dependencies": {"type": "array", "items": {
                                                   "type": "object", "additionalProperties": False,
                                                   "required": ["name", "tag", "commit", "repository", "path",
                                                                "action"],
                                                   "properties": {"name": {"type": "string"},
                                                                  "tag": {"type": "string"},
                                                                  "commit": {"type": "string"},
                                                                  "repository": {"type": "string"},
                                                                  "path": {"type": "string"},
                                                                  "action": {"enum": ["clone", "refresh", "same",
                                                                                      "blocked", "on-request"]},
                                                                  "submodules": {"enum": ["", "recursive"]},
                                                                  "unreleased": {"type": "string"},
                                                                  "asked": {"type": "boolean"},
                                                                  "previous": {"type": "string"},
                                                                  "reason": {"type": "string"}}}},
                                               "variable": {"type": "string"},
                                               "declared": {"type": "boolean"},
                                               "notes": STRINGS, "blocked": {"type": "boolean"}}}),
        cli.execute("protect", "protect",
                    "Report what the default branch should require, derived from the workflows and the runs it"
                    " carries rather than typed by hand, and what the release environment of a product the"
                    " socle releases should admit; --apply writes each, and a refusal of one does not stop"
                    " the other.",
                    handle_protect,
                    operands=[DIR_OPERAND],
                    options=[cli.flag("--apply", "Write the protection and the release environment instead of"
                                      " only reporting them; exit 2 when either is refused."),
                             cli.flag("--allow-lock", "Require names no merged pull request has produced yet."),
                             cli.flag("--allow-narrow", "Stop requiring checks the plan leaves out, beyond alias replacements."),
                             cli.flag("--without-legs", "Propose the required list minus the socle's matrix legs:"
                                      " the narrowing that comes before an adoption renames them. The release"
                                      " environment is neither read nor written.")],
                    schema={"type": "object", "additionalProperties": False,
                            "required": ["repository", "branch", "proposed"],
                            "properties": {"repository": {"type": "string"}, "branch": {"type": "string"},
                                           "protected": {"type": "boolean"}, "caller": {"type": "string"},
                                           "socleContexts": {"type": "array", "items": {"type": "string"}},
                                           "observed": {"type": "array", "items": {"type": "string"}},
                                           "fromTag": {"type": "array", "items": {"type": "string"}},
                                           "required": {"type": "array", "items": {"type": "string"}},
                                           "missingFromRuns": {"type": "array", "items": {"type": "string"}},
                                           "requiredButNeverRun": {"type": "array", "items": {"type": "string"}},
                                           "proposed": {"type": "array", "items": {"type": "string"}},
                                           "seenOnSome": {"type": "object"},
                                           "pullRequests": {"type": "integer"},
                                           "applied": {"type": "boolean"}, "note": {"type": "string"},
                                           "protectedBy": {"type": "array", "items": {"type": "string"}},
                                           "ruleset": {"type": "array", "items": {"type": "string"}},
                                           "narrowing": {"type": "boolean"},
                                           "dropped": {"type": "array", "items": {"type": "string"}},
                                           "kept": {"type": "array", "items": {"type": "string"}},
                                           "unread": {"type": "array", "items": {"type": "string"}},
                                           "keeps": {"type": "object"}, "creates": {"type": "object"},
                                           "writes": {"type": "array", "items": {
                                               "type": "object", "required": ["method", "endpoint"],
                                               "properties": {"method": {"enum": ["PATCH", "PUT"]},
                                                              "endpoint": {"type": "string"},
                                                              "body": {"type": "object"}}}},
                                           "written": STRINGS, "nothingToChange": {"type": "boolean"},
                                           "before": {"type": "object", "required": ["classic", "required", "rulesets"],
                                                      "properties": {"classic": {"type": ["object", "null"]},
                                                                     "required": {"type": "array",
                                                                                  "items": {"type": "string"}},
                                                                     "rulesets": {"type": "array",
                                                                                  "items": {"type": "string"}}}},
                                           "dropping": {"type": "array", "items": {"type": "string"}},
                                           "replaced": {"type": "array", "items": {
                                               "type": "object", "required": ["required", "by"],
                                               "properties": {"required": {"type": "string"},
                                                              "by": {"type": "string"}}}},
                                           "gone": {"type": "array", "items": {"type": "string"}},
                                           "refused": {"type": "object", "required": ["message", "hint"],
                                                       "properties": {"message": {"type": "string"},
                                                                      "hint": {"type": "string"}}},
                                           "environment": {
                                               "type": "object", "additionalProperties": False,
                                               "required": ["name", "exists", "state", "writes", "refused", "applied"],
                                               "properties": {
                                                   "name": {"type": "string"},
                                                   "exists": {"type": ["boolean", "null"]},
                                                   "policy": {"enum": ["custom", "protected-branches", "all", None]},
                                                   "rules": {"type": "array", "items": {"type": "object"}},
                                                   "holds": {"type": ["object", "null"]},
                                                   "state": {"type": "string"}, "becomes": {"type": "string"},
                                                   "writes": {"type": "array", "items": {
                                                       "type": "object", "required": ["method", "endpoint", "body"],
                                                       "properties": {"method": {"enum": ["PUT", "POST"]},
                                                                      "endpoint": {"type": "string"},
                                                                      "body": {"type": "object"}}}},
                                                   "refused": STRINGS, "unread": STRINGS,
                                                   "gate": {"type": "array", "items": STATUS_LINE},
                                                   "note": {"type": "string"},
                                                   "applied": {"type": "boolean"}, "written": STRINGS,
                                                   "before": {"type": "object"}}}}}),
        cli.execute("rehearse", "rehearse",
                    "Replay one step of one Linux job in a Docker container on a copy of the working tree: the"
                    " packaging of the release, the check of a leg of the shared CI with --check, or a channel's"
                    " publish script against a published release with --channel. The result names the step it"
                    " replayed, and the steps and jobs it did not.",
                    handle_rehearse,
                    operands=[DIR_OPERAND, cli.operand("TARGET", "linux-x86_64 or linux-arm64;"
                                                       " absent with --channel.", required=False,
                                                       choices=list(PLATFORMS))],
                    options=[*SOCLE_OPTIONS,
                             cli.option("--image", "Container image.", cli.argument("IMAGE"), default=DEFAULT_IMAGE),
                             cli.option("--package-command", "Command producing dist/; TARGET is substituted."
                                        " Another line than the default names no step of the release.",
                                        cli.argument("COMMAND"), default="bash scripts/package-release.sh TARGET"),
                             cli.flag("--check", "Replay the check step of the shared CI's leg for TARGET instead of"
                                      " packaging: its packages, its line, no compiler named; dist/ is left alone."
                                      " For any product whose ci.yml calls check-product.yml, however it publishes."),
                             cli.option("--check-command", "The line to run with --check, when the leg's own is not"
                                        " wanted or cannot be read for certain from ci.yml; another line than the"
                                        " leg's names no step.", cli.argument("COMMAND")),
                             cli.flag("--keep", "Keep the container for inspection."),
                             cli.option("--channel", "Run the product's publish script against a published"
                                        " release instead of a build; needs --tag.",
                                        cli.argument("NAME"), requires=("--tag",)),
                             cli.option("--tag", "The published tag the channel carries.",
                                        cli.argument("TAG"), requires=("--channel",))],
                    schema={"type": "object", "additionalProperties": False,
                            "required": ["product"],
                            "properties": {"product": {"type": "string"}, "target": {"type": "string"},
                                           "platform": {"type": "string"}, "image": {"type": "string"},
                                           "mode": {"enum": ["package", "check"]}, "artifacts": STRINGS,
                                           "channel": {"type": "string"}, "tag": {"type": "string"},
                                           "repository": {"type": "string"}, "command": {"type": "string"},
                                           "replayed": {"type": "string"}, "notReplayedSteps": STRINGS,
                                           "notReplayed": STRINGS, "unlikeTheRunner": STRINGS,
                                           "assets": STRINGS, "recorded": STRINGS,
                                           "differs": STRINGS, "marker": {"type": "object"},
                                           "matches": {"type": ["boolean", "null"]}}}),
        cli.execute("render", "render", "Replace @KEY@ placeholders of a template, refusing to leave one unrendered.",
                    handle_render,
                    operands=[cli.operand("TEMPLATE", "Template file."), cli.operand("OUTPUT", "File written."),
                              cli.operand("KEY=VALUE", "Placeholder values.", required=False, variadic=True)],
                    schema={"type": "object", "additionalProperties": False, "required": ["template", "output", "placeholders"],
                            "properties": {"template": {"type": "string"}, "output": {"type": "string"}, "placeholders": STRINGS}}),
        cli.transaction("tap", "tap", "Publish a rendered formula into the Homebrew tap with a signed commit; plan without --apply.",
                        handle_tap, commit=True,
                        operands=[cli.operand("PRODUCT", "Formula name."), cli.operand("TAG", "Release tag vX.Y.Z."),
                                  cli.operand("FORMULA", "Rendered formula file.")],
                        options=[cli.flag("--skip-style", "Do not run brew style.")],
                        schema={"type": "object", "additionalProperties": False,
                                "required": ["mode", "product", "version", "repository", "formula", "changed"],
                                "properties": {"mode": {"enum": ["plan", "apply"]}, "product": {"type": "string"},
                                               "version": {"type": "string"}, "repository": {"type": "string"},
                                               "formula": {"type": "string"}, "changed": {"type": "boolean"},
                                               "diff": {"type": "string"}, "commit": {"type": "string"}, "install": {"type": "string"}}}),
        cli.execute("self-test", "self-test", "Run the socle's own tests.", handle_self_test,
                    options=[cli.flag("--verbose", "Report every test."),
                             cli.option("--jobs", "Test classes run at a time; defaults to the number of processors.",
                                        cli.argument("N"))],
                    schema={"type": "object", "additionalProperties": False, "required": ["tests", "passed"],
                            "properties": {"tests": {"type": "string"}, "passed": {"type": "boolean"}}}),
        cli.execute("vendor", "vendor", "Refresh bin/maelys_cli.py from maelys-cli at dependencies/maelys-cli.pin and record its digest.",
                    handle_vendor,
                    schema={"type": "object", "additionalProperties": False,
                            "required": ["tag", "commit", "sha256", "path", "changed"],
                            "properties": {"tag": {"type": "string"}, "commit": {"type": "string"}, "sha256": {"type": "string"},
                                           "path": {"type": "string"}, "changed": {"type": "boolean"}}}),
    ], guide="release socle of the Maelys repositories", text=TEXT)
