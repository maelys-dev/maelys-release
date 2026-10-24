# SPDX-License-Identifier: MPL-2.0
"""Plan and apply the two sides of a prose migration through the current host."""
from __future__ import annotations

import json
import os
import pathlib
import posixpath
import re
import shutil
import sys
import tempfile
import urllib.parse

from maelys_cli import EXIT_OK, Failure, Invocation
from . import reading

from .checkouts import author_identity, git_base
from .constants import CLI_CONTRACT, CLI_REFERENCE
from .context import Context
from .declarations import Declarations
from .forge import forge
from .github import destination_is_public, github_repository
from .host import git, run
from .project import names_git_gave, declared_mechanism, engaged_documents, project_of
from .texts import is_generated, names_documentation
from .writes import confirm_ref


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
    # git writes a name as the repository holds it where core.quotePath is
    # off, and one that is not UTF-8 ended `migrate` here on a decoding
    # error, as it did at the two readings of describe_the_clone.
    try:
        completed = run(["git", "grep", "-l", "--fixed-strings", "--", relative], cwd=project)
    except UnicodeDecodeError as error:
        raise names_git_gave(error) from None
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
    decl = reading.read_declarations(context, project, product, declared_mechanism(project))
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
    confirm_ref(clone, remote, f"refs/heads/{branch}", git("rev-parse", "HEAD", cwd=clone),
                f"the branch {branch}",
                f"Read it: git ls-remote {remote} refs/heads/{branch}. Both sides of a migration are"
                " reviewed before anything is merged; run the command again once it is understood.")


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
                # The README keeps a path its prose names, and never names
                # the destination: whoever reviews says where it points.
                if document in line and (path.suffix.lower() != ".md" or not named or relative == "README.md"):
                    references.append({"path": relative, "line": number, "names": document})
            # A build or packaging rule that names docs/ as a whole installs
            # or copies whatever it matches, and that set is about to shrink.
            if (path.name in BUILD_FILES or relative.startswith(("scripts/", "packaging/"))) \
                    and "docs/" in line and "*" in line:
                globs.append({"path": relative, "line": number, "names": line.strip()[:80]})
    return references, globs


# A Markdown link, inline or image: its text, its target, and a title.
LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*(<[^>\n]*>|[^)\s]+)((?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?)\s*\)")
# A reference definition, `[label]: target "title"`, on a line of its own. A
# footnote, `[^1]: text`, is not one: its text is prose, not a target.
DEFINITION = re.compile(r"^( {0,3}\[([^\]\n^][^\]\n]*)\]:[ \t]*)(<[^>\n]*>|\S+)(.*)$", re.MULTILINE)
# A reference to a definition: `[text][label]`, `[label][]`.
REFERENCE = re.compile(r"(?<![!\]\\])\[([^\[\]\n]+)\]\[([^\[\]\n]*)\]")
# The shortcut `[label]` alone, which is not a footnote, a link or a definition.
SHORTCUT = re.compile(r"(?<![!\]\\])\[([^\[\]\n^][^\[\]\n]*)\](?![\[(:])")
# What leads an item of a list or a row of a table.
LED = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+|\|\s*)")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")


def in_prose(text: str, change) -> str:
    """TEXT with CHANGE applied to its prose alone, never to its code.

    A fenced block and a code span are code: `[file](../Makefile)` in a
    shell block is a command, and `a[1]` in C an index. Each run of prose
    lines goes to CHANGE whole, its code spans held out of reach.
    """
    blocks, current, fence = [], [], None
    for line in text.split("\n"):
        opening = FENCE.match(line)
        if fence is None and opening:
            blocks.append((False, current))
            current, fence = [line], opening.group(1)
            continue
        current.append(line)
        if fence is not None and opening and opening.group(1)[0] == fence[0] \
                and len(opening.group(1)) >= len(fence) and not line.strip()[len(opening.group(1)):]:
            blocks.append((True, current))
            current, fence = [], None
    blocks.append((fence is not None, current))
    out = []
    for code, lines in blocks:
        if not lines:
            continue
        block = "\n".join(lines)
        if not code:
            spans = []

            def hold(match: re.Match) -> str:
                spans.append(match.group(0))
                return f"\x00{len(spans) - 1}\x00"

            block = CODE_SPAN.sub(hold, block)
            block = change(block)
            block = re.sub(r"\x00([0-9]+)\x00", lambda match: spans[int(match.group(1))], block)
        out.append(block)
    return "\n".join(out)


def prose_of(text: str) -> str:
    """The prose of TEXT, its code blanked: what its definitions are read from."""
    held = []
    in_prose(text, lambda block: held.append(block) or block)
    return "\n".join(held)


def link_path(owner: str, target: str) -> tuple[str, str] | None:
    """The repository path a relative link of the file OWNER names, and its anchor.

    None for what names no file of the repository: a URL, an anchor of the
    page itself, a path above the root. A path that starts with `/` is read
    from the root, as the forge reads it. 0.64.7 compared the target with
    `docs/x.md` as written, so a link from docs/ itself (`x.md`,
    `../docs/x.md`) and a link with an anchor (`docs/x.md#part`) stayed
    pointing at a document that had left, and nothing said so.
    """
    target = target[1:-1] if target.startswith("<") and target.endswith(">") else target
    if not target or target.startswith(("#", "//")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
        return None
    path, _, anchor = target.partition("#")
    path = urllib.parse.unquote(path.split("?", 1)[0])
    base = "" if path.startswith("/") else posixpath.dirname(owner)
    resolved = posixpath.normpath(posixpath.join(base, path.lstrip("/")))
    if resolved in ("..", ".") or resolved.startswith("../"):
        return None
    return resolved, anchor


def names_moved(owner: str, target: str, moved: set[str]) -> bool:
    """Whether TARGET, in the file OWNER, links a moved document.

    Read from its file first, then from the root: `examples/README.md`
    linking `docs/guide.md` meant the guide, whatever a renderer makes of it,
    and it named the guide when 0.64.7 compared paths as written.
    """
    return any(found is not None and found[0] in moved
               for found in (link_path(owner, target), link_path("", target)))


def moved_labels(text: str, owner: str, moved: set[str]) -> set[str]:
    """The labels whose definition names a moved document: the first one of each, as Markdown reads them."""
    first = {}
    for match in DEFINITION.finditer(prose_of(text)):
        first.setdefault(match.group(2).lower(), match.group(3))
    return {label for label, target in first.items() if names_moved(owner, target, moved)}


def unlink_moved(text: str, owner: str, moved: set[str]) -> tuple[str, int, int]:
    """TEXT of the file OWNER with every link to a moved document reduced to its text.

    A reference definition that names one goes, and each reference to it
    keeps its text. Returns the text, the links unlinked and the definitions
    dropped. An image keeps its link: a moved document is prose, not a
    picture. Code is never touched.
    """
    labels = moved_labels(text, owner, moved)
    unlinked = dropped = 0

    def inline(match: re.Match) -> str:
        nonlocal unlinked
        if match.group(1) or not names_moved(owner, match.group(3), moved):
            return match.group(0)
        unlinked += 1
        return match.group(2)

    def reference(match: re.Match) -> str:
        nonlocal unlinked
        label = match.group(2) if len(match.groups()) > 1 and match.group(2) else match.group(1)
        if label.lower() not in labels:
            return match.group(0)
        unlinked += 1
        return match.group(1)

    def change(block: str) -> str:
        nonlocal dropped
        kept = []
        for line in block.split("\n"):
            definition = DEFINITION.fullmatch(line)
            if definition and names_moved(owner, definition.group(3), moved):
                dropped += 1
                continue
            kept.append(line)
        block = LINK.sub(inline, "\n".join(kept))
        if labels:
            block = REFERENCE.sub(reference, block)
            block = SHORTCUT.sub(reference, block)
        return block

    return in_prose(text, change), unlinked, dropped


def point_outward(root: pathlib.Path, product: str, moved: set[str], repository: str, ref: str) -> tuple[int, list[dict]]:
    """Point what the moved documents link in the product at the product, at REF.

    A moved document keeps its relative links to another moved document,
    which stay valid where they land; a link to a file that stays in the
    product (`../src/x.c`, `../include/x.h`, a document that does not move)
    names nothing in the repository that receives it -- datalog carried 18
    of them into maelys-docs, rewritten by hand. It becomes a link to that
    file of the product at REF, an image to the file itself rather than to
    its page. Without a repository to name, the links stay and are reported.
    """
    rewritten, left = 0, []
    for document in sorted(moved):
        landed = root / product / document[len("docs/"):]
        if landed.suffix.lower() != ".md" or not landed.is_file():
            continue
        try:
            text = landed.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            left.append({"path": f"{product}/{document[len('docs/'):]}", "names": "(not UTF-8: not read)"})
            continue

        def outward(target: str, image: bool = False) -> str | None:
            found = link_path(document, target)
            if found is None or found[0] in moved:
                return None
            path, anchor = found
            if not repository:
                left.append({"path": f"{product}/{document[len('docs/'):]}", "names": target})
                return None
            return forge().file_url(repository, ref, path, raw=image) + (f"#{anchor}" if anchor else "")

        def inline(match: re.Match) -> str:
            nonlocal rewritten
            url = outward(match.group(3), image=bool(match.group(1)))
            if url is None:
                return match.group(0)
            rewritten += 1
            return f"{match.group(1)}[{match.group(2)}]({url}{match.group(4)})"

        def definition(match: re.Match) -> str:
            nonlocal rewritten
            url = outward(match.group(3))
            if url is None:
                return match.group(0)
            rewritten += 1
            return f"{match.group(1)}{url}{match.group(4)}"

        changed = in_prose(text, lambda block: DEFINITION.sub(definition, LINK.sub(inline, block)))
        if changed != text:
            landed.write_text(changed, encoding="utf-8")
    return rewritten, left


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
        text, _, _ = unlink_moved(text, relative, set(moved))
        for document in moved:
            destination = f"{data['repository'].split('/')[-1]}/{product}/{document[len('docs/'):]}"
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


def product_message(product: str, data: dict, named: bool) -> str:
    """The commit on the product's side, under the rule its files are under.

    A commit message of a public repository is as public as its README. The
    files were held to `[docs] named` and this was not: maelys-system, which
    does not declare the word, received the name of the documentation
    repository in the one line the refusal above did not read, and its
    operator rewrote the message by hand before merging.
    """
    documents = f"{data['documents']} document{'s' if data['documents'] != 1 else ''}"
    if named:
        return (f"docs: the prose moves to {data['repository']}/{product}/\n\n"
                f"{documents}, with their history, are now in {data['repository']}."
                " This side removes them, points the README there and rewrites the Markdown that"
                " named them.\n" + migration_notes(data))
    return ("docs: the prose leaves this repository\n\n"
            f"{documents} are no longer kept here; their history is kept with them."
            " This side removes them, says so in the README and rewrites the Markdown that"
            " named them.\n" + migration_notes(data))


def rewrite_readme(clone: pathlib.Path, product: str, data: dict) -> str:
    """Point the README at the prose that left, and say nothing more.

    The socle does not rewrite a product's own README: it cannot say what the
    product is. It replaces the lines that were links to the documents that
    moved -- an item of a list or a row of a table led by one, a reference
    definition -- with one line naming where they went. A link inside a
    sentence loses its link and keeps its text, as in every other Markdown
    file: 0.64.7 dropped the whole line, and the README of datalog lost the
    middle of six sentences. A path named in prose stays, and is reported.

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
    text = readme.read_text(encoding="utf-8")
    labels = moved_labels(text, "README.md", moved)
    dropped = 0

    def led_by_a_moved_link(line: str) -> bool:
        marker = LED.match(line)
        if not marker:
            return False
        inline = LINK.match(line, marker.end())
        if inline:
            return not inline.group(1) and names_moved("README.md", inline.group(3), moved)
        reference = REFERENCE.match(line, marker.end())
        return bool(reference) and (reference.group(2) or reference.group(1)).lower() in labels

    def drop(block: str) -> str:
        nonlocal dropped
        lines, kept, emptied = block.split("\n"), [], False
        for line in lines:
            if led_by_a_moved_link(line):
                dropped += 1
                emptied = emptied or line.lstrip().startswith("|")
                continue
            kept.append(line)
        if emptied:
            # A table whose every row went keeps no header of its own.
            delimiter = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
            index = 0
            while index < len(kept) - 1:
                if kept[index].lstrip().startswith("|") and delimiter.fullmatch(kept[index + 1]) \
                        and not (index + 2 < len(kept) and kept[index + 2].lstrip().startswith("|")):
                    del kept[index:index + 2]
                    continue
                index += 1
        return "\n".join(kept)

    text = in_prose(text, drop)
    text, unlinked, definitions = unlink_moved(text, "README.md", moved)
    dropped += definitions
    if not dropped and not unlinked:
        return "unchanged: it named none of them"
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
    said = [f"{dropped} link lines replaced by one pointer" if dropped else "one pointer added"]
    if unlinked:
        said.append(f"{unlinked} link{'s' if unlinked != 1 else ''} in prose unlinked, their text kept")
    done = ", ".join(said)
    if public:
        return done
    reason = "is private" if public is False else "could not be checked, so it is assumed private"
    return (f"{done}, the pointer naming no repository:"
            f" {data['repository']} {reason}. Point the README at the product's site by hand")


def describe_the_clone(project: pathlib.Path, product: str, data: dict) -> None:
    """Hold what the migration says to the commit it clones, which is HEAD.

    The plan is read from the working tree and both sides are cloned from
    HEAD, so the two disagreed wherever the tree did. maelys-system migrated
    with VERSION at 0.12.3 uncommitted over a HEAD at 0.12.2: the prose of
    HEAD arrived under the version of the tree. And the pin took its tag
    from the nearest one and its commit from HEAD -- past the tag, a line 1
    and a line 2 that name two commits, which `dependencies` refuses in the
    repository that receives it.
    """
    try:
        changed = [line[3:] for line in run(["git", "status", "--porcelain", "--untracked-files=no"],
                                            cwd=project).stdout.splitlines() if line.strip()]
    except UnicodeDecodeError as error:
        raise names_git_gave(error, skip=3) from None
    if changed:
        raise Failure("PRECONDITION_FAILED",
                      f"{project} has uncommitted changes to {', '.join(changed)}: the migration clones HEAD,"
                      " so it would move a prose and state a version that are not the ones in this tree.",
                      "Commit or stash them, then run migrate again.")
    try:
        committed = set(git("ls-tree", "-r", "--name-only", "HEAD", "--", "docs", cwd=project,
                            check=False).splitlines())
    except UnicodeDecodeError as error:
        raise names_git_gave(error) from None
    absent = [entry["path"] for entry in data["moving"] if entry["path"] not in committed]
    if absent:
        raise Failure("PRECONDITION_FAILED",
                      f"{', '.join(absent)} of {product} is not committed: it has no history to move.",
                      "Commit it, or take it out of the list, then run migrate again.")
    data["version"] = git("show", "HEAD:VERSION", cwd=project).strip()
    # The nearest tag when HEAD is that tag, and `vX.Y.Z-N-gSHA` past it:
    # both resolve to the commit on line 2, which a bare nearest tag did not.
    data["tag"] = git("describe", "--tags", "HEAD", cwd=project)


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
    describe_the_clone(project, product, data)
    base = git_base()
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
        # A link out of the prose names the product where the prose was
        # taken: the tag when HEAD is one, its commit otherwise.
        ref = data["tag"] if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", data["tag"]) else data["commit"]
        data["outward"], data["outwardLeft"] = point_outward(
            documents, product, {entry["path"] for entry in data["moving"]}, github_repository(project), ref)
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
        git("commit", "-q", "-m", product_message(product, data, decl.docs_named), cwd=product_clone)
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
    if data.get("outward"):
        lines.append(f"outward  {data['outward']} link{'s' if data['outward'] != 1 else ''} of the moved prose"
                     f" to files that stay now name {data['product']} where the prose was taken")
    for entry in data.get("outwardLeft", []):
        lines.append(f"BY HAND  {entry['path']} links {entry['names']}, which stays in {data['product']}:"
                     " its origin is not on the forge, so the link names nothing where the prose lands")
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
