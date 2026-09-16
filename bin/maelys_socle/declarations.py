# SPDX-License-Identifier: MPL-2.0
"""Product declaration data and parsers; callers supply the file contents."""
from __future__ import annotations

import pathlib
import re

from .constants import (
    CHECK_PLATFORMS, DECLARATION_FILE, SOCLE_CHANNEL_KINDS, SOCLE_COMMIT_VERIFICATIONS,
    SOCLE_GATES, SOCLE_MANIFEST_PATTERNS, SOCLE_MECHANISM, SOCLE_RUNNER_KEYS, SOCLE_TARGETS,
)


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
        # Files of docs/ that are neither generated, engaged by LICENSING.md
        # nor data: they belong in maelys-docs/<product>/.
        self.prose: list[str] = []
        # Prose README.md links: a public README holds it until a site exists.
        self.readme_held: list[str] = []
        # [ci] own: no shared CI is called, and no ci.yml is written.
        self.own_ci = False
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

    Line 1 is the nearest tag, for humans; line 2 the commit, for everything
    else. A dependency outside maelys-dev says where it lives, once, on a
    `repository <https URL>` line: maelys-http builds Mbed TLS from a pinned
    commit because the distribution's version is under its security floor.
    A dependency whose build needs its submodules says so on a `submodules`
    line, or `submodules recursive`: a submodule's commit is pinned by its
    superproject, but its URL comes from a .gitmodules this pin does not
    name, so fetching one is declared, never automatic.

    Read here rather than in read_declarations alone, because the command
    that materialises the pins needs the same reading and a second one would
    drift from it.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return {}, [f"dependencies/{name}.pin: the file is named after the repository, [a-z0-9-]"]
    lines = text.splitlines()
    if len(lines) < 2 or not re.fullmatch(r"[0-9a-f]{40}", lines[1].strip()):
        return {}, [f"dependencies/{name}.pin: line 2 must be the pinned commit (line 1 its tag)"]
    pin: dict = {"name": name, "tag": lines[0].strip(), "commit": lines[1].strip(),
                 "repository": "", "submodules": None}
    problems: list[str] = []
    for attribute in lines[2:]:
        if attribute.startswith("repository "):
            repository = attribute[len("repository "):].strip()
            if not re.fullmatch(r"https://[^\s]+", repository):
                problems.append(f"dependencies/{name}.pin: the repository line must be an https URL")
            else:
                pin["repository"] = repository
        elif attribute.split(" ")[0] == "submodules":
            mode = attribute[len("submodules"):].strip()
            if mode not in ("", "recursive"):
                problems.append(f"dependencies/{name}.pin: the submodules line takes nothing, or recursive")
            else:
                pin["submodules"] = mode
    return pin, problems


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
    unknown: list[str] = []
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line in ("[targets]", "[manifest]", "[sbom]", "[runners]", "[dependencies]",
                        "[commit]", "[channels]", "[gate]", "[cut]", "[package]", "[check]", "[ci]", "[docs]"):
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
        if section == "docs":
            # Read by declared_word(), which rendering the managed block needs.
            if line not in ("unnamed", "named"):
                raise ValueError(f"[docs] holds 'named' or 'unnamed', not {line!r}, at line {number}")
            if docs_unnamed:
                raise ValueError(f"[docs] holds one word, at line {number}")
            docs_unnamed = True
            continue
        if section == "ci":
            # Read by own_ci(), which the ci.yml rule needs before the rest of
            # the file is parsed; validated here with everything else.
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
            and not commit_verification and not package and not checks and not own_ci and not docs_unnamed:
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
