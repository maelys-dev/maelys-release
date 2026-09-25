# SPDX-License-Identifier: MPL-2.0
"""Prepare a line of the socle's allowed signers, and say what it would do.

A formatter, never the invariant. What a release is held to is the file the
workflows read; what this command adds is that the gesture is reproducible
and its refusals are the ones `ssh-keygen` would make -- a line this command
writes is a line the release will accept, and a line it refuses is one the
release would have refused after the tag was pushed.

It writes one file and nothing else: no GitHub call, no commit, no push. The
operator reads the diff, opens the pull request, and the conventions take
over from there.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import subprocess
import tempfile

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation
from .context import Context
from .identity import allowed_signers
from .project import project_of
from .release_checks import (SSH_KEY_TYPES, allowed_signer_keys, signer_date, signer_line_refuses,
                             signer_options)


NAMESPACE = 'namespaces="git"'


def fingerprint(kind: str, material: str) -> str:
    """SHA256:... of one key, from ssh-keygen, or "" when it cannot say."""
    with tempfile.NamedTemporaryFile("w", suffix=".pub", encoding="utf-8") as handle:
        handle.write(f"{kind} {material}\n")
        handle.flush()
        done = subprocess.run(["ssh-keygen", "-l", "-f", handle.name], capture_output=True, text=True)
    match = re.search(r"SHA256:\S+", done.stdout)
    return match.group(0) if done.returncode == 0 and match else ""


def read_key(path: pathlib.Path) -> tuple[str, str]:
    """(type, material) of a public key file, refusing anything else.

    A private key, a certificate, an empty file: each is a mistake worth a
    sentence rather than a line in a security file.
    """
    if not path.is_file():
        raise Failure("NOT_FOUND", f"{path} is not a file.", "Pass the public half of the key, its .pub file.")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("-----BEGIN"):
        raise Failure("VALIDATION_FAILED", f"{path} holds a private key, not a public one.",
                      "Pass the .pub beside it: an allowed signers file names public keys.")
    parts = text.split()
    if len(parts) < 2 or not parts[0].startswith(SSH_KEY_TYPES):
        raise Failure("VALIDATION_FAILED", f"{path} does not read as an ssh public key.",
                      "The first two words are the key type and its material, as in ssh-ed25519 AAAA...")
    if not fingerprint(parts[0], parts[1]):
        raise Failure("VALIDATION_FAILED", f"ssh-keygen does not read {path} as a key.",
                      "The release workflow runs the same tool; a key it cannot read signs nothing here.")
    return parts[0], parts[1]


def entries_of(path: pathlib.Path) -> list[dict]:
    """What the file says today, each line with what would refuse it now."""
    if not path.is_file():
        return []
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    found = []
    for principal, options, kind, material in allowed_signer_keys(path.read_text(encoding="utf-8")):
        refused = signer_line_refuses(signer_options(options), today)
        found.append({"principal": principal, "options": options, "type": kind,
                      "fingerprint": fingerprint(kind, material), "signsToday": not refused,
                      "refusal": refused})
    return found


def added_line(principal: str, kind: str, material: str, comment: str) -> str:
    return " ".join([principal, NAMESPACE, kind, material] + ([comment] if comment else [])) + "\n"


def retired_line(line: str, at: str) -> str:
    """The same line, with valid-before at `at`; never a line removed.

    A deleted line makes the releases that key signed unreplayable, and a
    failed publication is replayed on the tag it already has.
    """
    parts = line.split()
    index = next(index for index, token in enumerate(parts[1:], start=1) if token.startswith(SSH_KEY_TYPES))
    options = signer_options(" ".join(parts[1:index]))
    options["valid-before"] = at
    rendered = ",".join(name if value == "" else f'{name}="{value}"' for name, value in options.items())
    return " ".join([parts[0], rendered] + parts[index:]) + "\n"


def matches(entry: dict, wanted: str) -> bool:
    return wanted in (entry["principal"], entry["fingerprint"])


def handle_signers(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """List, add or retire, in a socle checkout, writing one file at most."""
    project = project_of(invocation)[0]
    # Asked before the file is looked for: "share/agents not found" answers a
    # question nobody asked when the directory is simply another repository.
    if not (project / "share" / "agents").is_dir():
        raise Failure("PRECONDITION_FAILED", f"{project} is not a maelys-release checkout.",
                      "The allowed signers belong to the socle; pass its directory.")
    path = allowed_signers(project)
    add, retire = invocation.option("--add"), invocation.option("--retire")
    data: dict = {"file": str(path), "action": "list", "signers": entries_of(path),
                  "wrote": False, "next": []}
    if not add and not retire:
        return data, EXIT_OK
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    if add:
        data["action"] = "add"
        principal = str(invocation.option("--principal", ""))
        if not principal:
            raise Failure("VALIDATION_FAILED", "--add needs the --principal that key signs as.",
                          "An allowed signers line begins with the identity, an email in this fleet.")
        kind, material = read_key(pathlib.Path(str(add)).expanduser())
        for entry in data["signers"]:
            if entry["fingerprint"] and entry["fingerprint"] == fingerprint(kind, material):
                raise Failure("PRECONDITION_FAILED",
                              f"{path.name} already names that key, as {entry['principal']}.",
                              "Retire the line rather than naming the same key twice.")
        updated = original + ("" if original.endswith("\n") or not original else "\n") \
            + added_line(principal, kind, material, "")
        data["line"] = added_line(principal, kind, material, "").rstrip("\n")
        data["next"] = [
            "Sign the commit that adds this line with the key it adds: that is the proof the asker holds it.",
            "Open a pull request here; the maintainer of this repository decides, in the open.",
            "Cut a socle release: without a tag, the line reaches no product.",
            "Each product receives it when it adopts that socle, and not before.",
        ]
    else:
        data["action"] = "retire"
        wanted = str(retire)
        at = str(invocation.option("--at", "")) or datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%d%H%M%SZ")
        if not signer_date(at):
            raise Failure("VALIDATION_FAILED", f"--at {at} is not a time ssh-keygen reads.",
                          "Write YYYYMMDD, or YYYYMMDDHHMM[SS], with a trailing Z and no separator.")
        lines = original.splitlines(keepends=True)
        chosen = [index for index, entry in enumerate(data["signers"]) if matches(entry, wanted)]
        if not chosen:
            raise Failure("NOT_FOUND", f"{path.name} names no signer {wanted}.",
                          "Pass the principal of the line, or the SHA256 fingerprint this command lists.")
        keyed = [index for index, line in enumerate(lines)
                 if line.strip() and not line.strip().startswith("#")]
        updated, retired = "".join(lines), []
        for position in chosen:
            line = lines[keyed[position]]
            lines[keyed[position]] = retired_line(line, at)
            retired.append(data["signers"][position]["principal"])
        updated = "".join(lines)
        data["line"] = lines[keyed[chosen[0]]].rstrip("\n")
        data["retired"] = retired
        data["next"] = [
            "This rotates the key: it stops signing new tags once a product adopts a socle carrying the line.",
            "It revokes nothing. Remove the key from its GitHub account to reach every pin the same evening.",
            "The tags it signed while it was trusted keep verifying: the workflows judge at verified_at.",
            "SECURITY.md gives the order, fastest first.",
        ]
    data["diff"] = updated != original
    if invocation.flag("--apply"):
        path.write_text(updated, encoding="utf-8")
        data["wrote"] = True
        data["signers"] = entries_of(path)
    return data, EXIT_OK if data["diff"] else EXIT_VIOLATIONS


def text_signers(data: dict) -> str:
    lines = [f"{'file':<8} {data['file']}"]
    for entry in data["signers"]:
        state = "signs" if entry["signsToday"] else "quiet"
        lines.append(f"{state:<8} {entry['principal']} {entry['fingerprint'] or entry['type']}"
                     + (f" -- {entry['refusal']}" if entry["refusal"] else ""))
    if data["action"] != "list":
        lines.append(f"{data['action']:<8} {data.get('line', '')}")
        for step in data["next"]:
            lines.append(f"{'next':<8} {step}")
        lines.append(f"signers: {'wrote' if data['wrote'] else 'plan only; add --apply to write'} {data['file']}")
    else:
        lines.append(f"signers: {len(data['signers'])} named in {data['file']}")
    return "\n".join(lines) + "\n"


def only_grew(before: str, after: str) -> list[str]:
    """Why `after` is not `before` plus what a fleet may add, or an empty list.

    The command is a formatter; this is the invariant, and it is held where
    a hand can be held: on the difference between two versions of the file.
    A line removed makes the releases that key signed unreplayable, and a
    `valid-before` pushed later, or dropped, un-retires a key that was
    retired -- both are the edit a mistake or a bad day produces, and
    neither is visible in the line that was written last.
    """
    was = {(kind, material): signer_options(options)
           for _, options, kind, material in allowed_signer_keys(before)}
    now = {(kind, material): signer_options(options)
           for _, options, kind, material in allowed_signer_keys(after)}
    refusals = []
    for key, options in was.items():
        short = (fingerprint(*key) or key[1])[:24]
        if key not in now:
            refusals.append(f"the line naming {short} was removed: retire it with valid-before instead,"
                            " or the releases it signed can no longer be replayed")
            continue
        retired, still = options.get("valid-before", ""), now[key].get("valid-before", "")
        if retired and not still:
            refusals.append(f"{short} was retired and is not any more: valid-before disappeared")
        elif retired and still and signer_date(still) > signer_date(retired):
            refusals.append(f"{short} was retired on {retired} and the line now says {still}:"
                            " a retirement moves earlier, never later")
    return refusals
