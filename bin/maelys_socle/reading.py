# SPDX-License-Identifier: MPL-2.0
"""What the socle reads of a product: its declarations, its documents, its fuzzing, its branch,
the identity its commits carry. One reading, used by every command.
"""
from __future__ import annotations

import os
import pathlib
import re

from . import authorship, dependencies
from .constants import AGENT_BRANCHES, CARRY_USES, CI_USES, CLI_DECLARATIONS, CLI_GENERATOR, CLI_REFERENCE, DECLARATION_FILE, DOCUMENTS_REPOSITORY, FORMER_DECLARATION_FILE, FUZZ_DIRECTORIES, FUZZ_TARGET, GENERATED_HEAD_LINES, LEGACY_CLI_REFERENCES, LEGS_JOB, LEGS_USES, PROGRAM, PROSE_SUFFIXES, PUBLISHED_DOWNLOAD, RUNNER_INPUTS, SOCLE_MANIFEST_PATTERNS, SOCLE_MECHANISM, SOCLE_REPOSITORY, SOCLE_RUNNER_KEYS, SOCLE_TARGETS, UNPUBLISHED
from .context import Context
from .cut import entry_heading
from .declarations import Declarations, PIN_ATTRIBUTES, parse_cli_declarations, parse_packages, parse_pin, parse_release, version_tuple
from .dependencies import BESIDE_CALL, CHECKOUT_SCRIPTS, DEPENDENCIES_VARIABLE, SOCLE_NAME, SOCLE_VARIABLE, sibling_readers
from .github import github_repository
from .host import git
from .product_workflows import pinned_socle, read_workflows
from .project import declared_value, declared_word, engaged_documents, is_utf8, unread_names
from .texts import documentation_name_lines, is_generated, readme_links, unfenced, verified_by
from .workflows import job_names, names_alone, sanitizers_twice, uses_commit, yaml_scalar


# The bullet of the managed blocks that names the documentation repository,
# and what stands in its place for a repository that declares [docs] unnamed.
DOCS_BULLET = re.compile(r"^- The prose of this repository lives in `[^`]*`.*?(?=^- |\Z)",
                         re.MULTILINE | re.DOTALL)


DOCS_UNNAMED_BULLET = (
    "- `docs/` carries what a machine writes and what this repository engages\n"
    "  publicly, and no other prose. Where the prose lives is not named from this\n"
    "  repository's files, which a public reader may see: do not name a\n"
    "  documentation repository here. A private repository that wants the name\n"
    "  declares `[docs] named` in `maelys-release.conf`.\n")


def read_fuzz(decl: Declarations) -> None:
    """Observe the harnesses and who runs them; never refuse either answer.

    The harnesses are a fact on disk. Whether a CI job runs them is read
    from ci.yml, which the product owns, so the socle cannot know whether a
    silence is an oversight or a choice: it says so and stops there. Nothing
    here rises above a note, because check counts anything but ok and note
    as a violation.
    """
    for name in FUZZ_DIRECTORIES:
        directory = decl.project / name
        if directory.is_dir() and any(directory.iterdir()):
            decl.fuzz_harnesses = name
            break
    ci = decl.project / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8") if ci.is_file() else ""
    # The value of fuzz_command, not its presence: `fuzz_command: ''` and a
    # commented value turn the socle's job off, as a sanitizer_command of
    # the same shape taught protect (agent-cli-spec).
    declared = re.search(r"^\s+fuzz_command:(.*)$", text, re.MULTILINE)
    command = yaml_scalar(declared.group(1)) if declared else ""
    socle_runs = command in ("|", "|-", ">", ">-") or command.strip("'\"") != ""
    own = FUZZ_TARGET.search(text)
    known = " nor ".join(f"{name}/" for name in FUZZ_DIRECTORIES)
    if not decl.fuzz_harnesses:
        # What the conventions can see, and no more: a harness elsewhere, or
        # a fuzzer run outside ci.yml, is invisible here, so this says
        # "detected" and never "none". maelys-cli named the blind spot: a
        # product without harness nor command was told nothing at all.
        decl.fuzz_runs = "none"
        if socle_runs or own:
            runs = f"ci.yml runs {command.strip()}" if socle_runs else "a job of ci.yml runs a fuzz target"
            decl.add("note", f"no fuzzing detected by the conventions: neither {known} exists, though {runs};"
                             " the harnesses the conventions know live there", "conventions")
        else:
            decl.add("note", f"no fuzzing detected by the conventions: neither {known} exists, and no job of"
                             " .github/workflows/ci.yml runs a fuzz target", "conventions")
        return
    if socle_runs:
        decl.fuzz_runs = "socle"
        decl.add("ok", f"{decl.fuzz_harnesses}/: the socle's fuzz job runs it", "conventions")
    elif own:
        decl.fuzz_runs = "own"
        decl.add("ok", f"{decl.fuzz_harnesses}/: a job of this repository runs it", "conventions")
    else:
        decl.fuzz_runs = "none"
        decl.add("note", f"{decl.fuzz_harnesses}/: no job of .github/workflows/ci.yml runs it"
                         + (" (fuzz_command is empty)" if declared else "") + "."
                         " 'fuzz_command: make fuzz-smoke' on the socle's check job would,"
                         " and the socle schedules nothing beyond a pull request", "conventions")


def read_branch(decl: Declarations) -> None:
    """Note a branch named after the agent that created it; never refuse one.

    A branch name is not a property of the working tree, and in CI the
    checkout is detached, where the ref is GitHub's and not the author's:
    the note therefore fires where it is useful, on the author's own
    checkout before the push. Counting the fleet's branches is
    maelys-platform's, which reads the remote refs of every repository.
    """
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=decl.project, check=False)
    prefix, separator, _ = branch.partition("/")
    if separator and prefix in AGENT_BRANCHES:
        decl.add("note", f"branch {branch} is named after the tool that created it, not after its change:"
                         " a branch carries the prefix its change would take in a commit", "conventions")


def read_authorship(decl: Declarations) -> None:
    """Note an address made up on this machine, here and in what already carries one; never refuse one.

    Where the branch note fires, and for its reason: on the author's own
    checkout, before the push, which is the last moment the commits can
    still be written again. A detached HEAD is not enough to tell a runner
    here -- a job started by a push checks its branch out by name, with no
    identity and nothing to push, as a clone made a minute ago -- so the
    runner is asked, and says so through its environment. The refusal is
    the pre-push hook's, which a repository enables or does not.

    `check` re-executes the socle a product pins, so a product reads these
    notes from the version it adopted, not from the one that runs it.
    """
    for message in authorship.notes(decl.project):
        decl.add("note", message, "conventions")


def read_declarations(context: Context, project: pathlib.Path, product: str, mechanism: str = SOCLE_MECHANISM) -> Declarations:
    """What the socle reads of a product: every rule in turn, each a function of its own.

    The order is the order the checks are reported in, and a later rule may read what an
    earlier one set on the declaration: the pins before what they ask of a build, the
    declaration file before the channels it declares.
    """
    decl = Declarations(project, product, mechanism)
    read_version(decl)
    read_packager(decl)
    read_changelog(decl)
    read_formulas(decl)
    read_verifier(decl)
    read_renderer(decl)
    read_pins(decl)
    read_pin_notes(decl)
    read_stray_checkouts(decl)
    read_documentation_name(decl)
    read_own_ci(decl)
    read_docs(decl)
    read_packages(decl)
    read_declaration_file(decl)
    read_fuzz(decl)
    read_channel_publisher(decl)
    read_branch(decl)
    read_authorship(decl)
    read_strategy(decl)
    read_coming(context, decl)
    return decl


def read_version(decl: Declarations) -> None:
    """VERSION: present, and X.Y.Z."""
    project = decl.project
    version_file = project / "VERSION"
    if not version_file.is_file():
        decl.add("missing", "VERSION file", "conventions")
    else:
        text = version_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text):
            decl.version = text
            decl.add("ok", "VERSION file", "conventions")
        else:
            decl.add("missing", "VERSION must be X.Y.Z", "conventions")


def read_packager(decl: Declarations) -> None:
    """scripts/package-release.sh: present and executable."""
    project = decl.project
    packager = project / "scripts" / "package-release.sh"
    decl.add("ok" if packager.is_file() and os.access(packager, os.X_OK) else "missing",
             "scripts/package-release.sh TARGET writing dist/")


def read_changelog(decl: Declarations) -> None:
    """CHANGELOG.md: present, and the entry of the version it says was published."""
    project = decl.project
    changelog = project / "CHANGELOG.md"
    if not changelog.is_file():
        decl.add("missing", "CHANGELOG.md", "conventions")
    else:
        decl.add("ok", "CHANGELOG.md", "conventions")
        # The release of VERSION needs its dated entry; the workflow only
        # compares VERSION with the tag, so the entry is checked here.
        if decl.version:
            # A heading present twice is refused, `Unreleased` included:
            # maelys-cli carried five `## Unreleased`, one per pull request,
            # and `cut` would have found them at the worst moment.
            titles = re.findall(r"^## (\S+)", changelog.read_text(encoding="utf-8"), re.MULTILINE)
            for title in sorted({title for title in titles if titles.count(title) > 1}):
                decl.add("missing", f"CHANGELOG.md has {titles.count(title)} entries titled ## {title}: one entry per"
                                    " version, and one Unreleased that every change completes", "conventions")
            pattern = rf"^## {re.escape(decl.version)}\s.*[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
            if re.search(pattern, changelog.read_text(encoding="utf-8"), re.MULTILINE):
                decl.add("ok", f"CHANGELOG.md has a dated ## {decl.version} entry", "conventions")
            elif decl.version == UNPUBLISHED:
                # An entry describes a release, and 0.0.0 is the absence of
                # one: VERSION says what was published last, and nothing
                # was. The rule above asked for it all the same, and only
                # `new` could answer in the right place -- it writes the
                # entry first, so every release lands above it. A repository
                # that existed before its adoption wrote it by hand when
                # `check` asked, above the entry of its first release, and
                # its changelog has read upside down since.
                decl.add("ok", f"VERSION {UNPUBLISHED} says nothing is published yet: CHANGELOG.md owes it no entry",
                         "conventions")
            else:
                decl.add("missing", f"CHANGELOG.md needs a dated entry: {entry_heading(project, decl.version)}",
                         "conventions")
            # What becomes of the ones written by hand: said, never refused.
            # Only this entry is held to a place, because it is the only one
            # that can be: nothing was ever published before nothing. An
            # order of versions in general is not the socle's to judge -- a
            # maintenance release is written after a later minor.
            # The entry is `## 0.0.0` and nothing glued to it: `0.0.0-draft`
            # is somebody's own title. And what it stands above is another
            # version: a second `## 0.0.0` is the duplicate refused above,
            # not a release. Fenced examples are not headings.
            versions = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])([^\n]*)$",
                                  unfenced(changelog.read_text(encoding="utf-8")), re.MULTILINE)
            zero = next((index for index, (version, rest) in enumerate(versions)
                             if version == UNPUBLISHED and rest[:1] in ("", " ", "\t")), None)
            above = next((version for version, _ in versions[zero + 1:] if version != UNPUBLISHED), "") \
                if zero is not None else ""
            if above:
                decl.add("note", f"CHANGELOG.md carries ## {UNPUBLISHED} above ## {above}: that entry"
                                 " says nothing was published yet, so no release comes before it. Nothing"
                                 " asks for it any more: delete it, or move it to the end", "conventions")


def read_formulas(decl: Declarations) -> None:
    """The Homebrew formula templates, one tap job each."""
    project = decl.project
    # Every packaging/homebrew/NAME.rb.in becomes one tap job publishing NAME:
    # a formula is named after what it installs, not after the repository.
    homebrew = project / "packaging" / "homebrew"
    decl.formulas = sorted(path.name[: -len(".rb.in")] for path in homebrew.glob("*.rb.in")) if homebrew.is_dir() else []
    if decl.formulas:
        decl.add("ok", "Homebrew formula templates: " + " ".join(decl.formulas))
    else:
        decl.add("note", "no packaging/homebrew/*.rb.in: no tap job")


def read_verifier(decl: Declarations) -> None:
    """scripts/verify-release.sh, when the product ships one."""
    project = decl.project
    verifier = project / "scripts" / "verify-release.sh"
    if verifier.is_file() and os.access(verifier, os.X_OK):
        # bash, like the package_command beside it. `sh` is dash on Ubuntu
        # and bash in POSIX mode on macOS, so a bashism in this script — a
        # `set -o pipefail` — passes on the operator's machine and on the
        # first stop of cut, and fails on the runner at the tag, which is
        # the one moment nothing can be retried cheaply. maelys-datalog
        # nearly paid for it.
        decl.verify_command = "bash scripts/verify-release.sh TARGET"
        decl.add("ok", "scripts/verify-release.sh TARGET runs before packaging at the tag")


def read_renderer(decl: Declarations) -> None:
    """scripts/render-homebrew-formula.sh, which the socle's own rendering replaced."""
    project = decl.project
    renderer = project / "scripts" / "render-homebrew-formula.sh"
    if renderer.is_file() and os.access(renderer, os.X_OK):
        decl.render_command = "sh scripts/render-homebrew-formula.sh TAG OUTPUT"
        # The tap's render job runs on macOS and the release was built on
        # Linux. The socle's own path downloads the tag's source archive and
        # hashes what a user downloads; a command that rebuilds it locally
        # hashes what that runner produced, and `git archive | gzip` is not
        # byte for byte the same across platforms. A product paid this by
        # hand on a published formula.
        # Said to a product that does the right thing too, until 0.48.0: the
        # advice was added the moment an executable renderer existed, without
        # reading a line of it, and maelys-egress -- whose script downloads
        # the tag's archive and hashes that -- looked for what it had done
        # wrong. The evidence can only silence the note, never raise a
        # violation: the tag's own archive URL in the renderer says it hashes
        # what a downloader gets, and its absence says the socle could not
        # tell, which is not the same as a defect.
        rendered = renderer.read_text(encoding="utf-8", errors="replace")
        names = [shape for shape in PUBLISHED_DOWNLOAD if shape in rendered]
        if names:
            decl.add("ok", f"scripts/render-homebrew-formula.sh names {names[0]}: it hashes bytes it"
                     " downloaded, not the archive this macOS runner would rebuild")
        else:
            # What this says is what the socle knows, which is that it cannot
            # tell. The note asserted a fact about the product instead -- "it
            # renders on macOS while the release was built on Linux" -- and
            # every product it has ever reached downloads before hashing. It
            # was relayed to one of them as a finding worth more than a note,
            # and their session corrected the relay; a check that cannot know
            # must not assert.
            decl.add("note", "the socle cannot tell from here whether"
                     " scripts/render-homebrew-formula.sh hashes bytes it downloaded or an archive it"
                     " rebuilt: the tap renders on macOS while the release was built on Linux, and"
                     " 'git archive | gzip' is not byte for byte the same across platforms, so a formula"
                     " naming the digest of a local rebuild fails every 'brew install'. Nothing here names"
                     f" {' or '.join(PUBLISHED_DOWNLOAD)}, which is what the socle looks for -- a renderer"
                     " that fetches the published bytes another way is right and unrecognised", "release")
        decl.add("ok", "scripts/render-homebrew-formula.sh renders the formula")


def read_pins(decl: Declarations) -> None:
    """Every dependencies/NAME.pin: its tag, its commit and what its later lines declare."""
    project = decl.project
    # Dependencies: one dependencies/NAME.pin per repository the build reads,
    # the pinned commit on line 2 and, on line 1 for humans, the tag that
    # commit carries -- parse_pin says what line 1 holds when it carries
    # none. The managed scripts/checkout-dependency.sh clones each of them
    # from the pin.
    dependencies = project / "dependencies"
    for pin_file in sorted(dependencies.glob("*.pin")) if dependencies.is_dir() else []:
        pin, problems = parse_pin(pin_file.name[: -len(".pin")], pin_file.read_text(encoding="utf-8"))
        for problem in problems:
            decl.add("missing", problem, "conventions")
        decl.pin_problems.extend(problems)
        if not pin:
            continue
        # Two pins for one commit only where a workflow already names the
        # socle. maelys-platform names it nowhere -- its one workflow runs its
        # own tests -- and pins it to choose the socle that runs `declarations`
        # across the fleet: a consumer of the program, whose pin is the only
        # source. The rule assumed the `uses:` line without reading it, and told
        # that repository to delete what it runs on.
        if pin["name"] == SOCLE_NAME and pinned_socle(project):
            decl.add("missing", f"dependencies/{SOCLE_NAME}.pin: the socle is pinned by the 'uses:' line"
                                " of the generated workflows, which is the one source; a second would be"
                                " two pins for one commit. Delete it -- 'dependencies DIR --apply'"
                                f" materialises the socle under the same root and prints {SOCLE_VARIABLE}",
                     "conventions")
            continue
        if pin["name"] == SOCLE_NAME:
            decl.add("ok", f"dependencies/{SOCLE_NAME}.pin: no workflow of this repository names the socle,"
                           " so this pin is the one source, a dependency like any other", "conventions")
        decl.dependencies.append(pin["name"])
        decl.pins[pin["name"]] = pin
        if pin["repository"]:
            decl.foreign[pin["name"]] = pin["repository"]
        if pin["submodules"] is not None:
            decl.submodules[pin["name"]] = pin["submodules"]
        if pin["unreleased"]:
            decl.unreleased[pin["name"]] = pin["unreleased"]
        for line in pin["unknown"]:
            # Named, never fatal, and never silent: neither this socle nor
            # the script it installed reads the line, so whatever it was
            # meant to declare is declared to nobody here.
            decl.add("note", f"dependencies/{pin['name']}.pin: {line!r} starts with no word this socle knows"
                     f" ({', '.join(PIN_ATTRIBUTES[:4])}): nothing here reads it, and neither does the managed"
                     " script", "conventions")


def read_pin_notes(decl: Declarations) -> None:
    """What the pins ask of a build, said once they are all read."""
    project = decl.project
    if (project / "adapter").is_dir():
        decl.add("refused", "adapter/ is the pre-0.14 layout: git mv adapter/<NAME>_PIN dependencies/<name>.pin"
                 " and adapter/PACKAGES dependencies/packages, then delete adapter/ and the Makefile lines that read it")
    if decl.dependencies:
        decl.add("ok", "pinned dependencies: " + " ".join(decl.dependencies), "conventions")

    for name, repository in sorted(decl.foreign.items()):
        decl.add("ok", f"dependencies/{name}.pin is cloned from {repository}, not maelys-dev", "conventions")
    for name, mode in sorted(decl.submodules.items()):
        decl.add("ok", f"dependencies/{name}.pin initialises its submodules"
                 + (" recursively" if mode else "") + ", from the URLs its .gitmodules names", "conventions")
    for name in sorted(name for name in decl.dependencies if decl.pins.get(name, {}).get("on_request")):
        decl.add("ok", f"dependencies/{name}.pin is cloned on request only: the jobs that clone every pin skip"
                 f" it, and 'sh scripts/checkout-dependency.sh {name} DESTINATION' brings it to the one that"
                 " reads it", "conventions")
    for name, branch in sorted(decl.unreleased.items()):
        pin = decl.pins[name]
        # An `ok`, and it says where its own reading stops. `check` reads the
        # product's files and holds no clone of what they pin: that the tag
        # is an ancestor and that the branch still holds the commit are read
        # from a checkout, by the one command that has one.
        decl.add("ok", f"dependencies/{name}.pin pins {pin['commit'][:7]} on {branch} of {pin['repository']},"
                 f" past {pin['tag']} and under no tag, on purpose. Read from the pin alone, its grammar and"
                 f" the string of its URL: nothing here asked that repository anything, and '{PROGRAM}"
                 f" dependencies DIR --apply' is what asks whether {branch} still holds it", "conventions")


def read_stray_checkouts(decl: Declarations) -> None:
    """scripts/checkout-*.sh that the socle does not manage: refused."""
    project = decl.project
    scripts = project / "scripts"
    managed_names = {name.split("/")[-1] for name in CHECKOUT_SCRIPTS}
    stray = sorted(path.name for path in scripts.glob("checkout-*.sh")
                   if path.name not in managed_names) if scripts.is_dir() else []
    if stray:
        decl.add("refused", "scripts/" + " scripts/".join(stray)
                 + ": dependencies are cloned from dependencies/*.pin by the managed"
                 " scripts/checkout-dependency.sh NAME and"
                 " scripts/checkout-dependencies.sh DESTINATION; delete these")


def read_documentation_name(decl: Declarations) -> None:
    """Whether the documentation repository may be named, and the files that name it."""
    project = decl.project
    # The two words the declaration carries before the rest of it parses:
    # a ci.yml rule and the managed block need them even when a later
    # section is refused, so they are read by the tolerant scan and not by
    # parse_release, which validates them with everything else. Read once
    # here; what renders the block and what preflight refuses read the
    # declaration, not the file again.
    named = declared_value(project, "docs", "named")
    decl.docs_named = named is not None
    decl.documents = named or ""
    if decl.docs_named:
        decl.add("ok", f"{DECLARATION_FILE} [docs] named: the managed blocks name {decl.documents or 'nothing'};"
                 " preflight refuses it on a public repository", "conventions")
    elif declared_word(project, "docs", "unnamed"):
        decl.add("note", f"{DECLARATION_FILE} [docs] unnamed is the default since 0.58.0; the line can go",
                 "conventions")
    # The rule at the reading end, on every tracked file: a repository that
    # does not declare [docs] named carries the name of the documentation
    # repository in none of them. 0.60.0 read the three seeded texts only,
    # and one search over the fleet found two more files, both rewritten by
    # migrate before 0.59.2 and never looked for on disk. The managed block
    # is the socle's to rewrite at adoption, and CHANGELOG.md is history a
    # product does not rewrite; everything else is the product's, said with
    # its lines. A note in 0.60.0 and a refusal from 0.61.0, announced.
    if not decl.docs_named:
        # The tracked files of a repository; every file of a directory that
        # is not one yet, .git apart -- a check that read nothing without a
        # repository would have said nothing where it had said something.
        if (project / ".git").exists():
            try:
                tracked = git("ls-files", "-z", cwd=project, check=False).split("\0")
            except UnicodeDecodeError as error:
                # A tracked name that is not UTF-8 ended here `check`,
                # `declarations`, `adopt`, `preflight` and `dependencies`: a
                # decoding error, exit 1 and no verdict. Reading through it
                # is not a patch -- the readers of a name open the file by it
                # and write it back -- so the command ends here in words. git
                # has answered and the error holds the answer: the names are
                # read from its bytes, and git is not asked again.
                names = sorted(name for name in bytes(error.object).split(b"\0") if not is_utf8(name))
                if not names:
                    raise
                raise unread_names(names) from None
        else:
            tracked = [path.relative_to(project).as_posix() for path in project.rglob("*")
                       if path.is_file() and ".git/" not in path.as_posix()]
        # The files a reader reads: prose. Code and data that carry the name
        # are the checker's own knowledge, or a fixture of it; every leak the
        # fleet found was Markdown. A violation since 0.61.0, as 0.60.0
        # announced; the socle itself is held to it, with no exception.
        # A CHANGELOG.md anywhere is history -- the socle's own frozen copies
        # in its references included -- and history is not rewritten.
        for relative in sorted(name for name in tracked if name and name.rpartition("/")[2] != "CHANGELOG.md"
                               and name.lower().endswith(PROSE_SUFFIXES)):
            path = project / relative
            if not path.is_file():
                continue
            # The name is ASCII, and is looked for whatever the rest of the
            # file is written in: a prose in Latin-1 was skipped here
            # without a word, by a rule that says it searched every file.
            # It is read with what does not decode replaced, and what is
            # found there is a note: the file was never refused, and a
            # patch turns no green product red.
            decoded = True
            try:
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text, decoded = path.read_text(encoding="utf-8", errors="replace"), False
            except OSError:
                continue
            lines = documentation_name_lines(text)
            if lines and not decoded:
                decl.add("note", f"{relative} is not UTF-8 and was read with what does not decode replaced: it names"
                                 f" the documentation repository at line{'s' if len(lines) > 1 else ''}"
                                 f" {', '.join(str(n) for n in lines[:8])}{', …' if len(lines) > 8 else ''}, which"
                                 f" this repository's files do not unless {DECLARATION_FILE} declares [docs] named"
                                 " OWNER/NAME. Noted, where a file that is UTF-8 is refused for it: remove the name"
                                 " (a public reader may see it), or declare the repository on a private one",
                         "conventions")
            elif lines:
                decl.documentation_named.append(relative)
                decl.add("missing", f"{relative} names the documentation repository at line"
                                    f"{'s' if len(lines) > 1 else ''} {', '.join(str(n) for n in lines[:8])}"
                                    f"{', …' if len(lines) > 8 else ''}, which this repository's files do not unless"
                                    f" {DECLARATION_FILE} declares [docs] named OWNER/NAME: remove the name (a public"
                                    " reader may see it), or declare the repository on a private one", "conventions")


def read_own_ci(decl: Declarations) -> None:
    """[ci] own: a repository whose CI is entirely its own."""
    project = decl.project
    product = decl.product
    ci = project / ".github" / "workflows" / "ci.yml"
    # A repository whose CI is entirely its own calls no shared CI, and
    # `check` must not create a ci.yml for it nor warn that none calls
    # check-product.yml. The socle is the first: its CI lints the workflows
    # it ships and runs its own tests, and three `make check` legs mean
    # nothing to a Python program with no Makefile. A declaration, and not
    # an exception for one name.
    decl.own_ci = declared_word(project, "ci", "own")
    if decl.own_ci:
        if ci.is_file() and CI_USES.search(ci.read_text(encoding="utf-8")):
            decl.add("missing", f"{DECLARATION_FILE} declares [ci] own and .github/workflows/ci.yml calls the"
                     " socle's check-product.yml: remove the one that is not true", "conventions")
        else:
            decl.add("ok", f"{DECLARATION_FILE} [ci] own: this repository's CI is its own and calls no shared"
                     " CI; adopt writes no ci.yml", "conventions")
    elif ci.is_file():
        text = ci.read_text(encoding="utf-8")
        if CI_USES.search(text):
            decl.calls_socle_ci = True
            decl.add("ok", ".github/workflows/ci.yml calls check-product.yml of the socle")
            # The carry is written by hand and kept at the check's commit by
            # adopt. Read here too: a pin held at the end that writes is a
            # pin nothing holds between two adoptions, and bundles made by
            # one socle feeding the check of another is the second contract
            # this rule exists to prevent.
            carry_at, check_at = uses_commit(CARRY_USES, text), uses_commit(CI_USES, text)
            if carry_at and carry_at != check_at:
                decl.add("missing", ".github/workflows/ci.yml calls carry-dependencies.yml at"
                         f" {carry_at[:7]} and check-product.yml at {check_at[:7]}: the bundles one socle"
                         " makes would feed the check of another. Run"
                         f" '{PROGRAM} adopt {project} --apply', which puts both at one commit")
            for number in sanitizers_twice(text):
                decl.add("note", f".github/workflows/ci.yml line {number} sanitizes, and the socle's own"
                         " sanitizers job runs too: sanitizer_command is opt-out and this call does not"
                         " set it, so the pull request builds the same instrumented tree twice."
                         " maelys-egress passes 'sanitizer_command: \"\"' and keeps its own job, which is"
                         " the choice the socle cannot make for a product", "conventions")
        else:
            decl.add("warn", ".github/workflows/ci.yml does not call the socle's check-product.yml: add a job"
                     f" 'uses: {SOCLE_REPOSITORY}/.github/workflows/check-product.yml@SHA # TAG'"
                     f" with 'product: {product}', as adopt would create it")


def read_packages(decl: Declarations) -> None:
    """dependencies/packages: the system packages the build installs."""
    project = decl.project
    dependencies = project / "dependencies"
    packages = dependencies / "packages"
    if packages.is_file():
        try:
            decl.linux_packages, decl.macos_packages = parse_packages(packages.read_text(encoding="utf-8"))
            decl.add("ok", f"dependencies/packages: linux [{decl.linux_packages}] macos [{decl.macos_packages}]",
                     "conventions")
        except ValueError as error:
            decl.add("missing", f"dependencies/packages: {error}", "conventions")


def read_declaration_file(decl: Declarations) -> None:
    """maelys-release.conf, and the former home of the declaration."""
    project = decl.project
    ci = project / ".github" / "workflows" / "ci.yml"
    # The former home is still read, so a repository mid-move keeps its
    # targets and its channels; carrying it is the violation, not a silence.
    release, former = project / DECLARATION_FILE, project / FORMER_DECLARATION_FILE
    if former.is_file():
        # warn, not missing: check still exits 2 — the repository must move —
        # but adopt is the remedy, and a status that blocked adopt would make
        # the remedy unreachable. Carrying both files is refused instead: the
        # socle cannot choose between them, and a human must delete one.
        decl.add("refused" if release.is_file() else "warn",
                 f"{FORMER_DECLARATION_FILE}: the declarations moved to {DECLARATION_FILE} in 0.37.0"
                 + (f"; this repository carries both, and {DECLARATION_FILE} is the one read"
                    if release.is_file()
                    else f"; 'adopt {project} --apply' moves it with git mv, keeping its history"),
                 "conventions")
        if not release.is_file():
            release = former
    if release.is_file():
        try:
            decl.targets, decl.manifest_patterns, decl.channels, decl.channel_gates, \
                decl.sbom_pattern, decl.macos_runner, decl.runners, decl.dependencies_apart, \
                decl.commit_verification, decl.gate, decl.after_version, unknown, decl.package, \
                decl.legs = \
                parse_release(release.read_text(encoding="utf-8"))
            for section in unknown:
                decl.add("missing", f"{release.name}: section {section} is not one this socle knows"
                                    " ([targets], [manifest], [channels], [gate], [cut], [privacy]); what it declares"
                                    " is not applied here", "conventions")
            if decl.after_version:
                # The command is this repository's, and cut runs it on the
                # operator's machine: what the socle can check is that the
                # files it names are here. The check is a convention, not a
                # release one — cut serves a repository whatever publishes it.
                absent = [word for word in decl.after_version.split()
                          if "/" in word and not (project / word).exists()]
                if absent:
                    decl.add("missing", f"{release.name} [cut] after-version names "
                             + " ".join(absent) + ", which this repository does not carry", "conventions")
                else:
                    decl.add("ok", f"{release.name} [cut] after-version: {decl.after_version}", "conventions")
            if decl.targets:
                decl.add("ok", f"{release.name} targets: "
                               + " ".join(name for name, _ in decl.targets))
            for name, platform, command in decl.legs:
                decl.add("ok", f"{release.name} [check] {name} on {platform}: {command} — runs as"
                               f" '{LEGS_JOB} / {name}', from the job of ci.yml adopt writes", "conventions")
            ci_text = ci.read_text(encoding="utf-8") if decl.legs and ci.is_file() else ""
            if decl.legs and ci_text and not decl.calls_socle_ci:
                decl.add("missing", f"{release.name} [check] declares legs, and .github/workflows/ci.yml does"
                                    " not call check-product.yml: adopt writes the legs job beside that call"
                                    " and nowhere else", "conventions")
            elif decl.legs and LEGS_JOB in job_names(ci_text.split("\n")) \
                    and not any(LEGS_USES.search(line) for line in names_alone(ci_text.split("\n"))):
                decl.add("refused", f".github/workflows/ci.yml has a job of its own named {LEGS_JOB}, the name"
                                    f" adopt writes [check] under: rename that job, or its contexts and the"
                                    " legs' would be one prefix", "conventions")
            if decl.package:
                verifying = [name for name, _ in decl.targets] or list(SOCLE_TARGETS)
                decl.add("ok", f"{release.name} [package] {' '.join(decl.package)}: every target of the"
                               f" release verifies ({', '.join(verifying)}), and these alone package")
            if decl.manifest_patterns:
                decl.add("ok", f"{release.name} manifest: " + SOCLE_MANIFEST_PATTERNS
                               + " " + " ".join(decl.manifest_patterns))
            if decl.sbom_pattern:
                decl.add("ok", f"{release.name} sbom: {decl.sbom_pattern},"
                               " attested against the files it names")
            if decl.commit_verification:
                decl.add("ok", f"{release.name} [commit] {decl.commit_verification}: the release asks"
                         " that of the commit the tag names, beyond the tag's own signature")
            read_dependencies_apart(decl, release)
            read_runner_declarations(decl, release)
        except ValueError as error:
            decl.add("missing", f"{release.name}: {error}")


def read_dependencies_apart(decl: Declarations, release: pathlib.Path) -> None:
    """[dependencies] apart: the pins are materialised away from the product, and nothing may read them beside it.

    Called inside the reading of the declaration file, whose refusal of a value it shares.
    """
    project = decl.project
    if decl.dependencies_apart:
        # What is said here is what the declaration asks and what the
        # socle does with it, never what the build reads: release.yml
        # may still hold the sibling checkouts of a former adoption,
        # and this line stood as an "ok" two lines above the file that
        # contradicted it. maelys-egress read that pair.
        decl.add("ok", f"{release.name} [dependencies] apart: the pins are materialised under"
                 f" ${DEPENDENCIES_VARIABLE}, never beside the product")
        # What the trial of 0.45.0 found, reported before a push
        # instead of by six red jobs after it: the socle exports the
        # root in the job it runs, and a job of the product clones
        # on its own runner where that path names nothing.
        #
        # A note, and the status matters here. `check` collects
        # everything that is not ok or note into its violations, so
        # a warning makes it exit 2 while adopt still proceeds --
        # the worst pairing, a product adopting and going red. And a
        # second red would say nothing new: a job that clones beside
        # itself already fails on the build's own message. Naming
        # the line is the whole value.
        #
        # Every tracked file, and not ci.yml alone: 0.45.0 named the
        # fifteen lines of one workflow to maelys-egress and left
        # four other files silent -- an image, two scripts with a
        # hard-coded fallback, and a third they only found after a
        # second red round, in the line below the one they had just
        # fixed. Three red CI rounds for what one search names at
        # once.
        # The socle is the last neighbour a build still read: it is
        # not a pin, so the search could not see it, and two products
        # keep a `MAELYS_RELEASE_DIR ?= ../maelys-release` whose
        # target moves as much as any working copy.
        for path, number, name, text in sibling_readers(
                project, decl.dependencies + ([SOCLE_NAME] if pinned_socle(project) else [])):
            if name and DEPENDENCIES_VARIABLE in text:
                decl.add("note", f"{path} line {number} falls back to ../{name} when"
                         f" ${DEPENDENCIES_VARIABLE} is unset, which is the fallback nobody"
                         " ever sees taken: it reads whatever is next door, and the working"
                         " copy of someone who also develops that dependency is what sits"
                         f" there. Refuse instead: {text}", "conventions")
            elif name == SOCLE_NAME:
                decl.add("note", f"{path} line {number} reads ../{name}, a working copy that moves"
                         f" as much as any other. ${SOCLE_VARIABLE} names the socle this"
                         " repository pins, written by 'dependencies DIR --apply' and by"
                         f" scripts/checkout-dependencies.sh in a job: {text}", "conventions")
            elif name:
                decl.add("note", f"{path} line {number} reads ../{name}, and the pins are under"
                         f" ${DEPENDENCIES_VARIABLE} since this repository declares"
                         f" '[dependencies] apart': {text}", "conventions")
            elif BESIDE_CALL.search(text) and path.startswith(".github/workflows/"):
                decl.add("note", f"{path} line {number} clones beside the product,"
                         " which the build no longer reads: one line gives it the root instead,"
                         " 'sh scripts/checkout-dependencies.sh \"$RUNNER_TEMP/dependencies\""
                         " >>\"$GITHUB_ENV\"'", "conventions")
            else:
                decl.add("note", f"{path} line {number} clones one pin on its own, and the root"
                         " is then nobody's: 'sh scripts/checkout-dependencies.sh DESTINATION'"
                         f" writes every pin there and prints ${DEPENDENCIES_VARIABLE} for the"
                         f" build to read: {text}", "conventions")
    elif decl.dependencies:
        # Read after the declaration file and not with the pins: the
        # pins are read first, so a rule placed there sees a
        # declaration that has not been parsed yet. That is the
        # second time in a day; the fix is the placement, and the
        # test below is what makes it stay fixed.
        decl.add("missing", f"dependencies/ pins {' '.join(decl.dependencies)} and"
                 f" {release.name} does not declare '[dependencies] apart': the build still"
                 f" assumes a sibling, which is also where the working copies of whoever"
                 f" develops those dependencies live. Read ${DEPENDENCIES_VARIABLE} for the"
                 f" root -- '{PROGRAM} dependencies DIR --apply' writes the checkouts and"
                 " prints the variable -- then declare it", "conventions")


def read_runner_declarations(decl: Declarations, release: pathlib.Path) -> None:
    """[runners]: the machine each leg of the shared CI runs on, and what ci.yml passes for it.

    Called inside the reading of the declaration file, whose refusal of a value it shares.
    """
    project = decl.project
    for key in SOCLE_RUNNER_KEYS:
        if not decl.runners.get(key):
            continue
        decl.add("ok", f"{release.name} [runners] {key}: " + " ".join(decl.runners[key])
                       + " (honoured on a private repository only)")
        # ci.yml belongs to the product: adopt keeps its socle line
        # current and writes nothing else there. A declared runner
        # reaches release.yml, which the socle owns, and is reported
        # here rather than written -- the socle reports drift in what
        # it does not own instead of editing it.
        ci_file = project / ".github" / "workflows" / "ci.yml"
        if ci_file.is_file() and f"{RUNNER_INPUTS[key]}:" not in ci_file.read_text(encoding="utf-8"):
            decl.add("warn", f"[runners] {key} declares {' '.join(decl.runners[key])} and"
                     " .github/workflows/ci.yml does not pass it to check-product.yml:"
                     f" '{PROGRAM} adopt DIR --apply' writes the line")


def read_channel_publisher(decl: Declarations) -> None:
    """scripts/publish-channel.sh, for a product that declares channels."""
    project = decl.project
    publisher = project / "scripts" / "publish-channel.sh"
    publishes = publisher.is_file() and os.access(publisher, os.X_OK)
    if decl.channels and not publishes:
        decl.add("missing", f"{DECLARATION_FILE} declares "
                            + " ".join(name for name, _ in decl.channels)
                            + ", so scripts/publish-channel.sh TAG CHANNEL must exist and be"
                              " executable: the section says where to publish, the script how")
        decl.channels = []
    elif decl.channels:
        decl.add("ok", f"{DECLARATION_FILE} channels: "
                       + " ".join(f"{name} ({kind})" for name, kind in decl.channels))
        for name, _kind in decl.channels:
            if decl.channel_gates.get(name) == "none":
                decl.add("ok", f"[channels] {name} none: it publishes as soon as the release does,"
                               " with no approval of its own", "release")
            elif decl.channel_gates.get(name) == "reviewer":
                # The choice declared, which 0.52.0 promised to turn into an ok
                # and did not: a declared reviewer fell into the branch of a line
                # without a third word, and was told to declare what it had.
                # Found by maelys-datalog on its own `npm github-packages reviewer`.
                decl.add("ok", f"[channels] {name} reviewer: it asks for its own approval once the release"
                               " workflow has finished, as declared", "release")
            else:
                decl.add("note", f"[channels] {name} asks for its own approval, under the release's"
                                 " environment, and it becomes pending only once the release workflow"
                                 " has finished — hours later on some products. Declare"
                                 f" '{name} {_kind} none' to publish without one, or"
                                 f" '{name} {_kind} reviewer' to say this is the choice", "release")
    elif publishes:
        decl.add("note", f"scripts/publish-channel.sh exists but {DECLARATION_FILE} declares no"
                         " [channels]: nothing calls it, and no channel job is written")


# What a later version will refuse, said a version early. A rule that turns
# into a violation without notice is the cadence complaint in its sharpest
# form: a product adopts on Tuesday, conformant, and is in violation on
# Wednesday for something nobody told it was coming. Each entry names the
# version it bites in, what the product must do, and which products it
# reaches; `check` says it as a note, and the test below refuses to let an
# announcement outlive the version that was supposed to honour it -- so an
# entry still here when that version ships fails the socle's own suite until
# the rule is written or the date is moved, in the open.
COMING: tuple[tuple[str, str, str], ...] = (
    # 0.54.0 honoured the first announcement -- the leg rename -- after
    # moving it once, from 0.53.0, in the open. 0.60.0 honoured the second,
    # its epilogue: the aliases that made the rename survivable left once
    # nothing needed them, measured on the fleet the day of the cut.
    # The third: 0.60.0 noted a text naming the documentation repository;
    # 0.61.0 refuses it, honoured and removed.
)


def read_coming(context: Context, decl: Declarations) -> None:
    """Announce what a later version will refuse, to the products it reaches."""
    here = version_tuple((context.socle_root() / "VERSION").read_text(encoding="utf-8").strip())
    for version, reaches, says in COMING:
        if version_tuple(version) <= here:
            continue
        if reaches == "socle" and not decl.calls_socle_ci:
            continue
        if reaches == "channels" and not decl.channels:
            continue
        decl.add("note", f"coming in maelys-release {version}: {says}", "conventions")


def read_docs(decl: Declarations) -> None:
    """Classify docs/: the generated reference, public engagements, data, prose.

    The rule is maelys-platform's documentation policy: a product repository
    keeps what a machine writes and what LICENSING.md engages; its prose
    lives in maelys-docs/<product>/. The socle names what must move, and
    moves nothing.
    """
    docs = decl.project / "docs"
    reference = decl.project / CLI_REFERENCE
    # A product whose command line is built on the framework pins maelys-cli,
    # whose generator writes the reference. The socle owns the wrapper, so the
    # reference is produced the same way everywhere; the product's Makefile
    # calls it and overrides by environment what its layout requires.
    has_generator = "maelys-cli" in decl.dependencies or (decl.project / CLI_GENERATOR).is_file()
    commands = [name for name in decl.formulas if not name.startswith("lib")]
    declarations = decl.project / CLI_DECLARATIONS
    if declarations.is_file():
        try:
            decl.cli_programs, decl.cli_flags, decl.cli_build = \
                parse_cli_declarations(declarations.read_text(encoding="utf-8"))
        except ValueError as error:
            decl.add("missing", f"{CLI_DECLARATIONS}: {error}", "conventions")
    # A product whose command line is built on the framework pins maelys-cli,
    # whose generator writes the reference. The socle runs it: no product
    # carries a rule, a path or a drift check of its own for this.
    # The file, or the command line that earns one. Until 0.62.x this read
    # `has_generator and reference.is_file()`, so a product that pinned
    # maelys-cli and shipped a command got no reference and -- because the
    # note below was written for the other case -- no word about it either.
    # Nothing in the fleet is in that state today, measured on the ten
    # repositories: the two that pin maelys-cli both carry the file. What
    # this changes is what the next product finds.
    if has_generator and (reference.is_file() or commands):
        if not decl.cli_programs:
            decl.cli_programs = commands or [decl.product]
        decl.generates_reference = True
        decl.add("ok", f"{CLI_REFERENCE} is generated by the socle from maelys-cli's generator:"
                 f" {' '.join(decl.cli_programs)}", "conventions")
    elif has_generator and not reference.is_file():
        # A library on the framework: its formulas are lib*, it ships no
        # command, and there is nothing to generate a reference from. Said
        # out loud, because the silence here is what a product reported.
        decl.add("note", f"no {CLI_REFERENCE}: {decl.product} pins maelys-cli and declares no command"
                 " (its Homebrew templates are all lib*), so the socle generates no reference for it",
                 "conventions")
    legacy = [name for name in LEGACY_CLI_REFERENCES if (decl.project / name).is_file()]
    for name in legacy:
        decl.add("note", f"{name}: the generated CLI reference is {CLI_REFERENCE} for every product;"
                 f" git mv {name} {CLI_REFERENCE} and point the rule that writes it there", "conventions")
    if not docs.is_dir():
        return
    if reference.is_file():
        if is_generated(reference.read_text(encoding="utf-8")):
            decl.add("ok", f"{CLI_REFERENCE} is generated", "conventions")
        else:
            decl.add("warn", f"{CLI_REFERENCE} does not say in its first {GENERATED_HEAD_LINES} lines that it is"
                     " generated and must not be edited; the reference is written from describe, never by hand",
                     "conventions")
    elif not legacy and not has_generator and commands:
        # Only a product that ships a command can carry a reference: a library
        # publishes lib* alone and is told nothing about docs/cli.md.
        decl.add("note", f"no {CLI_REFERENCE}: a product with a command line carries its generated reference there",
                 "conventions")
    engaged = engaged_documents(decl.project)
    for path in sorted(docs.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(decl.project).as_posix()
        if relative == CLI_REFERENCE or relative in legacy or relative in engaged:
            continue
        if path.suffix.lower() != ".md":
            continue  # data, never prose
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # A document in another encoding ended every command that reads
            # the declarations on a decoding error. What is asked of it
            # here is in ASCII -- that it says it is generated, or what
            # verifies it -- and is read through what does not decode; the
            # file is said not to be UTF-8, and is sorted like any other.
            text = path.read_text(encoding="utf-8", errors="replace")
            decl.add("note", f"{relative} is not UTF-8: it is read with what does not decode replaced, for what"
                     " its first lines say of it, and is sorted as any other document", "conventions")
        if is_generated(text):
            continue
        check = verified_by(text)
        if check:
            decl.add("ok", f"{relative} is a reference {check} verifies against the code: it stays", "conventions")
            continue
        decl.prose.append(relative)
    readme = decl.project / "README.md"
    links = readme_links(readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else "",
                         github_repository(decl.project))
    for relative in decl.prose:
        if any(relative == link or relative.startswith(link + "/") for link in links):
            # `check` is offline: it sees the link, not whether a stranger can
            # follow it nor whether a site documents the product. So it says
            # the rule and asserts nothing; maelys-platform, which reads both,
            # decides `movable`.
            decl.readme_held.append(relative)
            decl.add("note", f"{relative}: prose, and README.md links it. If this repository is public and no"
                     " site documents the product, it stays: maelys-docs is private and the link would break"
                     " for every reader. 'maelys-platform docs' says which", "conventions")
            continue
        # The destination is private, and `check` is offline by design, so it
        # cannot tell whether this repository is public. A public product
        # whose README links a document moves it into a repository its
        # readers cannot open: the move is right for an agent and wrong for
        # a stranger, and the socle says so rather than advising blind.
        # `preflight`, which reads the API, answers it properly.
        decl.add("note", f"{relative}: prose belongs in maelys-docs/{decl.product}/"
                 f"{relative[len('docs/'):]}; move it with its history:"
                 f" 'maelys-platform docs --prose --only {decl.product} --format jsonl"
                 f" | {PROGRAM} migrate . --product {decl.product} --documents -'."
                 f" {DOCUMENTS_REPOSITORY.split('/')[-1]} is private: a document a public README links to"
                 " stays here until the destination is one its readers can open", "conventions")


def read_strategy(decl: Declarations) -> None:
    """Note a workflow that runs twice on a pull request.

    `push` with no branch filter and `pull_request` in the same workflow run
    the same jobs twice for every pushed commit of a pull request. It is a
    choice a repository may make, so this is a note and never a violation —
    but nobody could see it without opening the file.
    """
    for workflow in read_workflows(decl.project):
        if "push" in workflow["events"] and "pull_request" in workflow["events"] \
                and not workflow["branches"] and not workflow["tags"]:
            decl.add("note", f".github/workflows/{workflow['file']}: push names no branch and"
                             " pull_request is declared too, so every push of a pull request runs"
                             " it twice; 'branches: [main]' under push runs it once", "conventions")
