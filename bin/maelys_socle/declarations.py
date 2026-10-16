# SPDX-License-Identifier: MPL-2.0
"""Product declaration data and parsers; callers supply the file contents."""
from __future__ import annotations

import pathlib
import re

from maelys_cli import Failure

from .constants import (
    CHECK_PLATFORMS, DECLARATION_FILE, PROGRAM, SOCLE_CHANNEL_KINDS, SOCLE_COMMIT_VERIFICATIONS,
    SOCLE_GATES, SOCLE_MANIFEST_PATTERNS, SOCLE_MECHANISM, SOCLE_REPOSITORY, SOCLE_RUNNER_KEYS, SOCLE_TARGETS,
)
from .forge import forge

# The one shape a `repository` line takes: https, a host name, and a path of
# plain segments, with `.git` or without. Everything a URL offers beyond that
# is another spelling of the same place -- a port, a user, an empty or a dot
# segment, a percent-encoded letter, a dot after the host -- and each of them
# read as "not maelys-dev" while reaching it. A pin does not need them.
REPOSITORY_URL = re.compile(r"https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)"
                            r"(?P<path>(?:/[A-Za-z0-9._~-]+)+)")
# Where the fleet's own repositories live: the forge's hosts. On that host
# a repository is https://host/owner/name and nothing longer, and the owner
# is compared as the host compares it, whatever the case it is typed in.
FLEET_OWNER = SOCLE_REPOSITORY.split("/")[0]
# The words a line of a pin may start with, after line 2. `sha256` is the
# digest of a vendored file, which `vendor` writes and reads back.
PIN_ATTRIBUTES = ("repository", "submodules", "on-request", "unreleased", "sha256")
# What `git describe --tags` says of a commit past its tag.
DESCRIBED = re.compile(r"(.+)-[0-9]+-g([0-9a-f]{4,40})")
# What git refuses in the name of a ref, as `git check-ref-format` states it.
REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def ref_name_refusal(name: str) -> str:
    """Why git would not take `name` as a branch or a tag; "" when it would.

    Read from `git check-ref-format`, and held here rather than asked of
    git: `check` runs where the pin is, with no repository to ask, and a
    name git refuses must be refused before the first clone -- not by a
    fetch that fails on the second run.
    """
    if not name:
        return "it is empty"
    if REF_FORBIDDEN.search(name):
        return "it holds a space, a control character or one of ~ ^ : ? * [ \\"
    if name.startswith("-"):
        return "it starts with a dash"
    if ".." in name or "@{" in name or name == "@":
        return "it holds '..' or '@{', or is '@'"
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return "it starts or ends with a slash, or holds two in a row"
    if name.endswith("."):
        return "it ends with a dot"
    for component in name.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            return f"'{component}' starts with a dot or ends with .lock"
    return ""


def repository_refusal(url: str) -> str:
    """Why `url` is not the one shape a repository line takes; "" when it is."""
    shaped = REPOSITORY_URL.fullmatch(url)
    if not shaped:
        return ("the repository line must be an https URL in one shape, https://host/owner/name with .git or"
                " without: no user, no port, no empty segment, no percent-encoding, no dot after the host")
    segments = shaped.group("path").split("/")[1:]
    if any(segment in (".", "..") for segment in segments):
        return "the repository line must be an https URL whose path holds no '.' or '..' segment"
    if shaped.group("host").lower() in forge().hosts and len(segments) != 2:
        return (f"the repository line must be an https URL of the shape https://{shaped.group('host')}/owner/name"
                " on that host")
    return ""


def fleet_repository(url: str) -> bool:
    """Whether a `repository` URL, already in its one shape, names a repository of the fleet.

    A string comparison, and it decides what a string can: the host and the
    owner. It cannot know that another owner's repository is a fork of the
    fleet's, that a repository was renamed or transferred, or that another
    host -- the one MAELYS_GIT_BASE names on some machine -- serves the same
    repositories. The conventions say so.
    """
    shaped = REPOSITORY_URL.fullmatch(url)
    return bool(shaped) and shaped.group("host").lower() in forge().hosts \
        and shaped.group("path").split("/")[1].lower() == FLEET_OWNER.lower()


class Declarations:
    """What a product declares, read once and used by every command."""

    def __init__(self, project: pathlib.Path, product: str, mechanism: str = SOCLE_MECHANISM) -> None:
        self.project = project
        self.product = product
        # How this product publishes: the socle's workflows, a mechanism of
        # its own, or nothing. It decides which files adopt writes and which
        # checks apply; the conventions apply to all three.
        self.mechanism = mechanism
        self.version = ""
        self.formulas: list[str] = []
        self.render_command = ""
        self.dependencies: list[str] = []
        # name -> the pin as parse_pin reads it; dependencies keeps the order
        self.pins: dict[str, dict] = {}
        # what parse_pin refused: nothing is materialised on a broken pin
        self.pin_problems: list[str] = []
        self.linux_packages = ""
        self.macos_packages = ""
        self.generates_reference = False
        self.cli_programs: list[str] = []
        self.cli_flags: list[str] = []
        self.cli_build: list[str] = []
        self.verify_command = ""
        # (target, runner) of the declaration file; an empty runner keeps the
        # one release.yml holds for that target. Empty means the three the
        # socle builds by default.
        self.targets: list[tuple[str, object]] = []
        # Archive kinds SHA256SUMS covers beyond the socle's three.
        self.manifest_patterns: list[str] = []
        # (channel, registry kind) of the declaration file; one channel.yml job each.
        self.channels: list[tuple[str, str]] = []
        # The glob of the SBOM a build leaves in dist/, empty when none.
        self.sbom_pattern = ""
        # The prose files of this product that name the documentation
        # repository, so that an Impact line can ask the condition itself
        # rather than "is this repository public": a product read "0.60.0
        # asks you a gesture" and checked three conditions by hand to find
        # that none of them held.
        self.documentation_named: list[str] = []
        # "signed", "signed-on-default-branch", or "" for the workflow's
        # default of none.
        self.commit_verification = ""
        # Whether the build takes its dependencies from
        # MAELYS_DEPENDENCIES_DIR instead of assuming a sibling.
        self.dependencies_apart = False
        # Labels of the macOS runner for the socle's own jobs, empty for
        # the hosted default. Honoured on a private repository only.
        self.macos_runner: list[str] = []
        self.runners: dict[str, list[str]] = {}
        # Whether this product's ci.yml calls the socle's check job, which is
        # what decides who a coming change to that workflow reaches.
        self.calls_socle_ci = False
        # channel name -> the gate it declares, when it declares one.
        self.channel_gates: dict[str, str] = {}
        # The targets that package, when [package] says; empty means all.
        self.package: list[str] = []
        # The product's own legs of the shared CI: (name, platform, command).
        # Not `checks`, which is where this class keeps its diagnostics: the
        # first cut used that name, and the report looping over it appended
        # to the very list it walked until the self-test was killed.
        self.legs: list[tuple[str, str, str]] = []
        # "reviewer", "none", or "" when this repository has not chosen.
        self.gate = ""
        # What cut runs between writing VERSION and the bump commit, for a
        # product whose version is also materialised somewhere else.
        self.after_version = ""
        # Observation only: where the harnesses are, and who runs them.
        self.fuzz_harnesses = ""
        self.fuzz_runs = "none"
        # name -> repository, for a dependency that does not live in maelys-dev
        self.foreign: dict[str, str] = {}
        # name -> "" or "recursive", for a dependency whose build needs them
        self.submodules: dict[str, str] = {}
        # name -> the branch of its third party that holds a commit no tag
        # carries, pinned there on purpose
        self.unreleased: dict[str, str] = {}
        # Files of docs/ that are neither generated, engaged by LICENSING.md
        # nor data: they belong in maelys-docs/<product>/.
        self.prose: list[str] = []
        # Prose README.md links: a public README holds it until a site exists.
        self.readme_held: list[str] = []
        # [ci] own: no shared CI is called, and no ci.yml is written.
        self.own_ci = False
        # [docs] named OWNER/NAME: the managed blocks name that documentation
        # repository. Both words are read by declared_value() in
        # read_declarations, once; `documents` is the repository named, or
        # "" for an unnamed product.
        self.docs_named = False
        self.documents = ""
        self.checks: list[dict] = []

    @property
    def releases_here(self) -> bool:
        """Whether the socle's release mechanism applies to this product."""
        return self.mechanism == SOCLE_MECHANISM

    def applicable(self) -> list[dict]:
        """The checks that hold for this product: its mechanism's, and the conventions."""
        return [check for check in self.checks
                if check["scope"] == "conventions" or self.releases_here]

    @property
    def valid(self) -> bool:
        """Whether adopt may proceed; a warning is reported by check only."""
        return all(check["status"] in ("ok", "note", "warn") for check in self.applicable())

    def add(self, status: str, message: str, scope: str = "release") -> None:
        self.checks.append({"status": status, "message": message, "scope": scope})


def require_valid(decl: Declarations, action: str) -> None:
    if not decl.valid:
        problems = [check["message"] for check in decl.applicable() if check["status"] not in ("ok", "note")]
        raise Failure("PRECONDITION_FAILED", f"{decl.project} does not meet the product contract, so {action} is refused: "
                      + "; ".join(problems) + ".", f"Run '{PROGRAM} check {decl.project}' and fix every violation.")



def parse_packages(text: str) -> tuple[str, str]:
    """dependencies/packages: one package per line under [linux] or [macos]."""
    packages: dict[str, list[str]] = {"linux": [], "macos": []}
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line in ("[linux]", "[macos]"):
                section = line[1:-1]
                continue
            raise ValueError(f"unknown section {line} at line {number}")
        if not section:
            raise ValueError(f"package outside a [linux] or [macos] section at line {number}")
        if len(line.split()) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._@:-]*", line):
            raise ValueError(f"one package per line at line {number}: {raw}")
        packages[section].append(line)
    return " ".join(packages["linux"]), " ".join(packages["macos"])


def parse_pin(name: str, text: str) -> tuple[dict, list[str]]:
    """One dependencies/NAME.pin as data: (pin, problems); an empty pin when unusable.

    Line 2 is the commit, for everything that clones. Line 1 is for humans,
    and it is held to line 2 by `dependencies`, which has the checkout to
    read it against -- so it says one of three things, and never "the
    nearest tag" of a commit that carries none, which is what this text
    said while the code refused it:

    - the tag the commit carries, which is every pin on a release;
    - past a tag, what `git describe --tags` says of the commit,
      `vX.Y.Z-N-gSHA`: a name of the commit itself, and what `migrate`
      writes into the documentation repository;
    - under an `unreleased` line, the last tag before the commit -- an
      ancestor of it, and not the commit's own.

    A dependency outside maelys-dev says where it lives, once, on a
    `repository <https URL>` line: maelys-http builds Mbed TLS from a pinned
    commit because the distribution's version is under its security floor.
    A dependency whose build needs its submodules says so on a `submodules`
    line, or `submodules recursive`: a submodule's commit is pinned by its
    superproject, but its URL comes from a .gitmodules this pin does not
    name, so fetching one is declared, never automatic.

    A pin that one job alone reads says `on-request`: it is cloned when it
    is asked for by name, and by nothing that clones "every pin". maelys-http
    pins Mbed TLS twice, and its second pin serves one job of its own; the
    five shared jobs and the three release builds cloned it, submodules and
    all, without reading a file of it -- forty seconds a job on Linux, and
    on a private repository the macOS seconds are billed tenfold. The pin is
    still a pin: `check` and `cut` judge its tag and its commit as any other.

    A third party's commit that no tag carries says `unreleased <branch>`:
    it is on that branch, past the tag of line 1, on purpose -- the part a
    product builds is newer than any version the third party has published.
    The word is the reason, and the branch is what can be verified: a tag
    holds its commit for good, a branch holds it until somebody rewrites it,
    so the pin names the one ref its commit depends on and `dependencies`
    asks the repository whether it still does. Third party only: the line
    is refused without a `repository` line, and with one that names
    maelys-dev. A Maelys repository is pinned on a tag, and that rule does
    not move -- the fleet can tag what it needs, a third party cannot be
    asked to.

    One reading. The managed script is the reader that clones, and whatever
    it would read differently, or not read at all, is a problem here: a
    line with a space before or after it, a tab, another case, a colon, an
    attribute given twice, a URL in any shape but one. A word neither
    reader knows is returned under `unknown`, for a note.

    Read here rather than in read_declarations alone, because the command
    that materialises the pins needs the same reading and a second one would
    drift from it.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return {}, [f"dependencies/{name}.pin: the file is named after the repository, [a-z0-9-]"]
    where = f"dependencies/{name}.pin"
    # Read as the managed script reads it, which is the reading that clones:
    # lines end in LF, line 2 is forty hexadecimal digits and nothing else,
    # and an attribute is matched from the first column to the last. This
    # reader used to strip every line first, and the two then disagreed in
    # silence: `repository <URL>` followed by a space was a third party
    # here, and no line at all for the script, which cloned the fleet's
    # repository of that name.
    if "\r" in text:
        return {}, [f"{where}: the file holds carriage returns; a pin is read line by line, each ending in LF"]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 2 or not re.fullmatch(r"[0-9a-f]{40}", lines[1]):
        return {}, [f"{where}: line 2 must be the pinned commit (line 1 its tag)"]
    pin: dict = {"name": name, "tag": lines[0].strip(), "commit": lines[1],
                 "repository": "", "submodules": None, "on_request": False, "unreleased": "", "unknown": []}
    problems: list[str] = []
    if lines[0] != pin["tag"] or re.search(r"\s", pin["tag"]):
        problems.append(f"{where}: line 1 holds a tag and nothing else, with no space in or around it:"
                        f" {lines[0]!r}")
    elif pin["tag"].startswith("-"):
        # It is handed to git as a revision, and git reads a leading dash
        # as an option.
        problems.append(f"{where}: line 1 holds a tag, and {pin['tag']!r} starts with a dash")
    seen: set[str] = set()
    # Whether the file says where it lives at all, well or badly: a
    # repository line refused for itself is one problem, and is not
    # reported a second time as "no repository line".
    located = False
    for number, line in enumerate(lines[2:], 3):
        if line == "":
            continue
        located = located or line.strip().lower().startswith("repository")
        word, _, value = line.partition(" ")
        if line != line.strip() or "\t" in line:
            problems.append(f"{where}: line {number} is {line!r}; an attribute is its word at the start of the"
                            " line, one space and its value, and nothing before or after -- the managed script"
                            " matches it that way, and reads nothing else")
            continue
        if re.fullmatch(r"sha256:[0-9a-f]{64}", line):
            word = "sha256"
        elif word not in PIN_ATTRIBUTES[:4]:
            attempted = [known for known in PIN_ATTRIBUTES if line.lower().startswith(known)]
            if attempted:
                # Another case, a colon, a word glued to its value: a line
                # that means an attribute and is one for neither reader.
                problems.append(f"{where}: line {number} is {line!r}, which is not the {attempted[0]} line it"
                                f" starts like: {attempted[0]!r} in lower case, one space, its value"
                                + (" -- or sha256:<64 hexadecimal digits>" if attempted[0] == "sha256" else ""))
            else:
                # A word this socle does not know is named, never fatal, as
                # a section of the declaration file is: products pin the
                # socle by commit, and an attribute a newer socle reads
                # would otherwise refuse every product that writes it
                # before adopting.
                pin["unknown"].append(line)
            continue
        if word in seen:
            problems.append(f"{where}: the {word} line is given twice, and the managed script reads the first"
                            " where this reader read the last: one line")
            continue
        seen.add(word)
        if word == "repository":
            refusal = repository_refusal(value)
            if refusal:
                problems.append(f"{where}: {refusal}")
            else:
                pin["repository"] = value
        elif word == "submodules":
            if value not in ("", "recursive") or line not in ("submodules", "submodules recursive"):
                problems.append(f"{where}: the submodules line takes nothing, or recursive")
            else:
                pin["submodules"] = value
        elif word == "on-request":
            if line != "on-request":
                problems.append(f"{where}: the on-request line takes nothing")
            else:
                pin["on_request"] = True
        elif word == "unreleased":
            refusal = branch_refusal(value)
            if refusal:
                problems.append(f"{where}: the unreleased line names the branch that holds the commit, and"
                                f" nothing else: unreleased <branch>. {value!r} is not one: {refusal}")
            else:
                pin["unreleased"] = value
    if pin["unreleased"]:
        refusal = unreleased_refusal(pin, located)
        if refusal:
            problems.append(f"{where}: {refusal}")
    if problems:
        # A refused pin is one of them for nothing downstream: not listed
        # by `declarations`, not said to be "on purpose" by `check`.
        pin["unreleased"] = ""
    return pin, problems


def branch_refusal(branch: str) -> str:
    """Why `branch` cannot be what an `unreleased` line names; "" when it can.

    A name git takes as a branch, and a branch of the repository -- not one
    of the names a clone answers to whatever the repository holds. `HEAD`
    resolved on the first clone, as origin/HEAD, and was refused on every
    run after it.
    """
    refusal = ref_name_refusal(branch)
    if refusal:
        return refusal
    if branch.startswith("refs/"):
        return "a branch is named as the repository names it, main and not refs/heads/main"
    if re.fullmatch(r"[A-Z_]*HEAD", branch):
        return "it is a name git keeps for itself, not a branch of the repository"
    return ""


def unreleased_refusal(pin: dict, located: bool) -> str:
    """Why this pin may not say `unreleased`, from its own lines; "" when it may.

    What can be read without a checkout, which is all `check` has: the
    grammar, and the string of the URL. That line 1 is a tag of the
    repository and an ancestor of the commit, and that the branch holds the
    commit, are questions for a clone: `dependencies` answers them.
    """
    if not located:
        return ("the unreleased line is for a third party, which says where it lives on a"
                " 'repository <https URL>' line. A Maelys repository is pinned on a tag: tag it, and pin the tag")
    if pin["repository"] and fleet_repository(pin["repository"]):
        return (f"the unreleased line is for a third party, and {pin['repository']} is a Maelys repository."
                " A Maelys repository is pinned on a tag: tag it, and pin the tag")
    if not pin["tag"]:
        return ("with an unreleased line, line 1 is the last tag before the commit, and it is empty")
    described = DESCRIBED.fullmatch(pin["tag"])
    if described and pin["commit"].startswith(described.group(2)):
        return (f"with an unreleased line, line 1 is the last tag before the commit -- {described.group(1)}"
                f" -- and not what git describe says of it ({pin['tag']}): the line already says the commit"
                " is past its tag, and the socle verifies the tag is an ancestor")
    refusal = ref_name_refusal(pin["tag"])
    if refusal:
        return (f"with an unreleased line, line 1 is the last tag before the commit, and {pin['tag']!r} is not"
                f" the name of a tag: {refusal}")
    return ""


def parse_release(text: str) -> tuple[list[tuple[str, object]], list[str], list[tuple[str, str]], str, str, str]:
    """The declaration file: [targets], [manifest], [sbom], [runners], [dependencies], [commit], [channels], [gate] and [cut].

    A target line naming no label keeps the runner release.yml holds for that
    target, which only exists for the three the socle builds; one label is a
    runner label, several are a label set. The [targets] section replaces the
    matrix rather than adding to it, so a product that adds one target names
    the others it keeps. [manifest] adds to the socle's archive kinds.
    [commit] holds what the release asks of the commit the tag names, beyond
    the tag's own signature: 'signed', or 'signed-on-default-branch' which
    also requires it to be an ancestor of the default branch. [dependencies] holds the single word 'apart': the build reads
    MAELYS_DEPENDENCIES_DIR and the socle materialises the pins there rather
    than beside the product. [runners] holds one 'macos LABEL...' line, the runner the socle's own
    macOS jobs use on a private repository. [sbom] holds one glob, the document a build leaves in dist/ beside the
    packages it describes; the release then attests that document against
    the files the document itself names. [channels] names one 'CHANNEL REGISTRY' per line, each rendered into a
    channel.yml job that runs once the release is published. [cut] holds
    'after-version COMMAND', run by cut between writing VERSION and the
    bump commit, for a product that materialises its version somewhere
    else as well.
    """
    targets: list[tuple[str, object]] = []
    manifest: list[str] = []
    channels: list[tuple[str, str]] = []
    channel_gates: dict[str, str] = {}
    sbom = ""
    macos_runner: list[str] = []
    runners: dict[str, list[str]] = {}
    apart = False
    commit_verification = ""
    gate = ""
    after_version = ""
    package: list[str] = []
    checks: list[tuple[str, str, str]] = []
    own_ci = False
    docs_unnamed = False
    privacy_declared = False
    unknown: list[str] = []
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line in ("[targets]", "[manifest]", "[sbom]", "[runners]", "[dependencies]",
                        "[commit]", "[channels]", "[gate]", "[cut]", "[package]", "[check]", "[ci]", "[docs]",
                        "[privacy]"):
                section = line[1:-1]
                continue
            # A section this socle does not know is named and skipped, never
            # fatal. Products pin the socle by commit: one that adds a section
            # a newer socle understands would otherwise lose its targets, its
            # channels and its gate in silence, everywhere the older socle
            # still reads the file — including the fleet observer.
            unknown.append(f"{line} at line {number}")
            section = "unknown"
            continue
        if section == "unknown":
            continue
        if not section:
            raise ValueError(f"line outside a [targets], [manifest], [sbom], [runners],"
                             f" [dependencies], [commit], [channels], [gate] or [cut] section"
                             f" at line {number}")
        if section == "cut":
            key, _, command = line.partition(" ")
            if key != "after-version":
                raise ValueError(f"[cut] knows after-version, not {key}, at line {number}")
            if after_version:
                raise ValueError(f"[cut] holds one after-version, and line {number} is a second")
            if not command.strip():
                raise ValueError(f"[cut] after-version names no command at line {number}")
            after_version = command.strip()
            continue
        if section == "privacy":
            # Named here and judged in one place only, privacy.declared(),
            # when `check` runs the guard. A line the grammar refuses is a
            # violation of the conventions, said by the guard to every
            # repository whatever publishes it: said here it would be a
            # release-scope refusal, which a repository the socle does not
            # release never hears -- and a mistyped `refuse` would be a
            # silence.
            privacy_declared = True
            continue
        if section == "docs":
            # Read by declared_value(), which rendering the managed block needs.
            # `named` carries the repository it names, OWNER/NAME: the socle
            # spelled it for every named product until 0.60.0, and a public
            # socle that spells a private name in its texts is the leak its
            # own rule forbids. No product declared the bare word when it
            # went (measured on nine repositories), so nothing is announced.
            word, _, value = line.partition(" ")
            if word == "named" and re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", value.strip()):
                pass
            elif word == "named":
                raise ValueError(f"[docs] named takes the repository it names, OWNER/NAME, at line {number}"
                                 + (": the bare word spelled the socle's default until 0.60.0" if not value.strip()
                                    else f": not {value.strip()!r}"))
            elif line != "unnamed":
                raise ValueError(f"[docs] holds 'named OWNER/NAME' or 'unnamed', not {line!r}, at line {number}")
            if docs_unnamed:
                raise ValueError(f"[docs] holds one line, at line {number}")
            docs_unnamed = True
            continue
        if section == "ci":
            # Read by declared_word(), which the ci.yml rule needs before the
            # rest of the file is parsed; validated here with everything else.
            if line != "own":
                raise ValueError(f"[ci] holds the single word 'own', not {line!r}, at line {number}")
            own_ci = True
            continue
        if section == "check":
            # A leg of the product's own, run by the shared CI beside the
            # socle's three: a name, the platform it runs on, and the command.
            # maelys-git-core tests with and without an agent enabled, which
            # the shared matrix had no way to say, so it kept a CI of its own.
            name, _, rest = line.partition(" ")
            platform, _, command = rest.strip().partition(" ")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError(f"a [check] leg is named [a-z0-9-] at line {number}: {name}")
            if any(name == known for known, _, _ in checks):
                raise ValueError(f"[check] names the leg {name} twice, at line {number}")
            if platform not in CHECK_PLATFORMS:
                raise ValueError(f"a [check] leg runs on {' or '.join(CHECK_PLATFORMS)}, not {platform or 'nothing'},"
                                 f" at line {number}")
            if not command.strip():
                raise ValueError(f"the [check] leg {name} names no command at line {number}")
            checks.append((name, platform, command.strip()))
            continue
        if section == "package":
            # The targets that package; every target of [targets] verifies.
            # A source archive is the same tree everywhere and not the same
            # gzip bytes everywhere, so a product publishing one could not
            # verify on three targets without three archives clashing.
            for name in line.split():
                if name in package:
                    raise ValueError(f"[package] names {name} twice, at line {number}")
                package.append(name)
            continue
        if section == "gate":
            if gate:
                raise ValueError(f"[gate] holds one line, and line {number} is a second")
            if line not in SOCLE_GATES:
                raise ValueError(f"[gate] is {' or '.join(SOCLE_GATES)}, not {line}, at line {number}")
            gate = line
            continue
        if section == "commit":
            if commit_verification:
                raise ValueError(f"[commit] holds one line, and line {number} is a second")
            if line not in SOCLE_COMMIT_VERIFICATIONS:
                raise ValueError(f"[commit] is {' or '.join(SOCLE_COMMIT_VERIFICATIONS)},"
                                 f" not {line}, at line {number}")
            commit_verification = line
            continue
        if section == "dependencies":
            # One word, not a variable per dependency. Every Makefile of the
            # fleet reads <root>/<name> and nothing else, and the names it
            # uses for the roots differ -- SYSTEM_DIR here, MAELYS_SPEC_DIR
            # there -- so a declaration naming them would be a second list
            # to keep true, with nothing the root does not already give.
            if line != "apart":
                raise ValueError(f"[dependencies] holds the single word apart, not {line}, at line {number}")
            if apart:
                raise ValueError(f"[dependencies] holds one line, and line {number} is a second")
            apart = True
            continue
        if section == "runners":
            key, *labels = line.split()
            # The three legs of check-product.yml's matrix, named as the
            # workflow names them. macos keeps its bare word: it is what ten
            # repositories already declare, and renaming a key of a file the
            # socle reads would refuse every one of them at once.
            if key not in SOCLE_RUNNER_KEYS:
                raise ValueError(f"[runners] knows {', '.join(SOCLE_RUNNER_KEYS)}, not {key}, at line {number}")
            if key in runners:
                raise ValueError(f"[runners] holds one {key} line, and line {number} is a second")
            if not labels:
                raise ValueError(f"[runners] {key} names no label at line {number}")
            for label in labels:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
                    raise ValueError(f"a runner label is [A-Za-z0-9._-] at line {number}: {label}")
            runners[key] = labels
            if key == "macos":
                macos_runner = labels
            continue
        if section == "sbom":
            # One document per target, because one attestation carries one
            # predicate: actions/attest takes a single sbom-path, and a
            # second document would need a second step this workflow cannot
            # write without knowing how many there are.
            if sbom:
                raise ValueError(f"[sbom] holds one glob, and line {number} is a second")
            if len(line.split()) != 1 or not re.fullmatch(r"[A-Za-z0-9*][A-Za-z0-9*+._-]*", line):
                raise ValueError(f"one glob per line at line {number}: {raw}")
            sbom = line
            continue
        if section == "channels":
            name, *rest = line.split()
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError(f"a channel is named [a-z0-9-] at line {number}: {name}")
            # A third word is the gate this channel asks for. Absent, it is
            # the release's own environment -- which is a second approval,
            # measured four hours apart on one product's release, and was
            # the behaviour before anything could be declared.
            if len(rest) == 2:
                if rest[1] not in SOCLE_GATES:
                    raise ValueError(f"a channel's gate is {' or '.join(SOCLE_GATES)},"
                                     f" not {rest[1]}, at line {number}")
                channel_gates[name] = rest[1]
                rest = rest[:1]
            if len(rest) != 1:
                raise ValueError(f"a channel names one registry, and may add its gate,"
                                 f" at line {number}: {raw}")
            if rest[0] not in SOCLE_CHANNEL_KINDS:
                raise ValueError(f"the socle serves no {rest[0]} channel at line {number};"
                                 f" it serves {', '.join(SOCLE_CHANNEL_KINDS)}."
                                 " Trusted publishing on npmjs or PyPI matches an OIDC claim"
                                 " naming the socle rather than this repository once a reusable"
                                 " workflow publishes: such a channel stays in this repository")
            if any(name == known for known, _ in channels):
                raise ValueError(f"the channel {name} appears twice, at line {number}")
            channels.append((name, rest[0]))
            continue
        if section == "manifest":
            if len(line.split()) != 1 or not re.fullmatch(r"[A-Za-z0-9*][A-Za-z0-9*+._-]*", line):
                raise ValueError(f"one glob per line at line {number}: {raw}")
            if line in SOCLE_MANIFEST_PATTERNS.split():
                raise ValueError(f"{line} is already covered at line {number}")
            if line in manifest:
                raise ValueError(f"{line} appears twice, at line {number}")
            manifest.append(line)
            continue
        name, *labels = line.split()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise ValueError(f"a target is named [a-z0-9._-] at line {number}: {name}")
        if any(name == known for known, _ in targets):
            raise ValueError(f"the target {name} appears twice, at line {number}")
        if not labels and name not in SOCLE_TARGETS:
            raise ValueError(f"the target {name} names no runner at line {number},"
                             f" and the socle only has a default for {', '.join(SOCLE_TARGETS)}")
        for label in labels:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
                raise ValueError(f"a runner label is [A-Za-z0-9._-] at line {number}: {label}")
        runner: object = "" if not labels else (labels[0] if len(labels) == 1 else labels)
        targets.append((name, runner))
    if section and not unknown and not targets and not manifest and not channels \
            and not gate and not after_version and not sbom and not runners and not apart \
            and not commit_verification and not package and not checks and not own_ci and not docs_unnamed \
            and not privacy_declared:
        raise ValueError(f"{DECLARATION_FILE} declares no target, archive kind, channel, gate or cut command")
    if own_ci and checks:
        raise ValueError("[check] legs run from the ci.yml job that calls the shared CI, and [ci] own says"
                         " this repository calls none: declare one or the other")
    known = [name for name, _ in targets] or list(SOCLE_TARGETS)
    for name in package:
        if name not in known:
            raise ValueError(f"[package] names {name}, which is not a target of this release"
                             f" ({', '.join(known)})")
    return (targets, manifest, channels, channel_gates, sbom, macos_runner, runners, apart,
            commit_verification, gate, after_version, unknown, package, checks)


def parse_cli_declarations(text: str) -> tuple[list[str], list[str], str]:
    """docs/cli.reference: [build] holding the programs, [programs], [flags]."""
    sections: dict[str, list[str]] = {"build": [], "programs": [], "flags": []}
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line[1:-1] not in sections or not line.endswith("]"):
                raise ValueError(f"unknown section {line} at line {number}")
            section = line[1:-1]
            continue
        if not section:
            raise ValueError(f"line outside a [build], [programs] or [flags] section at line {number}")
        if section == "programs":
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", line):
                raise ValueError(f"one program per line at line {number}: {raw}")
            sections["programs"].append(line)
        elif section == "build":
            # Several lines, and not one: a product whose build tree is per
            # platform has a directory per target, and the socle would
            # otherwise print "the generator did not run here" on every
            # machine but one -- a note that stops being read. No pattern
            # language here: a glob would make the answer depend on what
            # happens to have been built, and this file is a declaration.
            if line.startswith("/") or ".." in pathlib.PurePosixPath(line).parts:
                raise ValueError(f"[build] is a path inside the product at line {number}: {raw}")
            if line in sections["build"]:
                raise ValueError(f"[build] names {line} twice, at line {number}")
            sections["build"].append(line)
        else:
            sections["flags"].extend(line.split())
    return sections["programs"], sections["flags"], sections["build"]


def version_tuple(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))
