# SPDX-License-Identifier: MPL-2.0
"""What the tracked files of a repository say about the machine and the person behind them.

Six families, each read by the shape of a value and never by a substring: a
sieve on `.home` reported three struct-field accesses on a clean product and
would have been switched off the same day.

    machine     this machine's own name, known only here and only before a push
    host        a home-network host: NAME.home, NAME.lan, NAME.local, NAME.home.arpa
    path        /Users/NAME, /home/NAME, C:\\Users\\NAME
    identifier  a UUID or a long identifier under an identity key
    email       an address this repository does not publish itself
    term        a line of the author's own list, which lives outside every repository

Four rules hold the module together, and each is the reverse of a defect a
privacy check is prone to:

- **No output shows what a family matched.** A check that copies what it
  found into a CI log publishes it a second time. A finding carries its value
  in memory, for the comparisons that need it, and leaves the module without
  it. A key and the name of a file are values too: each is examined by every
  family before it is shown, a match is a finding of its own, and it is
  shown by its position. And before anything leaves, every string that is
  about to is compared with every value found.
- **What was not read is said.** Archives, images, binary files, files above
  the size bound, submodules; a file named as JSON that did not parse and
  was read a line at a time; a file cut short because it held too much.
- **Linear, and bounded.** `check` runs on every pull request. git names the
  files that hold a shape, in one process, and only those are opened; a
  file is searched before it is parsed; a file that holds more than a
  bounded number of candidates is read up to there and said to be.
- **Nothing here is about secrets.** A token or a key is GitHub's secret
  scanning and push protection, which see every push and know the formats.
"""
from __future__ import annotations

import bisect
import json
import os
import pathlib
import re
import stat

from . import host
from .constants import DECLARATION_FILE

FAMILIES = ("machine", "host", "path", "identifier", "email", "term")
SAYS = {
    "machine": "this machine's name",
    "host": "a home-network host name",
    "path": "a personal path",
    "identifier": "an installation or account identifier",
    "email": "an e-mail address this repository does not publish",
    "term": "a term of your private list",
}
# Above this a file is not read, and is reported as such. Measured on the
# forty-four repositories of the fleet: one text file is above two mebibytes,
# and it is this repository's own recorded reference, under three.
MAX_BYTES = 4 * 1024 * 1024
# What the first bytes say, as git says it: a NUL is not text.
SNIFF = 8000
# One file and one family: the findings kept, and the candidates looked at.
# Beyond either, the file is said to be read in part. A log of eighty
# thousand personal paths names its file as well with a hundred lines as
# with all of them, and no input can make the reading longer than its size.
KEPT = 100
EXAMINED = 20000

ARCHIVES = frozenset((
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2", ".xz", ".txz", ".zst", ".7z", ".rar", ".jar", ".war",
    ".whl", ".egg", ".deb", ".rpm", ".dmg", ".pkg", ".iso", ".cab", ".apk", ".ipa", ".crate", ".gem", ".nupkg",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".epub"))
IMAGES = frozenset((
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".ico", ".icns", ".psd",
    ".avif", ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".pdf"))
BINARIES = frozenset((
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".wasm", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".class", ".pyc",
    ".sqlite", ".sqlite3", ".db", ".bin", ".dat", ".ds_store"))
UNREAD = {
    "archive": "archive", "image": "image, media or document whose metadata is not read", "binary": "binary",
    "size": f"above {MAX_BYTES // (1024 * 1024)} MiB", "submodule": "submodule", "absent": "absent from the working tree",
    "undecodable": "under a name that is not UTF-8",
}

# ---- which file is read how ---------------------------------------------------
#
# A file is a document by its name. The value families -- a host, an
# identifier -- are read in documents only; in any other text only the shapes
# that cannot be code are looked for. One list, read twice: by git, to name
# the candidates, and here, to say how each is read.

JSON_SUFFIXES = (".json", ".jsonl", ".ndjson", ".jsonc", ".json5", ".geojson", ".har", ".ipynb", ".webmanifest",
                 ".sarif")
LINE_JSON = (".jsonl", ".ndjson")
ASSIGNED_SUFFIXES = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties")
LOG_SUFFIXES = (".log", ".txt", ".out")
DOCUMENT_SUFFIXES = JSON_SUFFIXES + ASSIGNED_SUFFIXES + LOG_SUFFIXES


def kind(path: str) -> str:
    """How PATH is read: `json`, `assigned`, `log` or `text`."""
    name = pathlib.PurePosixPath(path).name.lower()
    if name.endswith(JSON_SUFFIXES):
        return "json"
    if name.endswith(ASSIGNED_SUFFIXES) or name.startswith(".env"):
        return "assigned"
    if name.endswith(LOG_SUFFIXES):
        return "log"
    return "text"


# ---- the shapes ---------------------------------------------------------------

LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOME_SUFFIX = r"(?:home\.arpa|home|lan|local)"
# A host as a whole token, wherever a value holds it: alone, in a URL, in a
# share, in a list, in a sentence. Never the tail of a longer name.
HOST_TOKEN = re.compile(rf"(?<![A-Za-z0-9_.-])((?:{LABEL}\.)+{HOME_SUFFIX})(?![A-Za-z0-9_-]|\.[A-Za-z0-9])",
                        re.IGNORECASE)
# The labels nobody's network carries: reserved for documentation, or the
# name every machine gives itself. `cluster.local` is Kubernetes' own domain.
PUBLIC_LABELS = frozenset(("example", "test", "invalid", "localhost"))
PUBLIC_HOST_SUFFIXES = (".cluster.local",)
# `user.home` is a property of the Java runtime and `settings.local` a file:
# a first label that is a word programs use for themselves, before one of the
# three suffixes and nothing else, names no machine. A list, measured on
# string values of public YAML and JSON, and it stays one.
CODE_LABELS = frozenset((
    "user", "self", "this", "args", "arg", "argv", "opts", "options", "os", "env", "environ", "process", "config",
    "conf", "cfg", "settings", "setting", "application", "app", "tsconfig", "jsconfig", "appsettings", "local",
    "system", "sys", "java", "path", "paths", "dir", "dirs", "props", "properties", "state", "ctx", "context",
    "params", "param", "request", "req", "response", "res", "data", "obj", "object", "item", "value", "values",
    "page", "pages", "view", "views", "route", "router", "link", "menu", "tab", "nav", "index", "main", "default",
    "compose", "docker-compose", "development", "dev", "production", "prod", "staging", "tests", "site", "home",
    "window", "document", "location", "module", "package", "project", "build", "node", "go", "is", "to", "at"))

NAME = r"([A-Za-z0-9_][A-Za-z0-9._-]*)"
# A home under a root, or under the roots a second system mounts the first
# at. Preceded by nothing that makes it the tail of a longer path or of a
# URL: `https://site.example/home/page` and `$ROOT/home/x` are not homes.
PERSONAL_PATH = re.compile(
    r"(?<![A-Za-z0-9_.~$})\]-])"
    r"(?:/mnt/[A-Za-z]|/System/Volumes/Data|/Volumes/[^/\s\"'\\]{1,64}|/private/var|/var|/host|/rootfs|/sysroot)?"
    r"\\?/(?:Users|home)\\?/" + NAME
    + r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}[Uu]sers[\\/]{1,2}" + NAME)
# Accounts a system creates, and the words a document writes where a name
# goes. Not one first name: a name invented for a fixture is declared by the
# repository that invents it, `fabricated NAME`, where a reader can see it.
SYSTEM_ACCOUNTS = frozenset((
    "shared", "public", "default", "guest", "runner", "runneradmin", "root", "ubuntu", "debian", "linuxbrew",
    "vscode", "codespace", "gitpod", "node", "nobody", "nonroot", "git", "www-data", "www", "ec2-user", "vagrant",
    "docker", "circleci", "jenkins", "travis", "buildkite-agent", "jovyan", "app", "appuser", "build", "builder",
    "admin", "deploy", "nginx", "apache", "postgres", "mysql", "redis", "web", "service", "worker", "agent",
    "user", "username", "user_name", "name", "you", "me", "yourname", "your-name", "your_name", "youruser",
    "your_user", "yourusername", "your_username", "your-username", "myuser", "logname", "someone", "somebody",
    "example", "foo", "bar", "test", "tester", "dev", "developer", "ci", "work", "site", "project", "xxx", "xxxx",
    "synthetic", "fake", "dummy", "sample", "fixture", "mock", "placeholder", "redacted", "anonymous"))

# One definition of an identity key, read three times: by git to name the
# candidates, by the search that precedes a parse, and by the rule itself.
# A name of the six, then at most three characters that are neither a letter
# nor a digit, then `id` or `ids`, at the end of the key: `installationId`,
# `device_id`, `User ID`, `user.id`, `accountIds`, `githubUserId`.
STEMS = ("installation", "device", "machine", "account", "user", "organization", "organisation")
STEM = "(?:" + "|".join(STEMS) + ")"
IDENTITY_KEY = re.compile(STEM + r"[^a-z0-9]{0,3}ids?\Z")
# And the object that carries one: `{"installation": {"id": ...}}`.
IDENTITY_PARENTS = frozenset(STEMS) | frozenset(stem + "s" for stem in STEMS)
UUID = re.compile(r"(?:urn:uuid:)?\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?")
UUID_INSIDE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                         r"(?![0-9a-fA-F])")
LONG_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/=-]{15,}")
# What public documentation prints where a UUID goes: RFC 4122's own, and the
# two an encyclopedia made the default of every tutorial.
DOCUMENTED_UUIDS = frozenset(("f81d4fae-7dec-11d0-a765-00a0c91e6bf6", "123e4567-e89b-12d3-a456-426614174000",
                              "550e8400-e29b-41d4-a716-446655440000"))
# An identifier that says of itself it was invented.
SYNTHETIC_WORDS = ("test", "example", "sample", "fake", "dummy", "fixture", "mock", "placeholder", "redacted",
                   "synthetic", "anonym", "xxxx", "deadbeef", "cafebabe", "feedface")
ORDER = "0123456789abcdefghijklmnopqrstuvwxyz"
RANK = {symbol: index for index, symbol in enumerate(ORDER)}
WRAPS = (("9", "0"), ("f", "0"), ("z", "a"))
# A key that is not shown, whatever it is: it holds what an identifier is made of.
OPAQUE = re.compile(r"[0-9a-fA-F]{16,}|[0-9]{6,}")

# The local part is bounded and starts where a word starts, so that a file of
# any size is read once.
ADDRESS = re.compile(rf"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{{0,63}})@((?:{LABEL}\.)+([A-Za-z]{{2,24}}))"
                     r"(?![A-Za-z0-9-])")
RESERVED_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")
RESERVED_SUFFIXES = (".example", ".invalid", ".test", ".localhost")
# What ends an address. Two letters are a country's, and the rest is a list:
# `W@x.data` is a matrix product, `getty@tty1.service` a unit, `icon@2x.png`
# a file, and a domain that ends like none of these is none of them. An
# address under a newer ending is missed, which the conventions say.
GENERIC_ENDINGS = frozenset((
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "name", "pro", "dev", "app", "xyz", "online",
    "site", "tech", "cloud", "email", "mail", "live", "life", "world", "today", "news", "blog", "shop", "store",
    "studio", "design", "agency", "digital", "solutions", "systems", "software", "services", "consulting",
    "network", "works", "tools", "team", "group", "company", "global", "academy", "university", "science",
    "education", "health", "care", "law", "legal", "finance", "bank", "social", "media", "games", "art", "page",
    "one", "top", "club", "link", "help", "support", "zone", "space", "codes", "engineering", "technology",
    "computer", "security", "wiki", "museum", "travel", "jobs", "mobi", "coop", "aero", "post", "africa", "asia",
    "cat", "nyc", "london", "paris", "berlin", "tokyo"))
# Two letters that end a file of code more often than an address.
CODE_ENDINGS = frozenset(("py", "rs", "sh", "md", "cc", "pl", "pm", "ps", "so", "ml", "tf", "mk", "mm", "sv", "st"))
NOTICE = re.compile(r"copyright|\(c\)|©|author|maintainer|contributor|signed-off-by|co-authored-by|reviewed-by",
                    re.IGNORECASE)
# Where a repository publishes an address itself, in the places GitHub reads them from.
PUBLISHING = re.compile(r"(?:\.github/|docs/)?(?:LICENSING|SECURITY)\.md\Z")
# An allowed-signers file under whatever name a repository gives it: a
# product of the fleet keeps its own as `.github/release-allowed-signers`.
SIGNERS = re.compile(r"(?:^|/)(?:[A-Za-z0-9._-]*[-_.])?allowed[-_]signers$")

# A name every machine of its kind carries says nothing of this one.
GENERIC_MACHINES = frozenset((
    "localhost", "macbook", "macbookpro", "macbookair", "macmini", "macstudio", "macpro", "imac", "ubuntu", "debian",
    "fedora", "raspberrypi", "desktop", "laptop", "server", "runner", "workstation", "computer"))
SPECIFIC = 6
SOFT = "-_. '’"
SEPARATORS = r"[-_. '’]?"

BLANK = re.compile(r"\s*")


def documents(text: str, start: int = 0) -> list:
    """The JSON documents TEXT holds from START, one after another; ValueError when anything else is there."""
    decoder, found = json.JSONDecoder(), []
    at = BLANK.match(text, start).end()
    while at < len(text):
        parsed, at = decoder.raw_decode(text, at)
        found.append(parsed)
        at = BLANK.match(text, at).end()
    return found


ASSIGNMENT = re.compile(r"""^[ \t]*(?:-[ \t]+)?(?:export[ \t]+)?(["']?)([^\s"':=#\[\]{}][^"':=\n]{0,200}?)\1[ \t]*[:=][ \t]*(.*)$""")
ITEM = re.compile(r"^[ \t]*-[ \t]+(.+)$")
TABLE = re.compile(r"^[ \t]*\[+[ \t]*([^\]\n]+?)[ \t]*\]+[ \t]*(?:#.*)?$")
OPENS = re.compile(r"""^([ \t]*)(?:-[ \t]+)?(["']?)([^\s"':=#\[\]{}][^"':=\n]{0,200}?)\2[ \t]*[:=][ \t]*[|>{\[]?-?[ \t]*$""")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')
SHOWN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
NOT_SHOWN = "(not shown)"

# What makes a line worth reading, searched before anything is parsed. The
# same shapes as the patterns git is given, in this dialect.
LINE_HINT = re.compile(
    r"[/\\](?:Users|home)[/\\]|[\\/]users[\\/]|[A-Za-z0-9._%+-]@[A-Za-z0-9]|\.(?:home|lan|local|arpa)(?![A-Za-z0-9_])"
    r"|" + STEM + r"[^A-Za-z0-9\n]{0,3}ids?|" + STEM + r"s?[\"']?[ \t]*[:=\]]"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
TOKEN_HINT = re.compile(r"[/\\](?:Users|home)[/\\]|[\\/]users[\\/]|[A-Za-z0-9._%+-]@[A-Za-z0-9]", re.IGNORECASE)
# The same, one shape at a time and for a lowered text: five searches that
# each know their first character are four times faster than one that does not.
# The first two are the shapes of a token, looked for in every file; the
# others are the shapes of a value and of a key, looked for in a document.
LOW_HINTS = (re.compile(r"[/\\](?:users|home)[/\\]"), re.compile(r"[a-z0-9._%+-]@[a-z0-9]"),
             re.compile(r"\.(?:home|lan|local|arpa)(?![a-z0-9_])"), re.compile(STEM + r"[^a-z0-9\n]{0,3}ids?"),
             re.compile(STEM + r"s?[\"']?[ \t]*[:=\]]"),
             re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"))
VALUE_HINT = re.compile(
    r"[/\\](?:Users|home)[/\\]|[\\/]users[\\/]|[A-Za-z0-9._%+-]@[A-Za-z0-9]|\.(?:home|lan|local|arpa)(?![A-Za-z0-9_])",
    re.IGNORECASE)
NAME_HINT = re.compile(
    r"(?:^|/)(?:Users|home)/|@|\.(?:home|lan|local|arpa)(?![A-Za-z0-9_])|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", re.IGNORECASE)

# The same shapes for git, which reads every tracked file in one process and
# names those that hold one. POSIX extended, matched without regard to case.
# A superset of the rules, never a subset: what it lets through is judged
# here, and what it stops would never have been found.
GIT_TOKENS = (r"[/\\](users|home)[/\\]", r"[[:alnum:]._%+-]@[[:alnum:]]([[:alnum:]-]*[[:alnum:]])?\.[[:alpha:]]")
GIT_VALUES = (r"\.(home|lan|local|arpa)([^[:alnum:]_]|$)",
              "(" + "|".join(STEMS) + r")[^[:alnum:]]{0,3}ids?",
              "(" + "|".join(STEMS) + r")s?[\"']?[[:space:]]*[]:=]",
              "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GIT_DOCUMENTS = tuple(f":(icase)*{suffix}" for suffix in DOCUMENT_SUFFIXES) + (":(icase).env*", ":(icase)*/.env*")
# What is not read for its name is not searched either: an archive of a
# hundred megabytes is a hundred megabytes of nothing git can use.
GIT_UNREAD = tuple(f":(exclude,icase)*{suffix}" for suffix in sorted(ARCHIVES | IMAGES | BINARIES))


# ---- what a repository declares -----------------------------------------------

ENCODED = re.compile(r"[A-Za-z0-9+/=_-]{20,}\Z")


def looks_encoded(token: str) -> bool:
    """Whether TOKEN could be a digest or an encoding of a value, by its shape alone.

    Sixteen hexadecimal digits in a row anywhere in it, whatever precedes
    them -- `md5:`, `sha256=`, a path; or twenty characters and more of the
    alphabets base64 uses, holding a digit, a capital and a small letter.
    That is all this reads. A reason made of words can still say anything,
    and no parser will know: the rule is for the habit, not against intent.
    """
    if re.search(r"[0-9a-fA-F]{16,}", token):
        return True
    return bool(ENCODED.match(token)) and any(c.isdigit() for c in token) \
        and any(c.isupper() for c in token) and any(c.islower() for c in token)


class Excuse:
    """One `except` line: a file, a family, a key when it names one, and why."""

    def __init__(self, number: int, pattern: str, family: str, key: "str | None", reason: str) -> None:
        self.number, self.pattern, self.family, self.key, self.reason = number, pattern, family, key, reason
        self.used = 0
        if pattern.endswith("/"):
            self.matches = lambda path: path.startswith(pattern)
        elif "*" in pattern:
            # One star, one component: it never crosses a `/`, and the
            # pattern is the whole path.
            whole = re.compile("[^/]*".join(re.escape(part) for part in pattern.split("*")) + r"\Z")
            self.matches = lambda path: bool(whole.match(path))
        else:
            self.matches = lambda path: path == pattern

    def covers(self, finding: dict) -> bool:
        if finding["family"] != self.family or not self.matches(finding["real"]):
            return False
        return self.key is None or (finding.get("in") is None and finding.get("key") == self.key)


class Settings:
    """`[privacy]` of maelys-release.conf: what refuses, what is invented, what stays."""

    def __init__(self) -> None:
        self.refuses: tuple[str, ...] = ()
        self.fabricated: dict[str, int] = {}
        self.excuses: list[Excuse] = []
        # The lines the grammar refused, by number and never by content.
        self.refused: list[str] = []


def parse_line(line: str, number: int, settings: "Settings | None" = None) -> None:
    """One line of `[privacy]`, refused by its number when it is not one of the three.

    The grammar of the file it lives in: a word, then what the word takes.

        refuse [FAMILY...]                           every family, or the ones named
        fabricated NAME...                           names invented for fixtures
        except PATH family FAMILY REASON             what stays, and why
        except PATH family FAMILY key KEY REASON

    PATH is a tracked path, a directory when it ends with `/`, or a path
    holding `*`, which stands for any run of characters inside one
    component. Nothing else is a pattern, and the whole path is compared.

    A refusal names the line and never quotes it: the line a repository got
    wrong may be the value it meant to keep out.
    """
    word, *rest = line.split()
    if word == "refuse":
        if any(name not in FAMILIES for name in rest):
            raise ValueError(f"line {number}: refuse takes nothing, or families among {', '.join(FAMILIES)}")
        if settings is not None:
            if settings.refuses:
                raise ValueError(f"line {number}: a second refuse line")
            settings.refuses = tuple(name for name in FAMILIES if name in rest) if rest else FAMILIES
    elif word == "fabricated":
        if not rest:
            raise ValueError(f"line {number}: fabricated names nothing")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]*", name) for name in rest):
            raise ValueError(f"line {number}: a fabricated name is made of [A-Za-z0-9._@+-]")
        if settings is not None:
            for name in rest:
                settings.fabricated.setdefault(name.lower(), number)
    elif word == "except":
        if len(rest) < 3 or rest[1] != "family" or rest[2] not in FAMILIES:
            raise ValueError(f"line {number}: except takes 'PATH family FAMILY REASON' or 'PATH family FAMILY key KEY"
                             f" REASON', with a family among {', '.join(FAMILIES)}")
        pattern, family, reason, key = rest[0], rest[2], rest[3:], None
        if reason[:1] == ["key"]:
            if len(reason) < 2 or not SHOWN_KEY.match(reason[1]):
                raise ValueError(f"line {number}: except names its key after the word key, made of [A-Za-z0-9_.-]")
            key, reason = reason[1], reason[2:]
        if pattern.startswith("/") or ".." in pattern.split("/") or "**" in pattern:
            raise ValueError(f"line {number}: except names a path inside the repository, and `*` stands for one"
                             " component at most")
        if not re.search(r"[^*./]", pattern):
            raise ValueError(f"line {number}: except names a file or a directory, not every one of them")
        if not reason:
            raise ValueError(f"line {number}: except gives its reason on the same line, and gives none")
        if any(looks_encoded(token) for token in rest):
            raise ValueError(f"line {number}: except designates a file and a family and holds nothing that looks like"
                             " a digest or an encoding of a value: the digest of a host name is a dictionary away"
                             " from the host name")
        if settings is not None:
            settings.excuses.append(Excuse(number, pattern, family, key, " ".join(reason)))
    else:
        raise ValueError(f"line {number}: neither refuse, fabricated nor except")


def declared(project: pathlib.Path) -> Settings:
    """`[privacy]` as the guard reads it, and the only place it is judged.

    parse_release names the section and reads none of its lines: a line the
    grammar refuses is said here, by the guard, in the scope every
    repository hears -- whatever publishes it.
    """
    settings = Settings()
    release = project / DECLARATION_FILE
    if not release.is_file():
        return settings
    section = ""
    for number, raw in enumerate(release.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
        elif section == "[privacy]":
            try:
                parse_line(line, number, settings)
            except ValueError as error:
                settings.refused.append(str(error))
    return settings


# ---- what only this machine knows ---------------------------------------------

def words(name: str) -> list[str]:
    """The runs of letters and digits of NAME, lowered: what is left when its separators go."""
    return re.findall(r"[^\W_]+", name.lower())


def spelled(name: str) -> str:
    return "".join(words(name))


def spelling(name: str) -> "tuple[str, str, str] | None":
    """(the longest word, the pattern, the pattern for git) NAME is looked for by; None when it has no word.

    The first pattern reads a lowered text. The second is what git is given
    to name the candidates: POSIX extended, and looser on purpose -- three
    characters of anything where the first has one soft separator, since a
    typographic apostrophe is three bytes to a git that reads bytes.

    Its words in order. Between two of them, what the name itself has: one of
    the soft separators, or nothing, where it has a hyphen, a space, a dot or
    nothing at all -- the same machine is written with and without them --
    and the very characters, with or without spaces around, where it has
    anything else. `r&d` is found as `R & D` and never as `rd`.
    """
    parts = re.findall(r"[^\W_]+|[\W_]+", name.lower().strip())
    found = [part for part in parts if re.match(r"[^\W_]", part)]
    if not found:
        return None
    pattern, posix = r"(?<![a-z0-9])", ""
    for index, part in enumerate(parts):
        if re.match(r"[^\W_]", part):
            pattern += re.escape(part)
            posix += part
        elif 0 < index < len(parts) - 1:
            gap = part.strip()
            soft = all(symbol in SOFT for symbol in part)
            pattern += SEPARATORS if soft else r"\s*" + re.escape(gap) + r"\s*"
            posix += ".{0,3}" if soft else "[[:space:]]*" + re.sub(r"([].[()*+?{}|^$\\])", r"\\\1", gap) + "[[:space:]]*"
    return max(found, key=len), pattern + r"(?![a-z0-9])", posix


class Local:
    """This machine's names and the author's own terms: read here, published nowhere.

    A name is looked for by its words, in order, whatever separates them and
    whatever their case: the machine `hostname` calls Atelier-Pro is written
    atelier-pro in one file, AtelierPro in another and "Atelier Pro" in the
    settings. It is looked for as a whole token, never inside a longer word.
    """

    def __init__(self, machines: "list[str] | None" = None, terms: "list[tuple[int, str]] | None" = None,
                 asked: bool = True, runner: bool = False) -> None:
        # The spellings of one machine collapse to one, the one with the most
        # words: `hostname -s` and scutil's LocalHostName usually differ by
        # nothing a reader sees, and the pattern of "atelier pro" finds
        # "atelierpro" as well.
        kept: dict[str, str] = {}
        for name in machines or []:
            flat = spelled(name)
            if len(flat) >= SPECIFIC and flat not in GENERIC_MACHINES and len(words(name)) > len(words(kept.get(flat, ""))):
                kept[flat] = name
        self.machines = sorted(kept)
        self.terms = [(number, term) for number, term in terms or [] if len(spelled(term)) >= 3]
        self.asked, self.runner = asked, runner
        # (family, line of the list, the longest word, the pattern): the word
        # is searched first, at the speed of a search, and the pattern only
        # where the word is.
        self.names: list[tuple[str, "int | None", str, "re.Pattern[str]"]] = []
        self.posix: list[str] = []
        for family, number, name in ([("machine", None, kept[flat]) for flat in self.machines]
                                     + [("term", number, term) for number, term in self.terms]):
            anchor, pattern, posix = spelling(name) or ("", "", "")
            if anchor:
                self.names.append((family, number, anchor, re.compile(pattern)))
                self.posix.append(posix)

    def find(self, low: str) -> "list[tuple[str, int | None, int, str]]":
        """(family, line of the list, offset, what matched) for every name in LOW, a lowered text."""
        found = []
        for family, number, anchor, pattern in self.names:
            if anchor in low:
                found.extend((family, number, match.start(), match.group(0)) for match in pattern.finditer(low))
        return found

    def says(self) -> str:
        if self.runner:
            return "this machine's name and private terms are not looked for on a CI runner, which has neither"
        if self.machines:
            machine = "this machine's name was looked for"
        elif not self.asked:
            machine = "this machine's name was not looked for (no hostname command here)"
        else:
            machine = "this machine's name was not looked for (too short or too common to be specific)"
        terms = f", and {len(self.terms)} term(s) of your private list" if self.terms else ""
        return machine + terms


def read_local() -> Local:
    """The names `hostname` and, on macOS, `scutil` give this machine; the author's list.

    Both are local by nature. A CI runner has a name too, and it is nobody's:
    looked for there, a runner named after a word of the tree would find it,
    and a repository that refuses the family would be refused for it. So
    where the host says it is a runner neither is read, and it is said.
    """
    if host.HOST.is_runner():
        return Local(runner=True)
    machines: list[str] = []
    asked = False
    for tool, queries in (("hostname", (["-s"],)), ("scutil", (["--get", "LocalHostName"], ["--get", "ComputerName"]))):
        if not host.HOST.which(tool):
            continue
        asked = True
        for arguments in queries:
            completed = host.HOST.run([tool, *arguments])
            name = completed.stdout.strip() if completed.returncode == 0 else ""
            if name:
                machines.append(name.split(".")[0] if tool == "hostname" else name)
    terms: list[tuple[int, str]] = []
    path = host.HOST.private_terms()
    if path is not None and path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if line and not line.startswith("#"):
                terms.append((number, line))
    return Local(machines, terms, asked=asked)


# ---- identifiers --------------------------------------------------------------

KNOWN_KEYS: dict[str, bool] = {}


def flat(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def identity_key(key: object) -> bool:
    """Whether KEY names an installation, a device, a machine, an account, a user or an organization."""
    if not isinstance(key, str):
        return False
    known = KNOWN_KEYS.get(key)
    if known is None:
        known = KNOWN_KEYS[key] = bool(IDENTITY_KEY.search(key.lower()))
    return known


def identity_of(parent: object, key: object) -> bool:
    """Whether KEY under PARENT carries an identity: an identity key, or `id` in an object named for one."""
    if identity_key(key):
        return True
    return isinstance(key, str) and isinstance(parent, str) and flat(key) == "id" and flat(parent) in IDENTITY_PARENTS


def near(one: str, other: str) -> bool:
    if (one, other) in WRAPS or (other, one) in WRAPS:
        return True
    return abs(RANK.get(one, -9) - RANK.get(other, -99)) <= 1


def synthetic(value: str) -> bool:
    """Whether an identifier was visibly made up: a precise rule, measured both ways.

    It says so (`test`, `example`, `fake`…), or public documentation prints
    it. Otherwise by what it is made of.

    A number is made up when it holds two different digits at most, or when
    each digit follows the one before it, up or down: 100000, 424242, 123456,
    9876543. Of the numbers of six digits 2 538 are, one in 355, each of
    them tried; of seven digits one in 1 900, of eight one in 8 500, of
    nine and more fewer than one in 50 000.

    Anything else -- a UUID, a token -- is made up when at least four of
    every five of its symbols equal, follow or precede, in the order 0-9a-z,
    the symbol one or two places before: the nil UUID, `aaaaaaaa-bbbb-…`,
    `12345678-1234-…`, the `a1b2c3d4-e5f6-7890-…` of every generated
    example. Of a million random UUIDs none is taken for one. Of random
    tokens of sixteen hexadecimal symbols one in 9 000 is, of twenty one in
    150 000, of twenty-four and more none in 300 000. A random token of
    letters and digits is taken for one about once in 10 000 to 40 000
    whatever its length, and by the first rule: it happens to spell `test`.
    """
    low = value.lower()
    if any(word in low for word in SYNTHETIC_WORDS):
        return True
    found = UUID.fullmatch(value.strip())
    if found and found.group(1).lower() in DOCUMENTED_UUIDS:
        return True
    symbols = [symbol for symbol in low if symbol in RANK]
    if len(symbols) < 2:
        return True
    if value.strip().isdigit():
        steps = {(int(symbols[index]) - int(symbols[index - 1])) % 10 for index in range(1, len(symbols))}
        return len(set(symbols)) <= 2 or steps in ({1}, {9})
    hits = sum(1 for index in range(1, len(symbols))
               if near(symbols[index], symbols[index - 1])
               or (index >= 2 and near(symbols[index], symbols[index - 2])))
    return hits * 5 >= (len(symbols) - 1) * 4


def identifier(value: object) -> bool:
    """A UUID, a token of sixteen characters or more holding a digit, a number of six digits or more."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        value = str(abs(value))
    if not isinstance(value, str):
        return False
    text = value.strip()
    if UUID.fullmatch(text):
        return not synthetic(text)
    if text.isdigit():
        return len(text) >= 6 and not synthetic(text)
    if LONG_IDENTIFIER.fullmatch(text) and any(symbol.isdigit() for symbol in text):
        return not synthetic(text)
    return False


# ---- one reading --------------------------------------------------------------

class Reading:
    """The findings of one pass, and everything a finding is judged against."""

    def __init__(self, settings: Settings, local: Local, published: "set[str] | None" = None) -> None:
        self.settings, self.local = settings, local
        self.published = published or set()
        self.findings: list[dict] = []
        self.seen: set[tuple] = set()
        # key -> (the key when it may be shown, the family it matched)
        self.keys: dict[str, "tuple[str | None, str | None, str]"] = {}
        self.domains: dict[str, str] = {}
        # What is being read: how a finding names it, what it really is,
        # and the blob or the commit it is, in a history.
        self.shown, self.real, self.context = "", "", {}
        self.leaves: set[tuple] = set()
        self.kept: dict[tuple, int] = {}
        self.partial: dict[tuple, None] = {}
        self.astext: dict[str, None] = {}

    def open(self, shown: str, real: "str | None" = None, **context: str) -> None:
        self.shown, self.real, self.context = shown, real or shown, context
        self.leaves = set()

    # -- the judgments ----------------------------------------------------------

    def fabricated(self, name: str) -> bool:
        return name.lower() in self.settings.fabricated

    def home_host(self, name: str) -> bool:
        """Whether NAME, a host that ends as a home network's does, is somebody's."""
        low = name.lower()
        labels = low.split(".")
        if PUBLIC_LABELS.intersection(labels) or low.endswith(PUBLIC_HOST_SUFFIXES):
            return False
        if len(labels) == 2 and labels[0] in CODE_LABELS:
            return False
        return not (self.fabricated(low) or self.fabricated(labels[0]))

    def personal(self, name: str) -> bool:
        """Whether NAME, after /Users/ or /home/, is somebody: not a letter, a system account, a placeholder or an invention."""
        return not (len(name) < 2 or name.lower() in SYSTEM_ACCOUNTS or self.fabricated(name))

    def domain(self, low: str, ending: str) -> str:
        """What a domain is: `home`, `reserved`, `none` when it ends like no domain, or `plain`."""
        verdict = self.domains.get(low)
        if verdict is None:
            if low.endswith((".home", ".lan", ".local", ".home.arpa")):
                verdict = "home"
            elif low in RESERVED_DOMAINS or low.endswith(RESERVED_SUFFIXES) \
                    or any(low.endswith("." + reserved) for reserved in RESERVED_DOMAINS):
                verdict = "reserved"
            elif not (ending in GENERIC_ENDINGS or (len(ending) == 2 and ending not in CODE_ENDINGS)):
                verdict = "none"
            elif self.fabricated(low) or "@" + low in self.published:
                verdict = "reserved"
            else:
                verdict = "plain"
            self.domains[low] = verdict
        return verdict

    def address(self, text: str, found: "re.Match[str]") -> "tuple[str, str] | None":
        """(family, address) for the address FOUND in TEXT, or None when it is nobody's."""
        low = found.group(2).lower()
        verdict = self.domain(low, found.group(3).lower())
        if verdict in ("reserved", "none"):
            return None
        name = found.group(1)
        whole = f"{name}@{low}".lower()
        # git@github.com:owner/name is a remote, and what stands before the
        # host of a URL -- scheme://user:password@host -- is not a
        # correspondent.
        if name == "git" and text[found.end():found.end() + 1] == ":":
            return None
        head = text[max(0, found.start() - 200):found.start()]
        if "://" in re.split(r"[\s\"'<(]", head)[-1]:
            return None
        if self.fabricated(whole):
            return None
        if verdict == "home":
            # What git writes when user.email is unset: the account, an at
            # sign, and the machine's name on its own network.
            return ("host", whole) if self.home_host(low) else None
        if re.search(r"no-?reply|do-?not-?reply", whole) or whole in self.published:
            return None
        return "email", whole

    def sensitive(self, text: str) -> "tuple[str, str] | None":
        """(family, what matched) when TEXT, a key or a name, would be a finding as a value."""
        if VALUE_HINT.search(text):
            for found in HOST_TOKEN.finditer(text):
                if self.home_host(found.group(1)):
                    return "host", found.group(1)
            for found in PERSONAL_PATH.finditer(text):
                name = (found.group(1) or found.group(2)).rstrip(".")
                if self.personal(name):
                    return "path", name
            for found in ADDRESS.finditer(text):
                judged = self.address(text, found)
                if judged:
                    return judged
        if self.local.names:
            for family, _, _, matched in self.local.find(text.lower()):
                return family, matched
        for found in UUID_INSIDE.finditer(text):
            if not synthetic(found.group(0)):
                return "identifier", found.group(0)
        return None

    def key(self, key: object) -> "tuple[str | None, str | None, str]":
        """(KEY when a message may name it, the family it matched as a value, what matched)."""
        if not isinstance(key, str):
            return None, None, ""
        known = self.keys.get(key)
        if known is None:
            matched = self.sensitive(key)
            shown = key if SHOWN_KEY.match(key) and not matched and not OPAQUE.search(key) else None
            known = self.keys[key] = (shown, matched[0] if matched else None, matched[1] if matched else "")
        return known

    # -- the findings -----------------------------------------------------------

    def add(self, family: str, value: str, **where: object) -> bool:
        """Keep one finding; False when this file holds too many of this family already."""
        finding = {"family": family, "path": self.shown, "real": self.real, "value": value, **self.context,
                   **{name: item for name, item in where.items() if item is not None}}
        # One value read by two readers is one finding.
        identity = (family, self.real, value.lower(), finding.get("line"), finding.get("in"), finding.get("blob"),
                    finding.get("commit"), finding.get("key") if "line" not in finding else None)
        if identity in self.seen:
            return True
        count = self.kept.get((self.real, family), 0)
        if count >= KEPT:
            self.partial[(self.shown, family)] = None
            return False
        self.kept[(self.real, family)] = count + 1
        self.seen.add(identity)
        self.findings.append(finding)
        return True

    def tokens(self, text: str, key: "str | None" = None, line: "int | None" = None, hosts: bool = False,
               routes: bool = False, where: "str | None" = None) -> None:
        """The whole-token shapes of TEXT: a path, an address, this machine, a private term.

        HOSTS says TEXT is a value of a document, where a host is one too.
        ROUTES says TEXT is a line of code or of prose, where `/home/WORD`
        closed by the quote that opened it is a route and not a home.
        """
        if VALUE_HINT.search(text):
            examined = 0
            for found in PERSONAL_PATH.finditer(text):
                examined += 1
                name = (found.group(1) or found.group(2)).rstrip(".")
                if not self.personal(name):
                    continue
                if routes and found.group(1) and text[found.start():found.start() + 6] == "/home/":
                    before, after = text[found.start() - 1:found.start()], text[found.end():found.end() + 2]
                    # The quote that opened it, or that quote escaped as the string around it requires.
                    if before and before in "\"'`" and (after[:1] == before or after == "\\" + before):
                        continue
                if examined > EXAMINED:
                    self.partial[(self.shown, "path")] = None
                    break
                if not self.add("path", name, key=key, line=line, **{"in": where}):
                    break
            if hosts:
                examined = 0
                for found in HOST_TOKEN.finditer(text):
                    examined += 1
                    if examined > EXAMINED:
                        self.partial[(self.shown, "host")] = None
                        break
                    if self.home_host(found.group(1)) \
                            and not self.add("host", found.group(1), key=key, line=line, **{"in": where}):
                        break
            if "@" in text:
                examined = 0
                for found in ADDRESS.finditer(text):
                    examined += 1
                    if examined > EXAMINED:
                        self.partial[(self.shown, "email")] = None
                        break
                    judged = self.address(text, found)
                    if not judged or (hosts and judged[0] == "host"):
                        continue
                    notice = None
                    if judged[0] == "email":
                        start = text.rfind("\n", 0, found.start()) + 1
                        end = text.find("\n", found.end())
                        notice = bool(NOTICE.search(text, start, end if end != -1 else len(text))) or None
                    if not self.add(judged[0], judged[1], key=key, line=line, notice=notice, **{"in": where}):
                        break
        if self.local.names:
            for family, number, _, matched in self.local.find(text.lower()):
                if not self.add(family, matched, key=key, line=line, term=number, **{"in": where}):
                    break

    def named(self, key: object, line: "int | None" = None) -> "str | None":
        """A key, examined as the value it may be; the key when a message may name it."""
        shown, family, matched = self.key(key)
        if family:
            self.add(family, matched, line=line, **{"in": "key"})
        return shown

    def value(self, parent: object, key: object, value: object, line: "int | None" = None, depth: int = 0) -> None:
        """One value under KEY, itself under PARENT: an identifier, and the tokens of a string."""
        if identity_of(parent, key) and identifier(value):
            shown = self.key(key)[0]
            if shown and not identity_key(key):
                above = self.key(parent)[0]
                shown = f"{above}.{shown}" if above else shown
            self.add("identifier", str(value), key=shown, line=line)
        if value.__class__ is not str or not value:
            return
        # A document says the same thing many times, and it is read once.
        leaf = (key, value, line)
        if leaf in self.leaves:
            return
        self.leaves.add(leaf)
        # A document may carry another as a string -- the body of a
        # request, kept as it was sent -- and that one is read as one too.
        if depth < 3 and value[0] in "{[" and len(value) > 1:
            stripped = value.strip()
            if stripped[-1] in "}]":
                try:
                    self.document(json.loads(stripped), line=line, depth=depth + 1)
                    return
                except (ValueError, RecursionError):
                    pass
        if VALUE_HINT.search(value) or self.local.names:
            self.tokens(value, key=self.key(key)[0], line=line, hosts=True)

    def document(self, document: object, line: "int | None" = None, depth: int = 0) -> None:
        """Every key and every value of a parsed JSON document."""
        stack: list[tuple[object, object, object]] = [(None, None, document)]
        while stack:
            parent, key, node = stack.pop()
            kind_of = node.__class__
            if kind_of is dict:
                for name, item in node.items():
                    if name not in self.keys:
                        self.key(name)
                    if self.keys[name][1]:
                        self.named(name, line)
                    stack.append((key, name, item))
            elif kind_of is list:
                if identity_of(parent, key):
                    stack.extend((parent, key, item) for item in node)
                else:
                    stack.extend((parent, key, item) for item in node if item.__class__ in (str, dict, list))
            else:
                self.value(parent, key, node, line=line, depth=depth)

    # -- one file ---------------------------------------------------------------

    def scalars(self, rest: str) -> list[str]:
        """The string scalars a value holds, as a line of YAML, TOML or JSON writes them."""
        rest = rest.strip().rstrip(",").strip()
        if rest[:1] in ("[", "{"):
            return [one or other for one, other in QUOTED.findall(rest)] or \
                [part.strip() for part in rest.strip("[]{}").split(",")]
        if rest[:1] in ('"', "'"):
            quoted = QUOTED.match(rest)
            return [quoted.group(1) or quoted.group(2) or ""] if quoted else [rest.strip("\"'")]
        # Up to a comment: the first `#` that follows a space, found once.
        cut = len(rest)
        for mark in (" #", "\t#"):
            at = rest.find(mark)
            if at != -1:
                cut = min(cut, at)
        return [rest[:cut].strip()]

    def above(self, lines: "Lines", number: int) -> "str | None":
        """The key the line NUMBER sits under: the nearest line above that opens a block and is indented less."""
        raw = lines.text(number)
        indent = len(raw) - len(raw.lstrip(" \t-"))
        # Lines are read in order: what was scanned for this indentation is
        # not scanned again, so that a file of list items is read once.
        scanned, parent = lines.parents.get(indent, (0, None))
        if number <= scanned:
            scanned, parent = 0, None
        for other in range(number - 1, max(scanned, number - 400, 0), -1):
            opened = OPENS.match(lines.text(other))
            if opened and len(opened.group(1)) < indent:
                parent = opened.group(3).strip()
                break
        else:
            if number - scanned > 400:
                parent = None
        lines.parents[indent] = (number, parent)
        return parent

    def line(self, raw: str, number: int, how: str, lines: "Lines") -> None:
        """One line that holds a shape, read for what the file is."""
        if how in ("json", "log"):
            start = 0 if raw.lstrip()[:1] in ("{", "[") else raw.find('{"')
            if start != -1:
                try:
                    for parsed in documents(raw, start):
                        self.document(parsed, line=number)
                    if start:
                        self.tokens(raw[:start], line=number)
                    return
                except (ValueError, RecursionError):
                    pass
            if how == "json":
                # Named as JSON, and this line is not: it is read as a line
                # of text, and the file is said to have been.
                self.astext[self.shown] = None
        if how == "text":
            self.tokens(raw, line=number, routes=True)
            return
        found = ASSIGNMENT.match(raw)
        key = found.group(2).strip() if found else None
        rest = found.group(3) if found else None
        if not found:
            item = ITEM.match(raw)
            if item:
                key, rest = self.above(lines, number), item.group(1)
        if rest is None:
            self.tokens(raw, line=number)
            table = TABLE.match(raw)
            if table and flat(table.group(1).split(".")[-1]) in IDENTITY_PARENTS:
                # `[installation]`, as TOML writes an object: its `id` is below.
                for other in range(number + 1, min(number + 200, len(lines.starts)) + 1):
                    below = lines.text(other)
                    if TABLE.match(below):
                        break
                    inside = ASSIGNMENT.match(below)
                    if inside and flat(inside.group(2)) == "id":
                        self.line(below, other, "assigned", lines)
            return
        opened = OPENS.match(raw)
        if opened and flat(opened.group(3)) in IDENTITY_PARENTS:
            # An object named for an identity: the `id` it holds is on a line
            # of its own, which nothing else would have brought here.
            for other in range(number + 1, min(number + 200, len(lines.starts)) + 1):
                below = lines.text(other)
                if not below.strip():
                    continue
                if len(below) - len(below.lstrip(" \t")) <= len(opened.group(1)) and below.strip() not in ("{", "["):
                    break
                inside = ASSIGNMENT.match(below)
                if inside and flat(inside.group(2)) == "id":
                    self.line(below, other, "assigned", lines)
        shown = self.named(key, number) if key is not None and found else self.key(key)[0]
        matched = found and self.key(key)[1]
        parent = None
        if key is not None and found and flat(key) == "id":
            parent = self.above(lines, number)
            # `[installation]` above `id = …`, as TOML writes an object.
            if parent is None:
                for other in range(number - 1, max(0, number - 400), -1):
                    table = TABLE.match(lines.text(other))
                    if table:
                        parent = table.group(1).split(".")[-1].strip("\"' ")
                        break
        if identity_of(parent, key):
            for scalar in self.scalars(rest):
                if identifier(scalar):
                    named_as = shown
                    if shown and parent and not identity_key(key):
                        above = self.key(parent)[0]
                        named_as = f"{above}.{shown}" if above else shown
                    self.add("identifier", scalar, key=named_as, line=number)
        # Everything after the key is the value, a comment included: this is
        # a document, and a host in it is a host. Then the line as a whole,
        # for a token the separator cut in two -- the drive of `C:\Users\…`
        # reads as a key -- unless the key was a finding already.
        self.tokens(rest, key=shown, line=number, hosts=True)
        if not matched:
            self.tokens(raw, line=number)

    def text(self, text: str, how: str) -> None:
        """One file: a JSON document when it is one, and otherwise the lines that hold a shape.

        Most files hold nothing any family could match, and the raw text
        says so at the speed of a search. A document that parses is read
        whole, key by key. Anything else is read by its lines, and only by
        those a shape was found on: a line of JSON as the document it is, a
        line of YAML or TOML as the assignment it is, a line of code or of
        prose for the shapes that cannot be code.
        """
        low = text.lower()
        # Lowering changes the length of a handful of characters; where it
        # did, the offsets are the original's and the slower search reads it.
        if len(low) == len(text):
            hints, searched = (LOW_HINTS[:2] if how == "text" else LOW_HINTS), low
        else:
            hints, searched = ((TOKEN_HINT if how == "text" else LINE_HINT),), text
        if not any(hint.search(searched) for hint in hints) \
                and not (self.local.names and any(anchor in low for _, _, anchor, _ in self.local.names)):
            return
        name = pathlib.PurePosixPath(self.real).name.lower()
        if how == "json" and not name.endswith(LINE_JSON):
            try:
                for parsed in documents(text):
                    self.document(parsed)
                return
            except (ValueError, RecursionError):
                pass
        lines = Lines(text)
        chosen: set[int] = set()
        for hint in hints:
            last, count = -1, 0
            for found in hint.finditer(searched):
                number = lines.number(found.start())
                if number != last:
                    chosen.add(number)
                    last, count = number, count + 1
                    if count > EXAMINED:
                        break
        if self.local.names and len(low) == len(text):
            chosen.update(lines.number(offset) for _, _, offset, _ in self.local.find(low))
        elif self.local.names:
            chosen.update(range(1, len(lines.starts) + 1))
        numbers = sorted(chosen)
        if len(numbers) > EXAMINED:
            self.partial[(self.shown, "every family")] = None
            del numbers[EXAMINED:]
        for number in numbers:
            self.line(lines.text(number), number, how, lines)


class Lines:
    """The lines of a text, found by offset and read by number without cutting the text up."""

    def __init__(self, text: str) -> None:
        self.source = text
        self.starts = [0] + [found.end() for found in re.finditer("\n", text)]
        self.parents: dict[int, "tuple[int, str | None]"] = {}

    def number(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def text(self, number: int) -> str:
        start = self.starts[number - 1]
        end = self.starts[number] - 1 if number < len(self.starts) else len(self.source)
        return self.source[start:end].rstrip("\r")


def decode(data: bytes) -> "str | None":
    """The text of DATA, or None when it is not text."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\0" in data[:SNIFF]:
        return None
    return data.decode("utf-8-sig", errors="replace")


def unread_reason(path: str) -> "str | None":
    """Why a file is not read, from its name alone."""
    name = pathlib.PurePosixPath(path).name.lower()
    suffix = pathlib.PurePosixPath(name).suffix or name
    if suffix in ARCHIVES:
        return "archive"
    if suffix in IMAGES:
        return "image"
    if suffix in BINARIES:
        return "binary"
    return None


def published_addresses(texts: "list[tuple[str, str]]") -> set[str]:
    """The addresses a repository publishes itself: LICENSING.md, SECURITY.md, its allowed signers.

    A principal of an allowed-signers file may be a pattern, `*@domain`: the
    domain is then published whole, and held as `@domain`.
    """
    found: set[str] = set()
    for path, text in texts:
        for match in ADDRESS.finditer(text):
            found.add(f"{match.group(1)}@{match.group(2)}".lower())
        if SIGNERS.search(path):
            found.update("@" + domain.lower() for domain in re.findall(rf"(?m)^\*@((?:{LABEL}\.)+[A-Za-z]{{2,24}})", text))
    return found


def name_of(reading: Reading, relative: str, index: int, what: str = "tracked path") -> "tuple[str, list[tuple[str, str]]]":
    """(how RELATIVE is shown, what its components matched) -- a name is a value like another.

    Each component is examined by every family, alone and without its last
    suffix; `Users/NAME` and `home/NAME` are read across two. A component
    that matched is replaced by its position, and so is the path's.
    """
    if not NAME_HINT.search(relative) and not reading.local.names:
        return relative, []
    parts = relative.split("/")
    matched: list[tuple[str, str]] = []
    hidden = set()
    for position, part in enumerate(parts):
        found = reading.sensitive(part) or reading.sensitive(part.rsplit(".", 1)[0])
        if not found and position and reading.personal(part) and re.fullmatch(NAME, part) and (
                parts[position - 1] == "Users"
                or (parts[position - 1] == "home" and position < len(parts) - 1
                    and (position == 1 or parts[position - 2] in ("rootfs", "sysroot", "var", "mnt", "root")))):
            found = ("path", part)
        if found:
            matched.append(found)
            hidden.add(position)
    if not matched:
        return relative, []
    shown = "/".join(NOT_SHOWN if position in hidden else part for position, part in enumerate(parts))
    return f"{what} {index}, {shown}", matched


def tracked_files(project: pathlib.Path) -> "tuple[list[str], int]":
    """(the tracked files, how many have a name that is not UTF-8); every file outside a repository, .git apart."""
    if not (project / ".git").exists():
        return sorted(path.relative_to(project).as_posix() for path in project.rglob("*")
                      if path.is_file() and ".git/" not in path.as_posix()), 0
    try:
        return sorted(name for name in host.git("ls-files", "-z", cwd=project, check=False).split("\0") if name), 0
    except UnicodeDecodeError:
        names, undecodable = [], 0
        for raw in host.HOST.feed(["git", "ls-files", "-z"], b"", cwd=project).stdout.split(b"\0"):
            try:
                names.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                undecodable += 1
        return sorted(name for name in names if name), undecodable


# The dialect git is asked in: Perl's where this git was built with it, which
# reads the same patterns three times faster, and POSIX extended where it was
# not. The patterns are written to mean the same in both.
DIALECTS = ["-P", "-E"]


def candidates(project: pathlib.Path, local: Local) -> "set[str] | None":
    """The tracked files that hold a shape, named by git in one reading of each; None outside a repository.

    Two questions, and each file is asked one: a document is searched for
    every shape, any other file for the shapes of a token; what is not read
    for its name is not searched. With no local name the patterns are
    arguments, and the two calls are recorded as they are. A machine's name
    and a private term are never an argument, which a process list shows and
    a recording keeps: they go to git on its standard input, with the rest.
    """
    if not (project / ".git").exists():
        return None
    found: set[str] = set()
    excluded = [":(exclude," + spec[2:] for spec in GIT_DOCUMENTS]
    for patterns, paths in ((GIT_TOKENS, [".", *excluded, *GIT_UNREAD]),
                            (GIT_TOKENS + GIT_VALUES, [*GIT_DOCUMENTS, *GIT_UNREAD])):
        for dialect in list(DIALECTS):
            command = ["git", "grep", "-l", "-z", "-i", dialect]
            if local.names:
                done = host.HOST.feed([*command, "-f", "-", "--", *paths],
                                      "\n".join([*patterns, *local.posix]).encode() + b"\n", cwd=project)
                listed = done.stdout
            else:
                for pattern in patterns:
                    command += ["-e", pattern]
                try:
                    done = host.run([*command, "--", *paths], cwd=project)
                    listed = done.stdout.encode("utf-8", errors="surrogateescape")
                except UnicodeDecodeError:
                    done = host.HOST.feed([*command, "--", *paths], b"", cwd=project)
                    listed = done.stdout
            # 0 is a match and 1 is none; anything else is a git that does
            # not speak this dialect, which is asked in the next and not
            # asked in this one again.
            if done.returncode in (0, 1) or len(DIALECTS) == 1:
                break
            DIALECTS.remove(dialect)
        for raw in listed.split(b"\0"):
            if raw:
                try:
                    found.add(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    pass
    return found


def read_tree(project: pathlib.Path, settings: Settings, local: Local) -> "tuple[Reading, dict]":
    """Read the tracked files; (the reading, what was searched, read, and not).

    git names the candidates. Every tracked name is examined, which opens
    nothing. A candidate is opened, one at a time, and read; nothing is held
    but the findings.
    """
    tracked, undecodable = tracked_files(project)
    holding = candidates(project, local)
    reading = Reading(settings, local)
    shown: dict[str, str] = {}
    unread: list[dict] = [{"path": f"({undecodable} tracked name(s))", "reason": "undecodable"}] if undecodable else []
    names: list[tuple[str, str, list]] = []
    for index, relative in enumerate(tracked, 1):
        label, matched = name_of(reading, relative, index)
        if matched:
            shown[relative] = label
            names.append((relative, label, matched))
    opened: list[str] = []
    searched = 0
    for relative in tracked:
        label = shown.get(relative, relative)
        reason = unread_reason(relative)
        if reason is None:
            try:
                status = os.lstat(project / relative)
            except OSError:
                reason = "absent"
            else:
                if stat.S_ISLNK(status.st_mode):
                    # The link is what is tracked, and its target is a string.
                    reading.open(label, relative)
                    reading.tokens(os.readlink(project / relative), hosts=True)
                    searched += 1
                    continue
                if stat.S_ISDIR(status.st_mode):
                    reason = "submodule"
                elif holding is None or relative in holding:
                    reason = "size" if status.st_size > MAX_BYTES else None
                    if reason is None:
                        opened.append(relative)
        if reason is not None:
            unread.append({"path": label, "reason": reason})
        else:
            searched += 1
    # What the repository publishes itself is read first: it decides what an
    # address elsewhere is.
    publishing = [relative for relative in opened if PUBLISHING.match(relative) or SIGNERS.search(relative)]
    texts = []
    for relative in publishing:
        try:
            text = decode((project / relative).read_bytes())
        except OSError:
            text = None
        if text is not None:
            texts.append((relative, text))
    reading.published = published_addresses(texts)
    read = 0
    for relative in opened:
        label = shown.get(relative, relative)
        try:
            text = decode((project / relative).read_bytes())
        except OSError:
            unread.append({"path": label, "reason": "absent"})
            searched -= 1
            continue
        if text is None:
            unread.append({"path": label, "reason": "binary"})
            searched -= 1
            continue
        read += 1
        reading.open(label, relative)
        reading.text(text, kind(relative))
    for relative, label, matched in names:
        reading.open(label, relative)
        for family, value in matched:
            reading.add(family, value, **{"in": "name"})
    return reading, {"tracked": len(tracked) + undecodable, "searched": searched, "read": read, "unread": unread}


# ---- the verdict --------------------------------------------------------------

def needles(finding: dict) -> list[str]:
    """What of a finding must appear nowhere, lowered: the value, and the part of it that is somebody's.

    A host is held as its name, a path as the name under the home, an
    identifier as it was written and as the UUID inside it, an address whole
    and as its host when the host is a home network's.
    """
    value = finding["value"].lower()
    found = [value]
    if finding["family"] == "identifier":
        inside = UUID_INSIDE.search(value)
        if inside:
            found.append(inside.group(0))
    if finding["family"] == "host" and "@" in value:
        found.append(value.split("@", 1)[1])
    return [needle for needle in found if len(needle) >= 2]


def whole(needle: str) -> str:
    """NEEDLE as a pattern: itself, between two characters that are neither a letter nor a digit."""
    return r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])"


def excuse(reading: Reading, project: pathlib.Path) -> list[str]:
    """Mark what `except` covers; return what is said of the lines that repeat what they excuse.

    Compared, without regard to case and as a whole word, with every line of
    the declaration file, comments included: the host of an excused host or
    URL, the name of an excused path, the address, the identifier whatever
    brackets it, the machine's name or the term as it was written. Not
    compared: anything derived from them -- a part, a translation, an
    encoding -- which the grammar refuses by shape where it can and no
    program can refuse in general.
    """
    release = project / DECLARATION_FILE
    declaration = release.read_text(encoding="utf-8", errors="replace").lower() if release.is_file() else ""
    repeats: set[int] = set()
    for finding in reading.findings:
        for candidate in reading.settings.excuses:
            if candidate.covers(finding):
                candidate.used += 1
                finding["excused"] = candidate.number
                if any(re.search(whole(needle), declaration) for needle in needles(finding)):
                    repeats.add(candidate.number)
                break
    return [f"{DECLARATION_FILE} holds, in the file itself, what its exception at line {number} excuses:"
            " an exception designates a file and a family and never says what is there" for number in sorted(repeats)]


def seal(readings: "list[Reading]") -> "re.Pattern[str] | None":
    """The pattern of everything found, which nothing that leaves may match.

    The last gate, behind every reader: a value found in one place and
    written in another -- under a key the first reader took for a word, in
    the name of a file -- is caught here whatever missed it.
    """
    found = sorted({needle for reading in readings for finding in reading.findings for needle in needles(finding)},
                   key=len, reverse=True)
    return re.compile("|".join(whole(needle) for needle in found), re.IGNORECASE) if found else None


def sealed(text: str, pattern: "re.Pattern[str] | None") -> str:
    """TEXT, or what stands for it when it holds a value some family found."""
    return pattern.sub(NOT_SHOWN, text) if pattern is not None else text


def places(findings: list[dict], shown: int = 3) -> str:
    """The first files in order, each with its lines; the rest counted."""
    by_path: dict[str, set] = {}
    for finding in findings:
        lines = by_path.setdefault(finding["path"], set())
        if "line" in finding:
            lines.add(finding["line"])
    parts = []
    for path in sorted(by_path)[:shown]:
        lines = sorted(by_path[path])
        parts.append(path + (f" line{'s' if len(lines) > 1 else ''} " + ", ".join(str(n) for n in lines[:4])
                             + (", …" if len(lines) > 4 else "") if lines else ""))
    more = len(by_path) - shown
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def group_message(family: str, where: "str | None", key: "str | None", term: "int | None", findings: list[dict],
                  unit: str, history: bool = False) -> str:
    """One line for one family in one kind of place: what, where, and never which."""
    files = len({finding["path"] for finding in findings})
    plural = "s" if files != 1 else ""
    what = SAYS[family]
    if family == "term":
        what = f"the term on line {term} of your private list"
    if family == "email":
        distinct = len({finding["value"] for finding in findings})
        notices = len({finding["value"] for finding in findings if finding.get("notice")})
        what = f"{distinct} e-mail address{'es' if distinct != 1 else ''} this repository does not publish itself" \
               + (f" ({notices} in a copyright or authorship notice)" if notices else "")
    head = "privacy: history: " if history else "privacy: "
    listed = places(findings)
    if where == "ref":
        return f"{head}{what} in the name of {files} ref{plural}: {listed}"
    if where == "name":
        return f"{head}{what} in the name of {files} {unit}{plural}, shown by position: {listed}"
    under = " written as a key, which is not shown," if where == "key" else f" under the key {key}" if key else ""
    if history and unit == "path":
        blobs = len({finding["blob"] for finding in findings})
        return f"{head}{what}{under} in {blobs} blob{'s' if blobs != 1 else ''} of {files} path{plural}: {listed}"
    if history:
        count = len({finding["commit"] for finding in findings})
        several = "s" if count != 1 else ""
        return (f"{head}{what} in what {count} commit{several} or tag{several} say{'s' if count == 1 else ''},"
                f" or in who made {'it' if count == 1 else 'them'}: {listed}")
    return f"{head}{what}{under} in {files} {unit}{plural}: {listed}"


def grouped(findings: list[dict]) -> "list[tuple[str, str | None, str | None, int | None, list[dict]]]":
    """One group per family, place and key; the e-mail family is one group, so that it drowns nothing."""
    groups: dict[tuple, list[dict]] = {}
    for finding in findings:
        where = finding.get("in")
        if finding["family"] == "email":
            identity: tuple = ("email", where, None, None)
        elif finding["family"] == "term":
            identity = ("term", where, None, finding.get("term"))
        elif finding["family"] in ("identifier", "host"):
            identity = (finding["family"], where, finding.get("key"), None)
        else:
            identity = (finding["family"], where, None, None)
        groups.setdefault(identity, []).append(finding)
    return [(family, where, key, term, groups[(family, where, key, term)])
            for family, where, key, term in sorted(groups, key=lambda item: (FAMILIES.index(item[0]), item[1] or "",
                                                                             item[2] or "", item[3] or 0))]


def public(finding: dict, pattern: "re.Pattern[str] | None") -> dict:
    """A finding as it leaves this module: where, never what."""
    out = {name: item for name, item in finding.items() if name not in ("value", "real")}
    out["path"] = sealed(out["path"], pattern)
    if "key" in out and pattern is not None and pattern.search(out["key"]):
        del out["key"]
    return out


def partly(reading: Reading, history: bool = False) -> "list[tuple[str, str]]":
    """What is said of the files read in part, and of those named as JSON that are not."""
    head = "privacy: history: " if history else "privacy: "
    said = []
    if reading.astext:
        paths = sorted(reading.astext)
        said.append(("note", f"{head}{len(paths)} file{'s' if len(paths) != 1 else ''} named as JSON did not parse:"
                             " read a line at a time, where a key is seen only on the line that holds its value: "
                             + "; ".join(paths[:3]) + (f"; and {len(paths) - 3} more" if len(paths) > 3 else "")))
    if reading.partial:
        paths = sorted({path for path, _ in reading.partial})
        said.append(("note", f"{head}{len(paths)} file{'s' if len(paths) != 1 else ''} held more than {KEPT} findings of"
                             f" one family, or more than {EXAMINED} lines to read: read up to there and no further: "
                             + "; ".join(paths[:3]) + (f"; and {len(paths) - 3} more" if len(paths) > 3 else "")))
    return said


def check_tree(project: pathlib.Path, history: bool = False) -> "tuple[list[tuple[str, str]], dict]":
    """The guard as `check` reads it: (status, message) lines and the data beside them."""
    settings = declared(project)
    local = read_local()
    reading, files = read_tree(project, settings, local)
    past = read_history(project, settings, local, reading.published) if history else None
    readings = [reading] + ([past[0]] if past else [])
    repeats = excuse(reading, project)
    if past:
        repeats = sorted(set(repeats).union(excuse(past[0], project)))
    pattern = seal(readings)
    live = [finding for finding in reading.findings if "excused" not in finding]
    excused = len(reading.findings) - len(live)
    by_reason: dict[str, int] = {}
    for entry in files["unread"]:
        by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
    if (project / ".git").exists():
        coverage = (f"privacy: {files['searched']} of {files['tracked']} tracked files searched for the shapes of six"
                    " families, " + (f"and the {files['read']} that held one read" if files["read"] else "and none held one"))
    else:
        # Outside a repository nothing is tracked, and the line does not pretend otherwise.
        coverage = f"privacy: {files['read']} of {files['tracked']} files read, every file since this is not a git repository"
    if files["unread"]:
        coverage += " (not read, and nothing is said of them: " + ", ".join(
            f"{count} {UNREAD[reason]}" for reason, count in sorted(by_reason.items())) + ")"
    coverage += "; " + local.says()
    coverage += f"; {excused} finding(s) excused by {DECLARATION_FILE}" if excused else ""
    coverage += "; nothing found" if not live else f"; {len(live)} finding(s), named below by place and never by value"
    checks: list[tuple[str, str]] = [("ok", coverage)]
    for family, where, key, term, findings in grouped(live):
        checks.append(("refused" if family in settings.refuses else "note",
                       group_message(family, where, key, term, findings, "tracked file")))
    checks.extend(partly(reading))
    for message in repeats:
        checks.append(("refused" if settings.refuses else "note", "privacy: " + message))
    # A line of [privacy] that is not one is a violation, whatever it meant
    # to be: a repository that asked for a refusal and mistyped it is told,
    # never left with a note it did not ask for.
    for message in settings.refused:
        checks.append(("refused", f"privacy: {DECLARATION_FILE} [privacy] {message}; the line is not applied, and"
                                  " nothing of what it holds is repeated here"))
    for candidate in settings.excuses:
        if not candidate.used:
            checks.append(("note", f"privacy: {DECLARATION_FILE} line {candidate.number} excuses nothing in the tracked"
                                   " files" + (" nor in the history" if history else "")
                                   + (": the line can go" if history else
                                      "; 'check DIR --history' says whether the history still needs it")))
    if live and not settings.refuses:
        checks.append(("note", f"privacy: nothing above is refused. '[privacy] refuse' in {DECLARATION_FILE} turns"
                               " it into a violation once this repository is clean; 'fabricated NAME' declares a"
                               " name invented for a fixture, and 'except PATH family FAMILY REASON' what stays"))
    files["unread"] = [{"path": sealed(entry["path"], pattern), "reason": entry["reason"]} for entry in files["unread"]]
    files["astext"] = [sealed(path, pattern) for path in sorted(reading.astext)]
    files["partial"] = sorted({sealed(path, pattern) for path, _ in reading.partial})
    data: dict = {"mode": "refuse" if settings.refuses else "note", "refuses": list(settings.refuses),
                  "files": files,
                  "local": {"machineNames": len(local.machines), "terms": len(local.terms), "runner": local.runner},
                  "findings": [public(finding, pattern) for finding in reading.findings]}
    if past:
        lines, data["history"] = history_report(past, pattern)
        checks.extend(lines)
    elif history:
        checks.append(("note", "privacy: history: this directory is not a git repository, and has no history to read"))
    # The last gate: whatever was found, by whichever reader, is in no line.
    return [(status, sealed(message, pattern)) for status, message in checks], data


# ---- the history, on demand ---------------------------------------------------

IDENTITY_LINE = re.compile(r"^(author|committer|tagger) (.*) <([^>]*)> [0-9]+ [+-][0-9]{4}$")
BATCH_BYTES = 32 * 1024 * 1024
REF_KINDS = (("refs/heads/", "branch"), ("refs/tags/", "tag"), ("refs/remotes/", "remote-tracking ref"),
             ("refs/pull/", "pull request head"))


def cat_file(project: pathlib.Path, names: list[str]) -> "dict[str, bytes]":
    """The content of each object named, read by one `git cat-file --batch`."""
    done = host.HOST.feed(["git", "cat-file", "--batch"], "".join(name + "\n" for name in names).encode(), cwd=project)
    found: dict[str, bytes] = {}
    data, offset = done.stdout, 0
    while offset < len(data):
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        offset = end + 1
        if len(header) != 3:
            continue  # "NAME missing"
        size = int(header[2])
        found[header[0].decode()] = data[offset:offset + size]
        offset += size + 1
    return found


def batches(objects: "list[tuple[str, int]]") -> "list[list[str]]":
    """OBJECTS, (name, size), in runs of bounded weight."""
    found: list[list[str]] = [[]]
    weight = 0
    for name, size in objects:
        if found[-1] and weight + size > BATCH_BYTES:
            found.append([])
            weight = 0
        found[-1].append(name)
        weight += size
    return [batch for batch in found if batch]


def read_history(project: pathlib.Path, settings: Settings, local: Local,
                 published: "set[str]") -> "tuple[Reading, dict] | None":
    """Every ref of this clone: its name, the blobs its trees reach, each once, and what its commits say.

    The question asked before a repository becomes public, and never on a
    pull request: it reads everything, and what it finds cannot be fixed by
    the next commit. So nothing here refuses -- a published tag is never
    rewritten, and a rule a repository cannot satisfy is a rule it switches
    off. What this did not read is part of the answer, and is said.

    Everything git answers is read as bytes: a history holds names that are
    not UTF-8, and one of them must not end the reading.
    """
    if not (project / ".git").exists():
        return None
    reading = Reading(settings, local, published)
    listed = host.HOST.feed(["git", "for-each-ref", "--format=%(objecttype) %(objectname) %(refname)"], b"", cwd=project)
    kinds: dict[str, int] = {}
    unread: dict[str, int] = {}
    tags: list[str] = []
    refs = 0
    for raw in listed.stdout.splitlines():
        fields = raw.split(b" ", 2)
        if len(fields) != 3:
            continue
        refs += 1
        try:
            name = fields[2].decode("utf-8")
        except UnicodeDecodeError:
            unread["undecodable"] = unread.get("undecodable", 0) + 1
            name = ""
        label = next((label for prefix, label in REF_KINDS if name.startswith(prefix)), "other ref")
        kinds[label] = kinds.get(label, 0) + 1
        if fields[0] == b"tag":
            tags.append(fields[1].decode())
        # A ref is named by somebody, and its name is a value like another.
        short = next((name[len(prefix):] for prefix, _ in REF_KINDS if name.startswith(prefix)), name)
        shown, matched = name_of(reading, short, refs, what=f"ref ({label})")
        reading.open(shown if matched else f"ref {refs} ({label})", name)
        for family, value in matched:
            reading.add(family, value, **{"in": "ref"})
    shallow = host.HOST.feed(["git", "rev-parse", "--is-shallow-repository"], b"", cwd=project).stdout.strip() == b"true"
    paths: dict[str, str] = {}
    # --missing=print: a partial clone names objects it never fetched, and
    # listing them must neither fail nor fetch. They are counted, not read.
    walked = host.HOST.feed(["git", "rev-list", "--objects", "--all", "--missing=print"], b"", cwd=project)
    for raw in walked.stdout.splitlines() if walked.returncode == 0 else []:
        name, _, path = raw.partition(b" ")
        if name.startswith(b"?"):
            unread["absent"] = unread.get("absent", 0) + 1
            continue
        try:
            paths.setdefault(name.decode(), path.decode("utf-8"))
        except UnicodeDecodeError:
            # Never read under a name that cannot be said, and counted
            # where the name is: in the tree that holds it.
            paths.setdefault(name.decode(), "\0")
    # An annotated tag is an object of its own, with a tagger and a message.
    for name in tags:
        paths.setdefault(name, "")
    described = host.HOST.feed(["git", "cat-file", "--batch-check"],
                               "".join(name + "\n" for name in paths).encode(), cwd=project)
    said: list[str] = []
    blobs: list[tuple[str, int]] = []
    trees: list[tuple[str, int]] = []
    for line in described.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 3:
            unread["absent"] = unread.get("absent", 0) + 1
            continue
        name, type_of, size = fields[0], fields[1], int(fields[2])
        if type_of in ("commit", "tag"):
            said.append(name)
        elif type_of == "tree" and paths.get(name) != "\0":
            trees.append((name, size))
        elif type_of == "blob" and paths.get(name) != "\0":
            reason = unread_reason(paths.get(name, "")) or ("size" if size > MAX_BYTES else None)
            if reason:
                unread[reason] = unread.get(reason, 0) + 1
            else:
                blobs.append((name, size))
    # The trees: every name this history ever gave a file or a directory.
    # A blob is listed under the first path it had and no other, so the
    # names are read where they all are.
    every: dict[str, None] = {}
    width = 32 if trees and len(trees[0][0]) == 64 else 20
    for batch in batches(trees):
        for tree, data in cat_file(project, batch).items():
            base, offset = paths.get(tree, ""), 0
            while offset < len(data):
                space = data.index(b" ", offset)
                end = data.index(b"\0", space)
                offset = end + 1 + width
                try:
                    name = data[space + 1:end].decode("utf-8")
                except UnicodeDecodeError:
                    unread["undecodable"] = unread.get("undecodable", 0) + 1
                    continue
                every[f"{base}/{name}" if base else name] = None
    named: dict[str, str] = {}
    for index, relative in enumerate(sorted(every), 1):
        shown, matched = name_of(reading, relative, index, what="path")
        if matched:
            named[relative] = shown
            reading.open(shown, relative)
            for family, value in matched:
                reading.add(family, value, **{"in": "name"})
    # The blobs, a bounded batch at a time: a history is larger than a tree,
    # and one answer holding all of it is the memory of the machine.
    read = 0
    for batch in batches(blobs):
        for blob, data in cat_file(project, batch).items():
            text = decode(data)
            if text is None:
                unread["binary"] = unread.get("binary", 0) + 1
                continue
            read += 1
            relative = paths.get(blob) or "(a blob no path names)"
            reading.open(named.get(relative, relative), relative, blob=blob[:12])
            reading.text(text, kind(relative))
    # The commits and the annotated tags: who, and what they wrote.
    addresses: set[str] = set()
    for start in range(0, len(said), 4000):
        for name, data in cat_file(project, said[start:start + 4000]).items():
            head, _, message = data.decode("utf-8", errors="replace").partition("\n\n")
            scratch = Reading(settings, local, published)
            scratch.open(f"the message of {name[:12]}", commit=name[:12])
            lines = Lines(message)
            for number in range(1, len(lines.starts) + 1):
                scratch.tokens(lines.text(number), line=number)
            for line in head.splitlines():
                identity = IDENTITY_LINE.match(line)
                if identity:
                    scratch.open(f"the {identity.group(1)} of {name[:12]}", commit=name[:12])
                    scratch.tokens(f"{identity.group(2)} <{identity.group(3)}>")
            for finding in scratch.findings:
                if finding["family"] == "email":
                    # An address in a history is an identity, and every history
                    # has them: counted, and left to the one command that
                    # prints them where only the author reads them.
                    addresses.add(finding["value"])
                else:
                    reading.open(finding["path"], commit=finding["commit"])
                    reading.add(finding["family"], finding["value"], line=finding.get("line"), term=finding.get("term"))
    return reading, {"refs": refs, "kinds": kinds, "shallow": shallow, "commits": len(said), "read": read,
                     "unread": unread, "addresses": len(addresses), "listed": walked.returncode == 0}


def history_report(past: "tuple[Reading, dict]", pattern: "re.Pattern[str] | None") -> "tuple[list[tuple[str, str]], dict]":
    """What is said of a history, and the data beside it."""
    reading, facts = past
    kinds, unread = facts["kinds"], facts["unread"]
    live = [finding for finding in reading.findings if "excused" not in finding]
    counted = ", ".join(f"{count} {kind_of}{'es' if kind_of == 'branch' and count != 1 else 's' if count != 1 else ''}"
                        for kind_of, count in sorted(kinds.items()))
    checks = [("ok", f"privacy: history: {facts['commits']} commits and annotated tags and {facts['read']} distinct blobs"
                     f" read through {facts['refs']} refs ({counted or 'none'}), and the name of each ref"
                     + ("; nothing found" if not live else f"; {len(live)} finding(s), below"))]
    for unit, chosen in (("ref", lambda finding: finding.get("in") == "ref"),
                         ("path", lambda finding: finding.get("in") == "name"),
                         ("path", lambda finding: "blob" in finding),
                         ("commit", lambda finding: "commit" in finding)):
        for family, where, key, term, findings in grouped([finding for finding in live if chosen(finding)]):
            checks.append(("note", group_message(family, where, key, term, findings, unit, history=True)))
    checks.extend(partly(reading, history=True))
    if facts["addresses"]:
        checks.append(("note", f"privacy: history: {facts['addresses']} distinct e-mail address"
                               f"{'es' if facts['addresses'] != 1 else ''} in author, committer and tagger lines and in"
                               " commit messages, outside noreply, example.* and what this repository publishes"
                               " itself. They are part of every clone and are not listed here:"
                               " 'git log --all --format=%ae%n%ce | sort -u' prints them where only you read them"))
    missing = []
    if not facts["listed"]:
        missing.append("git could not list the objects of this clone, and its trees were not read")
    if not kinds.get("pull request head"):
        missing.append("the pull request heads a hosting service keeps (GitHub: refs/pull/N/head) are not in an"
                       " ordinary clone and were not read -- 'git fetch origin \"+refs/pull/*/head:refs/pull/*/head\"'"
                       " brings them, and a zero that has not read them is not a zero")
    if facts["shallow"]:
        missing.append("this clone is shallow, and the history beyond what it holds was not read")
    if unread:
        # An object a tree names and this clone does not hold -- a partial
        # clone whose remote did not answer -- is absent from the clone,
        # which is not what "absent from the working tree" says of a file.
        said_of = {**UNREAD, "absent": "named by a tree and not in this clone"}
        missing.append("objects not read: " + ", ".join(f"{count} {said_of[reason]}"
                                                         for reason, count in sorted(unread.items())))
    missing.append("a ref is read as it was last fetched, and what no ref reaches is not read")
    checks.append(("note", "privacy: history: " + "; ".join(missing)))
    return checks, {"refs": kinds, "shallow": facts["shallow"], "commits": facts["commits"],
                    "blobs": {"read": facts["read"], "unread": unread}, "addresses": facts["addresses"],
                    "pullRequestHeads": bool(kinds.get("pull request head")),
                    "findings": [public(finding, pattern) for finding in reading.findings]}
