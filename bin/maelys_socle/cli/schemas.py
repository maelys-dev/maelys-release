# SPDX-License-Identifier: MPL-2.0
"""The output schemas and the shared options of the catalog: what describe publishes of each command."""
from __future__ import annotations

import maelys_cli as cli

from .. import privacy
from ..constants import MECHANISMS


# ---- the catalog --------------------------------------------------------------

CHECKS_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["status", "message", "scope"],
                                           "properties": {"status": {"type": "string"}, "message": {"type": "string"},
                                                          "scope": {"enum": ["conventions", "release"]}}}}
FILES_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["path", "action", "scope"],
                                          "properties": {"path": {"type": "string"},
                                                         "action": {"enum": ["create", "update", "same"]},
                                                         "scope": {"enum": ["conventions", "release"]},
                                                         "seeded": {"type": "boolean"},
                                                         "diff": {"type": "string"}, "reason": {"type": "string"},
                                                         "touches": {"enum": ["prose", "pin", "mechanism"]}}}}
MECHANISM_SCHEMA = {"enum": list(MECHANISMS)}
VERDICT_SCHEMA = {"type": "object", "required": ["valid", "violations"],
                  "properties": {"valid": {"type": "boolean"}, "violations": {"type": "array",
                                                                             "items": {"type": "string"}},
                                 "applicable": {"type": "boolean"}}}
SOCLE_SCHEMA = {"type": "object", "required": ["version", "tag", "sha"],
                "properties": {"version": {"type": "string"}, "tag": {"type": "string"}, "sha": {"type": "string"}}}
STRINGS = {"type": "array", "items": {"type": "string"}}
# One line of a gate or of a verdict: ok, note or fail, and what it says.
STATUS_LINE = {"type": "object", "required": ["status", "message"],
               "properties": {"status": {"type": "string"}, "message": {"type": "string"}}}
SOCLE_OPTIONS = (
    cli.option("--product", "Product name; defaults to the directory name.", cli.argument("NAME")),
    cli.option("--socle-sha", "Socle commit to pin instead of this checkout's HEAD (trials and tests).",
               cli.argument("SHA", "string", pattern="^[0-9a-f]{40}$"), hidden=True),
    cli.option("--socle-tag", "Socle tag written next to --socle-sha.", cli.argument("TAG"), hidden=True),
)
DIR_OPERAND = cli.operand("DIR", "Product repository.")
# Where, never what. A finding holds no value; a key or a path that some
# family matched is shown by its position, and `in` says when the match was
# the key, the name of the file or the name of a ref rather than a value.
PRIVACY_FINDINGS = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["family", "path"],
    "properties": {"family": {"enum": list(privacy.FAMILIES)}, "path": {"type": "string"},
                   "in": {"enum": ["key", "name", "ref"]},
                   "key": {"type": "string"}, "line": {"type": "integer"}, "term": {"type": "integer"},
                   "notice": {"type": "boolean"}, "excused": {"type": "integer"},
                   "blob": {"type": "string"}, "commit": {"type": "string"}}}}
PRIVACY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "refuses", "files", "local", "findings"],
    "properties": {
        "mode": {"enum": ["note", "refuse"]}, "refuses": STRINGS,
        "files": {"type": "object", "additionalProperties": False,
                  "required": ["tracked", "searched", "read", "unread", "astext", "partial"],
                  "properties": {"tracked": {"type": "integer"}, "searched": {"type": "integer"},
                                 "read": {"type": "integer"},
                                 "unread": {"type": "array", "items": {
                                     "type": "object", "additionalProperties": False, "required": ["path", "reason"],
                                     "properties": {"path": {"type": "string"},
                                                    "reason": {"enum": list(privacy.UNREAD)}}}},
                                 "astext": STRINGS, "partial": STRINGS}},
        "local": {"type": "object", "additionalProperties": False, "required": ["machineNames", "terms", "runner"],
                  "properties": {"machineNames": {"type": "integer"}, "terms": {"type": "integer"},
                                 "runner": {"type": "boolean"}}},
        "findings": PRIVACY_FINDINGS,
        "history": {"type": "object", "additionalProperties": False,
                    "required": ["refs", "shallow", "commits", "blobs", "addresses", "pullRequestHeads", "findings"],
                    "properties": {"refs": {"type": "object"}, "shallow": {"type": "boolean"},
                                   "commits": {"type": "integer"},
                                   "blobs": {"type": "object", "additionalProperties": False,
                                             "required": ["read", "unread"],
                                             "properties": {"read": {"type": "integer"},
                                                            "unread": {"type": "object"}}},
                                   "addresses": {"type": "integer"}, "pullRequestHeads": {"type": "boolean"},
                                   "findings": PRIVACY_FINDINGS}}}}
CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["valid", "product", "project", "mechanism", "socle", "pinned", "checks", "files",
                             "violations", "conventions", "release"],
                "properties": {"valid": {"type": "boolean"}, "product": {"type": "string"},
                               "project": {"type": "string"}, "mechanism": MECHANISM_SCHEMA,
                               "socle": SOCLE_SCHEMA,
                               "pinned": {"type": ["object", "null"]}, "checks": CHECKS_SCHEMA,
                               "files": FILES_SCHEMA, "violations": STRINGS,
                               "conventions": VERDICT_SCHEMA, "release": VERDICT_SCHEMA,
                               "privacy": PRIVACY_SCHEMA}}
CUT_SCHEMA = {"type": "object", "additionalProperties": False,
              "required": ["mode", "stage", "product", "project", "repository", "version", "tag", "branch",
                           "base", "gate", "checks", "ready"],
              "properties": {"mode": {"enum": ["plan", "apply"]}, "stage": {"enum": ["open", "tag"]},
                             "product": {"type": "string"}, "project": {"type": "string"},
                             "repository": {"type": "string"}, "version": {"type": "string"},
                             "previousVersion": {"type": "string"}, "tag": {"type": "string"},
                             "branch": {"type": "string"}, "base": {"type": "string"},
                             "changelog": {"type": "string"},
                             "gate": {"type": "array", "items": {
                                 "type": "object", "additionalProperties": False, "required": ["status", "message"],
                                 "properties": {"status": {"type": "string"}, "message": {"type": "string"}}}},
                             "checks": {"type": "array", "items": {
                                 "type": "object", "additionalProperties": False,
                                 "required": ["name", "status", "conclusion"],
                                 "properties": {"name": {"type": "string"}, "status": {"type": "string"},
                                                "conclusion": {"type": "string"}}}},
                             "ready": {"type": "boolean"}, "commit": {"type": "string"},
                             "pullRequest": {"type": "object", "additionalProperties": False,
                                             "required": ["number", "url", "state"],
                                             "properties": {"number": {"type": "integer"},
                                                            "url": {"type": "string"},
                                                            "state": {"type": "string"}}},
                             "pushed": {"type": "boolean"}, "verified": {"type": "boolean"},
                             "regenerated": STRINGS,
                             "next": {"type": "string"}}}
DOCS_CONTRACT_OPTION = cli.flag(
    "--docs-contract", "Refuse prose in docs/ and a CLI reference outside docs/cli.md, instead of noting them.")
MECHANISM_OPTION = cli.option(
    "--mechanism", "How the product publishes when it carries no release.yml yet.",
    cli.argument("MECHANISM", choices=list(MECHANISMS)))
