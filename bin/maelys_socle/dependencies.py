# SPDX-License-Identifier: MPL-2.0
"""Read dependency layouts and materialise the product's pinned checkouts."""
from __future__ import annotations

import os
import pathlib
import re

from maelys_cli import EXIT_OK, EXIT_VIOLATIONS, Failure, Invocation

from . import host, reading
from .checkouts import cache_root, checkout_state, git_base, materialise
from .constants import DECLARATION_FILE, FORMER_DECLARATION_FILE, PROGRAM, SOCLE_REPOSITORY
from .context import Context
from .declarations import Declarations, parse_pin
from .host import git, run
from .product_workflows import pinned_socle
from .project import is_utf8, project_of, spelled, unread_names


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
# What a question put to a repository may cost: answered in that many seconds
# by the clock, or given up on. git's own low-speed settings, which the
# managed script asks for the socle's tag with, cut one state only, an
# http(s) transfer that has stopped moving. Measured on this very question: a
# TLS handshake that never starts waited 300 seconds, and a remote that
# trickles, ssh with no banner and git:// were never cut. --apply over a
# checkout already at its pin reached no repository before this question
# existed: it now waits this long at most for each pin it asks about, and as
# long again where there are tags to bring.
PATIENCE = 10
# And it asks nobody: git is told not to prompt on a terminal, and the host
# gives a bounded call none, which leaves ssh nothing to ask on either. An
# askpass program and a credential helper stay as configured -- a job answers
# through them -- and wait no longer than the clock.
UNATTENDED = {"GIT_TERMINAL_PROMPT": "0"}
# What `git describe` says of a commit past a tag, as git reads it back, or a
# hash alone: measured on git 2.50, `<name>-<count>-g<hash>` with four to
# forty digits of either case. An older git also took `<anything>-g<hash>`.
BY_ITS_HASH = re.compile(r"(?:.+-[0-9]+-g)?([0-9a-fA-F]{4,40})")


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
    search = ["git", "grep", "-n", "-I", "-E",
              "-e", r"\.\./(" + alternatives + ")",
              "-e", r"checkout-dependency\.sh"]
    try:
        found = run(search, cwd=project).stdout
    except UnicodeDecodeError:
        # git writes the line it matched as the file holds it, and a line
        # need not be UTF-8: a comment in Latin-1 beside the match raised
        # here, and the caller read the error as a refusal of the
        # declaration file. Asked again as bytes, and only then. A name is
        # still read strictly -- one that is not UTF-8 is the refusal of
        # the tracked list, and no file is opened by a spelled name -- and
        # what does not decode in a line is spelled out.
        answer = host.HOST.feed(search, b"", cwd=project).stdout
        strange = sorted({record.partition(b":")[0] for record in answer.split(b"\n")
                          if not is_utf8(record.partition(b":")[0])})
        if strange:
            raise unread_names(strange) from None
        found = spelled(answer)
    migrated: dict[str, bool] = {}
    readers = []
    for line in found.splitlines():
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
    decl = reading.read_declarations(context, project, product, mechanism)
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
    everything = invocation.flag("--all")
    for name in decl.dependencies:
        pin = decl.pins[name]
        repository = dependency_repository(pin)
        path = directory / name
        if pin.get("on_request") and not everything:
            # What the managed script does in a job, done here: the pin is
            # named, with what would bring it, and nothing clones it. A
            # developer who runs the one job that reads it passes --all.
            entries.append(dependency_entry(pin, repository, path, "on-request", "", ""))
            continue
        action, reason, head = checkout_state(path, pin, repository, trusted)
        entries.append(dependency_entry(pin, repository, path, action, reason, head))
        if action == "blocked":
            blocked.append(reason)
    if apply and blocked:
        # Before the first clone: a directory half materialised is the state
        # the report describes, and the operator frees one path and reruns.
        raise Failure("PRECONDITION_FAILED", "Nothing written: " + "; ".join(blocked) + ".",
                      "Free these paths by hand -- they are working copies, or not the socle's -- or pass"
                      " --directory; the socle never deletes a checkout.")
    if apply:
        for entry in entries:
            if entry["action"] == "on-request":
                continue
            pin = decl.pins[entry["name"]]
            try:
                materialise(pathlib.Path(entry["path"]), pin, entry["repository"], entry["action"])
            except Failure as failure:
                if not pin.get("unreleased"):
                    raise
                # The day this line exists for. git's own words for it are
                # "Server does not allow request for unadvertised object",
                # raised here before anything was judged: the pin and its
                # branch were never named.
                raise unobtainable(pin, entry, failure) from None
    notes: list[str] = []
    for entry in entries:
        if entry["action"] in ("blocked", "on-request") or (entry["action"] == "clone" and not apply):
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
    # What line 1 says of line 2, held to the checkout in hand. A plan reads
    # that checkout and asks nothing, and git is kept from asking for it. A
    # clone carries every ref the repository had that day and no tag made
    # since -- a refresh fetches the commit by its hash, which follows none
    # -- so what the checkout lacks is said of the checkout, and of the
    # repository only once --apply has asked it. maelys-git-core pinned an
    # untagged commit of a branch, the branch was rewritten, the commit is
    # gone from everywhere, and the repository no longer configures --
    # nothing had ever told it that its pin was not a tag.
    for entry in entries:
        pin = decl.pins.get(entry["name"])
        path = directory / entry["name"]
        # An on-request pin this run did not bring is neither read nor asked.
        # Nothing looked at what sits at its path -- checkout_state is not
        # called for it -- so a working copy of another repository there
        # would be fetched from that repository's origin, and judged by its
        # refs under this pin's name.
        if not pin or entry["action"] in ("blocked", "on-request") or not (path / ".git").exists():
            continue
        if pin.get("unreleased"):
            # Judged only where the checkout is at the pin. One a plan would
            # still refresh may not hold the commit at all, and asking git
            # about it there is a walk that fails, or in a partial clone a
            # fetch nobody asked for.
            if apply or entry["action"] == "same":
                # A clone made by this very run already is the repository's
                # answer; anything older is asked again, and only by --apply.
                cloned = apply and entry["action"] == "clone"
                caught, said, entry["asked"] = unreleased_pin(pin, entry["repository"], path,
                                                              fetch=apply and not cloned, current=cloned)
                refused.extend(caught)
                notes.extend(said)
            continue
        # A line 1 that is the whole hash of line 2 is read from the two
        # lines. git takes forty digits for the object without looking at a
        # ref, so the answer is git's -- and in a partial clone git fetches
        # the object when the checkout does not hold it, from a plan too.
        named = pin["commit"] if pin["tag"].lower() == pin["commit"] \
            else named_by(pin["tag"], path, fetching=apply)
        # Only a third party can answer with the line: a Maelys repository
        # is tagged, and its messages are the ones they were.
        declare = (" A third party's commit that no tag carries yet says which branch holds it, on an"
                   " 'unreleased <branch>' line, with the last tag before it on line 1"
                   if pin["repository"] else "")
        if named and named != pin["commit"]:
            refused.append(f"{entry['name']}: line 1 names {pin['tag']}, which is {named[:7]}, and line 2"
                           f" is {pin['commit'][:7]}. A published tag never moves, so one of the two is"
                           " wrong; re-pin on the tag's own commit" + ("." + declare if declare else ""))
        elif not named or pin["repository"]:
            carried = tags_here(pin["commit"], path)
            spelled = f"names {pin['tag']}, which this checkout does not hold" if pin["tag"] else "is empty"
            # What the checkout holds is the checkout's answer, and it holds
            # the tags of the day it was cloned: every pin moved to a release
            # cut since then was told its commit carried no tag, and one
            # whose commit carried an older tag was told for ever that line
            # 1 was wrong. So a plan says what it read and that it did not
            # ask, and --apply asks the repository wherever the checkout
            # does not settle it: no tag here on the commit, or a line 1
            # that names nothing here. The clone this run made is not asked
            # again: its tags are that answer.
            asked = apply and entry["action"] != "clone" and not (carried and named)
            there = tagged_there(pin["commit"], path) if asked else []
            # Brought by name, every tag the repository carries on the commit
            # and no other, so that the next run reads them here and asks
            # nothing. One that would move a tag held here is declined by
            # git, and a name that is not UTF-8, which no refspec spells --
            # it is shown with the backslash no ref may hold -- is left.
            bringing = [tag for tag in there or [] if tag not in carried and "\\" not in tag]
            if bringing:
                unattended(["fetch", "--quiet", "--no-tags", "origin",
                            *(f"refs/tags/{tag}:refs/tags/{tag}" for tag in bringing)], path)
            if not carried and not apply:
                # Said where --apply would ask, and nowhere else: a Maelys
                # pin that names its commit by its hash is the shape
                # `migrate` writes, silent once the checkout holds the
                # commit and so silent before.
                if pin["repository"] or not by_its_hash(pin["tag"], pin["commit"]):
                    notes.append(f"{entry['name']}: this checkout holds no tag on {pin['commit'][:7]}, and"
                                 f" {entry['repository']} was not asked for one: --apply asks")
            elif not carried and there is None:
                notes.append(f"{entry['name']}: this checkout holds no tag on {pin['commit'][:7]}, and"
                             f" {entry['repository']} could not be asked for one: not verified")
            elif not carried and not there:
                # `named` and no tag is what `git describe` says of a commit
                # past its tag: a name of the commit itself. A Maelys pin in
                # that shape is what `migrate` writes, and stays silent; a
                # third party's is the untagged commit of the incident,
                # however line 1 spells it.
                notes.append(f"{entry['name']}: {pin['commit'][:7]} carries no tag. A rewritten branch takes"
                             " an untagged commit with it, and a pin on one stops resolving everywhere at"
                             " once -- for the socle, for CI, and for whoever clones next"
                             + ("." + declare if declare else ""))
            elif not named and (not asked or there is None):
                # The commit is tagged and line 1 names something else that
                # is nothing here: said as what was read, since a tag made
                # after this checkout last fetched its tags reads the same.
                notes.append(f"{entry['name']}: line 1 {spelled}, and {pin['commit'][:7]} carries"
                             f" {', '.join(carried)} in it: line 1 is wrong, or names a tag made since this"
                             " checkout last fetched its tags")
            elif not named and pin["tag"] not in there:
                # Asked, the repository settles it: the tags it carries on
                # the commit, or the checkout's when it carries none there.
                notes.append(f"{entry['name']}: line 1 {spelled}, and {pin['commit'][:7]} carries"
                             f" {', '.join(there or carried)} in {entry['repository'] if there else 'it'}:"
                             " line 1 is wrong")
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
        entries.append(dependency_entry(socle_pin, f"{git_base()}/{SOCLE_NAME}.git", socle_path,
                                        action, reason, head))
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


def named_by(line: str, path: pathlib.Path, fetching: bool = True) -> str:
    """The commit line 1 of a pin names in this checkout, or "" when it names none.

    A revision, and never an option, whatever the file spells on it: the
    line went to `git rev-list` bare, where `--all` is an instruction and
    answers with a commit.

    Without `fetching`, what the checkout does not hold is named by nothing:
    a plan asks no repository, and neither does git on its behalf.
    """
    listed = run(["git", "rev-list", "-n", "1", "--end-of-options", line], cwd=path,
                 env=None if fetching else without_fetching())
    return listed.stdout.strip() if listed.returncode == 0 else ""


def without_fetching() -> dict[str, str]:
    """The environment of a git read that must not turn into a fetch.

    In the partial clone the socle makes, git fetches by itself an object it
    is asked about and does not hold. One read of a plan names such an
    object: line 1, when it is the whole hash of a commit the checkout does
    not hold. GIT_NO_LAZY_FETCH stops that in git 2.45 and later; an older
    git does not know the variable and fetches, which is why the one shape
    that is a correct pin -- the hash of line 2 itself -- is never given to
    git at all. An abbreviated hash is not fetched by any git.
    """
    return {**os.environ, "GIT_NO_LAZY_FETCH": "1"}


def spelling(raw: bytes) -> str:
    """A tag's name as it can be printed: a name that is not UTF-8 keeps its bytes behind a backslash."""
    return raw.decode("utf-8", "backslashreplace")


def tags_here(commit: str, path: pathlib.Path) -> list[str]:
    """The tags this checkout holds on the commit; none for a commit it does not hold.

    Read as bytes, since a tag is named in whatever bytes its author
    typed, and never by way of a fetch.
    """
    listed = host.HOST.feed(["git", "tag", "--points-at", commit], b"", cwd=path, env=without_fetching())
    return [spelling(raw) for raw in listed.stdout.split(b"\n") if raw] if listed.returncode == 0 else []


def by_its_hash(line: str, commit: str) -> bool:
    """Whether line 1 names this commit by its hash, as `git describe` writes it past a tag.

    Read from the two lines, as git reads them: a checkout that does not
    hold the commit yet cannot resolve the name, and `named_by` answers
    nothing for it there. Four digits are enough for git and so for this,
    which does not know whether another object of that repository starts
    with the same four.
    """
    spelled = BY_ITS_HASH.fullmatch(line)
    return bool(spelled) and commit.startswith(spelled.group(1).lower())


def unattended(arguments: list[str], path: pathlib.Path) -> "bytes | None":
    """What git answers a question put to a repository, or None when it gave no answer in time.

    By the clock, and with nobody to prompt: the same call whether it is
    the question or the tags it brings back.
    """
    done = host.HOST.feed(["git", *arguments], b"", cwd=path, env={**os.environ, **UNATTENDED}, timeout=PATIENCE)
    return done.stdout if done.returncode == 0 else None


def tagged_there(commit: str, path: pathlib.Path) -> "list[str] | None":
    """The tags the repository carries on this commit, asked now; None when it gave no answer.

    Every tag, and not line 1's alone: the question is whether the commit
    carries one, whatever the pin calls it. The listing is read as bytes: a
    repository holds a tag named in Latin-1 on some commit of 2009, and
    that is not a reason for a run to end.

    An empty listing is an answer from a repository this checkout holds no
    tag of, and none from one it holds a tag of: a portal or a proxy
    answers 200 and nothing, git reads that as a repository with no tag,
    and "carries no tag" would be said on an answer that is not the
    repository's.
    """
    listed = unattended(["ls-remote", "--tags", "origin"], path)
    if listed is None or not listed.strip() and git("for-each-ref", "--count=1", "--format=%(objectname)",
                                                    "refs/tags", cwd=path, check=False):
        return None
    commits: dict[bytes, bytes] = {}
    for line in listed.split(b"\n"):
        target, _, ref = line.partition(b"\t")
        # A tag object is listed twice, and its `^{}` line is the commit it
        # names, whichever of the two comes first.
        if ref.endswith(b"^{}"):
            commits[ref[:-3]] = target
        else:
            commits.setdefault(ref, target)
    return [spelling(ref[len(b"refs/tags/"):]) for ref, target in commits.items() if target == commit.encode()]


def unreleased_pin(pin: dict, repository: str, path: pathlib.Path,
                   fetch: bool, current: bool) -> tuple[list[str], list[str], bool]:
    """(refusals, notes, asked) for a pin that says `unreleased <branch>`, read in its checkout.

    Two facts, and they are not known the same way.

    That line 1 is a tag before the commit is read from the tags this
    checkout holds: exactly the name of a tag, an ancestor of the commit,
    and not the commit itself -- a commit that carries a tag is a pin on
    that tag, and says nothing else. Any tag that is an ancestor is
    accepted, and one that is not the last is a note naming the nearer. Not
    a refusal, for two reasons. The fact line 1 states stays true whatever
    anybody does afterwards, where "last" stops being true the day the
    third party tags an older commit of its branch -- which is how a
    release is cut on a branch that moved on -- and a product must not turn
    red for that. And "nearest" is a count git makes across merges, not a
    property of the graph: what is verified is the property, that no tag
    lies between line 1 and the commit.

    That the branch still holds the commit is the remote's to say, and only
    as of the moment it is asked. A clone carries the branch's ref; a
    checkout this command refreshed later does not move it -- the refresh
    fetches the commit, by its hash -- so the ref in hand is the branch as
    of the last clone, and holding a re-pinned commit against it refuses a
    pin that is right (measured: `merge-base --is-ancestor` exits 1 on a
    commit the branch does hold). So with `fetch` the branch is fetched,
    deliberately, one ref, and `current` says the ref in hand is already
    the remote's answer: a clone this run made. Without either, nothing is
    refused on the branch at all, and `asked` comes back false -- as it does
    when the remote did not answer.

    And "no" is told from "cannot know". A checkout the socle did not make
    may be shallow, or hold no tag, and git then answers "not an ancestor"
    and "no such tag" for what it simply does not have. A shallow checkout
    refuses nothing on ancestry; a tag this checkout does not hold is a
    refusal only once the repository was asked for that very tag and
    answered that it has none, and a note before.
    """
    name, commit, branch, tag = pin["name"], pin["commit"], pin["unreleased"], pin["tag"]
    refusals: list[str] = []
    notes: list[str] = []
    ref, tag_ref = f"refs/remotes/origin/{branch}", f"refs/tags/{tag}"
    cloned = current
    shallow = git("rev-parse", "--is-shallow-repository", cwd=path, check=False) == "true"
    unread = "it is shallow" if shallow else "git could not walk its history"

    def ancestor(older: str, newer: str) -> "bool | None":
        """True, False, or None when this checkout cannot say."""
        code = run(["git", "merge-base", "--is-ancestor", older, newer], cwd=path).returncode
        if code == 0:
            return True
        # Exit 1 is git's "no", and in a shallow checkout also its answer
        # for a history cut short of the commit asked about.
        return False if code == 1 and not shallow else None

    def holds(full: str) -> bool:
        # Exactly this ref, and nothing a revision suffix makes of it:
        # `rev-parse refs/tags/v1.1.0^0` resolves, and v1.1.0^0 is no tag.
        return run(["git", "show-ref", "--verify", "--quiet", full], cwd=path).returncode == 0

    def commit_of(full: str) -> str:
        # ^{commit}: a tag may be annotated, and the question is about the
        # commit it names.
        return git("rev-parse", "--verify", "--quiet", f"{full}^{{commit}}", cwd=path, check=False) \
            if holds(full) else ""

    def remote_has(full: str) -> "bool | None":
        """Whether the repository holds this ref, asked now; None when it did not answer."""
        code = run(["git", "ls-remote", "--exit-code", "origin", full], cwd=path).returncode
        # Exit 2 is git's "the remote answered, and has no such ref".
        return True if code == 0 else False if code == 2 else None

    gone = (f"{name}: {repository} has no branch {branch}. The pin says {commit[:7]} is on it: the branch was"
            " deleted or renamed, or never was, and no ref this pin names holds that commit. Hold it where"
            " the product decides -- a mirror, under a tag of its own -- or re-pin")
    asked = current
    if fetch:
        fetched = run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}"], cwd=path)
        asked = current = fetched.returncode == 0
        if not current:
            if remote_has(f"refs/heads/{branch}") is False:
                asked = True
                refusals.append(gone)
            else:
                reason = (fetched.stderr.strip().splitlines() or ["no answer"])[-1]
                notes.append(f"{name}: {repository} could not be asked whether {branch} still holds"
                             f" {commit[:7]} ({reason}): not verified")
    tip = commit_of(ref) if current else ""
    if current and not tip:
        # A clone carries every branch the repository has: one it does not
        # carry is one the repository does not have.
        refusals.append(gone)
    elif tip:
        on = ancestor(commit, tip)
        if on is None:
            notes.append(f"{name}: whether {branch} holds {commit[:7]} cannot be read from this checkout"
                         f" ({unread}): not verified")
        elif not on:
            # What was read, and no more: whether the repository still
            # serves the commit was asked only of a run that just got it.
            served = ("This run obtained the commit from the repository" if cloned else
                      "This checkout holds the commit, and nothing here asked whether the repository still"
                      " serves it")
            refusals.append(f"{name}: {commit[:7]} is not on {branch} of {repository}, which is at {tip[:7]}:"
                            f" the branch was rewritten, or never held it. {served}. If no other ref"
                            " holds it there, it is fetchable only until the repository prunes it -- and"
                            " then stops resolving for the socle, for CI, and for whoever clones next. Hold it where"
                            " the product decides -- a mirror, under a tag of its own, which this checkout"
                            f" can push -- or re-pin on a commit of {branch}")
    last = "With an unreleased line, line 1 is the last tag before the commit"
    nearest = git("describe", "--tags", "--abbrev=0", commit, cwd=path, check=False)
    if not holds(tag_ref) and asked and not cloned:
        # Asked for by name: a checkout cloned without its tags follows
        # none when a branch is fetched, and "this checkout holds no such
        # tag" is then about the checkout.
        run(["git", "fetch", "--quiet", "origin", f"{tag_ref}:{tag_ref}"], cwd=path)
    if not holds(tag_ref):
        # A clone of this run carries every tag; otherwise the repository
        # is asked, when it is answering at all.
        there = False if cloned else remote_has(tag_ref) if asked else None
        if there is False:
            refusals.append(f"{name}: line 1 names {tag}, which is not a tag of {repository}. {last}"
                            + (f": {nearest}" if nearest else ""))
        else:
            notes.append(f"{name}: line 1 names {tag}, which this checkout does not hold, and {repository}"
                         + (" has it, where it could not be fetched from" if there else
                            " did not answer for it" if asked else " was not asked for it")
                         + ": not verified")
        return refusals, notes, asked
    named = commit_of(tag_ref)
    carried = git("tag", "--points-at", commit, cwd=path, check=False).split()
    before = ancestor(named, commit) if named and named != commit else None
    if not named:
        refusals.append(f"{name}: line 1 names {tag}, a tag that names no commit. {last}"
                        + (f": {nearest}" if nearest else ""))
    elif named == commit:
        refusals.append(f"{name}: line 1 names {tag}, which is {commit[:7]} itself. A commit that carries a tag"
                        " is a pin on that tag: remove the unreleased line")
    elif before is None:
        notes.append(f"{name}: whether {tag} is an ancestor of {commit[:7]} cannot be read from this checkout"
                     f" ({unread}): not verified")
    elif not before:
        refusals.append(f"{name}: line 1 names {tag}, which is {named[:7]} and not an ancestor of {commit[:7]}."
                        f" {last}" + (f": {nearest}" if nearest and nearest != tag else ""))
    elif carried:
        notes.append(f"{name}: {commit[:7]} carries {carried[0]}, so the unreleased line no longer says the"
                     f" truth. It is a pin on a tag: write {carried[0]} on line 1 and remove the line")
    else:
        # Between the two: it contains line 1, the commit contains it, and
        # it is neither. A tag on a merged side branch is not between.
        between = [other for other in git("tag", "--merged", commit, "--contains", named,
                                          cwd=path, check=False).split()
                   if commit_of(f"refs/tags/{other}") not in (named, commit, "")]
        if between:
            nearer = nearest if nearest in between else between[-1]
            notes.append(f"{name}: {tag} is not the last tag before {commit[:7]}: {nearer} lies between"
                         f" them. Write {nearer} on line 1")
    return refusals, notes, asked


def unobtainable(pin: dict, entry: dict, failure: Failure) -> Failure:
    """The failure to obtain an `unreleased` pin's commit, naming the pin and its branch.

    Says what was read and what that most likely means, and keeps the two
    apart: git's own last line, what the clone at that path holds of the
    branch, and then the reading -- never the reading alone.
    """
    name, commit, branch, repository = pin["name"], pin["commit"], pin["unreleased"], entry["repository"]
    path = pathlib.Path(entry["path"])
    # Every line git gave, once: the telling one is rarely the last.
    said = "; ".join(dict.fromkeys(line.strip() for line in failure.message.partition(" failed: ")[2].splitlines()
                                   if line.strip())) or failure.message.strip()
    tip = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}", cwd=path,
              check=False) if (path / ".git").exists() else ""
    if tip:
        read = (f"{path} holds {branch} at {tip[:7]} and could not be brought to that commit. The most likely"
                " reading: the branch was rewritten and the repository has pruned the commit, which no tag"
                " held")
    elif (path / ".git").exists():
        read = (f"{path} holds no branch {branch} of that repository, and could not be brought to that"
                " commit. The most likely reading: the branch was deleted or renamed and the commit pruned"
                " with it, since no tag held it")
    else:
        read = ("Nothing was cloned, so this may be the network or the URL, and says nothing yet of the"
                " commit")
    return Failure("PROCESS_FAILED",
                   f"{name}: {commit[:7]} could not be obtained from {repository}; the pin says it is on"
                   f" {branch}, under no tag. {read}. git said: {said}",
                   "If a machine, a CI cache or a mirror still holds the commit, push it under a tag to a"
                   f" repository the product controls and pin that; otherwise re-pin on a commit of {branch}."
                   + (f" What is at {path} is left as it is, and blocks the next run until it is moved aside."
                      if (path / ".git").exists() else ""))


def dependency_entry(pin: dict, repository: str, path: pathlib.Path,
                     action: str, reason: str, head: str) -> dict:
    """One line of the plan, for a pin or for the socle, in one shape.

    There were two builders: the pins' wrote `previous` on a refresh, and
    the socle's -- written apart, sixty lines below -- did not. JSON printed
    either; the text renderer read `previous` and died on a KeyError, with a
    Python trace instead of a plan, exactly when a product had just moved
    its socle pin and asked what that would refresh. A shape built in two
    places is two shapes, so it is built here.
    """
    entry = {"name": pin["name"], "tag": pin["tag"], "commit": pin["commit"], "repository": repository,
             "path": str(path), "action": action}
    if pin.get("submodules") is not None:
        entry["submodules"] = pin["submodules"]
    if pin.get("unreleased"):
        # `asked` is false until a run has the repository's answer: every
        # row of a plan carries it, whatever its action.
        entry["unreleased"] = pin["unreleased"]
        entry["asked"] = False
    if action == "blocked":
        entry["reason"] = reason
    elif action == "refresh":
        entry["previous"] = head
    return entry


def text_dependencies(data: dict) -> str:
    lines = []
    for entry in data["dependencies"]:
        head = f"{entry['name']} {entry['tag']} ({entry['commit'][:7]})"
        unasked = ""
        if entry.get("unreleased"):
            # The same place on the line, and not the same claim: the tag
            # is behind the commit, and what holds it is a branch.
            head = f"{entry['name']} past {entry['tag']} ({entry['commit'][:7]}) on {entry['unreleased']}"
            # Every row that did not ask says so, whatever it would do.
            # After --apply, a repository that did not answer is a note of
            # its own, with git's reason.
            how = "--all --apply" if entry["action"] == "on-request" else "--apply"
            if entry["action"] == "on-request" or (data["mode"] == "plan" and not entry.get("asked")):
                unasked = f", not asked: {how} asks the repository whether {entry['unreleased']} still holds it"
        if entry["action"] == "clone":
            lines.append(f"{'clone':<8} {head} from {entry['repository']}{unasked}")
        elif entry["action"] == "refresh":
            lines.append(f"{'refresh':<8} {head}, here {entry['previous'][:7]}{unasked}")
        elif entry["action"] == "blocked":
            lines.append(f"{'BLOCKED':<8} {entry['name']}: {entry['reason']}")
        elif entry["action"] == "on-request":
            lines.append(f"{'request':<8} {head} is cloned on request only: pass --all to bring it here{unasked}")
        else:
            lines.append(f"{'same':<8} {head}{unasked}")
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
