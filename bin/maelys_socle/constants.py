# SPDX-License-Identifier: MPL-2.0
"""Shared vocabulary and patterns; no filesystem or host access."""
from __future__ import annotations

import re

PROGRAM = "maelys-release"

PRODUCT = "Maelys Release"

SOCLE_REPOSITORY = "maelys-dev/maelys-release"

DEFAULT_TAP = "maelys-dev/homebrew-tap"

DOCUMENTS_REPOSITORY = "maelys-dev/maelys-docs"

# The files a reader reads, where the documentation repository's name is a
# leak: every one the fleet found was Markdown. Code and data that carry it
# are the checker's own knowledge, or a fixture of it.
PROSE_SUFFIXES = (".md", ".markdown", ".txt", ".rst")

DEFAULT_IMAGE = "ubuntu:26.04"

SOCLE_LINUX_PACKAGES = "build-essential dpkg-dev file rpm"  # the release.yml default

SOCLE_MANIFEST_PATTERNS = "*.tar.gz *.deb *.rpm"  # the release.yml default

# What a renderer that hashes published bytes names: the tag's source archive,
# which tap.yml's own path downloads; a release asset, which a product that
# publishes its own tarball hashes instead; and the command that fetches one.
# The first list held the source archive alone and left maelys-http -- which
# hashes releases/download/$tag/... -- with a note saying the opposite of what
# its script does. Two of three recognised is not a recogniser.
PUBLISHED_DOWNLOAD = ("archive/refs/tags", "releases/download", "gh release download")

# Registries a channel of the socle can serve. GitHub Packages publishes
# with the run's own token; npmjs and PyPI would need trusted publishing,
# which matches an OIDC claim naming this repository rather than the
# product once a reusable workflow runs the publication, and PyPI
# documents that a reusable workflow cannot be a trusted publisher.
SOCLE_CHANNEL_KINDS = ("github-packages",)

# What a product asks of its release environment. The socle verifies the
# answer a repository gave; it does not give it in the repository's place.
SOCLE_GATES = ("reviewer", "none")

# The legs whose runner a private repository may declare, named as
# check-product.yml's matrix names them -- except macos, which keeps the
# bare word ten repositories already carry.
SOCLE_RUNNER_KEYS = ("macos", "linux-x86_64", "linux-arm64")

# Where a leg of [check] runs, named as the socle's own legs are.
CHECK_PLATFORMS = ("linux", "linux-arm64", "macos")

# The job of ci.yml that calls check-legs.yml. Fixed, because it is the first
# half of every context a leg reports -- `legs / NAME` -- and a protection
# requires by that name.
LEGS_JOB = "legs"

# What release.yml asks of the commit a tag names, beyond the tag's own
# signature. The workflow has offered these since 0.21.0 and nothing rendered
# the input, so `none` was the only value a product could reach while the
# conventions described it as the one every product had chosen.
SOCLE_COMMIT_VERIFICATIONS = ("signed", "signed-on-default-branch")

# Where the fleet keeps its fuzz harnesses, preferred first. Five
# repositories use tests/fuzz, four use fuzz; the socle reads either and
# says which it found rather than refusing the second.
FUZZ_DIRECTORIES = ("tests/fuzz", "fuzz")

FUZZ_TARGET = re.compile(r"make\s+[A-Za-z0-9_.-]*fuzz[A-Za-z0-9_.-]*")

# The targets release.yml builds when a product declares none of its own.
SOCLE_TARGETS = ("linux-x86_64", "linux-arm64", "macos-arm64")

BOTTLES = '\'["macos-15","macos-26"]\''

PLATFORMS = {"linux-x86_64": "linux/amd64", "linux-arm64": "linux/arm64"}

# One name and one place for the reference a machine writes from describe
# (docs/policies/documentation.md of maelys-platform). Everything else in
# docs/ is prose, and prose lives in maelys-docs.
CLI_REFERENCE = "docs/cli.md"

CLI_CONTRACT = "docs/cli-contract.json"

# What a product declares about its own reference, when the conventions do
# not already say it: [programs] to describe, [flags] for the generator.
CLI_DECLARATIONS = "docs/cli.reference"

# maelys-cli carries the generator instead of pinning it: the framework is
# held to the same rule as the products it serves.
CLI_GENERATOR = "tools/generate_cli_reference.py"

LEGACY_CLI_REFERENCES = ("docs/cli-reference.md", "docs/generated/cli-reference.md")

# A generated file says so in its head, whatever wrote it: the word
# "generated" and a refusal to be edited, within the first ten lines. The
# generators live in maelys-cli and in the products, not here, so the socle
# fixes the rule rather than one spelling; the three in use satisfy it, and
# one of them sits on line 3, under the title.
GENERATED_HEAD_LINES = 10

GENERATED_MARK = re.compile(r"generated", re.IGNORECASE)

GENERATED_REFUSAL = re.compile(r"do not edit|don't edit|ne pas .diter", re.IGNORECASE)

# A reference a check of the repository holds against the code, written by
# hand: "<!-- VERIFIED by make api-doc-check against the public API; edit with
# the code. -->". maelys-platform's condition, character for character, so that
# `check` and `maelys-platform docs --prose` never sort one file two ways. It
# stays for the reason docs/cli.md does: the check that holds it must read it
# in the checkout it verifies.
VERIFIED_MARK = re.compile(r"verified by\s+(?P<check>\S.{0,120}?)\s+against\b.{0,200}?edit with the code",
                           re.I | re.S)

# A link a README makes to a page of docs/: inline, by reference, in HTML, or
# through the repository's own GitHub URL. maelys-platform's, as above.
README_LINK = re.compile(r"""(?:\]\(\s*<?|^\s{0,3}\[[^\]]+\]:\s*<?|\bhref\s*=\s*["'])(?P<target>[^\s)"'>]+)""",
                         re.I | re.M)

# What a product carries whatever publishes it, and what belongs to the
# socle's release mechanism. A product whose mechanism is custom or none
# receives and is checked on the first set only (docs/conventions.md,
# "Conventions and mechanism").
CONVENTION_MANAGED = ("AGENTS.md", "CLAUDE.md")

# Written once when missing, then owned by the product: the socle cannot
# judge prose it does not generate, so check reports absence as a note.
CONVENTION_SEEDED = ("RELEASING.md", "LICENSING.md", "SECURITY.md")

RELEASE_MANAGED = (
    ".github/workflows/release.yml",
    "scripts/checkout-dependency.sh",
    "scripts/checkout-dependencies.sh",
    ".claude/skills/maelys-release/SKILL.md",
    ".github/workflows/ci.yml",
)

MANAGED = (
    ".github/workflows/release.yml",
    "scripts/checkout-dependency.sh",
    "scripts/checkout-dependencies.sh",
    CLI_REFERENCE,
    CLI_CONTRACT,
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/skills/maelys-release/SKILL.md",
    "RELEASING.md",
    "LICENSING.md",
    "SECURITY.md",
    ".github/workflows/ci.yml",
)

# The agents that work in the fleet and name a worktree branch after
# themselves by default. The rule is that a branch carries the prefix of
# its change and never the name of the tool, so this is the one list the
# socle can close: it is the set of agents the socle writes instructions
# for, not a vocabulary of change types, which belongs to each repository.
AGENT_BRANCHES = ("claude", "codex")

# What a repository declares to this command: one grammar, one home. It sat
# in packaging/release until 0.37.0, and the eleven packaging/ of the fleet
# hold materials — formula templates, systemd units, Containerfiles, a kernel
# config — never settings, while eighteen repositories have no packaging/ at
# all and still want to declare. The name says the tool and the nature; the
# socle only ever reads this file, never writes it, so the comments a product
# puts beside each section are the product's.
DECLARATION_FILE = "maelys-release.conf"

FORMER_DECLARATION_FILE = "packaging/release"

SOCLE_MECHANISM = "maelys-release"

MECHANISMS = (SOCLE_MECHANISM, "custom", "none")

RELEASE_USES = re.compile(rf"{re.escape(SOCLE_REPOSITORY)}/\.github/workflows/release\.yml@[0-9a-f]{{40}}")

CI_USES = re.compile(rf"({re.escape(SOCLE_REPOSITORY)}/\.github/workflows/check-product\.yml@)[0-9a-f]{{40}} # \S+")

# The call that carries a product's private pins to the legs of that check. It
# is written by hand, since what travels is the product's choice, and kept at
# the check's own commit by adopt: a check at one socle fed by bundles of
# another is two contracts where there should be one.
CARRY_USES = re.compile(
    rf"({re.escape(SOCLE_REPOSITORY)}/\.github/workflows/carry-dependencies\.yml@)[0-9a-f]{{40}} # \S+")

LEGS_USES = re.compile(rf"({re.escape(SOCLE_REPOSITORY)}/\.github/workflows/check-legs\.yml@)[0-9a-f]{{40}} # \S+")

BEGIN = "<!-- maelys-release:begin -->"

END = "<!-- maelys-release:end -->"

# The input each declared leg fills, in the order check-product.yml lists
# them. A key here is a line under the call's `with:`, and nothing else.
RUNNER_INPUTS = {"macos": "macos_runner", "linux-x86_64": "linux_x86_64_runner",
                 "linux-arm64": "linux_arm64_runner"}

# What a line naming the socle looks like once its commit and tag are taken
# out: a pin that moves and nothing else is a third kind of change, neither
# the mechanism nor its prose.
PIN_SPELLINGS = (re.compile(r"@[0-9a-f]{40} # \S+"), re.compile(r"(maelys-release) v?[0-9][0-9.]*\S* \([0-9.]+\)"))

RUNS_ON = re.compile(r"^[ \t]*runs-on:[ \t]*(.*?)[ \t]*$", re.M)

# A runner label is what GitHub accepts as one. Anything else read out of a
# workflow is not a label the socle may vouch for: it goes to `unresolved`,
# where an observer blocks on it, rather than into `labels`, where it would
# pass and, worse, make a genuinely unreadable `runs-on` look resolved.
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

MATRIX_REFERENCE = re.compile(r"^\$\{\{\s*(?:fromJSON\()?\s*matrix\.([A-Za-z0-9_-]+)\s*\)?\s*\}\}$")

EXPRESSION = re.compile(r"\$\{\{")

WORKFLOW_EVENTS = ("push", "pull_request", "pull_request_target", "workflow_dispatch",
                   "workflow_call", "schedule", "release", "merge_group", "issue_comment",
                   "repository_dispatch")

JOB_ID = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)

CI_JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")

# What the socle's own sanitizers job builds by default: AddressSanitizer and
# UndefinedBehaviorSanitizer, on Linux. A product job building those on Linux
# builds the same instrumented tree a second time; TSan, MSan or a macOS job do
# not. Word boundaries, because a substring finds "asan" inside "pleasant".
SANITIZING = re.compile(r"\b(?:asan|ubsan)\b|fsanitize=(?:address|undefined)|\baddress,undefined\b", re.IGNORECASE)
