# SPDX-License-Identifier: MPL-2.0
"""Plan and publish tap formulas through the current host."""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import tempfile

from maelys_cli import EXIT_OK, Failure, Invocation
from . import host
from .checkouts import author_identity
from .constants import DEFAULT_TAP
from .host import git, run


def push_rebased(tap: pathlib.Path, branch: str, attempts: int = 3) -> int:
    """Push HEAD of the tap clone to BRANCH; after a rejected push, fetch,
    rebase onto the branch and push again, ATTEMPTS times at most.

    Returns the number of attempts the push took. Every product owns its own
    Formula/NAME.rb, so the rebase conflicts only when the same formula is
    published twice at once, which the signed-tag model excludes: the rebase
    is then aborted and the publication fails instead of guessing. The
    publish step of tap.yml does the same in shell.
    """
    pushed = None
    for attempt in range(1, attempts + 1):
        pushed = run(["git", "push", "-q", "origin", f"HEAD:{branch}"], cwd=tap)
        if pushed.returncode == 0:
            return attempt
        if attempt == attempts:
            break
        git("fetch", "-q", "origin", branch, cwd=tap)
        if run(["git", "rebase", "-q", f"origin/{branch}"], cwd=tap).returncode != 0:
            run(["git", "rebase", "--abort"], cwd=tap)
            raise Failure("PROCESS_FAILED", f"Rebasing onto origin/{branch} conflicts: the tap received another"
                          " change of the same formula.", "Read the tap's history of the formula, then publish again.")
    raise Failure("PROCESS_FAILED", f"git push to {branch} of the tap was rejected {attempts} times:"
                  f" {pushed.stderr.strip()}", "Check the tap's branch rules and the credentials, then publish again.")


def handle_tap(invocation: Invocation) -> tuple[dict, int]:
    """Plan or publish one rendered formula into the tap with a signed commit."""
    product, tag, formula = invocation.operands[0], invocation.operands[1], pathlib.Path(invocation.operands[2])
    apply = invocation.flag("--apply")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?", tag):
        raise Failure("VALIDATION_FAILED", f"TAG is vX.Y.Z or vX.Y.Z-PRERELEASE: {tag}.", "Pass the release tag.")
    if not formula.is_file():
        raise Failure("NOT_FOUND", f"Rendered formula not found: {formula}.", "Render the formula first.")
    version = tag[1:]
    repository = os.environ.get("TAP_REPOSITORY", DEFAULT_TAP)
    token = os.environ.get("TAP_TOKEN", "")
    url = os.environ.get("TAP_URL") or (
        f"https://x-access-token:{token}@github.com/{repository}.git" if token
        else f"https://github.com/{repository}.git")
    owner, _, name = repository.partition("/")
    data: dict = {"mode": "apply" if apply else "plan", "product": product, "version": version,
                  "repository": repository, "formula": f"Formula/{product}.rb", "changed": False}
    with tempfile.TemporaryDirectory(prefix="maelys-release-tap.") as temp:
        tap = pathlib.Path(temp) / "tap"
        git("clone", "-q", url, str(tap))
        (tap / "Formula").mkdir(exist_ok=True)
        shutil.copyfile(formula, tap / "Formula" / f"{product}.rb")
        if host.HOST.which("brew") and not invocation.flag("--skip-style"):
            style = run(["brew", "style", str(tap / "Formula" / f"{product}.rb")])
            if style.returncode != 0:
                raise Failure("VALIDATION_FAILED", f"brew style refused the formula: {style.stdout.strip()}",
                              "Fix the formula template and render it again.")
        git("add", f"Formula/{product}.rb", cwd=tap)
        if run(["git", "diff", "--cached", "--quiet"], cwd=tap).returncode == 0:
            return data, EXIT_OK
        data["changed"] = True
        data["diff"] = git("diff", "--cached", cwd=tap)
        if not apply:
            return data, EXIT_OK
        # Identity and signing live in the clone's configuration, so the
        # commit and any rebase of it are signed alike. The identity is the
        # operator's, as in `migrate`: forcing a machine name here put a tap
        # commit under an address no account stands behind, signed and still
        # never verified -- the third of the three sites maelys-http named,
        # and the one 0.53.0 first left out.
        author_identity(tap)
        key = os.environ.get("TAP_SIGNING_KEY", "")
        if key:
            key_file = pathlib.Path(temp) / "signing-key"
            key_file.write_text(key + "\n", encoding="utf-8")
            key_file.chmod(0o600)
            git("config", "gpg.format", "ssh", cwd=tap)
            git("config", "user.signingkey", str(key_file), cwd=tap)
            git("config", "commit.gpgsign", "true", cwd=tap)
        git("commit", "-q", "-m", f"Update {product} to {version}", cwd=tap)
        data["pushAttempts"] = push_rebased(tap, "main")
        # The rebase of a retried push rewrites the commit: report what the tap holds.
        data["commit"] = git("rev-parse", "HEAD", cwd=tap)
    data["install"] = f"brew install {owner}/{name.replace('homebrew-', '', 1)}/{product}"
    return data, EXIT_OK


def text_tap(data: dict) -> str:
    if not data["changed"]:
        return f"tap already carries {data['product']} {data['version']}\n"
    if data["mode"] == "plan":
        return data["diff"] + "\ntap: plan only; add --apply to commit and push\n"
    return f"{data['install']}\n"
