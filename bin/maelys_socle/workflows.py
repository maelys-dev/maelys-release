# SPDX-License-Identifier: MPL-2.0
"""Workflow parsing and rendering from text and declarations alone."""
from __future__ import annotations

import json
import re

from .constants import (
    BOTTLES, CI_JOB, CI_USES, DECLARATION_FILE, LABEL, LEGS_JOB, LEGS_USES, MATRIX_REFERENCE,
    PROGRAM, RUNNER_INPUTS, RUNS_ON, SANITIZING, SOCLE_LINUX_PACKAGES, SOCLE_MANIFEST_PATTERNS,
    SOCLE_REPOSITORY, SOCLE_RUNNER_KEYS, SOCLE_TARGETS, WORKFLOW_EVENTS,
)
from .declarations import Declarations


def runner_json(labels: list[str]) -> str:
    """A runs-on value: one label is a string, several are an array."""
    return json.dumps(labels[0] if len(labels) == 1 else labels)


def legs_job(decl: "Declarations", sha: str, tag: str) -> list[str]:
    """The job of ci.yml that calls check-legs.yml, or nothing when [check] is empty.

    The legs are single-quoted YAML, where a quote is written twice: a
    command is the product's shell and may well contain one. Runners follow
    [runners] as the check call's do, and check-legs.yml reads them on a
    private repository only.
    """
    if not decl.legs:
        return []
    value = json.dumps([{"name": name, "platform": platform, "command": command}
                        for name, platform, command in decl.legs])
    return [
        f"  {LEGS_JOB}:",
        f"    # Written by maelys-release adopt from [check] of {DECLARATION_FILE}; each leg",
        f"    # reports as '{LEGS_JOB} / NAME'. Declare there, never here.",
        f"    uses: {SOCLE_REPOSITORY}/.github/workflows/check-legs.yml@{sha} # {tag}",
        "    with:",
        f"      product: {decl.product}",
        f"      legs: '{value.replace(chr(39), chr(39) * 2)}'",
        *runner_inputs(decl),
    ]


def ci_legs(text: str, decl: "Declarations", sha: str, tag: str) -> str:
    """Set, replace or remove the legs job of ci.yml.

    The job is the socle's from its name to the next job: replaced where it
    stands, appended after the last job when it is new, and taken away with
    the blank line before it when [check] is emptied. Everything else of the
    file is left byte for byte. A job of the product's own that happens to be
    named `legs` is never touched: the declarations refuse that pairing.
    """
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.rstrip() == f"  {LEGS_JOB}:"), None)
    end = None
    if start is not None:
        end = start + 1
        while end < len(lines) and not CI_JOB.match(lines[end]) \
                and not (lines[end].strip() and not lines[end].startswith(" ")):
            end += 1
        if not any(LEGS_USES.search(line) for line in lines[start:end]):
            return text
        # Trailing blank lines and comments belong to what follows.
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")) \
                and not LEGS_USES.search(lines[end - 1]):
            end -= 1
    block = [line + "\n" for line in legs_job(decl, sha, tag)]
    if start is not None:
        if not block:
            if start > 0 and not lines[start - 1].strip():
                start -= 1
            del lines[start:end]
            return "".join(lines)
        lines[start:end] = block
        return "".join(lines)
    if not block:
        return text
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines) + "\n" + "".join(block)


def macos_runner_input(decl: Declarations) -> list[str]:
    """The macos_runner line of the tap's call, or nothing.

    tap.yml renders and lints a formula on macOS and knows no other leg, so
    it takes this one alone: passing it a Linux input would be an input the
    workflow does not declare, and the run would fail at startup.
    """
    if not decl.runners.get("macos"):
        return []
    return [f"      macos_runner: '{runner_json(decl.runners['macos'])}'"]


def runner_inputs(decl: Declarations) -> list[str]:
    """The runner lines of the check call, one per declared leg."""
    return [f"      {RUNNER_INPUTS[key]}: '{runner_json(decl.runners[key])}'"
            for key in SOCLE_RUNNER_KEYS if decl.runners.get(key)]


def ci_macos_runner(text: str, labels) -> str:
    """Set, replace or remove the runner lines of ci.yml's socle call.

    `labels` is the declaration's `[runners]` mapping; a bare list is read
    as the macos one, which is what every caller passed before three legs
    were declarable.

    ci.yml belongs to the product everywhere else, and the socle has always
    kept only the line naming itself current there. This adds one more line
    under the same job's with:, because a runner is declared once in
    maelys-release.conf and applied, never typed into a workflow by hand --
    and an empty declaration takes the line away again, so the file follows
    the declaration in both directions. Everything else is left byte for
    byte, blank lines and comments included.
    """
    lines = text.splitlines(keepends=True)
    at = next((index for index, line in enumerate(lines) if CI_USES.search(line)), None)
    if at is None:
        return text
    indent = len(lines[at]) - len(lines[at].lstrip())
    with_at = None
    for index in range(at + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        here = len(lines[index]) - len(lines[index].lstrip())
        if here < indent:
            break
        if here == indent and stripped == "with:":
            with_at = index
            break
    if with_at is None:
        return text
    body = indent + 2
    end = with_at + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped and (len(lines[end]) - len(lines[end].lstrip())) < body:
            break
        end += 1
    names = tuple(f"{name}:" for name in RUNNER_INPUTS.values())
    for index in reversed([i for i in range(with_at + 1, end)
                           if lines[i].strip().startswith(names)]):
        del lines[index]
    for key in reversed(SOCLE_RUNNER_KEYS):
        declared = labels.get(key) if isinstance(labels, dict) else (labels if key == "macos" else None)
        if declared:
            lines.insert(with_at + 1, f"{' ' * body}{RUNNER_INPUTS[key]}: '{runner_json(declared)}'\n")
    return "".join(lines)


def release_workflow(decl: Declarations, sha: str, tag: str, version: str) -> str:
    lines = [
        "name: release",
        "",
        f"# Managed by maelys-release {tag} ({version}). Regenerate with",
        f"# '{PROGRAM} adopt' of maelys-release; do not edit by hand.",
        # A job that calls a reusable workflow cannot be granted more than the
        # caller workflow declares at the top level, so the ceiling is set
        # here and each job narrows it.
        "on:",
        "  push:",
        '    tags: ["v*"]',
        "  workflow_dispatch:",
        "    inputs:",
        "      tag:",
        "        description: Existing signed tag whose release and Homebrew publication are replayed",
        "        required: true",
        "        type: string",
        "",
        "permissions:",
        "  contents: write",
        "  id-token: write",
        "  attestations: write",
        "",
        "jobs:",
        "  release:",
        f"    uses: {SOCLE_REPOSITORY}/.github/workflows/release.yml@{sha} # {tag}",
        "    permissions:",
        "      contents: write",
        "      id-token: write",
        "      attestations: write",
        "    with:",
        f"      product: {decl.product}",
        "      tag: ${{ inputs.tag || github.ref_name }}",
        *runner_inputs(decl),
    ]
    if decl.dependencies:
        lines.append("      dependency_checkout: |")
        if decl.dependencies_apart:
            # release.yml never fetches the socle, so this cannot call the
            # command; it uses the managed script's own DESTINATION, which
            # is the unit maelys-egress said it had used by hand five times.
            # The root is exported for the packaging command that follows,
            # so a release and a `make check` read their dependencies from
            # the same place -- and ../NAME names nothing here either.
            # One line, and no list of pins: the script reads them when it
            # runs, so a new pin is cloned without this file being
            # regenerated. That is the lesson of 0.6.0, where the job
            # started reading the product's declarations itself.
            lines.append('        sh scripts/checkout-dependencies.sh'
                         ' "$RUNNER_TEMP/dependencies" >>"$GITHUB_ENV"')
        else:
            lines.extend(f"        sh scripts/checkout-dependency.sh {name}" for name in decl.dependencies)
    if decl.verify_command:
        lines.append(f"      verify_command: {decl.verify_command}")
    if decl.linux_packages:
        lines.append(f"      linux_packages: {SOCLE_LINUX_PACKAGES} {decl.linux_packages}")
    if decl.macos_packages:
        lines.append(f"      macos_packages: {decl.macos_packages}")
    if decl.targets or decl.package:
        declared = decl.targets or [(name, "") for name in SOCLE_TARGETS]
        entries = []
        for name, runner in declared:
            entry: dict = {"target": name} if runner == "" else {"target": name, "runner": runner}
            if decl.package and name not in decl.package:
                entry["package"] = False
            entries.append(entry)
        lines.append(f"      targets: '{json.dumps(entries)}'")
    if decl.manifest_patterns:
        lines.append(f"      manifest_patterns: '{SOCLE_MANIFEST_PATTERNS} "
                     + " ".join(decl.manifest_patterns) + "'")
    if decl.sbom_pattern:
        lines.append(f"      sbom_pattern: '{decl.sbom_pattern}'")
    if decl.commit_verification:
        lines.append(f"      commit_verification: {decl.commit_verification}")
    for name, _kind in decl.channels:
        # An empty environment runs the job with no environment and no
        # approval: measured on run 34817132441 of this repository, a job
        # whose `environment:` expression was empty started at once, left no
        # pending deployment and created no environment. channel.yml runs
        # for one product in the fleet, so this was worth a probe rather
        # than a belief.
        gated = decl.channel_gates.get(name, "reviewer") != "none"
        lines.extend([
            "",
            f"  channel-{name}:",
            "    needs: release",
            "    if: needs.release.result == 'success'",
            f"    uses: {SOCLE_REPOSITORY}/.github/workflows/channel.yml@{sha} # {tag}",
            "    permissions:",
            # The ceiling, which each job of channel.yml narrows: the job that
            # runs this product's publish script keeps contents: read, and
            # only the one that attaches the marker writes.
            "      contents: write",
            "      packages: write",
            "    with:",
            f"      product: {decl.product}",
            "      tag: ${{ inputs.tag || github.ref_name }}",
            f"      channel: {name}",
            # bash, like verify_command and package_command: `sh` is dash on
            # the Ubuntu runner and bash in POSIX mode on the operator's
            # macOS, and 0.36.0 fixed only two of the three.
            "      publish_command: bash scripts/publish-channel.sh TAG CHANNEL",
            *[line for line in runner_inputs(decl) if "linux_x86_64_runner" in line],
        ])
        if not gated:
            lines.append("      release_environment: ''")
    for formula in decl.formulas:
        lines.extend([
            "",
            f"  tap-{formula}:",
            "    needs: release",
            "    if: needs.release.result == 'success'",
            f"    uses: {SOCLE_REPOSITORY}/.github/workflows/tap.yml@{sha} # {tag}",
            "    permissions:",
            "      contents: write",
            "      id-token: write",
            "      attestations: write",
            "    with:",
            f"      product: {formula}",
            "      tag: ${{ inputs.tag || github.ref_name }}",
        ])
        if decl.render_command:
            lines.append(f"      render_command: {decl.render_command} {formula}")
        lines.extend([
            f"      bottles: {BOTTLES}",
            *macos_runner_input(decl),
            "    secrets:",
            "      tap_token: ${{ secrets.HOMEBREW_TAP_TOKEN }}",
            "      tap_signing_key: ${{ secrets.HOMEBREW_TAP_SIGNING_KEY }}",
        ])
    return "\n".join(lines) + "\n"


def ci_workflow(decl: Declarations, sha: str, tag: str, version: str) -> str:
    """Created once from the same declarations, then owned by the product."""
    lines = [
        "name: ci",
        "",
        f"# Created by maelys-release {tag} ({version}) and owned by this repository: add",
        "# jobs next to check; adopt keeps the socle line of check current.",
        "on:",
        "  push:",
        "    branches: [main]",
        "  pull_request:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "jobs:",
        "  check:",
        f"    uses: {SOCLE_REPOSITORY}/.github/workflows/check-product.yml@{sha} # {tag}",
        "    with:",
        f"      product: {decl.product}",
    ]
    # Through the same writer adopt applies to an existing file, so a file
    # created here and the same file regenerated later are byte for byte one
    # text. They were not: this rendered the runner lines after `product:`
    # and the writer inserts them first, so a product that declared a runner
    # would have drifted against its own freshly created ci.yml.
    return ci_legs(ci_macos_runner("\n".join(lines) + "\n", decl.runners), decl, sha, tag)


def matrix_values(text: str, key: str) -> list[str]:
    """The literal values a matrix key lists, anywhere in one workflow.

    Line-based on purpose: the socle carries no YAML parser and vendors none.
    A value it cannot read as a literal is not guessed, it is reported
    unresolved, because a runner nobody could name must not read as absent.
    """
    values: list[str] = []
    for number, line in enumerate(text.splitlines()):
        if not re.fullmatch(rf"\s*{re.escape(key)}:\s*(\[.*\])?\s*", line):
            continue
        inline = re.search(r"\[(.*)\]", line)
        if inline:
            values.extend(item.strip().strip("\"'") for item in inline.group(1).split(",") if item.strip())
            continue
        indent = len(line) - len(line.lstrip())
        for following in text.splitlines()[number + 1:]:
            if not following.strip():
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break
            item = re.fullmatch(r"\s*-\s*(.+?)\s*", following)
            if not item:
                break
            values.append(item.group(1).strip("\"'"))
    # A matrix written as `include:` names the key on each entry rather than
    # once above a list, and that is the shape maelys-datalog and the socle's
    # own release.yml used. Collecting the scalar form too may pick up a key
    # of the same name elsewhere in the file, which adds a label that is not
    # a runner; for a control that refuses a self-hosted runner, naming one
    # too many is a nuisance and naming one too few is a hole.
    for line in text.splitlines():
        scalar = re.fullmatch(rf"\s*-?\s*{re.escape(key)}:\s*(?!\s*[\[|>])(\S.*?)\s*", line)
        if scalar:
            values.append(scalar.group(1).strip("\"'"))
    return sorted({value for value in values if LABEL.fullmatch(value)})


def file_runners(name: str, text: str) -> tuple[list, list]:
    """(labels, unresolved) of one workflow file, matrix references resolved."""
    labels: set = set()
    unresolved: list = []
    for match in RUNS_ON.finditer(text):
        value = match.group(1).strip().strip("\"'")
        # The line, so an operator finds what the socle could not read.
        where = f"{name}:{text.count(chr(10), 0, match.start()) + 1}"
        reference = MATRIX_REFERENCE.fullmatch(value)
        if reference:
            found = matrix_values(text, reference.group(1))
            if found:
                labels.update(found)
            else:
                unresolved.append(f"{where}: {value}")
            continue
        # `runs-on:` alone introduces a block list or a `group:` mapping on
        # the following lines; the socle does not read either, and says so.
        named = [item.strip().strip("\"'") for item in value.strip("[]").split(",") if item.strip()]
        if not named or not all(LABEL.fullmatch(item) for item in named):
            unresolved.append(f"{where}: {value or 'a block or a group below this line'}")
            continue
        labels.update(named)
    return sorted(labels), unresolved


def top_block(text: str, key: str) -> str:
    """The body of a top-level `key:`, or its inline value when it has one."""
    match = re.search(rf"^{re.escape(key)}:(.*)$", text, re.M)
    if not match:
        return ""
    if match.group(1).strip():
        return match.group(1).strip()
    rest = text[match.end():]
    end = re.search(r"^[A-Za-z]", rest, re.M)
    return rest[:end.start()] if end else rest


def sub_block(block: str, key: str) -> str:
    """The body of `key:` inside a block, by indentation."""
    match = re.search(rf"^(\s*){re.escape(key)}:\s*$", block, re.M)
    if not match:
        return ""
    indent = len(match.group(1))
    kept = []
    for line in block[match.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        kept.append(line)
    return "\n".join(kept)


def block_list(block: str, key: str) -> list:
    """The values `key:` lists: inline `[a, b]`, or a `- a` list below it.

    Unlike the runner reader this keeps every value as written: a branch
    filter is `main` but a tag filter is `v*`, and a reader that only
    accepted label-shaped words would drop the tag rule silently.
    """
    match = re.search(rf"^(\s*){re.escape(key)}:(.*)$", block, re.M)
    if not match:
        return []
    inline = match.group(2).strip()
    if inline:
        return [item.strip().strip("\"'") for item in inline.strip("[]").split(",") if item.strip()]
    indent = len(match.group(1))
    values = []
    for line in block[match.end():].splitlines():
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        item = re.fullmatch(r"\s*-\s*(.+?)\s*", line)
        if not item:
            break
        values.append(item.group(1).strip("\"'"))
    return values


def workflow_events(text: str) -> list:
    """The events of one workflow, in the three shapes GitHub accepts."""
    block = top_block(text, "on")
    if not block:
        return []
    found = []
    for event in WORKFLOW_EVENTS:
        if re.search(rf"^\s*-?\s*{re.escape(event)}\s*:", block, re.M) \
                or re.search(rf"(^|[\[,\s]){re.escape(event)}($|[\],\s])", block.split("\n")[0]):
            found.append(event)
    return found


def yaml_scalar(raw: str) -> str:
    """One line's YAML value, its trailing comment set apart.

    agent-cli-spec writes `sanitizer_command: ''   # no compiled code here`.
    Read whole, that was not empty, so the socle counted a sanitizers job that
    never runs among what it produces -- and `protect --apply` refused a
    rename, for want of a merged pull request that had run it. A `#` starts a
    comment only after whitespace and outside quotes.
    """
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        closing = value.find(quote, 1)
        while quote == "'" and closing != -1 and value[closing + 1:closing + 2] == "'":
            closing = value.find(quote, closing + 2)
        return value[:closing + 1] if closing != -1 else value
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def uses_commit(pattern: "re.Pattern[str]", text: str) -> str:
    """The commit a `uses:` line names, for a pattern whose group 1 ends at the @.

    The socle writes those lines and, since this, reads them back: a pin kept
    at one end only is a pin nothing holds between two adoptions.
    """
    match = pattern.search(text)
    return "" if not match else match.group(0)[len(match.group(1)):][:40]


def sanitizers_twice(text: str) -> list[int]:
    """One line per product job of ci.yml that sanitizes while the socle's job does too.

    `sanitizer_command` is opt-out: a call that does not set it turns the
    socle's job on, so a product with an ASan/UBSan job of its own pays for
    two instrumented builds of the same tree on every pull request. The
    socle cannot choose between them -- the product's job may cover more,
    or less -- so it says the fact and names the line.

    One line per job, the first that builds what the socle's job builds.
    maelys-system read seven, and every one was false: the job's own name
    `sanitizers:`, two TSan steps the socle's job never runs, and the ASan
    steps of a macOS job, where the socle's job never runs either. A job
    whose runs-on names macOS and no Linux image is not a duplicate; one
    whose runner cannot be read is counted, since it may be Linux.
    """
    if re.search(r"^\s+sanitizer_command:", text, re.MULTILINE):
        return []
    found = []
    job: list[tuple[int, str]] = []

    def close() -> None:
        if not job:
            return
        # The block that calls the socle is not a second job: its `with:`
        # names the commands, and a comment beside it says what they run.
        if any(CI_USES.search(line) for _, line in job):
            return
        runner = " ".join(line for _, line in job if re.match(r"^\s+runs-on:", line) or "runner" in line
                          or re.search(r"\b(?:ubuntu|macos)-", line))
        if re.search(r"macos", runner, re.IGNORECASE) and not re.search(r"ubuntu|linux", runner, re.IGNORECASE):
            return
        for number, line in job[1:]:
            # A YAML comment is not a job: the one this dropped names the
            # socle's own sanitizers in a sentence explaining the call.
            if not line.lstrip().startswith("#") and SANITIZING.search(line):
                found.append(number)
                return

    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            close()
            job = [(number, line)]
        elif job:
            job.append((number, line))
    close()
    return found
