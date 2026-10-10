# SPDX-License-Identifier: MPL-2.0
"""maelys_cli: the agent-cli/v2 contract for a Python command-line product.

The Python counterpart of libmaelys_cli: one declarative catalog drives the
parser, `help`, `describe`, the shell completion and `__complete`; success
is a JSON envelope on stdout with --format json, failure an envelope on
stderr; exit 0 completed, 1 failed, 2 a validation that found violations.
Transactions plan by default and write with --apply; --dry-run and --plan
are refused. Standard library only, Python 3.9 or later, one file: a
product copies it next to its program or puts it on its path, pinned by
dependencies/maelys-cli.pin like the C library.

    import maelys_cli as cli

    def greet(invocation):
        return {"greeting": f"Hello, {invocation.operands[0]}"}, cli.EXIT_OK

    PROGRAM = cli.Program("hello", "Hello", "0.1.0", [
        cli.read("greet", "greet", "Greet someone.", greet,
                 operands=[cli.operand("NAME", "Who to greet.")],
                 options=[cli.flag("--shout", "Capitals.")],
                 schema={"type": "object", "required": ["greeting"]}),
    ])
    sys.exit(PROGRAM.main())

A handler receives an Invocation and returns (data, exit_code), or raises
Failure(code, message, hint). The value kinds, the causal order of errors,
the built-in commands and the envelopes are those of the contract; the
conformance kit of maelys-dev/agent-cli-spec checks a program built on this
module from the outside.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import sys
from typing import Any, Callable, Optional, Union

CONTRACT = "agent-cli/v2"
SCHEMA_VERSION = 2
CATALOG_SCHEMA = 1
CLI_API = 1
FRAMEWORK = f"maelys_cli python {sys.version_info.major}.{sys.version_info.minor}"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_VIOLATIONS = 2

EXIT_CODES = {"0": "command completed", "1": "execution failed", "2": "valid report with violations"}
STABLE_CODES = ("INVALID_COMMAND", "VALIDATION_FAILED", "PRECONDITION_FAILED", "POLICY_FAILED", "ACCESS_DENIED",
                "NOT_FOUND", "IO_FAILED", "PROCESS_FAILED", "PROTOCOL_FAILED", "UNSUPPORTED", "UNEXPECTED")
# Segments that are not empty, separated by one dot (spec, section 2; C:
# valid_identifier). `unknown` is what an envelope names when no command was
# resolved, and no command may be called that.
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$")
PREFIX_GRAMMAR = re.compile(r"^[a-z]([a-z0-9.-]*[a-z0-9-])?$")
FORMATS = ("text", "json", "jsonl")
COLORS = ("auto", "always", "never")
SHELLS = ("bash", "zsh", "fish")
# Marks, among the options read from a line, a word that starts with one dash.
_DASH_WORD = object()

# What a stream command refuses, as the C parser does: the options that shape
# stdout. --color shapes the diagnostics on stderr and is accepted.
RENDERING = ("--format", "--json", "--compact", "--pretty", "--pager", "--field")
TRISTATE = ("auto", "always", "never")
SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
DURATION_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
# The values C holds in 64 bits: a number past them is refused there, and here.
UNSIGNED_MAXIMUM = 2 ** 64 - 1
INTEGER_MINIMUM, INTEGER_MAXIMUM = -(2 ** 63), 2 ** 63 - 1
# The hexadecimal digits of a digest, by algorithm (C: maelys_cli_digest_hex_digits).
DIGEST_DIGITS = {"sha1": 40, "sha256": 64, "sha384": 96, "sha512": 128}

Handler = Callable[["Invocation"], "tuple[Any, int]"]


def environment_format() -> str:
    """MAELYS_CLI_FORMAT selects json or text; any other value is ignored, as in C."""
    value = os.environ.get("MAELYS_CLI_FORMAT", "")
    return value if value in ("json", "text") else "text"


def terminal_color(mode: str) -> "tuple[bool, bool]":
    """The per-stream color decision of maelys_cli_terminal_detect(): (color_stdout,
    color_stderr). mode 'never' wins over everything; 'always' or CLICOLOR_FORCE force
    both streams on; otherwise NO_COLOR or TERM=dumb force both off; otherwise each
    stream follows its own isatty()."""
    if mode == "never":
        return False, False
    force = os.environ.get("CLICOLOR_FORCE")
    if mode == "always" or (force and force != "0"):
        return True, True
    no_color = os.environ.get("NO_COLOR")
    term = os.environ.get("TERM")
    if no_color or not term or term == "dumb":
        return False, False
    return sys.stdout.isatty(), sys.stderr.isatty()


def _prescan_color_never(argv: list) -> bool:
    """Whether argv already says --color never, checked before a command resolves
    (maelys_cli_run()'s prescan): the only thing an unresolved command line may do to
    coloring, since an unresolved '--color always' is not trusted."""
    never = False
    for index, word in enumerate(argv):
        if word == "--color=never" or (word == "--color" and index + 1 < len(argv)
                                        and argv[index + 1] == "never"):
            never = True
    return never


def _terminal_safe(text: str) -> str:
    """Render control characters visibly in human diagnostics."""
    escaped = []
    for character in text:
        code = ord(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif code < 0x20 or 0x7f <= code <= 0x9f:
            escaped.append(f"\\x{code:02x}")
        elif code in (0x061c, 0x200e, 0x200f, 0x2028, 0x2029) or 0x202a <= code <= 0x202e or 0x2066 <= code <= 0x2069:
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(character)
    return "".join(escaped)


class Failure(Exception):
    """A failure envelope: stable code, causal message, next safe action."""

    def __init__(self, code: str, message: str, hint: str = "", issues: Optional[list] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.issues = issues


# ---- declarations ------------------------------------------------------------------

def _describe_value(entry: dict, where: str, kind: Optional[str], choices: Optional[list], minimum: Optional[int],
                    maximum: Optional[int], algorithms: Optional[list], pattern: Optional[str],
                    digits: Union[int, list, None]) -> dict:
    """The value members an argument and an operand share (spec 2.6), in the
    shape the C reference emits: a hex value states its width as `digits`, an
    integer or a pair of widths, never a length range."""
    if pattern is not None and kind not in ("string", "path"):
        raise ValueError(f"{where} declares a pattern on a kind that is not string or path")
    if kind == "hex":
        if minimum is not None or maximum is not None:
            raise ValueError(f"{where} bounds a hex value with minimum/maximum; a hex width is digits")
        if digits is None:
            raise ValueError(f"{where} is a hex value without digits")
        widths = [digits] if isinstance(digits, int) else list(digits)
        if not widths or len(widths) > 2 or any(not isinstance(w, int) or w < 1 for w in widths) \
                or len(set(widths)) != len(widths):
            raise ValueError(f"{where} digits must be a positive width or two distinct widths, not {digits!r}")
    elif digits is not None:
        raise ValueError(f"{where} declares digits on a kind that is not hex")
    if choices is not None:
        entry["choices"] = list(choices)
    # An unsigned kind's floor is 0 whether declared or not; describe states
    # a bound only when the declaration does, as the C reference does.
    if minimum is not None and not (minimum == 0 and kind in ("unsigned", "size", "duration")):
        entry["minimum"] = minimum
    if maximum is not None:
        entry["maximum"] = maximum
    if algorithms is not None:
        entry["algorithms"] = list(algorithms)
    if digits is not None:
        entry["digits"] = digits if isinstance(digits, int) else list(digits)
    if pattern is not None:
        entry["pattern"] = pattern
    return entry


def argument(name: str, kind: str = "string", choices: Optional[list] = None, minimum: Optional[int] = None,
             maximum: Optional[int] = None, algorithms: Optional[list] = None, pattern: Optional[str] = None,
             digits: Union[int, list, None] = None) -> dict:
    """One option argument. A `hex` states its width with `digits`, an integer
    or a pair such as `[40, 64]`, as MAELYS_CLI_HEX and MAELYS_CLI_HEX_OR do."""
    return _describe_value({"name": name, "type": kind}, f"argument {name}", kind, choices, minimum, maximum,
                           algorithms, pattern, digits)


def option(long: str, summary: str, argument: Optional[dict] = None, default: Optional[str] = None,
           required: bool = False, repeatable: bool = False, requires: tuple = (), conflicts_with: tuple = (),
           group: Optional[str] = None, hidden: bool = False) -> dict:
    """One option descriptor; `argument` from argument(), None for a flag. `hidden`
    keeps it out of the synopsis, the help and the completion; describe lists it
    with `hidden: true` and the parser accepts it (spec 2.2)."""
    if not long.startswith("--") or len(long) < 3:
        raise ValueError(f"an option is spelled --name, not {long!r}")
    if hidden and required:
        raise ValueError(f"{long} is hidden and required; a required option must be shown")
    entry: dict = {"long": long, "required": required, "repeatable": repeatable, "summary": summary,
                   "requires": list(requires), "conflictsWith": list(conflicts_with)}
    if argument is not None:
        entry["argument"] = argument
    if default is not None:
        entry["default"] = default
    if group is not None:
        entry["group"] = group
    if hidden:
        entry["hidden"] = True
    return entry


def flag(long: str, summary: str, **keywords: Any) -> dict:
    return option(long, summary, None, **keywords)


def operand(name: str, summary: str, required: bool = True, variadic: bool = False, kind: Optional[str] = None,
            choices: Optional[list] = None, minimum: Optional[int] = None, maximum: Optional[int] = None,
            algorithms: Optional[list] = None, pattern: Optional[str] = None,
            digits: Union[int, list, None] = None) -> dict:
    """One operand; `kind` and its limits type it like an option's argument.
    An operand describes its value exactly as an argument does (spec 2.6):
    `digits` for a `hex`, `algorithms` for a `digest`, `pattern` for a
    `string` or `path`, enforced by the parser and exposed by describe."""
    entry: dict = {"name": name, "required": required, "variadic": variadic, "summary": summary}
    if kind is not None or choices is not None:
        entry["type"] = kind or "choice"
    return _describe_value(entry, f"operand {name}", kind, choices, minimum, maximum, algorithms, pattern, digits)


CONSTRAINT_KINDS = ("requires", "at-most-one", "exactly-one")


def constraint(kind: str, *options: str) -> dict:
    """One `input.constraints` entry the command states itself (spec 2.5):
    `kind` among `requires` (the first option requires every other),
    `at-most-one`, `exactly-one`, over at least two options of the command.
    `exactly-one` has no option-level form, so this is its only site.
    `all-or-none` is declared with `group=` on the options, its option-level
    form, and is refused here so that a rule has one declaration."""
    if kind == "all-or-none":
        raise ValueError("an all-or-none rule is declared with group= on its options, not as a constraint")
    if kind not in CONSTRAINT_KINDS:
        raise ValueError(f"a constraint kind is one of {', '.join(CONSTRAINT_KINDS)}, not {kind!r}")
    if len(options) < 2:
        raise ValueError(f"a {kind} constraint names at least two options")
    if len(set(options)) != len(options):
        raise ValueError(f"a {kind} constraint names each option once")
    for long in options:
        if not long.startswith("--") or len(long) < 3:
            raise ValueError(f"a constraint names options as --name, not {long!r}")
    return {"kind": kind, "options": list(options)}


def example(words: str, summary: str) -> dict:
    """One example of a command, for `examples=` of a declaration (spec 2.12,
    section 2; C: MAELYS_CLI_EXAMPLE). `words` is the command line without
    the program's name, starting with the command's pattern, its words
    separated by single spaces: a word cannot hold a space, so an example
    carries values that have none. `summary` says in one sentence what that
    line does. An example is an invocation the command accepts, with real
    values; the program parses it when it is built and refuses it otherwise,
    and nothing runs it."""
    if not words or not summary or _terminal_safe(words) != words or _terminal_safe(summary) != summary:
        raise ValueError("an example has words and a summary, without a control character")
    if words != words.strip(" ") or "  " in words:
        raise ValueError(f"the words of an example are separated by single spaces: {words!r}")
    return {"words": words.split(" "), "summary": summary}


def _command(identifier: str, pattern: str, purpose: str, handler: Optional[Handler], effect: Any,
             operands: tuple = (), options: tuple = (), schema: Optional[dict] = None, mode: str = "json-envelope",
             protocol: Optional[str] = None, external: bool = False, hidden: bool = False,
             unavailable: Optional[str] = None, unavailable_code: Optional[str] = None,
             passthrough: bool = False,
             synopsis: Optional[str] = None, constraints: tuple = (), examples: tuple = ()) -> dict:
    if not IDENTIFIER.match(identifier) or identifier == "unknown":
        raise ValueError("a command identifier is letters, digits and dashes in segments separated by one dot, "
                         f"starts with a letter and is not 'unknown': not {identifier!r}")
    # The code an unavailable command answers: UNSUPPORTED says "absent from
    # this build or version" and is wrong for a cause that is not absence,
    # so the declaration names the one that fits (C: .unavailable_code).
    if unavailable_code is not None:
        if unavailable is None:
            raise ValueError(f"{identifier}: an unavailable code needs an unavailable reason")
        if unavailable_code not in STABLE_CODES:
            raise ValueError(f"{identifier}: {unavailable_code!r} is not one of the stable codes")
    words = pattern.split()
    operands = list(operands)
    options = list(options)
    # On a transaction --expect has one meaning and one shape (spec 2.9,
    # section 4): what transaction(expect=True) declares, and a fingerprint
    # the schema requires. Refused here as C refuses it at startup.
    for item in options if isinstance(effect, dict) else ():
        if item["long"] != "--expect":
            continue
        value = item.get("argument", {})
        if value.get("type") != "digest" or value.get("algorithms") != ["sha256"] \
                or "--apply" not in item["requires"] or item["repeatable"] or item["required"] or item.get("hidden"):
            raise ValueError(f"{identifier}: --expect is reserved on a transaction for binding a plan; "
                             "declare it with transaction(expect=True), or name the option otherwise")
        if "fingerprint" not in ((schema or {}).get("required") or []):
            raise ValueError(f"{identifier}: a transaction that declares --expect lists \"fingerprint\" "
                             "in the required of its schema")
    variadic = [item for item in operands if item["variadic"]]
    if len(variadic) > 1 or (variadic and operands[-1] is not variadic[0]):
        raise ValueError(f"{identifier}: at most one operand is variadic, and it is the last one")
    # `synopsis` overrides the derived usage, as `.synopsis` does in C: the
    # catalog still declares every option and describe still lists it.
    if synopsis is None:
        synopsis = pattern
        for item in operands:
            piece = item["name"] + ("..." if item["variadic"] else "")
            synopsis += " " + (piece if item["required"] else f"[{piece}]")
        for item in options:
            if item.get("hidden"):
                continue
            piece = item["long"] + (f" {item['argument']['name']}" if "argument" in item else "")
            synopsis += " " + (piece if item["required"] else f"[{piece}]")
        if passthrough:
            synopsis += " [ARGUMENTS...]"
    elif not synopsis.startswith(pattern):
        raise ValueError(f"{identifier}: a synopsis starts with the pattern {pattern!r}, not {synopsis!r}")
    return {"id": identifier, "pattern": words, "usage": synopsis, "purpose": purpose, "effect": effect,
            "outputMode": mode, "protocol": protocol, "external": external, "hidden": hidden,
            "unavailable": unavailable, "unavailableCode": unavailable_code,
            "operands": operands, "options": options, "passthrough": passthrough,
            "constraints": list(constraints), "examples": list(examples),
            "outputSchema": schema or {"type": "object"}, "handler": handler}


def read(identifier: str, pattern: str, purpose: str, handler: Handler, **keywords: Any) -> dict:
    """Inspects or validates state; exit 2 reports violations in data."""
    return _command(identifier, pattern, purpose, handler, "read", **keywords)


def records(identifier: str, pattern: str, purpose: str, handler: Handler, **keywords: Any) -> dict:
    """A read whose data is {"count": N, "records": [...]}; accepts --format jsonl."""
    return _command(identifier, pattern, purpose, handler, "read", mode="json-records", **keywords)


def transaction(identifier: str, pattern: str, purpose: str, handler: Handler, commit: bool = False,
                options: tuple = (), expect: bool = False, **keywords: Any) -> dict:
    """Plans by default, writes with --apply; the handler reads invocation.apply.
    `expect=True` binds the application to the reviewed plan (spec 2.9, section
    4; C: MAELYS_CLI_EXPECT_OPTION): it declares `--expect FINGERPRINT`, and
    the schema must then list `fingerprint` in its `required`. The handler
    computes the fingerprint with Fingerprint and calls
    invocation.expect(fingerprint) before it writes anything."""
    effect = {"plan": "preview", "apply": "commit" if commit else "apply"}
    options = list(options) + [flag("--apply", "Apply the reviewed plan instead of only planning it.")]
    if expect:
        options.append(option("--expect", "Apply only the plan this fingerprint names; a plan that has changed "
                              "since is refused before anything is written.",
                              argument("FINGERPRINT", "digest", algorithms=["sha256"]), requires=("--apply",)))
    return _command(identifier, pattern, purpose, handler, effect, options=options, **keywords)


class Fingerprint:
    """The fingerprint of a plan (spec 2.9, section 4): a `sha256:HEX` string
    over the action the plan describes and over the state of the resources
    that action would touch. The same writes on the same state give the same
    fingerprint; another write, or the same write on another state, gives
    another. What goes in is the product's business; this is how it goes in,
    and it is the framing of maelys_cli_fingerprint_*() in C, byte for byte:
    each entry is its label, a presence byte and its value, the two strings
    preceded by their length on eight bytes. Add the entries in a fixed order."""

    def __init__(self) -> None:
        import hashlib  # here, not at the top: only a transaction that binds its plan pays for it
        self._hash = hashlib.sha256()

    def add(self, label: str, value: Union[bytes, str, None]) -> "Fingerprint":
        """One entry; `None` is an absent value, which differs from an empty one."""
        name = label.encode("utf-8")
        data = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else value
        self._hash.update(len(name).to_bytes(8, "big") + name)
        self._hash.update(b"\x00" if data is None else b"\x01")
        self._hash.update(len(data or b"").to_bytes(8, "big") + bytes(data or b""))
        return self

    def add_file(self, label: str, path: str, maximum_size: int) -> "Fingerprint":
        """The state of a file the action would touch: absent when nothing is
        at `path`, the sha256 of its content when it is a regular file read
        within `maximum_size`. Anything else raises the OSError: a state that
        cannot be read is not a state to bind a plan to."""
        import hashlib
        try:
            content = read_regular_file(path, 0, maximum_size)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            return self.add(label, None)
        return self.add(label, hashlib.sha256(content).hexdigest())

    def finish(self) -> str:
        return "sha256:" + self._hash.hexdigest()


def execute(identifier: str, pattern: str, purpose: str, handler: Handler, **keywords: Any) -> dict:
    """Deliberately runs a non-transactional action."""
    return _command(identifier, pattern, purpose, handler, "execute", **keywords)


def stream(identifier: str, pattern: str, purpose: str, handler: Handler, protocol: Optional[str] = None,
           **keywords: Any) -> dict:
    """Owns stdio for a protocol; refuses rendering options; the handler returns the exit status."""
    return _command(identifier, pattern, purpose, handler, "stream", mode="protocol-stream", protocol=protocol,
                    **keywords)


def external(identifier: str, pattern: str, purpose: str, handler: Handler, **keywords: Any) -> dict:
    """Hands over to another executable; every word after the pattern reaches it verbatim."""
    return _command(identifier, pattern, purpose, handler, "execute", mode="protocol-stream", external=True,
                    passthrough=True, **keywords)


GLOBAL_OPTIONS = [
    option("--format", "Select text for humans, json for one envelope or jsonl for records.",
           argument("VALUE", "choice", FORMATS), default="text"),
    flag("--json", "Exact alias of --format json."),
    flag("--compact", "Render JSON on a single line."),
    flag("--pretty", "--pretty=false selects compact JSON."),
    flag("--non-interactive", "Never prompt; fail instead of asking a question."),
    option("--color", "Control ANSI colors on terminals.", argument("VALUE", "choice", COLORS), default="auto"),
    option("--progress", "Show the progress of a long run on stderr in text mode: auto only when stderr is a terminal.",
           argument("VALUE", "choice", TRISTATE), default="auto"),
    flag("--verbose", "Add the details of the run on stderr in text mode; silent in JSON."),
    option("--pager", "Page the text rendering when stdout is a terminal; never in a pipe, in JSON or under --non-interactive.",
           argument("VALUE", "choice", TRISTATE), default="auto"),
    option("--field", "Render one top-level member of data instead of the whole result, by "
           "the text or jsonl rendering rules.", argument("NAME", "string")),
    flag("--help", "Show the help of the selected command."),
]
INVARIANTS = [
    "usage and agent discovery share one catalog",
    "transactional commands plan by default and require --apply",
    "stdout carries success data; stderr carries failures",
    "--json and --format json are identical",
    "unknown or duplicated options are refused",
]




# ---- help layout ----------------------------------------------------------------------
# The help is read by a person, in a terminal: it fits the width, puts a description beside a short
# label and below a long one, and breaks a line between words. This is the layout of src/app.c
# (help_wrap, help_entry), with the same widths and the same table of character widths.
HELP_WIDTH = 80
HELP_WIDTH_MINIMUM = 60
HELP_WIDTH_MAXIMUM = 100
HELP_LABEL_MAXIMUM = 28
_ZERO_WIDTH = ((0x0300, 0x036F), (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF), (0x20D0, 0x20FF), (0xFE00, 0xFE0F),
               (0xFE20, 0xFE2F), (0x200B, 0x200F), (0x2060, 0x2060), (0xFEFF, 0xFEFF))
_DOUBLE_WIDTH = ((0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
                 (0xFF00, 0xFF60), (0xFFE0, 0xFFE6), (0x1F300, 0x1F64F), (0x1F900, 0x1F9FF), (0x20000, 0x3FFFD))


def display_width(text: str) -> int:
    """Columns `text` takes in a terminal: none for a combining mark or a
    zero-width character, two for the East Asian wide and fullwidth ranges
    and for emoji, one otherwise."""
    width = 0
    for character in text:
        code = ord(character)
        if any(low <= code <= high for low, high in _ZERO_WIDTH):
            continue
        width += 2 if any(low <= code <= high for low, high in _DOUBLE_WIDTH) else 1
    return width


def _help_words(text: str, groups: bool) -> list:
    """The words of `text`; with `groups`, a space inside [...] does not separate two."""
    words, word, depth = [], "", 0
    for character in text:
        if character == " " and not (groups and depth > 0):
            if word:
                words.append(word)
            word = ""
            continue
        depth += 1 if character == "[" else -1 if character == "]" and depth > 0 else 0
        word += character
    return words + ([word] if word else [])


def _help_wrap(text: str, column: int, indent: int, width: int, groups: bool = False) -> str:
    """`text` wrapped between words so that no line passes `width`: the first
    line goes on at `column`, the following ones start after `indent` spaces."""
    out, at, first = "", column, True
    for word in _help_words(text, groups):
        wide = display_width(word)
        if not first and at + 1 + wide > width and at > indent:
            out += "\n" + " " * indent
            at = indent
        elif not first:
            out += " "
            at += 1
        out += word
        at += wide
        first = False
    return out


def _help_entry(label: str, text: str, label_width: int, width: int, groups: bool = False) -> str:
    """One entry of a list: the text beside the label when the label fits its column, below it otherwise."""
    wide = display_width(label)
    if wide <= label_width:
        column = label_width + 4
        return "  " + label + " " * (label_width - wide + 2) + _help_wrap(text, column, column, width) + "\n"
    return "  " + _help_wrap(label, 2, 8, width, groups) + "\n" + " " * 6 + _help_wrap(text, 6, 6, width) + "\n"


def _help_paragraph(text: str, width: int, groups: bool = False) -> str:
    return "  " + _help_wrap(text, 2, 6 if groups else 2, width, groups) + "\n"


def help_width(fmt: str = "text") -> int:
    """The width help is rendered at: the terminal's when stdout is one,
    within bounds that keep it readable; 80 anywhere else, so that what goes
    into a pipe, a file or data.text does not depend on a window."""
    if fmt != "text" or not sys.stdout.isatty():
        return HELP_WIDTH
    try:
        columns = int(os.environ.get("COLUMNS") or os.get_terminal_size(sys.stdout.fileno()).columns)
    except (OSError, ValueError):
        return HELP_WIDTH
    return max(HELP_WIDTH_MINIMUM, min(HELP_WIDTH_MAXIMUM, columns)) if columns > 0 else HELP_WIDTH

# ---- completion scripts ---------------------------------------------------------------
# A static script carries the candidates of the catalog it was generated from and launches no process
# at a Tab (agent-cli/v2 2.7, section 6): a Python program pays its interpreter at every launch, which a
# completion feels. Each of the three is builtin_complete of src/app.c, and _complete below, written in
# its shell over one table of rows, `|`-separated:
#   C|INDEX|PATTERN WORDS|ID|KIND    one shown, available command, in catalog order; KIND is c, s for a
#                                    stream (no shared option) or d for a delegate (__complete is called)
#   O|INDEX|--LONG|FLAGS|VALUES      one option of command INDEX, or of every command when INDEX is g;
#                                    FLAGS among a (takes an argument), e (its argument is a choice),
#                                    h (hidden), r (repeatable), or - for none; VALUES space-separated
#   P|INDEX|FLAG|VALUES              one operand of command INDEX, in order; FLAG is v when variadic
# A word the user typed is compared behind an `x` in fish, whose test would read `!`, `(` or `-n` as an
# operator; bash and zsh compare inside [[ ]], which reads none. And fish 4 reads `?` in a pattern as
# itself, not as one character: its script tests `--*` where the two others test `--?*`, the word `--`
# having been taken just above.
# Every word of a row matches _STATIC_WORD, so a row is inert inside single quotes in the three shells; a
# catalog holding anything else gets the script that calls __complete, which is always exact.
_SHELL_BARE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+\Z")


def _shell_word(word: str) -> str:
    """One word of an example as a shell reads it back (C: help_shell_words).
    A word made of the characters no shell interprets goes as it is; any
    other is single-quoted, a quote and a backslash leaving the quotes to be
    written \\' and \\\\, the one spelling sh, bash, zsh and fish read as the
    same word."""
    if _SHELL_BARE.match(word) and not word.startswith("="):
        return word
    return "'" + "".join("'\\" + character + "'" if character in "'\\" else character for character in word) + "'"


_STATIC_WORD = re.compile(r"^[A-Za-z0-9._:/+@%,=-]+$")

_STATIC_BASH = r"""# bash completion for @PROG@ @VERSION@, generated from its catalog
# Carries the candidates of that catalog: regenerate it when @PROG@ changes.
_@ID@_rows=(
@ROWS@
)
_@ID@_complete() {
    local IFS=$' \t\n' cur="${COMP_WORDS[COMP_CWORD]}" row rest idx pat id kind word name flags
    local best= bid= bkind= last= given=' ' found= len=0 n i k m pos np
    local -a prev pw out after olong oflags ovals pflags pvals
    COMPREPLY=()
    (( COMP_CWORD > 0 )) || return 0
    prev=("${COMP_WORDS[@]:1:COMP_CWORD-1}")
    n=${#prev[@]}
    for row in "${_@ID@_rows[@]}"; do
        case $row in 'C|'*) ;; *) continue ;; esac
        rest=${row#*|}; idx=${rest%%|*}; rest=${rest#*|}; pat=${rest%%|*}; rest=${rest#*|}
        id=${rest%%|*}; kind=${rest#*|}
        pw=($pat); k=${#pw[@]}
        (( k <= n && k > len )) || continue
        for (( i = 0; i < k; i++ )); do [[ ${prev[i]} == "${pw[i]}" ]] || continue 2; done
        best=$idx; len=$k; bid=$id; bkind=$kind
    done
    if [[ -z $best ]]; then
        for row in "${_@ID@_rows[@]}"; do
            case $row in 'C|'*) ;; *) continue ;; esac
            rest=${row#*|}; rest=${rest#*|}; pat=${rest%%|*}
            pw=($pat)
            (( ${#pw[@]} > n )) || continue
            for (( i = 0; i < n; i++ )); do [[ ${prev[i]} == "${pw[i]}" ]] || continue 2; done
            out[${#out[@]}]=${pw[n]}
        done
    elif [[ $bkind == d ]]; then
        IFS=$'\n'
        COMPREPLY=($("@PROG@" __complete -- "${prev[@]}" "$cur" 2>/dev/null))
        if [ ${#COMPREPLY[@]} -eq 0 ]; then
            COMPREPLY=($(compgen -f -- "$cur"))
        fi
        return 0
    else
        after=("${prev[@]:len}"); m=${#after[@]}
        (( m > 0 )) && last=${after[m-1]}
        for row in "${_@ID@_rows[@]}"; do
            case $row in
                "O|$best|"*|'O|g|'*)
                    rest=${row#*|}; idx=${rest%%|*}; rest=${rest#*|}; name=${rest%%|*}; rest=${rest#*|}
                    flags=${rest%%|*}; [[ $idx == g ]] && flags=${flags}g
                    olong[${#olong[@]}]=$name; oflags[${#oflags[@]}]=$flags; ovals[${#ovals[@]}]=${rest#*|} ;;
                "P|$best|"*)
                    rest=${row#*|}; rest=${rest#*|}
                    pflags[${#pflags[@]}]=${rest%%|*}; pvals[${#pvals[@]}]=${rest#*|} ;;
            esac
        done
        if [[ $last == --* && $last != *=* ]]; then
            for (( i = 0; i < ${#olong[@]}; i++ )); do
                if [[ ${olong[i]} == "$last" && ${oflags[i]} == *a* ]]; then
                    found=1; out=(${ovals[i]}); break
                fi
            done
        fi
        if [[ -n $found ]]; then
            :
        elif [[ $cur == --*=* ]]; then
            name=${cur%%=*}
            for (( i = 0; i < ${#olong[@]}; i++ )); do
                [[ ${olong[i]} == "$name" && ${oflags[i]} == *e* && ${oflags[i]} != *[hg]* ]] || continue
                for word in ${ovals[i]}; do out[${#out[@]}]="$name=$word"; done
            done
        elif [[ $cur == --* ]]; then
            for (( i = 0; i < m; i++ )); do
                [[ ${after[i]} == --* ]] && given="$given${after[i]%%=*} "
            done
            for (( i = 0; i < ${#olong[@]}; i++ )); do
                [[ ${oflags[i]} == *h* ]] && continue
                [[ ${oflags[i]} == *g* && $bkind == s ]] && continue
                [[ ${oflags[i]} == *r* || $given != *" ${olong[i]} "* ]] && out[${#out[@]}]=${olong[i]}
            done
        elif [[ ( $bid == help || $bid == describe ) && $m -eq 0 ]]; then
            for row in "${_@ID@_rows[@]}"; do
                case $row in 'C|'*) ;; *) continue ;; esac
                rest=${row#*|}; rest=${rest#*|}; rest=${rest#*|}
                out[${#out[@]}]=${rest%%|*}
            done
        elif (( ${#pflags[@]} > 0 )); then
            pos=0; np=${#pflags[@]}
            for (( i = 0; i < m; i++ )); do
                word=${after[i]}
                if [[ $word == -- ]]; then pos=$(( pos + m - i - 1 )); break; fi
                if [[ $word == --?* ]]; then
                    for (( k = 0; k < ${#olong[@]}; k++ )); do
                        [[ ${olong[k]} == "$word" ]] || continue
                        [[ ${oflags[k]} == *a* ]] && i=$(( i + 1 ))
                        break
                    done
                    continue
                fi
                pos=$(( pos + 1 ))
            done
            if (( pos < np )) || [[ ${pflags[np-1]} == v ]]; then
                (( pos < np )) || pos=$(( np - 1 ))
                out=(${pvals[pos]})
            fi
        fi
    fi
    for (( i = 0; i < ${#out[@]}; i++ )); do
        word=${out[i]}
        [[ $word == "$cur"* ]] || continue
        for (( k = 0; k < ${#COMPREPLY[@]}; k++ )); do [[ ${COMPREPLY[k]} == "$word" ]] && continue 2; done
        COMPREPLY[${#COMPREPLY[@]}]=$word
    done
    if [ ${#COMPREPLY[@]} -eq 0 ]; then
        IFS=$'\n'
        COMPREPLY=($(compgen -f -- "$cur"))
    fi
}
complete -o filenames -F _@ID@_complete @PROG@
"""

_STATIC_ZSH = r"""#compdef @PROG@
# zsh completion for @PROG@ @VERSION@, generated from its catalog
# Carries the candidates of that catalog: regenerate it when @PROG@ changes.
typeset -ga _@ID@_rows
_@ID@_rows=(
@ROWS@
)
_@ID@_complete() {
    local cur=${words[CURRENT]} row word name best= bid= bkind= last= given=' ' found=
    local -i len=0 n=0 i=0 k=0 m=0 pos=0 np=0
    local -a prev f pw out after olong oflags ovals pflags pvals keep
    (( CURRENT > 2 )) && prev=("${(@)words[2,CURRENT-1]}")
    n=${#prev}
    for row in "${_@ID@_rows[@]}"; do
        [[ $row == 'C|'* ]] || continue
        f=("${(@s:|:)row}"); pw=(${=f[3]}); k=${#pw}
        (( k <= n && k > len )) || continue
        for (( i = 1; i <= k; i++ )); do [[ ${prev[i]} == "${pw[i]}" ]] || continue 2; done
        best=${f[2]}; len=$k; bid=${f[4]}; bkind=${f[5]}
    done
    if [[ -z $best ]]; then
        for row in "${_@ID@_rows[@]}"; do
            [[ $row == 'C|'* ]] || continue
            f=("${(@s:|:)row}"); pw=(${=f[3]})
            (( ${#pw} > n )) || continue
            for (( i = 1; i <= n; i++ )); do [[ ${prev[i]} == "${pw[i]}" ]] || continue 2; done
            out+=("${pw[n+1]}")
        done
    elif [[ $bkind == d ]]; then
        out=(${(f)"$("@PROG@" __complete -- "${(@)words[2,CURRENT]}" 2>/dev/null)"})
        if (( ${#out} )); then
            compadd -- "${out[@]}"
        else
            _files
        fi
        return
    else
        (( len < n )) && after=("${(@)prev[len+1,n]}")
        m=${#after}
        (( m > 0 )) && last=${after[m]}
        for row in "${_@ID@_rows[@]}"; do
            case $row in
                ("O|$best|"*|'O|g|'*)
                    f=("${(@s:|:)row}")
                    [[ ${f[2]} == g ]] && f[4]=${f[4]}g
                    olong+=("${f[3]}"); oflags+=("${f[4]}"); ovals+=("${f[5]}") ;;
                ("P|$best|"*)
                    f=("${(@s:|:)row}")
                    pflags+=("${f[3]}"); pvals+=("${f[4]}") ;;
            esac
        done
        if [[ $last == --* && $last != *=* ]]; then
            for (( i = 1; i <= ${#olong}; i++ )); do
                if [[ ${olong[i]} == "$last" && ${oflags[i]} == *a* ]]; then
                    found=1; out=(${=ovals[i]}); break
                fi
            done
        fi
        if [[ -n $found ]]; then
            :
        elif [[ $cur == --*=* ]]; then
            name=${cur%%=*}
            for (( i = 1; i <= ${#olong}; i++ )); do
                [[ ${olong[i]} == "$name" && ${oflags[i]} == *e* && ${oflags[i]} != *[hg]* ]] || continue
                for word in ${=ovals[i]}; do out+=("$name=$word"); done
            done
        elif [[ $cur == --* ]]; then
            for (( i = 1; i <= m; i++ )); do
                [[ ${after[i]} == --* ]] && given+="${after[i]%%=*} "
            done
            for (( i = 1; i <= ${#olong}; i++ )); do
                [[ ${oflags[i]} == *h* ]] && continue
                [[ ${oflags[i]} == *g* && $bkind == s ]] && continue
                [[ ${oflags[i]} == *r* || $given != *" ${olong[i]} "* ]] && out+=("${olong[i]}")
            done
        elif [[ ( $bid == help || $bid == describe ) && $m -eq 0 ]]; then
            for row in "${_@ID@_rows[@]}"; do
                [[ $row == 'C|'* ]] || continue
                f=("${(@s:|:)row}"); out+=("${f[4]}")
            done
        elif (( ${#pflags} > 0 )); then
            np=${#pflags}
            for (( i = 1; i <= m; i++ )); do
                word=${after[i]}
                if [[ $word == '--' ]]; then pos=$(( pos + m - i )); break; fi
                if [[ $word == --?* ]]; then
                    for (( k = 1; k <= ${#olong}; k++ )); do
                        [[ ${olong[k]} == "$word" ]] || continue
                        [[ ${oflags[k]} == *a* ]] && i=$(( i + 1 ))
                        break
                    done
                    continue
                fi
                pos=$(( pos + 1 ))
            done
            if (( pos < np )) || [[ ${pflags[np]} == v ]]; then
                (( pos < np )) || pos=$(( np - 1 ))
                out=(${=pvals[pos+1]})
            fi
        fi
    fi
    for word in "${out[@]}"; do
        [[ -n $word && $word == "$cur"* ]] || continue
        (( ${keep[(Ie)$word]} )) && continue
        keep+=("$word")
    done
    if (( ${#keep} )); then
        compadd -- "${keep[@]}"
    else
        _files
    fi
}
if [[ ${funcstack[1]} == _@PROG@ ]]; then
    _@ID@_complete "$@"
else
    compdef _@ID@_complete @PROG@
fi
"""

_STATIC_FISH = r"""# fish completion for @PROG@ @VERSION@, generated from its catalog
# Carries the candidates of that catalog: regenerate it when @PROG@ changes.
set -g __@ID@_rows \
@ROWS@
function __@ID@_complete
    set -l tokens (commandline -opc)
    set -l current (commandline -ct)
    set -l cur "$current"
    set -l prev $tokens[2..-1]
    set -l n (count $prev)
    set -l best ''
    set -l len 0
    set -l bid ''
    set -l bkind ''
    set -l out
    for row in $__@ID@_rows
        string match -q -- 'C|*' $row; or continue
        set -l f (string split -- '|' $row)
        set -l pw (string split -- ' ' $f[3])
        set -l k (count $pw)
        test $k -le $n -a $k -gt $len; or continue
        set -l same 1
        set -l i 1
        while test $i -le $k
            test "x$prev[$i]" = "x$pw[$i]"; or set same 0
            set i (math $i + 1)
        end
        test $same -eq 1; or continue
        set best $f[2]
        set len $k
        set bid $f[4]
        set bkind $f[5]
    end
    if test -z "$best"
        for row in $__@ID@_rows
            string match -q -- 'C|*' $row; or continue
            set -l f (string split -- '|' $row)
            set -l pw (string split -- ' ' $f[3])
            test (count $pw) -gt $n; or continue
            set -l same 1
            set -l i 1
            while test $i -le $n
                test "x$prev[$i]" = "x$pw[$i]"; or set same 0
                set i (math $i + 1)
            end
            test $same -eq 1; and set -a out $pw[(math $n + 1)]
        end
    else if test "$bkind" = d
        set out ("@PROG@" __complete -- $prev "$cur" 2>/dev/null)
        if test (count $out) -gt 0
            printf '%s\n' $out
        else
            __fish_complete_path "$cur"
        end
        return
    else
        set -l after
        test $len -lt $n; and set after $prev[(math $len + 1)..$n]
        set -l m (count $after)
        set -l last ''
        test $m -gt 0; and set last $after[$m]
        set -l olong
        set -l oflags
        set -l ovals
        set -l pflags
        set -l pvals
        for row in $__@ID@_rows
            set -l f (string split -- '|' $row)
            if test "$f[1]" = O; and test "$f[2]" = "$best" -o "$f[2]" = g
                set -a olong $f[3]
                if test "$f[2]" = g
                    set -a oflags "$f[4]g"
                else
                    set -a oflags "$f[4]"
                end
                set -a ovals "$f[5]"
            else if test "$f[1]" = P; and test "$f[2]" = "$best"
                set -a pflags "$f[3]"
                set -a pvals "$f[4]"
            end
        end
        set -l no (count $olong)
        set -l found 0
        if string match -q -- '--*' "$last"; and not string match -q -- '*=*' "$last"
            set -l i 1
            while test $i -le $no
                if test "x$olong[$i]" = "x$last"; and string match -q -- '*a*' "$oflags[$i]"
                    set found 1
                    set out (string split -n -- ' ' "$ovals[$i]")
                    break
                end
                set i (math $i + 1)
            end
        end
        if test $found -eq 1
            true
        else if string match -q -- '--*=*' "$cur"
            set -l name (string replace -r -- '=.*$' '' "$cur")
            set -l i 1
            while test $i -le $no
                if test "x$olong[$i]" = "x$name"; and string match -q -- '*e*' "$oflags[$i]"; and not string match -qr -- '[hg]' "$oflags[$i]"
                    for word in (string split -n -- ' ' "$ovals[$i]")
                        set -a out "$name=$word"
                    end
                end
                set i (math $i + 1)
            end
        else if string match -q -- '--*' "$cur"
            set -l given
            for word in $after
                string match -q -- '--*' "$word"; and set -a given (string replace -r -- '=.*$' '' "$word")
            end
            set -l i 1
            while test $i -le $no
                if string match -q -- '*h*' "$oflags[$i]"
                    true
                else if string match -q -- '*g*' "$oflags[$i]"; and test "$bkind" = s
                    true
                else if string match -q -- '*r*' "$oflags[$i]"; or not contains -- "$olong[$i]" $given
                    set -a out $olong[$i]
                end
                set i (math $i + 1)
            end
        else if test "$bid" = help -o "$bid" = describe; and test $m -eq 0
            for row in $__@ID@_rows
                set -l f (string split -- '|' $row)
                test "$f[1]" = C; and set -a out $f[4]
            end
        else if test (count $pflags) -gt 0
            set -l pos 0
            set -l np (count $pflags)
            set -l i 1
            while test $i -le $m
                set -l word "$after[$i]"
                if test "x$word" = x--
                    set pos (math $pos + $m - $i)
                    break
                end
                if string match -q -- '--*' "$word"
                    set -l j 1
                    while test $j -le $no
                        if test "x$olong[$j]" = "x$word"
                            string match -q -- '*a*' "$oflags[$j]"; and set i (math $i + 1)
                            break
                        end
                        set j (math $j + 1)
                    end
                else
                    set pos (math $pos + 1)
                end
                set i (math $i + 1)
            end
            if test $pos -lt $np; or test "$pflags[$np]" = v
                set -l slot $np
                test $pos -lt $np; and set slot (math $pos + 1)
                set out (string split -n -- ' ' "$pvals[$slot]")
            end
        end
    end
    set -l keep
    set -l width (string length -- "$cur")
    for word in $out
        if test $width -gt 0
            set -l head (string sub -l $width -- "$word")
            test "x$head" = "x$cur"; or continue
        end
        contains -- "$word" $keep; or set -a keep $word
    end
    if test (count $keep) -gt 0
        printf '%s\n' $keep
    else
        __fish_complete_path "$cur"
    end
end
complete -c @PROG@ -f -a '(__@ID@_complete)'
"""

# ---- values ---------------------------------------------------------------------------

def parse_value(kind: str, text: str, spec: dict, where: str, usage: str) -> Any:
    """The typed value of one argument or operand, or a VALIDATION_FAILED."""
    def refuse(what: str) -> Failure:
        return Failure("VALIDATION_FAILED", f"{where} takes {what}, not '{text}'.", f"Use '{usage}'.")
    if kind == "boolean":
        if text not in ("true", "false"):
            raise refuse("true or false")
        return text == "true"
    if kind == "string":
        # The value MUST match the declared pattern (spec 2.3), written in the
        # common subset of ECMA-262 and POSIX ERE; search semantics, as ERE.
        if "pattern" in spec and not re.search(spec["pattern"], text):
            raise refuse(f"a value matching {spec['pattern']}")
        return text
    # Digits are the ten ASCII ones: `\d` also reads the digits of other
    # scripts, which C refuses. And a number is one C holds in 64 bits.
    if kind in ("integer", "unsigned"):
        if not re.fullmatch(r"-?[0-9]+" if kind == "integer" else r"[0-9]+", text):
            raise refuse("an integer" if kind == "integer" else "an unsigned integer")
        value = int(text)
        if not (INTEGER_MINIMUM <= value <= INTEGER_MAXIMUM if kind == "integer" else value <= UNSIGNED_MAXIMUM):
            raise refuse("an integer" if kind == "integer" else "an unsigned integer")
    elif kind == "size":
        match = re.fullmatch(r"([0-9]+)([KMGT]?)", text)
        if not match or int(match.group(1)) * SIZE_UNITS[match.group(2)] > UNSIGNED_MAXIMUM:
            raise refuse("a size such as 512, 4K, 16M, 2G or 1T")
        value = int(match.group(1)) * SIZE_UNITS[match.group(2)]
    elif kind == "duration":
        match = re.fullmatch(r"([0-9]+)(ms|s|m|h|d)", text)
        if not match or int(match.group(1)) * DURATION_UNITS[match.group(2)] > UNSIGNED_MAXIMUM:
            raise refuse("a duration with its unit: ms, s, m, h or d")
        value = int(match.group(1)) * DURATION_UNITS[match.group(2)]
    elif kind == "path":
        if not text:
            raise refuse("a non-empty path")
        if "pattern" in spec and not re.search(spec["pattern"], text):
            raise refuse(f"a value matching {spec['pattern']}")
        return text
    elif kind == "absolute-path":
        if not text.startswith("/"):
            raise refuse("an absolute path")
        return text
    elif kind == "choice":
        if text not in spec.get("choices", []):
            raise refuse(", ".join(spec.get("choices", [])))
        return text
    elif kind == "hex":
        # The width is `digits`, one or two, as the C parser reads it.
        widths = spec["digits"] if isinstance(spec["digits"], list) else [spec["digits"]]
        if not re.fullmatch(r"[0-9a-f]+", text) or len(text) not in widths:
            raise refuse(" or ".join(str(w) for w in widths) + " lowercase hexadecimal digits")
        return text
    elif kind == "sha256":
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            raise refuse("a SHA-256 digest of 64 lowercase hexadecimal characters")
        return text
    elif kind == "digest":
        match = re.fullmatch(r"([a-z0-9-]+):([0-9a-f]+)", text)
        if not match or (spec.get("algorithms") and match.group(1) not in spec["algorithms"]) \
                or len(match.group(2)) != DIGEST_DIGITS.get(match.group(1), len(match.group(2))):
            # The length is the algorithm's, as in C: `sha256:ab` is no digest.
            raise refuse("ALGORITHM:HEX with " + ", ".join(spec.get("algorithms", ["a declared algorithm"])))
        return text
    else:
        raise Failure("UNEXPECTED", f"Value kind {kind!r} of {where} is not known.", "Fix the catalog.")
    if ("minimum" in spec and value < spec["minimum"]) or ("maximum" in spec and value > spec["maximum"]):
        bounds = f"between {spec.get('minimum', '-inf')} and {spec.get('maximum', '+inf')}"
        raise Failure("VALIDATION_FAILED", f"{where} must be {bounds}, not {text}.", f"Use '{usage}'.")
    return value


def _parse_flag(value: Optional[str], name: str, usage: str) -> bool:
    """Parse the explicit spellings accepted by the C flag parser."""
    if value is None:
        return True
    if value in ("true", "yes", "on", "1"):
        return True
    if value in ("false", "no", "off", "0"):
        return False
    raise Failure("VALIDATION_FAILED",
                  f"Option {name} takes true, false, yes, no, on, off, 1 or 0, not '{value}'.",
                  f"Use '{usage}'.")


# ---- files -----------------------------------------------------------------------
# The counterpart of maelys/cli/files.h: same requirements, same errno, same
# explanations, the file judged is the file read.

FILE_REGULAR = 1 << 0
FILE_NO_SYMLINK = 1 << 1
FILE_OWNER_TRUSTED = 1 << 2
FILE_NOT_WRITABLE_BY_OTHERS = 1 << 3
FILE_PRIVATE = 1 << 4
FILE_EXECUTABLE = 1 << 5
FILE_SINGLE_LINK = 1 << 6
FILE_OWNER_CALLER = 1 << 7
FILE_TRUSTED_DIRECTORY = 1 << 8

WRITE_REPLACE = "replace"
WRITE_NO_REPLACE = "no-replace"

EFTYPE = getattr(errno, "EFTYPE", errno.EINVAL)


class FileError(OSError):
    """An OSError with the short stable explanation of files.h (`out_error`)."""

    def __init__(self, error_number: int, explanation: str, filename: Optional[str] = None) -> None:
        super().__init__(error_number, os.strerror(error_number), filename)
        self.explanation = explanation


def file_error_code(error_number: int) -> str:
    """The stable code for an errno of this section, as maelys_cli_file_error_code()."""
    if error_number in (errno.ENOENT, errno.ENOTDIR):
        return "NOT_FOUND"
    if error_number in (errno.EACCES, errno.EPERM):
        return "ACCESS_DENIED"
    if error_number in (errno.EFBIG, errno.ELOOP, errno.EMLINK, errno.EINVAL, errno.EISDIR, EFTYPE):
        return "VALIDATION_FAILED"
    return "IO_FAILED"


def file_failure(error: OSError, what: Optional[str] = None) -> Failure:
    """The Failure for an OSError, as maelys_cli_fail_file(): code from the errno,
    message `what: strerror`, hint from the explanation when the error carries one."""
    number = error.errno or 0
    subject = what or error.filename or "operation failed"
    explanation = getattr(error, "explanation", None)
    hint = f"{explanation[0].upper()}{explanation[1:]}." if explanation else \
        "Inspect the named path or resource, correct its state and retry."
    return Failure(file_error_code(number), f"{subject}: {os.strerror(number) if number else error}", hint)


def _judge(status: os.stat_result, requirements: int) -> Optional["tuple[int, str]"]:
    if requirements & FILE_REGULAR and not stat.S_ISREG(status.st_mode):
        return EFTYPE, "path is not a regular file"
    if requirements & FILE_OWNER_CALLER and status.st_uid != os.geteuid():
        return errno.EPERM, "file is not owned by the caller"
    if requirements & FILE_OWNER_TRUSTED and status.st_uid not in (0, os.geteuid()):
        return errno.EPERM, "file is owned by an untrusted user"
    if requirements & FILE_NOT_WRITABLE_BY_OTHERS and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return errno.EPERM, "file is writable by group or world"
    if requirements & FILE_PRIVATE and status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return errno.EPERM, "file grants permissions to group or world"
    if requirements & FILE_SINGLE_LINK and status.st_nlink != 1:
        return errno.EMLINK, "file has more than one hard link"
    if requirements & FILE_EXECUTABLE and not status.st_mode & stat.S_IXUSR:
        return errno.EACCES, "file is not executable"
    return None


def _judge_resolved_directory(path: str, status: os.stat_result) -> Optional["tuple[int, str]"]:
    """The directory that holds the file once symbolic links are resolved must be
    owned by root or the caller, closed to group and world, and still hold that
    very object (judge_resolved_directory of src/files.c). It answers who may
    replace a file, which its own modes do not, and is what makes following a
    link worth as much as refusing one."""
    resolved = os.path.realpath(path)
    try:
        os.lstat(resolved)
    except OSError as error:
        return error.errno or errno.ENOENT, "path does not resolve to an existing file"
    parent, name = os.path.split(resolved)
    if not name:
        return errno.EINVAL, "resolved path has no file name"
    try:
        directory = os.open(parent or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        return error.errno, "directory of the file is not accessible"
    try:
        try:
            directory_status = os.fstat(directory)
        except OSError as error:
            return error.errno, "directory status of the file is not accessible"
        if not stat.S_ISDIR(directory_status.st_mode) or \
                directory_status.st_uid not in (0, os.geteuid()) or \
                directory_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            # Which directory, in the words an operator can act on: when the
            # path itself is a link, the directory at fault is the one at the
            # other end. The question is the last component, not whether
            # realpath() moved: an ancestor that is a link moves it too.
            return errno.EPERM, ("file resolves into a directory owned or writable by an untrusted user"
                                 if os.path.islink(path) else
                                 "file is in a directory owned or writable by an untrusted user")
        try:
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError as error:
            return error.errno, "file is not an entry of the directory it resolves to"
        if (entry.st_dev, entry.st_ino) != (status.st_dev, status.st_ino):
            return errno.EPERM, "file changed while its directory was judged"
    finally:
        os.close(directory)
    return None


def check_file(path: str, requirements: int) -> None:
    """Judges the path by lstat/stat without opening it (maelys_cli_check_file);
    for a file that is not read here. Raises FileError."""
    if not path:
        raise FileError(errno.EINVAL, "path is empty", path)
    try:
        status = os.lstat(path)
    except OSError as error:
        raise FileError(error.errno, "path does not exist or is not accessible", path) from None
    if requirements & FILE_NO_SYMLINK and stat.S_ISLNK(status.st_mode):
        raise FileError(errno.ELOOP, "path is a symbolic link", path)
    if stat.S_ISLNK(status.st_mode):
        try:
            status = os.stat(path)
        except OSError as error:
            raise FileError(error.errno, "symbolic link target is not accessible", path) from None
    verdict = _judge(status, requirements)
    if not verdict and requirements & FILE_TRUSTED_DIRECTORY:
        verdict = _judge_resolved_directory(path, status)
    if verdict:
        raise FileError(verdict[0], verdict[1], path)


def _open_trusted(path: str, requirements: int) -> "tuple[int, os.stat_result]":
    if not path:
        raise FileError(errno.EINVAL, "path is empty", path)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if requirements & FILE_NO_SYMLINK:
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        number = errno.ELOOP if error.errno == errno.EMLINK else error.errno
        explanation = "path is a symbolic link" if number == errno.ELOOP else \
            "path does not exist or is not accessible"
        raise FileError(number, explanation, path) from None
    try:
        try:
            status = os.fstat(descriptor)
        except OSError as error:
            raise FileError(error.errno, "file status is not accessible", path) from None
        verdict = _judge(status, requirements | FILE_REGULAR)
        if not verdict and requirements & FILE_TRUSTED_DIRECTORY:
            verdict = _judge_resolved_directory(path, status)
        if verdict:
            raise FileError(verdict[0], verdict[1], path)
        try:
            fcntl.fcntl(descriptor, fcntl.F_SETFL, fcntl.fcntl(descriptor, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        except OSError as error:
            raise FileError(error.errno, "descriptor flags are not adjustable", path) from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, status


def open_trusted(path: str, requirements: int) -> int:
    """Opens one regular file read-only and applies the requirements to the
    descriptor opened (maelys_cli_open_trusted): the open never blocks, a FIFO or
    a device is refused as not regular, FILE_NO_SYMLINK opens with O_NOFOLLOW,
    otherwise a link is followed and its target judged, FILE_TRUSTED_DIRECTORY
    saying where that target may lie. The descriptor is close-on-exec,
    blocking, at offset zero."""
    return _open_trusted(path, requirements)[0]


def zero(buffer: bytearray) -> None:
    """Overwrites a mutable buffer with zeros; for a secret before it is dropped."""
    buffer[:] = bytes(len(buffer))


def _read_bounded(descriptor: int, maximum: int, explanation_path: Optional[str]) -> bytearray:
    buffer = bytearray()
    try:
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(buffer)))
            if not chunk:
                return buffer
            buffer.extend(chunk)
            if len(buffer) > maximum:
                raise FileError(errno.EFBIG, "file is larger than the maximum size", explanation_path)
    except BaseException:
        zero(buffer)
        raise


def read_trusted_file(path: str, requirements: int, minimum_size: int, maximum_size: int) -> bytearray:
    """open_trusted() then a read of the whole file bounded by the bytes actually
    read (maelys_cli_read_trusted_file): EFBIG above maximum_size or below
    minimum_size, whatever the size observed before the read. The buffer is a
    bytearray, zeroed before release on any failure after the open; zero() it
    when it held a secret."""
    if minimum_size < 0 or minimum_size > maximum_size:
        raise FileError(errno.EINVAL, "size bounds are invalid", path)
    descriptor, status = _open_trusted(path, requirements)
    try:
        if status.st_size > maximum_size:
            raise FileError(errno.EFBIG, "file is larger than the maximum size", path)
        buffer = _read_bounded(descriptor, maximum_size, path)
        if len(buffer) < minimum_size:
            zero(buffer)
            raise FileError(errno.EFBIG, "file is smaller than the minimum size", path)
        return buffer
    finally:
        os.close(descriptor)


def read_regular_file(path: str, minimum_size: int, maximum_size: int) -> bytearray:
    """read_trusted_file() without requirement: links are followed, a regular
    file is required, the bytes read must lie in the bounds."""
    return read_trusted_file(path, 0, minimum_size, maximum_size)


def write_file_atomic(path: str, data: bytes, mode: int, policy: str) -> None:
    """Writes through a private temporary in the destination directory, fsyncs it
    and publishes it atomically (maelys_cli_write_file_atomic). WRITE_REPLACE
    renames over the target; WRITE_NO_REPLACE links it and fails with EEXIST when
    any entry, a dangling link included, already occupies the path."""
    if not path or mode == 0 or policy not in (WRITE_REPLACE, WRITE_NO_REPLACE):
        raise FileError(errno.EINVAL, "write arguments are invalid", path)
    if policy == WRITE_NO_REPLACE and os.path.lexists(path):
        raise FileError(errno.EEXIST, "path already exists", path)
    import tempfile  # here, not at the top: no command but a write pays for it
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=directory)
    published = False
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if policy == WRITE_REPLACE:
            os.replace(temporary, path)
            published = True
        else:
            os.link(temporary, path)
            published = True
            try:
                os.unlink(temporary)
            except OSError:
                pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary)
            except OSError:
                pass


# ---- the program ------------------------------------------------------------------

def _cell(value: Any) -> str:
    """One field of the section 7 pipe form: a string unquoted and escaped, anything
    else compact JSON. Shared by record_text() and field_text()."""
    if isinstance(value, str):
        escapes = {"\\": "\\\\", "\t": "\\t", "\r": "\\r", "\n": "\\n"}
        return "".join(escapes.get(char, f"\\u{ord(char):04x}") if ord(char) < 32 or char == "\\"
                       or ord(char) == 127 else char for char in value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def record_text(records: list) -> str:
    """The text form of records (spec 2.3, section 7): one tab-separated row per
    record, the columns being the union of the member names sorted by code
    point, a missing member an empty field, strings unquoted and escaped,
    every other value compact JSON. The same rows on a terminal and in a pipe."""
    columns = sorted({key for record in records if isinstance(record, dict) for key in record})
    return "".join("\t".join(_cell(record[key]) if key in record else "" for key in columns) + "\n"
                   for record in records if isinstance(record, dict))


def field_text(value: Any) -> str:
    """--field's text rendering (spec 2.4, section 5): the pipe rules of section 7
    extended to every shape. An array whose every element is an object gives one
    row per object (record_text(), unmodified: --field records on a json-records
    command equals its existing rendering); any other array gives one value per
    line; an object gives one row, its members as columns; anything else gives
    its escaped value on one line."""
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return record_text(value)
        return "".join(_cell(item) + "\n" for item in value)
    if isinstance(value, dict):
        return record_text([value])
    return _cell(value) + "\n"


def field_jsonl(value: Any) -> str:
    """--field's jsonl rendering (spec 2.4, section 5): an array gives one compact
    JSON value per line, anything else exactly one line; the rendering is total,
    so what a format accepts never depends on the data."""
    items = value if isinstance(value, list) else [value]
    return "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
                   for item in items)


def pager_command() -> Optional[list]:
    """The pager PAGER names, split with POSIX quoting and no expansion; ["less"]
    when unset; None when disabled (empty or blank) or unparseable."""
    setting = os.environ.get("PAGER")
    if setting is None:
        return ["less"]
    import shlex  # here, not at the top: only a terminal with PAGER set pays for it
    try:
        words = shlex.split(setting)
    except ValueError:
        return None
    return words or None


def page_text(text: str, invocation: "Invocation") -> bool:
    """Sends the text rendering through the pager when spec 2.3 section 5 allows
    it: text mode, stdout a terminal, --pager not never, no --non-interactive.
    Returns True when the pager consumed the text; the caller writes it to
    stdout otherwise. The pager is the one program resolved through PATH: the
    user's own choice, started only on that user's terminal."""
    if invocation.format != "text" or invocation.pager == "never" or invocation.non_interactive:
        return False
    if invocation.command["outputMode"] == "protocol-stream" or not sys.stdout.isatty():
        return False
    words = pager_command()
    if not words:
        return False
    env = dict(os.environ)
    if "PAGER" not in env:
        env.setdefault("LESS", "FRX")
    import subprocess  # here, not at the top: only a paged terminal pays for it
    sys.stdout.flush()
    try:
        subprocess.run(words, input=text, text=True, check=False, env=env)
    except (OSError, ValueError):
        return False
    return True


class Invocation:
    """What a handler receives: the resolved command and its validated input."""

    def __init__(self, program: "Program", command: dict, operands: list, options: dict,
                 fmt: str, compact: bool, non_interactive: bool, raw_operands: list,
                 verbose: bool = False, progress: str = "auto", pager: str = "auto",
                 color: str = "auto", field: Optional[str] = None) -> None:
        self.program = program
        self.command = command
        self.operands = operands
        self.raw_operands = raw_operands
        self.options = options
        self.format = fmt
        self.compact = compact
        self.non_interactive = non_interactive
        self.verbose = verbose and fmt == "text"
        self.progress = progress
        self.pager = pager
        self.color = color
        self.field = field
        self._progress_shown = False
        self._expect_checked = False

    @property
    def color_stdout(self) -> bool:
        """Resolved --color decision for stdout (maelys_cli_terminal_detect()'s
        color_stdout): a handler building its own human rendering reads this,
        never re-parses --color or the environment itself."""
        return terminal_color(self.color)[0]

    @property
    def color_stderr(self) -> bool:
        """Resolved --color decision for stderr (color_stderr); what the runtime
        itself uses for a failure or a warning rendering."""
        return terminal_color(self.color)[1]

    @property
    def progress_wanted(self) -> bool:
        """Text mode and --progress always, or auto with stderr a terminal."""
        if self.format != "text" or self.progress == "never":
            return False
        return self.progress == "always" or sys.stderr.isatty()

    def detail(self, message: str) -> None:
        """One detail line `PROGRAM: message` on stderr when --verbose is active in
        text mode (maelys_cli_detail); nothing in JSON; never a failure rendering."""
        if not self.verbose:
            return
        self.progress_done()
        sys.stderr.write(f"{self.program.program}: {_terminal_safe(message)}\n")
        sys.stderr.flush()

    def show_progress(self, message: str) -> None:
        """Transient progress (maelys_cli_progress): rewritten in place on a terminal
        and erased by progress_done(); one line per call on another stderr."""
        if not self.progress_wanted:
            return
        if sys.stderr.isatty():
            sys.stderr.write(f"\r{_terminal_safe(message)}\033[K")
            self._progress_shown = True
        else:
            sys.stderr.write(f"{_terminal_safe(message)}\n")
        sys.stderr.flush()

    def progress_done(self) -> None:
        if self._progress_shown:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            self._progress_shown = False

    def option(self, name: str, default: Any = None) -> Any:
        """The typed value of an option, its declared default, or `default`."""
        if name in self.options:
            return self.options[name]
        definition = next((item for item in self.command["options"] if item["long"] == name), None)
        if definition and "default" in definition and "argument" in definition:
            return parse_value(definition["argument"]["type"], definition["default"], definition["argument"],
                               f"Option {name}", self.command["usage"])
        return default

    def flag(self, name: str) -> bool:
        return bool(self.options.get(name, False))

    @property
    def apply(self) -> bool:
        return self.flag("--apply")

    def expect(self, fingerprint: str) -> None:
        """Binds --apply to the reviewed plan (transaction(expect=True); C:
        maelys_cli_expect). `fingerprint` is what the handler has just computed
        for the action it is about to perform on the state as it now is.
        Returns when --expect was not given or names it, and the handler puts
        the same string in data["fingerprint"]; raises PRECONDITION_FAILED
        otherwise. Call it before the first write, in plan mode as well: a
        caller that reads that code concludes that nothing changed."""
        self._expect_checked = True
        expected = self.options.get("--expect")
        if expected is not None and expected != fingerprint:
            raise Failure("PRECONDITION_FAILED",
                          f"The plan --expect names is no longer the one '{self.command['id']}' would apply: "
                          "the action or the state it touches has changed. Nothing was written.",
                          "Plan again without --apply, review the new plan, then apply it with its fingerprint.")


class Program:
    def __init__(self, program: str, product: str, version: str, commands: list, guide: str = "",
                 text: Optional[dict] = None, framework: str = FRAMEWORK, static_completion: bool = True) -> None:
        self.program = program
        self.product = product
        self.version = version
        self.framework = framework
        # False for a program whose catalog depends on the machine it runs on -- commands made
        # unavailable by what is installed, say: a script generated where the package was built would
        # carry that machine's catalog under the same version.
        self.static_completion = static_completion
        self.guide_line = guide or product
        self.text = dict(text or {})
        self.catalog = [
            read("help", "help", "Show the help of the program or of one command.", self._help,
                 operands=[operand("COMMAND_ID", "Command identifier, the family of commands under one, "
                                   "or `conventions`.", required=False)],
                 schema={"type": "object", "required": ["text", "commands"],
                         "properties": {"text": {"type": "string"},
                                        "commands": {"type": "array", "items": {"type": "string"}}}}),
            read("version", "version", "Show the product, program, version and contract.", self._version,
                 schema={"type": "object", "required": ["product", "program", "version", "contract", "cliApi", "framework"]}),
            read("describe", "describe", "Show the machine-readable catalog, or one descriptor.", self._describe,
                 operands=[operand("COMMAND_ID", "Command identifier.", required=False)],
                 options=[flag("--summary", "Every descriptor without its schema and exit codes."),
                          option("--prefix", "Select the command namespace PREFIX.",
                                 argument("PREFIX", "string", pattern=PREFIX_GRAMMAR.pattern),
                                 requires=("--summary",), conflicts_with=("COMMAND_ID",))],
                 schema={"type": "object", "required": ["schemaVersion", "kind", "program", "commands"]}),
            read("completion", "completion", "Print the shell completion script generated from the catalog.",
                 self._completion, operands=[operand("SHELL", "Shell.", choices=list(SHELLS))],
                 schema={"type": "object", "required": ["shell", "script"]}),
            records("complete.candidates", "__complete", "Completion candidates for the words given after --.",
                    self._complete, operands=[operand("WORDS", "Words typed so far.", required=False, variadic=True)],
                    hidden=True, schema={"type": "object", "required": ["count", "records"]}),
        ]
        self._builtin_count = len(self.catalog)
        self._family_words: Optional[list] = None
        seen = set()
        for command in commands:
            if command["id"] in seen or any(command["id"] == built["id"] for built in self.catalog):
                raise ValueError(f"duplicate command identifier {command['id']!r}")
            for item in command["options"]:
                pattern = item.get("argument", {}).get("pattern")
                if pattern is not None:
                    try:
                        re.compile(pattern)
                    except re.error as error:
                        raise ValueError(f"{command['id']}: {item['long']} pattern is invalid: {error}") from None
            seen.add(command["id"])
            self._check_references(command)
            self.catalog.append(command)
        # usage in help; describe uses the catalog's synopsis
        self.catalog[0]["usage"] = "help [COMMAND_ID] | --help"
        self.catalog[1]["usage"] = "version | --version"
        # The examples last: parsing one needs the whole catalog.
        for command in self.catalog:
            self._check_examples(command)

    def _check_examples(self, command: dict) -> None:
        """An example is an invocation the command accepts (spec 2.12, section
        2; C: validate_examples): it starts with the command's pattern and
        parses as any command line does, and shows no hidden option. It is
        parsed, never run. After the pattern of a delegate the words are the
        other executable's."""
        hidden = {item["long"] for item in command["options"] if item.get("hidden")}
        for item in command["examples"]:
            words = item["words"]
            spelled = " ".join(words)
            if words[:len(command["pattern"])] != command["pattern"]:
                raise ValueError(f"{command['id']}: an example does not start with the pattern "
                                 f"'{' '.join(command['pattern'])}': '{spelled}'")
            if command["passthrough"]:
                continue
            before = words[:words.index("--")] if "--" in words else words
            if "--help" in before:
                raise ValueError(f"{command['id']}: an example invokes the command, it does not ask for its help: "
                                 f"'{spelled}'")
            unavailable, command["unavailable"] = command["unavailable"], None
            try:
                invocation, resolved = self.parse(list(words))
            except Failure as failure:
                raise ValueError(f"{command['id']}: an example the command does not accept: '{spelled}': "
                                 f"{failure.message}") from None
            finally:
                command["unavailable"] = unavailable
                self.resolved_command_id = ""
                self._family_words = None
            if resolved is not command:
                raise ValueError(f"{command['id']}: an example names another command: '{spelled}'")
            shown = sorted(name for name in invocation.options if name in hidden
                           and any(word.split("=", 1)[0] == name for word in before))
            if shown:
                raise ValueError(f"{command['id']}: an example shows the hidden option {shown[0]}: '{spelled}'")

    def _check_references(self, command: dict) -> None:
        names = {item["long"] for item in command["options"]} | {item["long"] for item in GLOBAL_OPTIONS}
        operands = {item["name"] for item in command["operands"]}
        for item in command["options"]:
            for reference in item["requires"]:
                if reference not in names:
                    raise ValueError(f"{command['id']}: {item['long']} requires unknown option {reference}")
            for reference in item["conflictsWith"]:
                if not (reference in names or (not reference.startswith("--") and reference in operands)):
                    raise ValueError(f"{command['id']}: {item['long']} conflicts with unknown {reference}")
        # A stated rule names options of the command itself, as C does.
        own = {item["long"] for item in command["options"]}
        for rule in command["constraints"]:
            for reference in rule["options"]:
                if reference not in own:
                    raise ValueError(f"{command['id']}: {rule['kind']} constraint names unknown option {reference}")

    # ---- catalog views ----

    def descriptor(self, command: dict, summary: bool = False) -> dict:
        entry: dict = {"id": command["id"], "pattern": command["pattern"], "usage": command["usage"],
                       "purpose": command["purpose"], "effect": command["effect"], "outputMode": command["outputMode"]}
        if command["protocol"]:
            entry["protocol"] = command["protocol"]
        entry.update({"external": command["external"], "hidden": command["hidden"],
                      "available": command["unavailable"] is None})
        if command["unavailable"] is not None:
            entry["unavailableReason"] = command["unavailable"]
        entry["input"] = {"synopsis": command["usage"], "operands": command["operands"],
                          "options": command["options"], "constraints": self.constraints(command),
                          "passthrough": command["passthrough"]}
        if not summary and command["examples"]:
            # The summary omits them, as it omits the schema: it is the form
            # an agent pays for on every discovery (spec 2.12, section 1).
            entry["examples"] = [{"words": list(item["words"]), "summary": item["summary"]}
                                 for item in command["examples"]]
        if not summary:
            entry["outputSchema"] = command["outputSchema"]
            entry["exitCodes"] = dict(EXIT_CODES)
        return entry

    @staticmethod
    def constraints(command: dict) -> list:
        entries = []
        groups: dict = {}
        for item in command["options"]:
            for other in item["requires"]:
                entries.append({"kind": "requires", "options": [item["long"], other]})
            for other in item["conflictsWith"]:
                if other.startswith("--"):
                    entries.append({"kind": "at-most-one", "options": [item["long"], other]})
            if "group" in item:
                groups.setdefault(item["group"], []).append(item["long"])
        entries.extend({"kind": "all-or-none", "options": members} for members in groups.values())
        # The rules the command states itself (spec 2.5), after the derived
        # ones: exactly-one has no other site.
        entries.extend({"kind": rule["kind"], "options": list(rule["options"])} for rule in command["constraints"])
        return entries

    def command_by_id(self, identifier: str) -> dict:
        for command in self.catalog:
            if command["id"] == identifier:
                return command
        raise Failure("INVALID_COMMAND", f"Unknown command identifier '{identifier}'.",
                      "Run describe --summary to list the command identifiers.")

    def resolve(self, words: list) -> "tuple[Optional[dict], int]":
        best = None
        for command in self.catalog:
            pattern = command["pattern"]
            if words[:len(pattern)] == pattern and (best is None or len(pattern) > len(best["pattern"])):
                best = command
        return best, len(best["pattern"]) if best else 0

    @staticmethod
    def _option_label(item: dict) -> str:
        value = item.get("argument")
        if value and value.get("choices"):
            spelled = f"{item['long']} {'|'.join(value['choices'])}"
        elif value:
            spelled = f"{item['long']} {value['name']}"
        else:
            spelled = item["long"]
        return spelled + (" (repeatable)" if item["repeatable"] else "")

    @staticmethod
    def _option_text(item: dict) -> str:
        text = item["summary"]
        if "default" in item:
            text += f" Default: {item['default']}."
        if item["required"]:
            text += " Required."
        text += "".join(f" Requires {name}." for name in item["requires"])
        text += "".join(f" Conflicts with {name}." for name in item["conflictsWith"])
        return text

    def _options_help(self, options: list, width: int) -> str:
        shown = [(self._option_label(item), self._option_text(item)) for item in options if not item.get("hidden")]
        column = max([display_width(label) for label, _ in shown if display_width(label) <= HELP_LABEL_MAXIMUM] or [0])
        return "".join(_help_entry(label, text, column, width) for label, text in shown)

    def command_help(self, command: dict, width: int = HELP_WIDTH) -> str:
        """The help of one command: its usage, purpose, effect, output mode,
        operands and options, laid out within `width` columns."""
        text = "USAGE\n" + _help_paragraph(f"{self.program} {command['usage']}", width, groups=True)
        text += "\n" + _help_wrap(command["purpose"], 0, 0, width) + "\n\n"
        if command["unavailable"] is not None:
            text += "UNAVAILABLE IN THIS BUILD\n" + _help_paragraph(command["unavailable"], width) + "\n"
        effect = command["effect"]
        text += "EFFECT\n  " + (f"{effect['plan']} by default; {effect['apply']} with --apply"
                                if isinstance(effect, dict) else effect) + "\n"
        output = command["outputMode"] + (f" owned by protocol {command['protocol']}" if command["protocol"] else "") \
            + (" (arguments are passed to an external program)" if command["external"] else "")
        text += "\nOUTPUT\n" + _help_paragraph(output, width)
        if command["operands"]:
            column = max([display_width(item["name"]) for item in command["operands"]
                          if display_width(item["name"]) <= HELP_LABEL_MAXIMUM] or [0])
            text += "\nOPERANDS\n" + "".join(
                _help_entry(item["name"], item["summary"] + ("" if item["required"] else " Optional."), column, width)
                for item in command["operands"])
        if any(not item.get("hidden") for item in command["options"]):
            text += "\nOPTIONS\n" + self._options_help(command["options"], width)
        if command["examples"]:
            # A line to copy: on one line whatever the width, the one place
            # the help passes it on purpose. Wrapped, the first half was a
            # command of its own and the second another. The sentence wraps.
            # And spelled for a shell: a word holding `$`, `*` or a quote,
            # copied as declared, would be another word once pasted.
            text += "\nEXAMPLES\n" + "".join(
                f"  {self.program} {' '.join(_shell_word(word) for word in item['words'])}\n"
                + " " * 6 + _help_wrap(item["summary"], 6, 6, width) + "\n"
                for item in command["examples"])
        text += "\nGLOBAL OPTIONS\n" + _help_paragraph(
            f"Run '{self.program} help conventions' for --format, --json, --compact, --non-interactive, --color "
            "and the others.", width)
        return text

    def _family(self, prefix: Optional[str] = None, words: Optional[list] = None) -> list:
        """The shown commands of a family: under the identifier `prefix`, the
        namespace describe --prefix selects, or whose pattern starts with
        `words` and has more."""
        shown = [command for command in self.catalog if not command["hidden"]]
        if prefix is not None:
            return [command for command in shown if command["id"] == prefix or command["id"].startswith(f"{prefix}.")]
        return [command for command in shown
                if len(command["pattern"]) > len(words or []) and command["pattern"][:len(words or [])] == words]

    def family_help(self, name: str, commands: list, width: int = HELP_WIDTH) -> str:
        """The help of a family: each of its commands with its usage, the purpose below."""
        text = f"{self.program} {name} - commands\n\nCOMMANDS\n"
        for command in commands:
            purpose = command["purpose"] + (" (unavailable in this build)" if command["unavailable"] is not None else "")
            text += _help_entry(command["usage"], purpose, 0, width, groups=True)
        return text + "\n" + _help_wrap(
            f"Run '{self.program} help COMMAND_ID' or '{self.program} COMMAND --help' for the operands and options "
            "of one command.", 0, 0, width) + "\n"

    def warn(self, message: str) -> None:
        """A diagnostic on stderr, `program: warning: message`, as maelys_cli_warn(); never stdout."""
        sys.stderr.write(f"{self.program}: warning: {_terminal_safe(message)}\n")
        sys.stderr.flush()

    def guide(self, width: int = HELP_WIDTH) -> str:
        """The general help: each command by its pattern and its purpose, the
        product's first, then the ones every program has; the usage of a
        command is in its own help, the usage of a family in the family's."""
        text = _help_wrap(f"{self.program} {self.version} - {self.guide_line}", 0, 0, width) + "\n\n"
        text += f"USAGE\n  {self.program} COMMAND [OPERANDS] [OPTIONS]\n"
        column = display_width(f"{self.program} help conventions")
        text += _help_entry(f"{self.program} help COMMAND_ID", "the operands and options of one command", column, width)
        text += _help_entry(f"{self.program} help FAMILY", "the commands of one family, with their usage", column, width)
        text += _help_entry(f"{self.program} help conventions",
                            "the options every command takes, and the agent contract", column, width)
        text += "\nCOMMANDS\n"
        visible = [(index, command) for index, command in enumerate(self.catalog) if not command["hidden"]]
        column = max([display_width(" ".join(command["pattern"])) for _, command in visible
                      if display_width(" ".join(command["pattern"])) <= HELP_LABEL_MAXIMUM] or [0])
        own = [command for index, command in visible if index >= self._builtin_count]
        built_in = [command for index, command in visible if index < self._builtin_count]
        for group in (own, built_in):
            if group is built_in and own and built_in:
                text += "\n"
            for command in group:
                purpose = command["purpose"] + (" (unavailable in this build)" if command["unavailable"] is not None else "")
                text += _help_entry(" ".join(command["pattern"]), purpose, column, width)
        # What every program has in common is named, not repeated: spelled
        # out, the options and the contract were two thirds of this screen.
        names = ", ".join(item["long"] for item in GLOBAL_OPTIONS if not item.get("hidden"))
        text += "\nGLOBAL OPTIONS\n" + _help_paragraph(
            f"{names}. Run '{self.program} help conventions' for what each one does.", width)
        text += "\nAGENT CONTRACT\n" + _help_paragraph(
            f"Use --format json --non-interactive, and run '{self.program} describe --summary --format json' first. "
            f"'{self.program} help conventions' has the rest of the contract.", width)
        return text

    def conventions_help(self, width: int = HELP_WIDTH) -> str:
        """What every program built on the framework has in common: the options
        every command takes and the contract an agent relies on, whole."""
        return f"{self.program} - conventions\n\nGLOBAL OPTIONS\n" + self._options_help(GLOBAL_OPTIONS, width) \
            + "\nAGENT CONTRACT\n" + _help_paragraph(
                f"Use --format json --non-interactive. Run '{self.program} describe --summary --format json' first, "
                f"then '{self.program} describe COMMAND_ID --format json' for the exact input and output contract. "
                "Exit 0 is success, 1 is execution failure, and 2 is a completed validation report with violations. "
                "Transactions plan by default and require --apply. Stream commands reserve stdout for their "
                "protocol. Success data is written to stdout only; diagnostics and failures go to stderr.", width)

    # ---- built-in handlers ----

    def _version(self, invocation: Invocation) -> "tuple[dict, int]":
        return {"product": self.product, "program": self.program, "version": self.version, "contract": CONTRACT,
                "cliApi": CLI_API, "framework": self.framework}, EXIT_OK

    def _help(self, invocation: Invocation) -> "tuple[dict, int]":
        width = help_width(invocation.format)
        words, self._family_words = self._family_words, None
        if words:
            # `PROGRAM note --help`: the words named no command, but a family.
            family = self._family(words=words)
            return {"text": self.family_help(" ".join(words), family, width),
                    "commands": [command["id"] for command in family]}, EXIT_OK
        if invocation.operands:
            query = invocation.operands[0]
            command = next((item for item in self.catalog if item["id"] == query), None)
            if command is not None:
                return {"text": self.command_help(command, width), "commands": [command["id"]]}, EXIT_OK
            # Not a command: a family, the namespace describe --prefix selects;
            # or the one topic, which a command or a family of that name hides.
            family = self._family(prefix=query)
            if not family and query == "conventions":
                return {"text": self.conventions_help(width), "commands": []}, EXIT_OK
            if not family:
                raise Failure("INVALID_COMMAND", f"Unknown command identifier or family: {query}.",
                              "Run 'help' without operands to list the commands and their families.")
            return {"text": self.family_help(query, family, width),
                    "commands": [command["id"] for command in family]}, EXIT_OK
        return {"text": self.guide(width), "commands": [c["id"] for c in self.catalog if not c["hidden"]]}, EXIT_OK

    def _describe(self, invocation: Invocation) -> "tuple[dict, int]":
        data: dict = {"schemaVersion": CATALOG_SCHEMA, "program": self.program, "product": self.product,
                      "version": self.version, "contract": CONTRACT, "cliApi": CLI_API, "framework": self.framework}
        prefix = invocation.option("--prefix")
        if prefix is not None:
            if not PREFIX_GRAMMAR.fullmatch(prefix):
                raise Failure("VALIDATION_FAILED", f"Option --prefix takes a command identifier prefix, not '{prefix}'.",
                              "Use lowercase letters, digits, '.' and '-', starting with a letter.")
            selected = [self.descriptor(c, summary=True) for c in self.catalog
                        if c["id"] == prefix or c["id"].startswith(f"{prefix}.")]
            if not selected:
                raise Failure("INVALID_COMMAND", f"No command in namespace: {prefix}.",
                              "Run describe --summary --format json and select a returned command identifier.")
            data.update({"kind": "summary", "filter": {"kind": "command-prefix", "value": prefix},
                         "commands": selected})
            return data, EXIT_OK
        if invocation.operands:
            data["kind"] = "command"
            data["commands"] = [self.descriptor(self.command_by_id(invocation.operands[0]))]
        elif invocation.flag("--summary"):
            data["kind"] = "summary"
            data["commands"] = [self.descriptor(c, summary=True) for c in self.catalog]
        else:
            data["kind"] = "catalog"
            data["globalOptions"] = GLOBAL_OPTIONS
            data["invariants"] = INVARIANTS
            data["output"] = {"contract": CONTRACT, "schemaVersion": SCHEMA_VERSION,
                              "stdout": "success data only; protocol streams are explicit exceptions",
                              "stderr": "diagnostics and failure envelopes"}
            data["commands"] = [self.descriptor(c) for c in self.catalog]
        return data, EXIT_OK

    def _completion(self, invocation: Invocation) -> "tuple[dict, int]":
        shell = invocation.operands[0]
        return {"shell": shell, "script": self.completion_script(shell)}, EXIT_OK

    def completion_script(self, shell: str, static: Optional[bool] = None) -> str:
        """The completion script `completion SHELL` prints, for bash, zsh or fish. Static by default: it
        carries the candidates of this catalog and its version, launches no process at a Tab, and calls
        `__complete` only after a delegate's pattern. `static=False`, or `static_completion=False` on the
        program, gives the script that calls `__complete` at every completion, which is also what a
        catalog with a word the static form cannot carry receives. Both offer the words `__complete`
        returns."""
        if shell not in SHELLS:
            raise ValueError(f"a completion script is for one of {', '.join(SHELLS)}, not {shell!r}")
        rows = self._static_rows() if (self.static_completion if static is None else static) else None
        if rows is None:
            return self._dynamic_script(shell)
        template = {"bash": _STATIC_BASH, "zsh": _STATIC_ZSH, "fish": _STATIC_FISH}[shell]
        quoted = [f"'{row}'" for row in rows]
        table = " \\\n".join(quoted) if shell == "fish" else "\n".join(quoted)
        identifier = re.sub(r"[^A-Za-z0-9]", "_", self.program)
        return (template.replace("@ROWS@", table).replace("@ID@", identifier)
                .replace("@PROG@", self.program).replace("@VERSION@", self.version))

    def _static_rows(self) -> Optional[list]:
        """The table a static script carries (see _STATIC_WORD), or None when a word of this catalog
        would not be inert in a shell: the script that calls __complete is exact for any catalog."""
        shown = [item for item in self.catalog if not item["hidden"] and item["unavailable"] is None]
        rows, details, words = [], [], [self.program, self.version]

        def option_rows(index: str, options: list) -> None:
            for item in options:
                value = item.get("argument")
                flags = ("a" if value is not None else "") + ("e" if (value or {}).get("type") == "choice" else "") \
                    + ("h" if item.get("hidden") else "") + ("r" if item["repeatable"] else "")
                values = self._value_candidates(value) if value is not None else []
                words.extend([item["long"], *values])
                details.append(f"O|{index}|{item['long']}|{flags or '-'}|{' '.join(values)}")

        for index, command in enumerate(shown):
            kind = "d" if command["external"] else "s" if command["outputMode"] == "protocol-stream" else "c"
            words.extend([command["id"], *command["pattern"]])
            rows.append(f"C|{index}|{' '.join(command['pattern'])}|{command['id']}|{kind}")
            if command["external"]:
                continue
            option_rows(str(index), command["options"])
            for item in command["operands"]:
                values = self._value_candidates(item)
                words.extend(values)
                details.append(f"P|{index}|{'v' if item['variadic'] else '-'}|{' '.join(values)}")
        option_rows("g", GLOBAL_OPTIONS)
        if not all(_STATIC_WORD.match(word) for word in words):
            return None
        return rows + details

    def _dynamic_script(self, shell: str) -> str:
        """The script that calls __complete at every completion."""
        # The three scripts are renderings of __complete (agent-cli/v2, section 6): they offer its words
        # and no others, and fall back to the shell's file completion when it returns none. src/app.c
        # prints the same text, where each line is explained; a line that differs is a defect.
        program = self.program
        function = "_" + re.sub(r"[^A-Za-z0-9]", "_", program) + "_complete"
        if shell == "bash":
            script = "\n".join([
                f"# bash completion for {program}, generated from its catalog",
                f"{function}() {{",
                "    local -a words",
                '    words=("${COMP_WORDS[@]:1:COMP_CWORD}")',
                "    local IFS=$'\\n'",
                f'    COMPREPLY=($("{program}" __complete -- "${{words[@]}}" 2>/dev/null))',
                "    if [ ${#COMPREPLY[@]} -eq 0 ]; then",
                '        COMPREPLY=($(compgen -f -- "${COMP_WORDS[COMP_CWORD]}"))',
                "    fi",
                "}",
                f"complete -o filenames -F {function} {program}",
            ]) + "\n"
        elif shell == "zsh":
            script = "\n".join([
                f"#compdef {program}",
                f"# zsh completion for {program}, generated from its catalog",
                f"{function}() {{",
                "    local -a candidates",
                f'    candidates=(${{(f)"$("{program}" __complete -- "${{(@)words[2,CURRENT]}}" 2>/dev/null)"}})',
                "    if (( ${#candidates} )); then",
                '        compadd -- "${candidates[@]}"',
                "    else",
                "        _files",
                "    fi",
                "}",
                f"if [[ ${{funcstack[1]}} == _{program} ]]; then",
                f'    {function} "$@"',
                "else",
                f"    compdef {function} {program}",
                "fi",
            ]) + "\n"
        else:
            script = "\n".join([
                f"# fish completion for {program}, generated from its catalog",
                f"function _{function}",
                "    set -l words (commandline -opc)",
                "    set -l current (commandline -ct)",
                f'    set -l candidates ("{program}" __complete -- $words[2..-1] "$current" 2>/dev/null)',
                "    if test (count $candidates) -gt 0",
                "        printf '%s\\n' $candidates",
                "    else",
                '        __fish_complete_path "$current"',
                "    end",
                "end",
                f"complete -c {program} -f -a '(_{function})'",
            ]) + "\n"
        return script

    def _complete(self, invocation: Invocation) -> "tuple[dict, int]":
        # The oracle of the completion scripts (agent-cli/v2, section 6). It follows builtin_complete of
        # src/app.c step by step and returns the same words in the same order: a difference is a defect.
        words = list(invocation.raw_operands)
        current = words[-1] if words else ""
        previous = words[:-1]
        shown = [item for item in self.catalog if not item["hidden"] and item["unavailable"] is None]
        command = None
        for other in shown:
            pattern = other["pattern"]
            if previous[:len(pattern)] == pattern and (command is None or len(pattern) > len(command["pattern"])):
                command = other
        candidates: list = []
        if command is None:
            # The next pattern word of every command that starts with the words given so far.
            candidates = [other["pattern"][len(previous)] for other in shown
                          if len(other["pattern"]) > len(previous) and other["pattern"][:len(previous)] == previous]
        elif command["external"]:
            # The words after a delegate's pattern are the delegate's, which this catalog does not hold:
            # none, and never this program's own options (section 9).
            pass
        else:
            after = previous[len(command["pattern"]):]
            shared = [] if command["outputMode"] == "protocol-stream" else GLOBAL_OPTIONS
            expecting = None
            if after and after[-1].startswith("--") and "=" not in after[-1]:
                expecting = next((item for item in command["options"] + GLOBAL_OPTIONS
                                  if item["long"] == after[-1] and "argument" in item), None)
            if expecting is not None:
                candidates = self._value_candidates(expecting["argument"])
            elif current.startswith("--") and "=" in current:
                # --option=VALUE: the value is completed with the option spelled.
                name = current.split("=", 1)[0]
                for item in command["options"]:
                    if item["long"] == name and not item.get("hidden") and item.get("argument", {}).get("type") == "choice":
                        candidates.extend(f"{name}={choice}" for choice in item["argument"].get("choices", []))
            elif current.startswith("--"):
                given = {word.split("=", 1)[0] for word in after if word.startswith("--")}
                candidates = [item["long"] for item in command["options"] + shared
                              if not item.get("hidden") and (item["repeatable"] or item["long"] not in given)]
            elif command["id"] in ("help", "describe") and not after:
                candidates = [other["id"] for other in shown]
            elif command["operands"]:
                position = self._operand_position(command, after)
                operands = command["operands"]
                if position < len(operands) or operands[-1]["variadic"]:
                    candidates = self._value_candidates(operands[min(position, len(operands) - 1)])
        matching = [word for word in dict.fromkeys(candidates) if word.startswith(current)]
        return {"count": len(matching), "records": [{"word": word} for word in matching]}, EXIT_OK

    @staticmethod
    def _value_candidates(value: dict) -> list:
        """The words a typed value can be completed with; paths and free values fall back to files."""
        if value.get("type") == "choice":
            return list(value.get("choices", []))
        if value.get("type") == "digest":
            return [f"{algorithm}:" for algorithm in value.get("algorithms", [])]
        return []

    @staticmethod
    def _operand_position(command: dict, after: list) -> int:
        """How many operands the complete words after the pattern already give."""
        position = 0
        index = 0
        while index < len(after):
            word = after[index]
            index += 1
            if word == "--":
                return position + len(after) - index
            if word.startswith("--") and len(word) > 2:
                definition = next((item for item in command["options"] + GLOBAL_OPTIONS if item["long"] == word), None)
                if definition is not None and "argument" in definition:
                    index += 1
                continue
            position += 1
        return position

    # ---- parsing ----

    def parse(self, argv: list) -> "tuple[Invocation, dict]":
        """Validate the command line in the contract's causal order."""
        # One line, one family: a line refused after `FAMILY --help` was read
        # must not hand its family to the next help this program is asked.
        self._family_words = None
        words: list = []
        raw: list = []
        passthrough: list = []
        index = 0
        command = None
        consumed = 0
        while index < len(argv):
            word = argv[index]
            command, consumed = self.resolve(words)
            if command is not None and command["passthrough"] and len(words) == consumed:
                passthrough = argv[index:]
                break
            if word == "--":
                passthrough = argv[index + 1:]
                break
            if word.startswith("--") and len(word) > 2:
                name, separator, value = word.partition("=")
                definition = next((item for item in GLOBAL_OPTIONS if item["long"] == name), None)
                if definition is None and command is not None:
                    definition = next((item for item in command["options"] if item["long"] == name), None)
                index += 1
                if definition and "argument" in definition and not separator and index < len(argv):
                    value, separator = argv[index], "="
                    index += 1
                # An option that ends the line without its value is kept without
                # one and refused below, at its turn: after the command is
                # resolved, so that the failure names it and an unknown command
                # is said first, and after the options written before it.
                raw.append((name, value if separator else None))
                continue
            if word.startswith("-") and word != "-":
                # There is no short option, and such a word is no operand either
                # (spec, section 8). It is kept and refused below at its turn,
                # as an option without its value is: after the command is
                # resolved, so that the failure names it, and after an unknown
                # command, which is said first.
                raw.append((word, _DASH_WORD))
                index += 1
                continue
            words.append(word)
            index += 1
        # A word that starts with one dash names no command and selects none:
        # `-x --help` resolves nothing, as in C.
        if not words and any(name in ("--help", "--version") for name, _ in raw) \
                and not any(value is _DASH_WORD for _, value in raw):
            words = ["help"] if any(name == "--help" for name, _ in raw) else ["version"]
            raw = [(name, value) for name, value in raw if name not in ("--help", "--version")]
        if not words:
            raise Failure("INVALID_COMMAND", "No command given.", f"Run '{self.program} help' or 'describe --summary'.")
        command, consumed = self.resolve(words)
        if command is None and any(name == "--help" for name, _ in raw) and self._family(words=words):
            # `PROGRAM note --help`: the words name no command, but a family of
            # them, and help was asked for: the help of that family.
            self._family_words = list(words)
            words = ["help"]
            raw = [(name, value) for name, value in raw if name != "--help"]
            command, consumed = self.resolve(words)
        if command is None:
            raise Failure("INVALID_COMMAND", f"Unknown command '{' '.join(words)}'.",
                          "Run describe --summary and use one of the listed command identifiers.")
        # A failure envelope names the resolved command from here on (section 7).
        self.resolved_command_id = command["id"]
        raw_operands = words[consumed:] + passthrough
        usage = command["usage"]
        fmt = environment_format()
        compact = False
        non_interactive = False
        verbose = False
        progress = "auto"
        pager = "auto"
        color = "auto"
        field = None
        format_requested = False
        rendering: list = []
        help_requested = False
        options: dict = {}
        seen: set = set()
        known = {item["long"]: item for item in command["options"]}
        for name, value in raw:
            if name in seen and not (name in known and known[name]["repeatable"]):
                raise Failure("VALIDATION_FAILED", f"Option {name} is given twice.", "Give each option once.")
            if value is _DASH_WORD:
                raise Failure("VALIDATION_FAILED", f"Option {name} is not supported: options are spelled --name.",
                              "Spell an option --name; write an operand that starts with a dash after --.")
            seen.add(name)
            declared = known.get(name) or next((item for item in GLOBAL_OPTIONS if item["long"] == name), None)
            if declared is not None and "argument" in declared and value is None:
                raise Failure("VALIDATION_FAILED", f"Option {name} needs a value {declared['argument']['name']}.",
                              "Run describe for this command and pass the option's argument.")
            if name in RENDERING:
                rendering.append(name)
            if name == "--format":
                fmt = parse_value("choice", value or "", {"choices": list(FORMATS)}, "Option --format", usage)
                format_requested = True
            elif name == "--json":
                fmt = "json" if _parse_flag(value, name, usage) else "text"
                format_requested = True
            elif name == "--compact":
                compact = _parse_flag(value, name, usage)
            elif name == "--pretty":
                compact = not _parse_flag(value, name, usage)
            elif name == "--non-interactive":
                non_interactive = _parse_flag(value, name, usage)
            elif name == "--color":
                color = parse_value("choice", value or "", {"choices": list(COLORS)}, "Option --color", usage)
            elif name == "--progress":
                progress = parse_value("choice", value or "", {"choices": list(TRISTATE)}, "Option --progress", usage)
            elif name == "--verbose":
                verbose = _parse_flag(value, name, usage)
            elif name == "--pager":
                pager = parse_value("choice", value or "", {"choices": list(TRISTATE)}, "Option --pager", usage)
            elif name == "--field":
                field = value
            elif name == "--help":
                help_requested = _parse_flag(value, name, usage)
            elif name in ("--dry-run", "--plan") and isinstance(command["effect"], dict):
                raise Failure("VALIDATION_FAILED", f"Option {name} is not supported by '{command['id']}': it plans by default.",
                              "Run without --apply to plan, then add --apply to write.")
            elif name not in known:
                raise Failure("VALIDATION_FAILED", f"Option {name} is not supported by '{command['id']}'. Use '{usage}'.",
                              "Run describe for this command and use only its declared options.")
            else:
                definition = known[name]
                if "argument" in definition:
                    typed = parse_value(definition["argument"]["type"], value, definition["argument"],
                                        f"Option {name}", usage)
                    if definition["repeatable"]:
                        options.setdefault(name, []).append(typed)
                    else:
                        options[name] = typed
                else:
                    options[name] = _parse_flag(value, name, usage)
        # The order of the refusals (spec, section 8). What one option says
        # alone was judged above, in the name of the command. --help comes
        # next: asked how a command is used, the program answers, whatever the
        # line lacks as a whole -- a dependency, a required option, an operand
        # -- and whether or not this build can run the command.
        if help_requested:
            # The line is an invocation of `help` from here on, and its rendering
            # is validated as one (C: maelys_cli_run after the substitution).
            help_command = self.command_by_id("help")
            self.resolved_command_id = help_command["id"]
            if fmt == "jsonl" and field is None:
                raise Failure("VALIDATION_FAILED", "--format jsonl is accepted only by json-records commands, not "
                              "'help'; add --field to render one member in jsonl.", "Use --format json.")
            return Invocation(self, help_command, [command["id"]], {}, fmt, compact, non_interactive,
                              [command["id"]], verbose, progress, pager, color, field), help_command
        # dependencies, conflicts, groups; then required; then operands
        def enabled(option_name: str) -> bool:
            return option_name in options and options[option_name] is not False

        for definition in command["options"]:
            name = definition["long"]
            if not enabled(name):
                continue
            for other in definition["requires"]:
                if not enabled(other):
                    raise Failure("VALIDATION_FAILED", f"Option {name} requires {other}.", f"Use '{usage}'.")
            for other in definition["conflictsWith"]:
                if other.startswith("--"):
                    if enabled(other):
                        raise Failure("VALIDATION_FAILED", f"Option {name} conflicts with {other}.", f"Use '{usage}'.")
                else:
                    position = next((i for i, item in enumerate(command["operands"]) if item["name"] == other), None)
                    if position is not None and len(raw_operands) > position:
                        raise Failure("VALIDATION_FAILED", f"Option {name} conflicts with the {other} operand.",
                                      f"Use '{usage}'.")
        groups: dict = {}
        for definition in command["options"]:
            if "group" in definition:
                groups.setdefault(definition["group"], []).append(definition["long"])
        for members in groups.values():
            present = [name for name in members if enabled(name)]
            if present and len(present) != len(members):
                raise Failure("VALIDATION_FAILED", f"Options {', '.join(members)} are given together or not at all.",
                              f"Use '{usage}'.")
        # The rules the command states itself (spec 2.5), in the same causal
        # slot as the dependencies they extend; exactly-one refuses zero as
        # it refuses two.
        for rule in command["constraints"]:
            present = [name for name in rule["options"] if enabled(name)]
            listed = ", ".join(rule["options"])
            if rule["kind"] == "requires" and enabled(rule["options"][0]):
                for other in rule["options"][1:]:
                    if not enabled(other):
                        raise Failure("VALIDATION_FAILED", f"Option {rule['options'][0]} requires {other}.",
                                      f"Use '{usage}'.")
            elif rule["kind"] == "at-most-one" and len(present) > 1:
                raise Failure("VALIDATION_FAILED", f"At most one of {listed} may be given.", f"Use '{usage}'.")
            elif rule["kind"] == "exactly-one" and len(present) != 1:
                raise Failure("VALIDATION_FAILED", f"Exactly one of {listed} must be given.", f"Use '{usage}'.")
        for definition in command["options"]:
            if definition["required"] and definition["long"] not in options:
                raise Failure("VALIDATION_FAILED", f"Option {definition['long']} is required by '{command['id']}'.",
                              f"Use '{usage}'.")
        operands: list = []
        if not command["passthrough"]:
            required = sum(1 for item in command["operands"] if item["required"])
            variadic = any(item["variadic"] for item in command["operands"])
            if len(raw_operands) < required or (not variadic and len(raw_operands) > len(command["operands"])):
                raise Failure("VALIDATION_FAILED", f"Operands do not match '{command['id']}'. Use '{usage}'.",
                              "Use the synopsis returned by describe and retry.")
            for position, value in enumerate(raw_operands):
                item = command["operands"][min(position, len(command["operands"]) - 1)]
                kind = item.get("type")
                operands.append(parse_value(kind, value, item, item["name"], usage) if kind else value)
        else:
            operands = list(raw_operands)
        # Then whether this build can run it: a line the command would refuse
        # is refused as such first, and a rendering flag is not what stops a
        # command that cannot run at all.
        if command["unavailable"] is not None:
            raise Failure(command.get("unavailableCode") or "UNSUPPORTED",
                          f"Command '{command['id']}' is not available in this build: {command['unavailable']}",
                          "Use another build of the product, or another command.")
        if command["outputMode"] == "protocol-stream" and rendering:
            raise Failure("VALIDATION_FAILED", f"Command '{command['id']}' owns its stdout and refuses {rendering[0]}.",
                          "Set MAELYS_CLI_FORMAT=json in the environment to receive its failure envelope as JSON.")
        if fmt == "jsonl" and field is None and command["outputMode"] != "json-records":
            raise Failure("VALIDATION_FAILED", f"--format jsonl is accepted only by json-records commands, not "
                          f"'{command['id']}'; add --field to render one member in jsonl.",
                          "Use --format json.")
        # data is governed by outputSchema; a filtered envelope would not validate
        # against it (spec 2.4). An environment MAELYS_CLI_FORMAT=json applies after
        # parsing and cannot be caught here; main() refuses it too, defensively.
        if field is not None and fmt == "json" and format_requested:
            raise Failure("VALIDATION_FAILED",
                          "--field conflicts with --format json: a filtered envelope would not "
                          "validate against the command's outputSchema.",
                          "Use --format text or --format jsonl with --field.")
        return Invocation(self, command, operands, options, fmt, compact, non_interactive, raw_operands,
                          verbose, progress, pager, color, field), command

    # ---- rendering ----

    @staticmethod
    def envelope(command_id: str, ok: bool, exit_code: int, payload: Any, compact: bool) -> str:
        body: dict = {"schemaVersion": SCHEMA_VERSION, "contract": CONTRACT, "command": command_id, "ok": ok,
                      "exitCode": exit_code}
        body["data" if ok else "error"] = payload
        return json.dumps(body, separators=(",", ":") if compact else None, indent=None if compact else 2,
                          ensure_ascii=False) + "\n"

    def render_text(self, command: dict, data: Any) -> str:
        renderer = self.text.get(command["id"])
        if renderer is not None:
            return renderer(data)
        if command["id"] == "help":
            return data["text"]
        if command["id"] == "completion":
            return data["script"]
        if command["id"] == "version":
            return f"{self.program} {self.version}\n"
        if command["outputMode"] == "json-records":
            return record_text(data.get("records", []))
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _colored(text: str, color: bool) -> str:
        return f"\033[31m{text}\033[0m" if color else text

    @staticmethod
    def _failure_color(invocation: Optional["Invocation"], argv: list) -> bool:
        """color_stderr of a resolved Invocation; before a command resolves, only an
        explicit --color never is trusted (maelys_cli_run()'s prescan), never an
        unresolved --color always."""
        if invocation is not None:
            return invocation.color_stderr
        mode = "never" if _prescan_color_never(argv) else "auto"
        return terminal_color(mode)[1]

    def main(self, argv: Optional[list] = None) -> int:
        argv = list(sys.argv[1:] if argv is None else argv)
        command_id = ""
        self.resolved_command_id = ""
        invocation: Optional[Invocation] = None
        fmt = environment_format()
        for index, word in enumerate(argv):
            if word in ("--json", "--format=json", "--format=jsonl"):
                fmt = "json"
            elif word == "--format" and index + 1 < len(argv) and argv[index + 1] in ("json", "jsonl"):
                fmt = "json"
        compact = "--compact" in argv or "--pretty=false" in argv
        try:
            invocation, command = self.parse(argv)
            command_id, fmt, compact = command["id"], invocation.format, invocation.compact
            if command["outputMode"] == "protocol-stream":
                result = command["handler"](invocation)
                return int(result[1] if isinstance(result, tuple) else result)
            if invocation.field is not None:
                # The refusals --field brings that the parser could not make,
                # decided on the catalog and before the handler (spec 2.9,
                # section 5; C: field_refused): a caller that reads
                # VALIDATION_FAILED concludes that nothing changed.
                if fmt == "json":
                    # Reached only via an environment MAELYS_CLI_FORMAT=json;
                    # an explicit --format json was refused by the parser.
                    raise Failure("VALIDATION_FAILED",
                                  "--field conflicts with --format json: a filtered envelope "
                                  "would not validate against the command's outputSchema.",
                                  "Use --format text or --format jsonl with --field.")
                if command["effect"] != "read":
                    # A command that can write -- a transaction, with or
                    # without --apply, or an execute -- accepts only a member
                    # its output schema requires: one it leaves optional is
                    # refused even when this run would have carried it.
                    required = command["outputSchema"].get("required")
                    if not isinstance(required, list) or not required:
                        raise Failure("VALIDATION_FAILED",
                                      f"Option --field is not accepted by '{command_id}': the command can "
                                      "write and its output schema requires no member.",
                                      "Run the command without --field and read the member from its result.")
                    if invocation.field not in required:
                        raise Failure("VALIDATION_FAILED",
                                      f"Option --field names '{invocation.field}', which '{command_id}' does "
                                      "not always return: a command that can write accepts only a member "
                                      "its output schema requires.",
                                      "Use a member the command's output schema requires; describe lists them.")
            data, exit_code = command["handler"](invocation)
            invocation.progress_done()
            if invocation.options.get("--expect") is not None and not invocation._expect_checked \
                    and isinstance(command["effect"], dict):
                # --expect was given and the handler answers without having
                # asked invocation.expect(): the caller would believe a
                # binding nobody checked (C: succeed_with).
                raise Failure("UNEXPECTED",
                              f"Command '{command_id}' answered without checking --expect: what it did is "
                              "not known to be the plan the fingerprint names.",
                              "Report this defect to the command implementation.")
            if invocation.field is not None:
                # A name absent from data is discoverable only now, once the
                # handler has run.
                if invocation.field not in data:
                    raise Failure("VALIDATION_FAILED",
                                  f"Option --field names '{invocation.field}', which "
                                  f"'{command_id}' does not have.",
                                  "Use a top-level member of the command's data.")
                value = data[invocation.field]
                if fmt == "jsonl":
                    sys.stdout.write(field_jsonl(value))
                else:
                    text = field_text(value)
                    if not page_text(text, invocation):
                        sys.stdout.write(text)
            elif fmt == "text":
                text = self.render_text(command, data)
                if not page_text(text, invocation):
                    sys.stdout.write(text)
            elif fmt == "jsonl":
                for record in data.get("records", []):
                    sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
            else:
                sys.stdout.write(self.envelope(command_id, True, exit_code, data, compact))
            sys.stdout.flush()
            return exit_code
        except Failure as failure:
            error: dict = {"code": failure.code, "message": failure.message}
            if failure.hint:
                error["hint"] = failure.hint
            if failure.issues:
                error["issues"] = failure.issues
            if fmt == "text":
                sys.stderr.write(self._colored(
                    f"{self.program}: [{failure.code}] {_terminal_safe(failure.message)}",
                    self._failure_color(invocation, argv)) + "\n")
                if failure.hint:
                    sys.stderr.write(f"Hint: {_terminal_safe(failure.hint)}\n")
            else:
                sys.stderr.write(self.envelope(command_id or self.resolved_command_id or "unknown", False,
                                               EXIT_FAILURE, error, compact))
            return EXIT_FAILURE
        except OSError as error:
            # An OSError a handler did not convert: same table as maelys_cli_fail_file().
            failure = file_failure(error)
            payload = {"code": failure.code, "message": failure.message, "hint": failure.hint}
            if fmt == "text":
                sys.stderr.write(self._colored(
                    f"{self.program}: [{failure.code}] {_terminal_safe(failure.message)}",
                    self._failure_color(invocation, argv)) + "\n")
                sys.stderr.write(f"Hint: {_terminal_safe(failure.hint)}\n")
            else:
                sys.stderr.write(self.envelope(command_id or self.resolved_command_id or "unknown", False,
                                               EXIT_FAILURE, payload, compact))
            return EXIT_FAILURE
