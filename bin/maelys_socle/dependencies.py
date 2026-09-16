# SPDX-License-Identifier: MPL-2.0
"""Read dependency layouts and materialise the product's pinned checkouts."""
from __future__ import annotations

import pathlib
import re

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from .checkouts import cache_root, checkout_state, git_base, materialise
from .constants import DECLARATION_FILE, FORMER_DECLARATION_FILE, PROGRAM, SOCLE_REPOSITORY
from .context import Context
from .declarations import Declarations, parse_pin
from .host import git, run
from .inspection import pinned_socle
from .project import project_of


DEPENDENCIES_VARIABLE = "MAELYS_DEPENDENCIES_DIR"
# The socle under a dependency root, which is not a pin: its commit is the
# one on the `uses:` line, and a dependencies/maelys-release.pin would be a
# second source for one commit.
SOCLE_NAME = SOCLE_REPOSITORY.split("/")[-1]
SOCLE_VARIABLE = "MAELYS_RELEASE_DIR"
# The prefix a note carries when it is not a note: `dependencies` collects
# these in one list, and this is what turns one line of it into a violation.
REFUSED = "REFUSED"
# The two managed clone scripts: one pin, or every pin into a given root.
CHECKOUT_SCRIPTS = ("scripts/checkout-dependency.sh", "scripts/checkout-dependencies.sh")
# A call of the singular with no destination: it clones beside the product,
# which a build that reads the root no longer looks at.
BESIDE_CALL = re.compile(r"checkout-dependency\.sh\s+[a-z0-9-]+\s*(&&|$)")
# Evidence that a file READS the root, which is what the exemption below
# means. Naming the variable is not reading it: a comment of one product's
# ci.yml says what MAELYS_DEPENDENCIES_DIR means, and that sentence alone
# was exempting the three real calls under it. A sigil, a bracket, a quote
# or an assignment -- `$VAR`, `${VAR}`, `$(VAR)`, `VAR=`, `VAR ?=`, `ENV
# VAR=` -- is a use; the bare word in prose is not.
DEPENDENCIES_READ = re.compile(r"[$({\[\"']\s*" + DEPENDENCIES_VARIABLE
                               + r"|" + DEPENDENCIES_VARIABLE + r"\s*[?:+]?[=:]"
                               + r"|env\.\s*" + DEPENDENCIES_VARIABLE)
# Where a line naming ../NAME is prose about this very migration rather than
# something that builds: the declaration file says why the fallback went, and
# a changelog says when. Markdown goes with them -- a README telling a human
# to clone next door is worth fixing, and not worth a note on every product
# forever. Comments are not exempt: three of the lines this rule found were
# comments of a Dockerfile, and their author changed all three.
SIBLING_EXEMPT = (DECLARATION_FILE, FORMER_DECLARATION_FILE) + CHECKOUT_SCRIPTS


def sibling_readers(project: pathlib.Path, names: list[str]) -> list[tuple[str, int, str, str]]:
    """Tracked lines that still assume a checkout beside the product.

    Yields (path, line number, dependency name, text); the name is empty
    when the line calls the one-pin script instead of naming a sibling
    path, which is the other way a tree reaches next door -- an image that
    clones each pin to a place of its own and exports none of them.

    Tracked files alone, which is the question: an untracked build tree
    naming a sibling is nobody's contract. Outside a repository `git grep`
    answers nothing and the rule stays quiet rather than walking whatever
    the directory happens to hold.

    A file that names the root variable somewhere has been through the
    migration, and what it still says about a sibling is prose about it: a
    Makefile explaining why the fallback went, a comment naming the
    neighbour it no longer reads. Measured on two products -- before its
    fixes maelys-egress had four such files and not one named the variable,
    after them all four did, and maelys-http, migrated, named it in both
    files this rule would otherwise have reported forever.

    The one line that escapes that exemption is the one that names both:
    a fallback kept beside a root that is already read is the prudent
    fallback nobody ever sees taken, which is the whole reason the ambient
    default went.
    """
    if not names:
        return []
    alternatives = "|".join(re.escape(name) for name in names)
    # The name must end at the match: ../maelys-system and ../maelys-systemd
    # are different neighbours, and only one of them is pinned here.
    sibling = re.compile(r"\.\./(" + alternatives + r")(?![A-Za-z0-9._-])")
    # A call clones a pin, so what follows the script is a pin's name or a
    # shell expansion. Naming the script is not calling it: one product's
    # source policy exempts it by name in a Python tuple, another's Makefile
    # explains in a comment what it fetches, and both read as calls to
    # anything looser. That is the shape of my own first fix, which would
    # have traded one false line for another.
    call = re.compile(r"checkout-dependency\.sh[\"']?\s+[\"']?"
                      r"(?:\$|(?:" + alternatives + r")(?![A-Za-z0-9._-]))")
    found = run(["git", "grep", "-n", "-I", "-E",
                 "-e", r"\.\./(" + alternatives + ")",
                 "-e", r"checkout-dependency\.sh"], cwd=project)
    migrated: dict[str, bool] = {}
    readers = []
    for line in found.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        if not number.isdigit() or path.endswith(".md") or path in SIBLING_EXEMPT:
            continue
        if path not in migrated:
            whole = project / path
            text_of = whole.read_text(encoding="utf-8", errors="replace") if whole.is_file() else ""
            # A reusable workflow runs in the repository that calls it, not in
            # the one that ships it: its lines describe its callers' layout.
            # The socle's check-product.yml clones beside the product for the
            # products that have not declared apart, and checking the socle
            # against itself read those lines as the socle's own.
            if path.startswith(".github/workflows/") and re.search(r"^\s*workflow_call\s*:", text_of, re.MULTILINE):
                migrated[path] = None
            # One exemption per root, not one per file: a Makefile that reads
            # $MAELYS_DEPENDENCIES_DIR has migrated its pins and may still
            # take the socle from ../maelys-release, which is exactly where
            # two products stand. The socle's root is its own variable.
            else:
                migrated[path] = (bool(DEPENDENCIES_READ.search(text_of)),
                                  bool(re.search(r"[$({\[\"']\s*" + SOCLE_VARIABLE
                                                 + r"|" + SOCLE_VARIABLE + r"\s*[?:+]?[=:]", text_of)))
        # The limit of the exemption, stated rather than hidden: a file that
        # reads the root and carries the fallback on ANOTHER line is silent
        # here. No product of the fleet is in that state today; the
        # one-line form, which is the one that has been written, is caught.
        if migrated[path] is None:
            continue
        match = sibling.search(text)
        root, variable = (migrated[path][1], SOCLE_VARIABLE) if match and match.group(1) == SOCLE_NAME \
            else (migrated[path][0], DEPENDENCIES_VARIABLE)
        if root and variable not in text:
            continue
        if match:
            readers.append((path, int(number), match.group(1), text.strip()))
        elif call.search(text):
            readers.append((path, int(number), "", text.strip()))
    return readers


def dependencies_home(product: str) -> pathlib.Path:
    """One flat directory per product for its pinned checkouts.

    Flat, because a dependency's own Makefile looks for ../NAME. Per
    product, because two products pin the same dependency at different
    commits and both are green: maelys-warden pins maelys-json v0.1.0 where
    maelys-oci pins v0.2.0. A directory shared across the fleet would make
    one of them wrong on every switch.
    """
    return cache_root() / "dependencies" / product


def dependency_repository(pin: dict) -> str:
    return pin["repository"] or f"{git_base()}/{pin['name']}.git"


def series(tag: str) -> str:
    """The compatibility series a tag belongs to: 0.Y below 1.0, X above.

    The socle does not know an ABI; it knows what the fleet's versions
    promise. Below 1.0 the minor is the break, above it the major is.
    """
    parts = tag.lstrip("v").split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return ""
    return f"0.{parts[1]}" if parts[0] == "0" else parts[0]


def neighbour_pins(decl: Declarations, name: str, path: pathlib.Path) -> list[str]:
    """What a materialised dependency pins itself, held against the product's.

    Not materialised, and deliberately: check-product.yml clones the pins of
    the product it builds and nothing below them, so a directory that held
    more would let a build pass here that CI refuses -- hiding a missing pin
    exactly where CI shows it. When a product's build needs what a
    dependency pins, the product pins it: maelys-oci pins maelys-json for
    the maelys-cli it builds.

    A different commit of the same series stays a note: the fleet disagrees
    today, maelys-warden pinning maelys-json v0.1.0 where maelys-oci pins
    v0.2.0, and both are green -- but those are two products, not one graph.
    Two series of one repository inside one graph is a refusal: a build links
    one and its own dependency was built against the other, which is the case
    maelys-git-core met and could not see. The socle knows the series, never
    the ABI, so a product ahead of what its dependency pins is caught too; no
    product of the fleet is in that state, and an escape hatch waits for one
    that is.
    """
    notes: list[str] = []
    refusals: list[str] = []
    pins_dir = path / "dependencies"
    for pin_file in sorted(pins_dir.glob("*.pin")) if pins_dir.is_dir() else []:
        pin, _problems = parse_pin(pin_file.name[: -len(".pin")], pin_file.read_text(encoding="utf-8"))
        if not pin:
            continue
        mine = decl.pins.get(pin["name"])
        if mine is None:
            notes.append(f"{name} pins {pin['name']} {pin['tag']} ({pin['commit'][:7]}), which {decl.product}"
                         f" does not: not materialised, as in CI; a build that needs it pins it here")
        elif mine["commit"] != pin["commit"]:
            theirs, ours = series(pin["tag"]), series(mine["tag"])
            if theirs and ours and theirs != ours:
                refusals.append(f"{name} pins {pin['name']} {pin['tag']} and {decl.product} pins"
                                f" {mine['tag']}: two series of one repository in one graph. What is"
                                f" materialised here is {mine['tag']}, so {name} is linked against"
                                " something it was not built for. Re-pin one of the two")
            else:
                notes.append(f"{name} pins {pin['name']} at {pin['tag']} ({pin['commit'][:7]}); {decl.product}"
                             f" pins {mine['tag']} ({mine['commit'][:7]}), which is what is here, as in CI")
    return notes + [f"{REFUSED} {message}" for message in refusals]


def handle_dependencies(invocation: Invocation, context: Context) -> tuple[dict, int]:
    """Every pin of the product at its commit, in a directory that is not a working copy."""
    project, product, mechanism = project_of(invocation)
    apply = invocation.flag("--apply")
    decl = context.read_declarations(project, product, mechanism)
    if decl.pin_problems:
        raise Failure("PRECONDITION_FAILED", "A pin cannot be read: " + "; ".join(decl.pin_problems) + ".",
                      f"Fix the pin; '{PROGRAM} check {project}' reports it.")
    chosen = str(invocation.option("--directory", "") or "")
    directory = (pathlib.Path(chosen).expanduser() if chosen else dependencies_home(product)).resolve()
    if directory == project or project in directory.parents:
        raise Failure("VALIDATION_FAILED", f"{directory} is the product or inside it.",
                      "The pinned checkouts live apart from the product; pass another --directory, or none.")
    # And not beside it either. <parent>/<name> is where the fleet's working
    # copies live -- ~/GitHubDocuments/maelys-cli is one of them -- and a
    # root there points every pin at somebody's checkout. This is the layout
    # 0.44.0 existed to leave behind; naming it explicitly costs one line.
    if directory == project.parent:
        raise Failure("VALIDATION_FAILED",
                      f"{directory} is where {project.name} itself sits, and where the working copies of"
                      " the repositories it pins sit too.",
                      "That is the layout these checkouts exist to replace; pass another --directory, or none.")
    entries: list[dict] = []
    blocked: list[str] = []
    # The socle's own root, where nothing but the socle writes: there the
    # location is proof enough and a checkout made before the mark existed is
    # marked in place. A root the operator chose gets no such benefit.
    trusted = directory == dependencies_home(product)
    for name in decl.dependencies:
        pin = decl.pins[name]
        repository = dependency_repository(pin)
        path = directory / name
        action, reason, head = checkout_state(path, pin, repository, trusted)
        entry = {"name": name, "tag": pin["tag"], "commit": pin["commit"], "repository": repository,
                 "path": str(path), "action": action}
        if pin["submodules"] is not None:
            entry["submodules"] = pin["submodules"]
        if action == "blocked":
            entry["reason"] = reason
            blocked.append(reason)
        elif action == "refresh":
            entry["previous"] = head
        entries.append(entry)
    if apply and blocked:
        # Before the first clone: a directory half materialised is the state
        # the report describes, and the operator frees one path and reruns.
        raise Failure("PRECONDITION_FAILED", "Nothing written: " + "; ".join(blocked) + ".",
                      "Free these paths by hand -- they are working copies, or not the socle's -- or pass"
                      " --directory; the socle never deletes a checkout.")
    if apply:
        for entry in entries:
            materialise(pathlib.Path(entry["path"]), decl.pins[entry["name"]],
                        entry["repository"], entry["action"])
    notes: list[str] = []
    for entry in entries:
        if entry["action"] == "blocked" or (entry["action"] == "clone" and not apply):
            continue
        path = pathlib.Path(entry["path"])
        # Left in place: a build, most likely, and the fleet builds its
        # dependencies out of tree so as not to leave any.
        untracked = [line for line in
                     run(["git", "ls-files", "--others", "--exclude-standard"], cwd=path).stdout.split("\n") if line]
        if untracked:
            notes.append(f"{entry['name']}: {len(untracked)} untracked file(s) left in place,"
                         f" {untracked[0]} first")
        if entry["name"] not in decl.foreign:
            notes.extend(neighbour_pins(decl, entry["name"], path))
    refused: list[str] = []
    # A pin that names a tag it does not point at, and a pin on no tag at
    # all. Read from the checkout in hand, so this stays offline: a partial
    # clone still carries every ref. maelys-git-core pinned an untagged
    # commit of a branch, the branch was rewritten, the commit is gone from
    # everywhere, and the repository no longer configures -- nothing had ever
    # told it that its pin was not a tag.
    for entry in entries:
        pin = decl.pins.get(entry["name"])
        path = directory / entry["name"]
        if not pin or entry["action"] == "blocked" or not (path / ".git").exists():
            continue
        named = git("rev-list", "-n", "1", pin["tag"], cwd=path, check=False)
        if named and named != pin["commit"]:
            refused.append(f"{entry['name']}: line 1 names {pin['tag']}, which is {named[:7]}, and line 2"
                           f" is {pin['commit'][:7]}. A published tag never moves, so one of the two is"
                           " wrong; re-pin on the tag's own commit")
        elif not named and not git("tag", "--points-at", pin["commit"], cwd=path, check=False):
            notes.append(f"{entry['name']}: {pin['commit'][:7]} carries no tag. A rewritten branch takes"
                         " an untagged commit with it, and a pin on one stops resolving everywhere at"
                         " once -- for the socle, for CI, and for whoever clones next")
    # And the socle itself, under the same root. Its commit is not a pin: it
    # is the one on the `uses:` line, which `pinned_socle` reads. CI has
    # cloned it at that commit into $RUNNER_TEMP since 0.41.0, so giving the
    # same root to a machine removes an asymmetry rather than creating one --
    # which is the objection I made when maelys-oci first asked, and it was
    # wrong.
    pinned = pinned_socle(project)
    if pinned:
        socle_pin = {"name": SOCLE_NAME, "tag": pinned["tag"], "commit": pinned["sha"],
                     "repository": "", "submodules": None}
        socle_path = directory / SOCLE_NAME
        action, reason, head = checkout_state(socle_path, socle_pin,
                                              f"{git_base()}/{SOCLE_NAME}.git", trusted)
        entries.append({"name": SOCLE_NAME, "tag": pinned["tag"], "commit": pinned["sha"],
                        "repository": f"{git_base()}/{SOCLE_NAME}.git", "path": str(socle_path),
                        "action": action, "reason": reason} if action == "blocked"
                       else {"name": SOCLE_NAME, "tag": pinned["tag"], "commit": pinned["sha"],
                             "repository": f"{git_base()}/{SOCLE_NAME}.git",
                             "path": str(socle_path), "action": action})
        if action == "blocked":
            blocked.append(socle_path.name)
        elif apply:
            materialise(socle_path, socle_pin, f"{git_base()}/{SOCLE_NAME}.git", action)
    notes.extend(f"{REFUSED} {message}" for message in refused)
    caught = [note for note in notes if note.startswith(f"{REFUSED} ")]
    data = {"mode": "apply" if apply else "plan", "product": product, "project": str(project),
            "directory": str(directory), "variable": DEPENDENCIES_VARIABLE,
            "declared": decl.dependencies_apart, "refused": [note[len(REFUSED) + 1:] for note in caught],
            "socle": str(directory / SOCLE_NAME) if pinned else "",
            "dependencies": entries, "notes": [note for note in notes if note not in caught],
            "blocked": bool(blocked)}
    return data, EXIT_VIOLATIONS if blocked or caught else EXIT_OK


def text_dependencies(data: dict) -> str:
    lines = []
    for entry in data["dependencies"]:
        head = f"{entry['name']} {entry['tag']} ({entry['commit'][:7]})"
        if entry["action"] == "clone":
            lines.append(f"{'clone':<8} {head} from {entry['repository']}")
        elif entry["action"] == "refresh":
            lines.append(f"{'refresh':<8} {head}, here {entry['previous'][:7]}")
        elif entry["action"] == "blocked":
            lines.append(f"{'BLOCKED':<8} {entry['name']}: {entry['reason']}")
        else:
            lines.append(f"{'same':<8} {head}")
    lines.extend(f"{'REFUSED':<8} {message}" for message in data.get("refused", []))
    lines.extend(f"{'note':<8} {note}" for note in data["notes"])
    count = len(data["dependencies"])
    if not count:
        lines.append(f"dependencies: {data['product']} pins nothing")
    elif data["blocked"]:
        lines.append("dependencies: nothing written; free the blocked paths, or pass --directory")
    elif data["mode"] == "apply":
        # Printed as an assignment so a shell can take it whole: the path is
        # given to the build, and giving it by hand is where a typo lives.
        lines.append(f"export   {data['variable']}={data['directory']}")
        if data.get("socle"):
            lines.append(f"export   {SOCLE_VARIABLE}={data['socle']}")
        if not data.get("declared"):
            lines.append(f"note     {data['product']} does not declare '[dependencies] apart' yet:"
                         " its build still assumes a sibling, and reading the variable is what"
                         " lets the declaration be true")
        lines.append(f"dependencies: {count} pinned checkout(s) in {data['directory']}")
    else:
        lines.append(f"dependencies: {count} pinned checkout(s) would be in {data['directory']}; add --apply")
    return "\n".join(lines) + "\n"
