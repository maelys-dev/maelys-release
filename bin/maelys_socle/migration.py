# SPDX-License-Identifier: MPL-2.0
"""Plan and apply the two sides of a prose migration through the current host."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

from maelys_cli import EXIT_OK, Failure, Invocation

from .checkouts import author_identity
from .constants import CLI_CONTRACT, CLI_REFERENCE
from .context import Context
from .declarations import Declarations
from .github import destination_is_public
from .host import git, run
from .project import declared_mechanism, engaged_documents, project_of
from .texts import is_generated, names_documentation


def read_prose_records(source: str) -> list[dict]:
    """The documents to move, as maelys-platform lists them.

    One JSON record per line, `docs --prose --only PRODUCT --format jsonl`:
    the classification lives there and is not recomputed here, so the socle
    and the platform cannot disagree about what is prose.
    """
    stream = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text(encoding="utf-8")
    records = []
    for number, line in enumerate(stream.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise Failure("VALIDATION_FAILED", f"Line {number} of the document list is not JSON: {error}.",
                          "Pass 'maelys-platform docs --prose --only PRODUCT --format jsonl'.") from error
        missing = [key for key in ("repository", "path", "destination") if not isinstance(record.get(key), str)]
        if missing:
            raise Failure("VALIDATION_FAILED", f"Record {number} has no {', '.join(missing)}.",
                          "Pass 'maelys-platform docs --prose --only PRODUCT --format jsonl'.")
        records.append(record)
    if not records:
        raise Failure("VALIDATION_FAILED", "The document list is empty.",
                      "Pass 'maelys-platform docs --prose --only PRODUCT --format jsonl'; nothing to move is not a migration.")
    return records


def referencing_files(project: pathlib.Path, relative: str) -> list[str]:
    """Files of the product that still name this document, which the move breaks."""
    completed = run(["git", "grep", "-l", "--fixed-strings", "--", relative], cwd=project)
    return [line for line in completed.stdout.splitlines() if line and line != relative]


def migration_plan(invocation: Invocation, context: Context) -> dict:
    return plan_with_declarations(invocation, context)[0]


def plan_with_declarations(invocation: Invocation, context: Context) -> tuple[dict, Declarations]:
    """The plan, and the declarations it was read with: `--apply` needs one word of them."""
    project, product, _ = project_of(invocation)
    records = read_prose_records(str(invocation.option("--documents", "-")))
    foreign = sorted({record["repository"] for record in records} - {product})
    if foreign:
        raise Failure("VALIDATION_FAILED", f"The list carries documents of {', '.join(foreign)}, not {product}.",
                      f"Pass 'maelys-platform docs --prose --only {product} --format jsonl'.")
    # No default: the socle's public reference of this command spelled the
    # private repository's name until 0.60.0. maelys-platform, which lists
    # the documents, names their destination.
    repository = str(invocation.option("--documents-repository", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise Failure("VALIDATION_FAILED",
                      "--documents-repository OWNER/NAME names where the prose moves; none was given."
                      if not repository else f"--documents-repository is OWNER/NAME, not {repository!r}.",
                      "Pass the documentation repository maelys-platform names, with the list it produces.")
    moving, unknown = [], []
    for record in records:
        relative = record["path"]
        destination = record["destination"]
        expected = f"{repository.split('/')[-1]}/{product}/{relative[len('docs/'):]}"
        if not relative.startswith("docs/") or destination != expected:
            unknown.append(f"{relative} -> {destination} (expected {expected})")
            continue
        if not (project / relative).is_file():
            unknown.append(f"{relative} is not a file of {product}")
            continue
        moving.append({"path": relative, "destination": destination,
                       "referencedBy": referencing_files(project, relative)})
    if unknown:
        raise Failure("VALIDATION_FAILED", "The list does not describe this migration: " + "; ".join(unknown) + ".",
                      "Regenerate it with maelys-platform for this product.")
    # What the product keeps, and on which ground: the plan says it before
    # anything is written, so the operator judges the whole docs/ at once.
    decl = context.read_declarations(project, product, declared_mechanism(project))
    moved = {entry["path"] for entry in moving}
    staying = []
    docs = project / "docs"
    for path in sorted(docs.rglob("*")) if docs.is_dir() else []:
        relative = path.relative_to(project).as_posix()
        if not path.is_file() or relative in moved:
            continue
        if relative == CLI_REFERENCE or relative == CLI_CONTRACT:
            reason = "the generated command-line reference and its contract"
        elif path.suffix.lower() != ".md":
            reason = "data, not prose"
        elif is_generated(path.read_text(encoding="utf-8")):
            reason = "generated: its head says so"
        elif relative in engaged_documents(project):
            reason = "LICENSING.md engages it publicly"
        else:
            reason = "not in the list maelys-platform produced"
        staying.append({"path": relative, "reason": reason})
    version = decl.version
    tag = git("describe", "--tags", "--abbrev=0", cwd=project, check=False) or ""
    commit = git("rev-parse", "HEAD", cwd=project, check=False)
    return {"mode": "plan", "product": product, "project": str(project), "repository": repository,
            "version": version, "tag": tag, "commit": commit,
            "moving": moving, "staying": staying, "documents": len(moving)}, decl


def push_branch(clone: pathlib.Path, remote: str, branch: str) -> None:
    """Send `branch` to `remote`, replacing an earlier attempt but nothing else.

    --force-with-lease needs to know what the remote holds, and a clone that
    has never fetched the branch knows nothing: it refuses on stale info,
    which is what a second run of a migration hits. The branch is fetched
    first, so the lease is judged against the ref it names.
    """
    run(["git", "fetch", "-q", remote, f"+refs/heads/{branch}:refs/remotes/lease/{branch}"], cwd=clone)
    known = run(["git", "rev-parse", "--verify", "-q", f"refs/remotes/lease/{branch}"], cwd=clone).stdout.strip()
    lease = f"--force-with-lease=refs/heads/{branch}:{known}" if known else "--force-with-lease"
    git("push", "-q", lease, remote, f"HEAD:refs/heads/{branch}", cwd=clone)


BUILD_FILES = ("Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "meson.build")


def surviving_references(clone: pathlib.Path, data: dict, named: bool = True) -> tuple[list[dict], list[dict]]:
    """What still names a moved document, and what names docs/ as a whole.

    The socle rewrites Markdown, and nothing else: a header, a Makefile or a
    script says what it says for reasons the socle does not know. It reports
    them instead, with file and line, because a reported problem half fixed
    is worse than either extreme.

    When the destination is not named (`named` false), the Markdown keeps
    the paths it named too, and those are reported the same way: the
    reviewer, not the socle, chooses the words that point outside a public
    repository without naming the private one.
    """
    moved = [entry["path"] for entry in data["moving"]]
    references, globs = [], []
    for path in sorted(clone.rglob("*")):
        if not path.is_file() or ".git/" in path.as_posix():
            continue
        relative = path.relative_to(clone).as_posix()
        if relative in moved:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            for document in moved:
                if document in line and (path.suffix.lower() != ".md" or not named):
                    references.append({"path": relative, "line": number, "names": document})
            # A build or packaging rule that names docs/ as a whole installs
            # or copies whatever it matches, and that set is about to shrink.
            if (path.name in BUILD_FILES or relative.startswith(("scripts/", "packaging/"))) \
                    and "docs/" in line and "*" in line:
                globs.append({"path": relative, "line": number, "names": line.strip()[:80]})
    return references, globs


def rewrite_markdown(clone: pathlib.Path, product: str, data: dict, named: bool = True) -> list[str]:
    """Point every Markdown file of the product away from what left.

    A link whose target leaves keeps its text and loses its link: the
    destination is a private repository, so there is nothing to link to. A
    path named in prose becomes the path it now has -- when the product
    declares `[docs] named`. Otherwise the path stays as it was and is
    reported for the reviewer's hand: the destination's name is what the
    rule of 0.58.0 keeps out of a public repository's files, and this
    rewrite wrote it into the AGENTS.md of a public one (maelys-cli), beside
    the managed block that had just stopped naming it. Files that are
    themselves moving keep their own relative links, which stay valid where
    they land.
    """
    moved = [entry["path"] for entry in data["moving"]]
    rewritten = []
    for path in sorted(clone.rglob("*.md")):
        relative = path.relative_to(clone).as_posix()
        if relative in moved or relative == "README.md" or ".git/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        original = text
        for document in moved:
            destination = f"{data['repository'].split('/')[-1]}/{product}/{document[len('docs/'):]}"
            text = re.sub(rf"\[([^\]]*)\]\({re.escape(document)}\)", r"\1", text)
            if named:
                text = text.replace(document, destination)
        if text != original:
            path.write_text(text, encoding="utf-8")
            rewritten.append(relative)
    return rewritten


def migration_notes(data: dict) -> str:
    """What the reviewer must finish by hand, in the commit that asks for it."""
    lines = []
    if data.get("remaining"):
        lines.append("\nStill naming a document that left, in files the socle does not rewrite:")
        lines.extend(f"  {entry['path']}:{entry['line']} names {entry['names']}" for entry in data["remaining"])
    if data.get("globs"):
        lines.append("\nNaming docs/ as a whole, so matching fewer files from now on:")
        lines.extend(f"  {entry['path']}:{entry['line']} {entry['names']}" for entry in data["globs"])
    return "\n".join(lines) + ("\n" if lines else "")


def rewrite_readme(clone: pathlib.Path, product: str, data: dict) -> str:
    """Point the README at the prose that left, and say nothing more.

    The socle does not rewrite a product's own README: it cannot say what the
    product is. It replaces the links to the documents that moved with one
    line naming where they went, and leaves every other section untouched.

    That line does not name a destination a reader cannot open. maelys-docs
    is private, and a public README naming it sends the reader to a 404 and
    plants the private reference the fleet audit blocks a repository on
    before opening it. The managed AGENTS.md block is where an agent reads
    the destination; the README is read by strangers. When the destination
    cannot be checked, the socle assumes it cannot be opened: naming it
    wrongly is worse than naming nothing.
    """
    readme = clone / "README.md"
    if not readme.is_file():
        return "absent"
    moved = {entry["path"] for entry in data["moving"]}
    kept, dropped = [], 0
    for line in readme.read_text(encoding="utf-8").split("\n"):
        if any(path in line for path in moved):
            dropped += 1
            continue
        kept.append(line)
    if not dropped:
        return "unchanged: it named none of them"
    text = "\n".join(kept)
    public = destination_is_public(data["repository"])
    if public:
        pointer = f"The documentation of {product} is in `{data['repository']}`, directory `{product}/`.\n"
    else:
        pointer = f"The documentation of {product} is no longer kept in this repository.\n"
    if "\n## Documentation\n" in text:
        head, _, tail = text.partition("\n## Documentation\n")
        rest = tail.split("\n## ", 1)
        text = head + "\n## Documentation\n\n" + pointer + ("\n## " + rest[1] if len(rest) > 1 else "")
    else:
        text = text.rstrip("\n") + "\n\n## Documentation\n\n" + pointer
    readme.write_text(re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n", encoding="utf-8")
    if public:
        return f"{dropped} link lines replaced by one pointer"
    reason = "is private" if public is False else "could not be checked, so it is assumed private"
    return (f"{dropped} link lines replaced by one pointer naming no repository:"
            f" {data['repository']} {reason}. Point the README at the product's site by hand")


def handle_migrate(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Move a product's prose into maelys-docs, with its history, and out of the product.

    Both sides of one move: without the removal the prose is duplicated, and
    without the addition it is lost. maelys-docs receives a branch carrying
    the rewritten history; the product receives a pull request, never a
    direct write.
    """
    data, decl = plan_with_declarations(invocation, context)
    if not invocation.flag("--apply"):
        return data, EXIT_OK
    data["mode"] = "apply"
    if not data["tag"]:
        raise Failure("PRECONDITION_FAILED",
                      f"{data['product']} has no tag, so the prose would describe no release.",
                      "Release the product first: maelys-docs pins the tag whose prose it carries.")
    project = pathlib.Path(data["project"])
    product = data["product"]
    base = os.environ.get("MAELYS_GIT_BASE", "https://github.com/maelys-dev")
    branch = f"migrate/{product}"
    with tempfile.TemporaryDirectory(prefix="maelys-release-migrate.") as temp:
        work = pathlib.Path(temp)
        # The history is rewritten on a throwaway clone of the product, never
        # on the product itself: filter-repo rewrites every commit it keeps.
        source = work / "source"
        git("clone", "-q", str(project), str(source))
        paths = [argument for entry in data["moving"] for argument in ("--path", entry["path"])]
        run(["git", "filter-repo", *paths, "--path-rename", f"docs/:{product}/", "--force"], cwd=source)
        if not (source / product).is_dir():
            raise Failure("FAILED", "git filter-repo left no history on the new path.",
                          "Check that git-filter-repo is installed and that the documents are tracked.")
        documents = work / "documents"
        git("clone", "-q", f"{base}/{data['repository'].split('/')[-1]}.git", str(documents))
        author_identity(documents)
        git("checkout", "-q", "-B", branch, cwd=documents)
        git("remote", "add", "source", str(source), cwd=documents)
        git("fetch", "-q", "source", cwd=documents)
        head = git("rev-parse", "source/HEAD", cwd=documents, check=False) or git("rev-parse", "source/main", cwd=documents)
        git("merge", "--allow-unrelated-histories", "--no-edit", "-q", head, cwd=documents)
        # The pin says which release of the product this prose describes, in
        # the one format the fleet uses: dependencies/<name>.pin, tag on line
        # 1 and commit on line 2. The socle refuses adapter/<NAME>_PIN in a
        # product since 0.14.0, so it does not write one here either.
        (documents / "dependencies").mkdir(exist_ok=True)
        (documents / "dependencies" / f"{product}.pin").write_text(
            f"{data['tag']}\n{data['commit']}\n", encoding="utf-8")
        (documents / product / "VERSION").write_text(f"{data['version']}\n", encoding="utf-8")
        git("add", "-A", cwd=documents)
        if not run(["git", "diff", "--cached", "--quiet"], cwd=documents).returncode:
            raise Failure("PRECONDITION_FAILED",
                          f"{data['repository']} already carries this prose of {product}: nothing to add.",
                          "The documents have been migrated already; re-run maelys-platform's list to see"
                          " what is left, or migrate another product.")
        # No commit.gpgsign=false: it did not merely skip a signature, it
        # switched off one the operator had asked for -- so on a default
        # branch that requires signed commits, a migration opened a branch
        # that could not merge, by construction.
        git("commit", "-q", "-m",
            f"{product}: the prose of {data['tag'] or data['commit'][:7]}, with its history", cwd=documents)
        data["documentsBranch"] = branch
        data["documentsCommit"] = git("rev-parse", "HEAD", cwd=documents)

        # The product side: the same move, seen from the repository that
        # loses the documents. It travels as a branch for a pull request,
        # never as a write into the operator's checkout.
        product_clone = work / "product"
        git("clone", "-q", str(project), str(product_clone))
        author_identity(product_clone)
        git("checkout", "-q", "-B", branch, cwd=product_clone)
        for entry in data["moving"]:
            git("rm", "-q", "--", entry["path"], cwd=product_clone)
        data["readme"] = rewrite_readme(product_clone, product, data)
        data["rewritten"] = rewrite_markdown(product_clone, product, data, decl.docs_named)
        data["remaining"], data["globs"] = surviving_references(product_clone, data, decl.docs_named)
        # The same predicate as plan's, on what this side is about to commit:
        # a rewrite that names the destination in a repository that does not
        # declare the word is refused here, whatever produced it.
        if not decl.docs_named:
            for path in sorted(product_clone.rglob("*.md")):
                relative = path.relative_to(product_clone).as_posix()
                if ".git/" in path.as_posix() or relative in {entry["path"] for entry in data["moving"]}:
                    continue
                if relative in (*data["rewritten"], "README.md") and names_documentation(path.read_text(encoding="utf-8")):
                    raise Failure("PRECONDITION_FAILED",
                                  f"the migration would write the name of the documentation repository into"
                                  f" {relative}, and {product} does not declare [docs] named.",
                                  "This is a defect of the socle's rewriting, not of the product: report it.")
        git("add", "-A", cwd=product_clone)
        git("commit", "-q", "-m",
            f"docs: the prose moves to {data['repository']}/{product}/\n\n"
            f"{data['documents']} documents, with their history, are now in {data['repository']}."
            " This side removes them, points the README there and rewrites the Markdown that"
            " named them.\n" + migration_notes(data), cwd=product_clone)
        data["productBranch"] = branch
        data["productCommit"] = git("rev-parse", "HEAD", cwd=product_clone)

        data["pushed"] = bool(invocation.flag("--push"))
        if data["pushed"]:
            push_branch(documents, "origin", branch)
            # The clone was made from the operator's checkout, so its origin
            # is a local path: the product's own remote is asked for here.
            remote = git("remote", "get-url", "origin", cwd=project, check=False) or f"{base}/{product}.git"
            push_branch(product_clone, remote, branch)
        else:
            # Without --push both sides stay reviewable on disk: a migration
            # writes into two repositories, so it is never pushed by default.
            kept = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") \
                / "maelys-release" / f"migrate-{product}"
            shutil.rmtree(kept, ignore_errors=True)
            kept.mkdir(parents=True)
            shutil.copytree(documents, kept / "documents")
            shutil.copytree(product_clone, kept / "product")
            data["kept"] = str(kept)
    return data, EXIT_OK


def text_migrate(data: dict) -> str:
    lines = [f"move     {entry['path']} -> {entry['destination']}"
             + (f"  (still named by {', '.join(entry['referencedBy'])})" if entry["referencedBy"] else "")
             for entry in data["moving"]]
    lines.extend(f"stays    {entry['path']}  ({entry['reason']})" for entry in data["staying"])
    label = data["tag"] or data["commit"][:7]
    if data["mode"] == "plan":
        lines.append(f"migrate: plan only; {data['documents']} documents of {data['product']} {label}"
                     f" would move to {data['repository']}/{data['product']}/, with their history")
        lines.append("migrate: add --apply to write both sides, --push to send the two branches")
        return "\n".join(lines) + "\n"
    lines.append(f"readme   {data['readme']}")
    for relative in data.get("rewritten", []):
        lines.append(f"pointed  {relative}")
    for entry in data.get("remaining", []):
        lines.append(f"BY HAND  {entry['path']}:{entry['line']} still names {entry['names']}")
    for entry in data.get("globs", []):
        lines.append(f"BY HAND  {entry['path']}:{entry['line']} names docs/ as a whole"
                     f" and will match fewer files: {entry['names']}")
    lines.append(f"migrate: {data['repository']} branch {data['documentsBranch']} ({data['documentsCommit'][:7]}),"
                 f" {data['product']} branch {data['productBranch']} ({data['productCommit'][:7]})")
    lines.append("migrate: pushed; open one pull request on each side"
                 if data["pushed"] else f"migrate: nothing pushed; both clones are in {data['kept']}")
    return "\n".join(lines) + "\n"
