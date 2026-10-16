# SPDX-License-Identifier: MPL-2.0
"""`new`: create a Maelys repository, its base files and then the socle's own.
"""
from __future__ import annotations

import datetime
import pathlib
import re

from maelys_cli import EXIT_OK, Failure, Invocation
from .. import authorship, dependencies
from ..constants import MECHANISMS, PROGRAM, SOCLE_MECHANISM, UNPUBLISHED
from ..context import Context
from ..declarations import require_valid
from ..forge import forge
from ..host import run
from ..reading import read_declarations
from ..socle_origin import socle_data, socle_identity
from ..staging import plan, stage
from ..texts import LABELS


def resolve_dependency(spec: str) -> tuple[str, str, str]:
    """`name@vX.Y.Z` to (name, tag, commit), the tag resolved on the remote."""
    name, separator, tag = spec.partition("@")
    if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise Failure("VALIDATION_FAILED", f"A dependency is NAME@vX.Y.Z, not {spec!r}.",
                      "Pass --depends maelys-system@v0.9.1, once per dependency.")
    base = forge().clone_base()
    # The dereferenced tag (^{}) is the commit an annotated tag names, which
    # is what a pin holds; a lightweight tag answers on the first line.
    listed = run(["git", "ls-remote", "--tags", f"{base}/{name}.git", tag, f"{tag}^{{}}"])
    if listed.returncode != 0:
        raise Failure("NOT_FOUND", f"{base}/{name}.git could not be read for {tag}.",
                      "Check the dependency name and that the remote is reachable.")
    commits = {line.split("\t")[1]: line.split("\t")[0] for line in listed.stdout.splitlines() if "\t" in line}
    commit = commits.get(f"refs/tags/{tag}^{{}}") or commits.get(f"refs/tags/{tag}")
    if not commit:
        raise Failure("NOT_FOUND", f"{name} has no tag {tag}.", f"Pick a tag that {base}/{name} publishes.")
    return name, tag, commit


def scaffold(context: Context, project: pathlib.Path, product: str, dependencies: list[tuple[str, str, str]]) -> dict[str, str]:
    """The files a repository carries before the socle writes anything into it."""
    today = datetime.date.today().isoformat()
    license_text = (context.socle_root() / "LICENSE").read_text(encoding="utf-8")
    if "Mozilla Public License" not in license_text.split("\n", 1)[0]:
        raise Failure("PRECONDITION_FAILED", "The socle's LICENSE is not the MPL-2.0 text it copies.",
                      "Run this from a maelys-release checkout.")
    files = {
        "LICENSE": license_text,
        # VERSION says what was published last, and a scaffold has published
        # nothing: 0.0.0. The scaffold used to say 0.1.0, and `cut DIR 0.1.0`
        # then refused the first release of every new product as not coming
        # after the current version -- found on the pilot's first day.
        # Its entry is no longer one `check` asks for. It stays because this
        # is the one command that can write it where it belongs, first, and
        # because it tells the reader of a new repository what to do next.
        "VERSION": UNPUBLISHED + "\n",
        "CHANGELOG.md": f"# Changelog\n\n## {UNPUBLISHED} — {today}\n\n- Created by maelys-release new. Nothing is published"
                        f" yet: VERSION says what was published last, and the first release of {product}"
                        f" is cut as 0.1.0 once its dated entry is written above this one.\n",
        "README.md": f"# {product}\n\n*One paragraph: what this repository is, and what it is not.*\n\n"
                     f"## Install\n\n*How a user gets it.*\n\n## Build\n\n```sh\nmake check\n```\n",
        # A release must not silently publish nothing: the stub fails until
        # the product says how it packages.
        "scripts/package-release.sh":
            "#!/bin/sh\n# Package $1 (linux-x86_64, linux-arm64, macos-arm64) into dist/,\n"
            "# with one .sha256 next to each artifact. Written by maelys-release new.\n"
            "set -eu\n"
            'echo "scripts/package-release.sh: $1 is not implemented yet" >&2\n'
            "exit 1\n",
    }
    for name, tag, commit in dependencies:
        files[f"dependencies/{name}.pin"] = f"{tag}\n{commit}\n"
    del project
    return files


def handle_new(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Create a Maelys repository: the base files, then the socle's own."""
    directory = pathlib.Path(invocation.operands[0])
    product = str(invocation.option("--product", "")) or directory.name
    if not re.fullmatch(r"[a-z0-9-]+", product):
        raise Failure("VALIDATION_FAILED", f"Product name must be [a-z0-9-]: {product}.",
                      "Pass --product NAME with a valid name.")
    apply = invocation.flag("--apply")
    mechanism = str(invocation.option("--mechanism", "")) or SOCLE_MECHANISM
    if mechanism not in MECHANISMS:
        raise Failure("VALIDATION_FAILED", f"Unknown mechanism: {mechanism}.",
                      "Pass --mechanism maelys-release, custom or none.")
    if directory.exists():
        remaining = [path.name for path in directory.iterdir() if path.name != ".git"]
        if remaining:
            raise Failure("PRECONDITION_FAILED", f"{directory} is not empty: {', '.join(sorted(remaining)[:5])}.",
                          f"'{PROGRAM} new' creates a repository; run 'adopt' on one that exists.")
    dependencies = [resolve_dependency(str(spec)) for spec in invocation.option("--depends", [])]
    files = scaffold(context, directory, product, dependencies)
    written: list[dict] = []
    for relative, content in files.items():
        entry = {"path": relative, "action": "create", "scope": "conventions"}
        if apply:
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if relative.startswith("scripts/"):
                path.chmod(path.stat().st_mode | 0o111)
        written.append(entry)
    data: dict = {"mode": "apply" if apply else "plan", "product": product, "project": str(directory),
                  "mechanism": mechanism, "version": UNPUBLISHED,
                  "dependencies": [{"name": name, "tag": tag, "commit": commit}
                                   for name, tag, commit in dependencies],
                  "files": written, "adopted": [],
                  # Before the first commit of a repository is when an identity
                  # git made up can still be replaced for nothing.
                  "notes": authorship.identity_notes(directory)}
    if not apply:
        return data, EXIT_OK
    # The socle's own files come from adopt, so new and adopt can never
    # disagree on what a repository carries.
    sha, tag = socle_identity(context, str(invocation.option("--socle-sha", "")), str(invocation.option("--socle-tag", "")))
    decl = read_declarations(context, directory.resolve(), product, mechanism)
    require_valid(decl, "new")
    data["adopted"] = plan(decl, stage(context, decl, sha, tag), apply=True)
    data["socle"] = socle_data(context, sha, tag)
    return data, EXIT_OK


def text_new(data: dict) -> str:
    lines = [f"create   {entry['path']}" for entry in data["files"]]
    lines.extend(f"{entry['action']:<8} {entry['path']}" for entry in data["adopted"])
    lines.extend(f"{LABELS['note']:<8} {note}" for note in data.get("notes", []))
    for dependency in data["dependencies"]:
        lines.append(f"pin      dependencies/{dependency['name']}.pin"
                     f" {dependency['tag']} ({dependency['commit'][:7]})")
    if data["mode"] == "plan":
        lines.append(f"new: plan only; add --apply to create {data['product']} in {data['project']}")
    else:
        lines.append(f"new: {data['product']} {data['version']} created in {data['project']}"
                     f" ({data['mechanism']} mechanism)")
        lines.append("next: write scripts/package-release.sh and a Makefile whose 'check' passes,"
                     " then commit and open the repository")
    return "\n".join(lines) + "\n"
