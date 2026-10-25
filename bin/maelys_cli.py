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
import functools
import json
import os
import re
import stat
import sys
from typing import Any, Callable, Iterator, Optional, Union

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
# And, on a line that selected the default command, a word that is no option.
_BARE_WORD = object()

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


def _prescan_language(argv: list) -> Optional[str]:
    """The first --lang before `--` (C: prescan_language)."""
    for index, word in enumerate(argv):
        if word == "--":
            break
        if word.startswith("--lang="):
            return word[len("--lang="):]
        if word == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
    return None


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


EXTENSION_NAME = re.compile(r"x-[a-z0-9][a-z0-9-]*")
CODE_GRAMMAR = re.compile(r"[A-Z][A-Z0-9_]*")


# BEGIN translations generated by scripts/i18n.py from i18n/*.tsv: edit those files.
_FRAMEWORK_TRANSLATIONS = {
    'fr': {
        'USAGE':
            'UTILISATION',
        'EXAMPLES':
            'EXEMPLES',
        'DEFAULT COMMAND':
            'COMMANDE PAR DÉFAUT',
        'OPERANDS':
            'OPÉRANDES',
        'OPTIONS':
            'OPTIONS',
        'GLOBAL OPTIONS':
            'OPTIONS GLOBALES',
        'COMMANDS':
            'COMMANDES',
        'AGENTS':
            'AGENTS',
        'UNAVAILABLE IN THIS BUILD':
            'INDISPONIBLE DANS CETTE COMPILATION',
        'The rest of the line goes to another program, word for word.':
            'Le reste de la ligne va à un autre programme, mot pour mot.',
        'It shows a plan; --apply commits it.':
            'Elle montre un plan ; --apply le valide.',
        'It shows a plan; --apply applies it.':
            "Elle montre un plan ; --apply l'applique.",
        'It acts at once, without a plan.':
            'Elle agit tout de suite, sans plan.',
        'Its output is the %s protocol: --format and the other rendering options do not apply.':
            "Sa sortie suit le protocole %s : --format et les autres options de rendu ne s'appliquent pas.",
        'Its output is its own: --format and the other rendering options do not apply.':
            "Sa sortie lui est propre : --format et les autres options de rendu ne s'appliquent pas.",
        'A line that names no command runs this one: an empty line, or one that starts with an option other than --help and --version. Its operands come after --:':
            'Une ligne qui ne nomme aucune commande lance celle-ci : une ligne vide, ou une ligne qui commence par une option autre que --help et --version. Ses opérandes viennent après -- :',
        'Optional.':
            'Facultatif.',
        "Run '%s --help' for --format, --json, --compact, --non-interactive, --color and the others.":
            "Lancez '%s --help' pour --format, --json, --compact, --non-interactive, --color et les autres.",
        '%s - commands':
            '%s - commandes',
        '(unavailable in this build)':
            '(indisponible dans cette compilation)',
        "Run '%s help COMMAND_ID' or '%s COMMAND --help' for the operands and options of one command.":
            "Lancez '%s help COMMAND_ID' ou '%s COMMAND --help' pour les opérandes et les options d'une commande.",
        'one command, its options and examples':
            'une commande, ses options et ses exemples',
        'the commands of one family':
            "les commandes d'une famille",
        "%s [options] runs '%s', as %s %s [options] does: the options in front, other than --help and --version, are its.":
            "%s [options] lance '%s', comme %s %s [options] : les options placées devant, autres que --help et --version, sont les siennes.",
        'Also:':
            'Aussi :',
        "'%s help COMMAND' says why.":
            "'%s help COMMAND' dit pourquoi.",
        'Not available in this build:':
            'Indisponible dans cette compilation :',
        "Run '%s describe --summary --format json' first, and build every line from its input, never from this text.":
            "Lancez d'abord '%s describe --summary --format json', et construisez chaque ligne à partir de ce qu'il décrit, jamais à partir de ce texte.",
        'Default: %s.':
            'Par défaut : %s.',
        'Required.':
            'Obligatoire.',
        'Requires --%s.':
            'Exige --%s.',
        'Conflicts with --%s.':
            'Incompatible avec --%s.',
        '(repeatable)':
            '(répétable)',
        'Show the help of the program, of one command or of one family.':
            "Affiche l'aide du programme, d'une commande ou d'une famille.",
        'Show the product, program, version and contract.':
            'Affiche le produit, le programme, la version et le contrat.',
        'Show the machine-readable catalog, its summary or one descriptor.':
            'Affiche le catalogue lisible par une machine, son résumé ou un descripteur.',
        'Print the shell completion script generated from the catalog.':
            'Affiche le script de complétion du shell, généré à partir du catalogue.',
        'Return completion candidates for a partial command line.':
            "Renvoie les candidats de complétion d'une ligne de commande partielle.",
        'Shell whose completion script is printed.':
            'Shell dont le script de complétion est affiché.',
        'Command line words after the program name; the last one is the prefix being completed.':
            'Mots de la ligne de commande après le nom du programme ; le dernier est le début à compléter.',
        'Stable command identifier whose help is shown, or the family of commands under an identifier.':
            "Identifiant stable de la commande dont l'aide est affichée, ou famille des commandes sous un identifiant.",
        'Stable command identifier returned by the catalog.':
            'Identifiant stable de commande, tel que le catalogue le renvoie.',
        'Return the compact command inventory without output schemas.':
            "Renvoie l'inventaire compact des commandes, sans schémas de sortie.",
        'Restrict the summary to one command namespace: the command named PREFIX and every command whose identifier starts with PREFIX followed by a dot.':
            "Restreint le résumé à un espace de noms : la commande nommée PREFIX et toute commande dont l'identifiant commence par PREFIX suivi d'un point.",
        'Select text for humans, json for one envelope or jsonl for records.':
            'Choisit text pour les personnes, json pour une enveloppe ou jsonl pour des enregistrements.',
        'Exact alias of --format json.':
            'Alias exact de --format json.',
        'Render JSON on a single line.':
            'Écrit le JSON sur une seule ligne.',
        '--pretty=false selects compact JSON.':
            '--pretty=false choisit le JSON compact.',
        'Never prompt; fail instead of asking a question.':
            'Ne pose jamais de question ; échoue plutôt que de demander.',
        'Control ANSI colors on terminals.':
            'Règle les couleurs ANSI sur un terminal.',
        'Show the progress of a long run on stderr in text mode: auto only when stderr is a terminal.':
            "Affiche l'avancement d'une longue exécution sur stderr en mode texte : auto seulement quand stderr est un terminal.",
        'Add the details of the run on stderr in text mode; silent in JSON.':
            "Ajoute les détails de l'exécution sur stderr en mode texte ; rien en JSON.",
        'Page the text rendering when stdout is a terminal; never in a pipe, in JSON or under --non-interactive.':
            'Pagine le rendu texte quand stdout est un terminal ; jamais dans un tube, en JSON ni sous --non-interactive.',
        'Render one top-level member of data instead of the whole result, by the text or jsonl rendering rules.':
            'Rend un seul membre de premier niveau de data au lieu du résultat entier, selon les règles du rendu text ou jsonl.',
        'Show the help of the selected command.':
            "Affiche l'aide de la commande choisie.",
        '%s: %s':
            '%s : %s',
        "'%s' does not produce records and cannot render jsonl.":
            "'%s' ne produit pas d'enregistrements et ne peut pas rendre du jsonl.",
        "'%s' is a stream command and does not support CLI output rendering.":
            "'%s' est une commande de flux et n'accepte pas les options de rendu.",
        "'%s' is neither an option nor the value of one: the line names no command and runs the default command '%s', whose operands are written after --.":
            "'%s' n'est ni une option ni la valeur d'une option : la ligne ne nomme aucune commande et lance la commande par défaut '%s', dont les opérandes s'écrivent après --.",
        "'%s' needs an interactive confirmation that automation cannot provide.":
            "'%s' demande une confirmation interactive qu'une automatisation ne peut pas donner.",
        "--%s belongs to group '%s' and requires --%s.":
            "--%s fait partie du groupe '%s' et exige --%s.",
        '--%s conflicts with --%s.':
            '--%s est incompatible avec --%s.',
        '--%s conflicts with operand %s.':
            "--%s est incompatible avec l'opérande %s.",
        '--%s is not supported; planning is the default for transactional commands.':
            "--%s n'est pas accepté : une commande transactionnelle planifie par défaut.",
        "--%s is required by '%s'. Use '%s'.":
            "--%s est exigé par '%s'. Utilisez '%s'.",
        '--%s requires --%s.':
            '--%s exige --%s.',
        "--field conflicts with --format json: a filtered envelope would not validate against the command's outputSchema.":
            "--field est incompatible avec --format json : une enveloppe filtrée ne serait plus conforme à l'outputSchema de la commande.",
        'At most one of %s may be given.':
            'Une seule au plus de ces options peut être donnée : %s.',
        'Cannot execute %s: %s.':
            "Impossible d'exécuter %s : %s.",
        'Exactly one of %s must be given.':
            'Exactement une de ces options doit être donnée : %s.',
        'External command %s is unusable: %s.':
            'La commande externe %s est inutilisable : %s.',
        "External command '%s' for '%s' is not installed.":
            "La commande externe '%s' de '%s' n'est pas installée.",
        'Invalid command prefix: %s.':
            'Préfixe de commande invalide : %s.',
        'Invalid option spelling: %s.':
            'Option mal écrite : %s.',
        'Invalid parser arguments.':
            "Arguments de l'analyseur invalides.",
        'No command in namespace: %s.':
            "Aucune commande dans l'espace de noms : %s.",
        "Operands do not match '%s'. Use '%s'.":
            "Les opérandes ne correspondent pas à '%s'. Utilisez '%s'.",
        'Option %s is not supported: options are spelled --name.':
            "L'option %s n'est pas acceptée : une option s'écrit --nom.",
        'Option --%s declares a pattern outside the common subset of ECMA-262 and POSIX ERE.':
            "L'option --%s déclare un motif hors du sous-ensemble commun à ECMA-262 et POSIX ERE.",
        'Option --%s expects %zu lowercase hexadecimal digits.':
            "L'option --%s attend %zu chiffres hexadécimaux en minuscules.",
        'Option --%s expects %zu or %zu lowercase hexadecimal digits.':
            "L'option --%s attend %zu ou %zu chiffres hexadécimaux en minuscules.",
        'Option --%s expects ALGORITHM:HEX with lowercase hexadecimal digits (%s).':
            "L'option --%s attend ALGORITHME:HEX, en chiffres hexadécimaux minuscules (%s).",
        'Option --%s expects a boolean.':
            "L'option --%s attend un booléen.",
        'Option --%s expects a byte size between %llu and %llu (K, M, G or T suffix accepted).':
            "L'option --%s attend une taille en octets entre %llu et %llu (suffixe K, M, G ou T accepté).",
        'Option --%s expects a duration with a unit (ms, s, m, h or d) between %llu and %llu milliseconds.':
            "L'option --%s attend une durée avec son unité (ms, s, m, h ou d) entre %llu et %llu millisecondes.",
        'Option --%s expects a value matching %s.':
            "L'option --%s attend une valeur qui corresponde à %s.",
        'Option --%s expects an absolute path.':
            "L'option --%s attend un chemin absolu.",
        'Option --%s expects an integer between %lld and %lld.':
            "L'option --%s attend un entier entre %lld et %lld.",
        'Option --%s expects an unsigned integer between %llu and %llu.':
            "L'option --%s attend un entier positif ou nul entre %llu et %llu.",
        'Option --%s expects one of: %s.':
            "L'option --%s attend l'une de ces valeurs : %s.",
        'Option --%s has an unknown value kind.':
            "L'option --%s a une sorte de valeur inconnue.",
        "Option --%s is not supported by '%s'. Use '%s'.":
            "L'option --%s n'est pas acceptée par '%s'. Utilisez '%s'.",
        'Option --%s may be supplied only once.':
            "L'option --%s ne peut être donnée qu'une fois.",
        'Option --%s requires a non-empty value.':
            "L'option --%s exige une valeur non vide.",
        'Option --%s requires a value.':
            "L'option --%s exige une valeur.",
        "Option --field is not accepted by '%s': the command can write and its output schema requires no member.":
            "L'option --field n'est pas acceptée par '%s' : la commande peut écrire et son schéma de sortie n'exige aucun membre.",
        "Option --field names '%s', which '%s' does not always return: a command that can write accepts only a member its output schema requires.":
            "L'option --field nomme '%s', que '%s' ne renvoie pas toujours : une commande qui peut écrire n'accepte qu'un membre exigé par son schéma de sortie.",
        "Option --field names '%s', which '%s' does not have.":
            "L'option --field nomme '%s', que '%s' n'a pas.",
        "The plan --expect names is no longer the one '%s' would apply: the action or the state it touches has changed. Nothing was written.":
            "Le plan que nomme --expect n'est plus celui que '%s' appliquerait : l'action ou l'état qu'elle touche a changé. Rien n'a été écrit.",
        "Too many operands for '%s'.":
            "Trop d'opérandes pour '%s'.",
        "Too many options for '%s'.":
            "Trop d'options pour '%s'.",
        'Unknown %s command: %s.':
            'Commande %s inconnue : %s.',
        'Unknown command identifier or family: %s.':
            'Identifiant de commande ou famille inconnus : %s.',
        'Unknown command identifier: %s.':
            'Identifiant de commande inconnu : %s.',
        "Command '%s' answered without checking --expect: what it did is not known to be the plan the fingerprint names.":
            "La commande '%s' a répondu sans vérifier --expect : rien ne garantit que ce qu'elle a fait soit le plan que nomme l'empreinte.",
        "Command '%s' could not serialize its data.":
            "La commande '%s' n'a pas pu sérialiser ses données.",
        "Command '%s' emitted a record that is not an object.":
            "La commande '%s' a émis un enregistrement qui n'est pas un objet.",
        "Command '%s' emitted an invalid record.":
            "La commande '%s' a émis un enregistrement invalide.",
        "Command '%s' failed with %s.":
            "La commande '%s' a échoué avec %s.",
        "Command '%s' produced invalid JSON data at byte %zu.":
            "La commande '%s' a produit des données JSON invalides à l'octet %zu.",
        'Could not describe the catalog.':
            'Impossible de décrire le catalogue.',
        'Could not render help.':
            "Impossible de rendre l'aide.",
        'Could not render the completion script.':
            'Impossible de rendre le script de complétion.',
        'Could not serialize the catalog.':
            'Impossible de sérialiser le catalogue.',
        'Could not serialize the completion script.':
            'Impossible de sérialiser le script de complétion.',
        'Could not serialize the version.':
            'Impossible de sérialiser la version.',
        'Could not write candidates.':
            "Impossible d'écrire les candidats.",
        'Application declaration is incomplete.':
            "La déclaration de l'application est incomplète.",
        "Catalog command '%s' collides with '%s'.":
            "La commande '%s' du catalogue entre en collision avec '%s'.",
        "Catalog command '%s' constraint %zu has an unknown kind.":
            "La commande '%s' du catalogue : la contrainte %zu est d'une sorte inconnue.",
        "Catalog command '%s' constraint %zu names --%s twice.":
            "La commande '%s' du catalogue : la contrainte %zu nomme --%s deux fois.",
        "Catalog command '%s' constraint %zu names fewer than two options.":
            "La commande '%s' du catalogue : la contrainte %zu nomme moins de deux options.",
        "Catalog command '%s' constraint %zu names unknown option --%s.":
            "La commande '%s' du catalogue : la contrainte %zu nomme l'option inconnue --%s.",
        "Catalog command '%s' counts examples it does not declare.":
            "La commande '%s' du catalogue compte des exemples qu'elle ne déclare pas.",
        "Catalog command '%s' declares --%s twice.":
            "La commande '%s' du catalogue déclare --%s deux fois.",
        "Catalog command '%s' declares operand %zu incorrectly.":
            "La commande '%s' du catalogue déclare mal l'opérande %zu.",
        "Catalog command '%s' declares option %zu incorrectly.":
            "La commande '%s' du catalogue déclare mal l'option %zu.",
        "Catalog command '%s' declares the effect %s outside a transaction: use read or execute, or declare the transaction with its apply_effect and --apply.":
            "La commande '%s' du catalogue déclare l'effet %s hors d'une transaction : utilisez read ou execute, ou déclarez la transaction avec son apply_effect et --apply.",
        "Catalog command '%s' has a synopsis longer than %u bytes.":
            "La commande '%s' du catalogue a un synopsis de plus de %u octets.",
        "Catalog command '%s' has an example it does not accept: '%s': %s":
            "La commande '%s' du catalogue a un exemple qu'elle n'accepte pas : '%s' : %s",
        "Catalog command '%s' has an example of more than %u words.":
            "La commande '%s' du catalogue a un exemple de plus de %u mots.",
        "Catalog command '%s' has an example that does not start with its pattern '%s': '%s'.":
            "La commande '%s' du catalogue a un exemple qui ne commence pas par son motif '%s' : '%s'.",
        "Catalog command '%s' has an example that shows the hidden option --%s: '%s'.":
            "La commande '%s' du catalogue a un exemple qui montre l'option cachée --%s : '%s'.",
        "Catalog command '%s' has an example whose words are not separated by single spaces: '%s'.":
            "La commande '%s' du catalogue a un exemple dont les mots ne sont pas séparés par une seule espace : '%s'.",
        "Catalog command '%s' has an example without words or without a summary, or with a control character.":
            "La commande '%s' du catalogue a un exemple sans mots ou sans résumé, ou avec un caractère de contrôle.",
        "Catalog command '%s' has an invalid identifier.":
            "La commande '%s' du catalogue a un identifiant invalide.",
        "Catalog command '%s' has an invalid output mode.":
            "La commande '%s' du catalogue a un mode de sortie invalide.",
        "Catalog command '%s' has an invalid output schema at byte %zu.":
            "La commande '%s' du catalogue a un schéma de sortie invalide à l'octet %zu.",
        "Catalog command '%s' has an invalid pattern.":
            "La commande '%s' du catalogue a un motif invalide.",
        "Catalog command '%s' has no effect.":
            "La commande '%s' du catalogue n'a pas d'effet.",
        "Catalog command '%s' has no purpose.":
            "La commande '%s' du catalogue n'a pas de but.",
        "Catalog command '%s' marked unavailable must name a reason and have neither handler nor delegate.":
            "La commande '%s' du catalogue, marquée indisponible, doit donner une raison et n'avoir ni handler ni délégué.",
        "Catalog command '%s' names '%s' as its unavailable code, which is not one of the stable codes.":
            "La commande '%s' du catalogue nomme '%s' comme code d'indisponibilité, qui n'est pas un des codes stables.",
        "Catalog command '%s' names a protocol but is not a stream command.":
            "La commande '%s' du catalogue nomme un protocole mais n'est pas une commande de flux.",
        "Catalog command '%s' names an unavailable code without an 'unavailable' reason.":
            "La commande '%s' du catalogue nomme un code d'indisponibilité sans raison 'unavailable'.",
        "Catalog command '%s' needs exactly one of handler or delegate, or an 'unavailable' reason.":
            "La commande '%s' du catalogue doit avoir exactement un handler ou un délégué, ou une raison 'unavailable'.",
        "Catalog command '%s' operand %s declares a pattern on a kind that is not string or path.":
            "La commande '%s' du catalogue : l'opérande %s déclare un motif sur une sorte qui n'est ni string ni path.",
        "Catalog command '%s' operand %s declares a pattern outside the common subset of ECMA-262 and POSIX ERE: %s.":
            "La commande '%s' du catalogue : l'opérande %s déclare un motif hors du sous-ensemble commun à ECMA-262 et POSIX ERE : %s.",
        "Catalog command '%s' operand %s has an invalid type declaration.":
            "La commande '%s' du catalogue : l'opérande %s a une déclaration de type invalide.",
        "Catalog command '%s' operand %s names unknown digest algorithm '%s'.":
            "La commande '%s' du catalogue : l'opérande %s nomme un algorithme d'empreinte inconnu, '%s'.",
        "Catalog command '%s' option --%s declares a pattern on a kind that is not string or path.":
            "La commande '%s' du catalogue : l'option --%s déclare un motif sur une sorte qui n'est ni string ni path.",
        "Catalog command '%s' option --%s declares a pattern outside the common subset of ECMA-262 and POSIX ERE: %s.":
            "La commande '%s' du catalogue : l'option --%s déclare un motif hors du sous-ensemble commun à ECMA-262 et POSIX ERE : %s.",
        "Catalog command '%s' option --%s has a default '%s' that its own kind refuses.":
            "La commande '%s' du catalogue : l'option --%s a une valeur par défaut, '%s', que sa propre sorte refuse.",
        "Catalog command '%s' option --%s has an invalid value declaration.":
            "La commande '%s' du catalogue : l'option --%s a une déclaration de valeur invalide.",
        "Catalog command '%s' option --%s is hidden and required; a required option must be shown.":
            "La commande '%s' du catalogue : l'option --%s est cachée et obligatoire ; une option obligatoire doit être montrée.",
        "Catalog command '%s' option --%s names group '%s' with fewer than two members.":
            "La commande '%s' du catalogue : l'option --%s nomme le groupe '%s', qui a moins de deux membres.",
        "Catalog command '%s' option --%s names unknown digest algorithm '%s'.":
            "La commande '%s' du catalogue : l'option --%s nomme un algorithme d'empreinte inconnu, '%s'.",
        "Catalog command '%s' option --%s references unknown option --%s.":
            "La commande '%s' du catalogue : l'option --%s renvoie à l'option inconnue --%s.",
        "Catalog command '%s' redeclares transport option --%s.":
            "La commande '%s' du catalogue redéclare l'option globale --%s.",
        "Catalog command '%s' states an all-or-none constraint; declare .group on its options instead, the one form the entry is derived from.":
            "La commande '%s' du catalogue déclare une contrainte tout-ou-rien ; déclarez plutôt .group sur ses options, la seule forme d'où l'entrée est dérivée.",
        "Catalog default_command '%s' %s.":
            "Le default_command '%s' du catalogue %s.",
        "Delegate command '%s' cannot declare options; arguments are passed through.":
            "La commande déléguée '%s' ne peut pas déclarer d'options : ses arguments sont transmis tels quels.",
        "Help palette %s '%s' is not the parameters of an SGR sequence.":
            "La couleur %s '%s' de la palette de l'aide n'est pas faite de paramètres SGR.",
        "Transactional command '%s' declares --expect, which is reserved for binding a plan: declare it with MAELYS_CLI_EXPECT_OPTION, or name the option otherwise.":
            "La commande transactionnelle '%s' déclare --expect, réservée à la liaison d'un plan : déclarez-la avec MAELYS_CLI_EXPECT_OPTION, ou nommez l'option autrement.",
        'Transactional command \'%s\' declares --expect: its output schema must list "fingerprint" in its top-level "required".':
            'La commande transactionnelle \'%s\' déclare --expect : son schéma de sortie doit lister "fingerprint" dans son "required" de premier niveau.',
        "Transactional command '%s' must declare effect preview, apply_effect apply or commit, and an --apply option.":
            "La commande transactionnelle '%s' doit déclarer l'effet preview, l'apply_effect apply ou commit, et une option --apply.",
        'Correct the stated operand and retry.':
            "Corrigez l'opérande indiqué et réessayez.",
        'Correct the stated option and retry.':
            "Corrigez l'option indiquée et réessayez.",
        'Correct the stated option value and retry.':
            "Corrigez la valeur de l'option indiquée et réessayez.",
        'Fill program, product, version and commands.':
            'Remplissez program, product, version et commands.',
        'Fix the command catalog declaration.':
            'Corrigez la déclaration du catalogue de commandes.',
        'Give every command a unique identifier and pattern.':
            'Donnez à chaque commande un identifiant et un motif uniques.',
        'Inspect the named path or resource, correct its state and retry.':
            'Examinez le chemin ou la ressource nommés, corrigez leur état et réessayez.',
        'Install the external command with safe ownership and modes.':
            'Installez la commande externe avec un propriétaire et des droits sûrs.',
        'Install the optional component that provides this command.':
            'Installez le composant facultatif qui fournit cette commande.',
        'Keep one of the listed options and retry.':
            'Gardez une seule des options listées et réessayez.',
        'Name a shown, available read, stream or transaction command of the product as default_command, or declare none.':
            "Nommez comme default_command une commande du produit montrée, disponible, de lecture, de flux ou transactionnelle, ou n'en déclarez aucune.",
        'Plan again without --apply, review the new plan, then apply it with its fingerprint.':
            'Planifiez de nouveau sans --apply, relisez le nouveau plan, puis appliquez-le avec son empreinte.',
        'Reduce the number of operands and retry.':
            "Réduisez le nombre d'opérandes et réessayez.",
        'Reduce the number of options and retry.':
            "Réduisez le nombre d'options et réessayez.",
        'Remove either the option or the operand and retry.':
            "Retirez l'option ou l'opérande et réessayez.",
        'Remove one of the conflicting options and retry.':
            'Retirez une des options incompatibles et réessayez.',
        'Remove rendering flags; stdout is reserved for the declared protocol stream.':
            'Retirez les options de rendu : stdout est réservé au flux du protocole déclaré.',
        'Remove the duplicate option and retry.':
            "Retirez l'option en double et réessayez.",
        'Remove the legacy flag for a plan. Add --apply only after reviewing that plan.':
            "Retirez l'ancienne option pour obtenir un plan. N'ajoutez --apply qu'après avoir relu ce plan.",
        'Report this defect to the command implementation.':
            "Signalez ce défaut à l'auteur de la commande.",
        "Run 'describe --summary --format json' to discover valid command identifiers and usage.":
            "Lancez 'describe --summary --format json' pour connaître les identifiants de commande valides et leur usage.",
        "Run 'help' without operands to list the commands and their families.":
            "Lancez 'help' sans opérande pour lister les commandes et leurs familles.",
        'Run describe --summary --format json and select a returned command identifier.':
            'Lancez describe --summary --format json et choisissez un des identifiants de commande renvoyés.',
        'Run describe --summary --format json and select a returned command namespace.':
            'Lancez describe --summary --format json et choisissez un des espaces de noms renvoyés.',
        'Run describe for this command and use only its declared options.':
            "Lancez describe pour cette commande et n'utilisez que les options qu'elle déclare.",
        'Run the command without --field and read the member from its result.':
            'Lancez la commande sans --field et lisez le membre dans son résultat.',
        'Shorten operands, options or their placeholders.':
            'Raccourcissez les opérandes, les options ou leurs noms de valeur.',
        'Spell an option --name; write an operand that starts with a dash after --.':
            'Écrivez une option --nom ; écrivez après -- un opérande qui commence par un tiret.',
        'Supply every dependent option and retry.':
            'Donnez toutes les options dépendantes et réessayez.',
        'Supply exactly one of the listed options and retry.':
            'Donnez exactement une des options listées et réessayez.',
        'Supply the dependent option and retry.':
            "Donnez l'option dépendante et réessayez.",
        'Supply the documented option value and retry.':
            "Donnez la valeur d'option documentée et réessayez.",
        'Supply the explicit option that authorizes this action, or run interactively.':
            "Donnez l'option explicite qui autorise cette action, ou lancez la commande de façon interactive.",
        'Supply the required option and retry.':
            "Donnez l'option obligatoire et réessayez.",
        'Supply the whole option group or none of it.':
            "Donnez tout le groupe d'options ou aucune.",
        'Use --format json for this command, or add --field to render one member in jsonl; jsonl otherwise is reserved for record streams.':
            "Utilisez --format json pour cette commande, ou ajoutez --field pour rendre un membre en jsonl ; sinon jsonl est réservé aux flux d'enregistrements.",
        'Use --format text or --format jsonl with --field.':
            'Utilisez --format text ou --format jsonl avec --field.',
        'Use a command identifier prefix such as repo or repo.mirror.':
            "Utilisez un préfixe d'identifiant de commande comme repo ou repo.mirror.",
        "Use a member the command's output schema requires; describe lists them.":
            'Utilisez un membre exigé par le schéma de sortie de la commande ; describe les liste.',
        "Use a top-level member of the command's data.":
            'Utilisez un membre de premier niveau des données de la commande.',
        'Use the synopsis returned by describe and retry.':
            'Utilisez le synopsis que renvoie describe et réessayez.',
        'Use true or false for an explicit boolean.':
            'Utilisez true ou false pour un booléen explicite.',
        'Verify the external command binary and retry.':
            "Vérifiez l'exécutable de la commande externe et réessayez.",
        'Write each color as SGR parameters: digits and \';\', such as "1" or "1;36".':
            'Écrivez chaque couleur en paramètres SGR : des chiffres et \';\', comme "1" ou "1;36".',
        "Write the command's pattern first, or the operands of the default command after --.":
            "Écrivez d'abord le motif de la commande, ou bien les opérandes de la commande par défaut après --.",
        'Write the example as a real invocation of the command, or remove it.':
            "Écrivez l'exemple comme une vraie invocation de la commande, ou retirez-le.",
        'Catalog languages are declared incorrectly: a list and its count, at most %u.':
            'Les langues du catalogue sont mal déclarées : une liste et son nombre, au plus %u.',
        "Catalog language '%s' is not a well-formed language tag.":
            "La langue '%s' du catalogue n'est pas une étiquette de langue bien formée.",
        "Catalog language '%s' is the canonical language %s, which has no table.":
            "La langue '%s' du catalogue est la langue canonique %s, qui n'a pas de table.",
        "Catalog language '%s' is declared twice.":
            "La langue '%s' du catalogue est déclarée deux fois.",
        "Catalog language '%s' counts translations it does not declare.":
            "La langue '%s' du catalogue compte des traductions qu'elle ne déclare pas.",
        "Catalog language '%s' translation %zu is empty or holds a control character.":
            "La langue '%s' du catalogue : la traduction %zu est vide ou contient un caractère de contrôle.",
        "Catalog language '%s' translates '%s' without its conversions, in order.":
            "La langue '%s' du catalogue traduit '%s' sans ses conversions, dans l'ordre.",
        'Declare each language once by a well-formed tag, and translate each text with the conversions of its source.':
            'Déclarez chaque langue une fois, par une étiquette bien formée, et traduisez chaque texte avec les conversions de sa source.',
        "Option %s takes true, false, yes, no, on, off, 1 or 0, not '%s'.":
            "L'option %s attend true, false, yes, no, on, off, 1 ou 0, et non '%s'.",
        "Use '%s'.":
            "Utilisez '%s'.",
        "Unknown command identifier '%s'.":
            "Identifiant de commande inconnu : '%s'.",
        'Run describe --summary to list the command identifiers.':
            'Lancez describe --summary pour lister les identifiants de commande.',
        'No command given.':
            'Aucune commande donnée.',
        "Run '%s help' or 'describe --summary'.":
            "Lancez '%s help' ou 'describe --summary'.",
        "Unknown command '%s'.":
            "Commande inconnue : '%s'.",
        'Run describe --summary and use one of the listed command identifiers.':
            'Lancez describe --summary et utilisez un des identifiants de commande listés.',
        "Command '%s' is not available in this build: %s":
            "La commande '%s' n'est pas disponible dans cette compilation : %s",
        'Use another build of the product, or another command.':
            'Utilisez une autre compilation du produit, ou une autre commande.',
        "Command '%s' owns its stdout and refuses %s.":
            "La commande '%s' possède sa sortie standard et refuse %s.",
        'Set MAELYS_CLI_FORMAT=json in the environment to receive its failure envelope as JSON.':
            "Définissez MAELYS_CLI_FORMAT=json dans l'environnement pour recevoir son enveloppe d'échec en JSON.",
        "--format jsonl is accepted only by json-records commands, not '%s'; add --field to render one member in jsonl.":
            "--format jsonl n'est accepté que par les commandes json-records, pas par '%s' ; ajoutez --field pour rendre un membre en jsonl.",
        "--format jsonl is accepted only by json-records commands, not 'help'; add --field to render one member in jsonl.":
            "--format jsonl n'est accepté que par les commandes json-records, pas par 'help' ; ajoutez --field pour rendre un membre en jsonl.",
        'Use --format json.':
            'Utilisez --format json.',
        "Option --prefix takes a command identifier prefix, not '%s'.":
            "L'option --prefix attend un préfixe d'identifiant de commande, et non '%s'.",
        "Use lowercase letters, digits, '.' and '-', starting with a letter.":
            "Utilisez des lettres minuscules, des chiffres, '.' et '-', en commençant par une lettre.",
        'Option %s is given twice.':
            "L'option %s est donnée deux fois.",
        'Give each option once.':
            'Donnez chaque option une seule fois.',
        'Option %s needs a value %s.':
            "L'option %s a besoin d'une valeur %s.",
        "Run describe for this command and pass the option's argument.":
            "Lancez describe pour cette commande et donnez l'argument de l'option.",
        'Options %s are given together or not at all.':
            'Les options %s se donnent ensemble ou pas du tout.',
        "Option %s is required by '%s'.":
            "L'option %s est obligatoire pour '%s'.",
        'Option %s requires %s.':
            "L'option %s exige %s.",
        'Requires %s.':
            'Exige %s.',
        'Conflicts with %s.':
            'Incompatible avec %s.',
        'Option %s conflicts with %s.':
            "L'option %s est incompatible avec %s.",
        'Option %s conflicts with the %s operand.':
            "L'option %s est incompatible avec l'opérande %s.",
        'Value kind %s of %s is not known.':
            'La sorte de valeur %s de %s est inconnue.',
        'Fix the catalog.':
            'Corrigez le catalogue.',
        "Option %s is not supported by '%s': it plans by default.":
            "L'option %s n'est pas acceptée par '%s' : elle planifie par défaut.",
        'Run without --apply to plan, then add --apply to write.':
            'Lancez sans --apply pour planifier, puis ajoutez --apply pour écrire.',
        "Option %s is not supported by '%s'. Use '%s'.":
            "L'option %s n'est pas acceptée par '%s'. Utilisez '%s'.",
        "Option %s takes true or false, not '%s'.":
            "L'option %s attend true ou false, et non '%s'.",
        "%s takes true or false, not '%s'.":
            "%s attend true ou false, et non '%s'.",
        "Option %s takes a value matching %s, not '%s'.":
            "L'option %s attend une valeur qui corresponde à %s, et non '%s'.",
        "%s takes a value matching %s, not '%s'.":
            "%s attend une valeur qui corresponde à %s, et non '%s'.",
        "Option %s takes an integer, not '%s'.":
            "L'option %s attend un entier, et non '%s'.",
        "%s takes an integer, not '%s'.":
            "%s attend un entier, et non '%s'.",
        "Option %s takes an unsigned integer, not '%s'.":
            "L'option %s attend un entier positif ou nul, et non '%s'.",
        "%s takes an unsigned integer, not '%s'.":
            "%s attend un entier positif ou nul, et non '%s'.",
        "Option %s takes a size such as 512, 4K, 16M, 2G or 1T, not '%s'.":
            "L'option %s attend une taille comme 512, 4K, 16M, 2G ou 1T, et non '%s'.",
        "%s takes a size such as 512, 4K, 16M, 2G or 1T, not '%s'.":
            "%s attend une taille comme 512, 4K, 16M, 2G ou 1T, et non '%s'.",
        "Option %s takes a duration with its unit: ms, s, m, h or d, not '%s'.":
            "L'option %s attend une durée avec son unité : ms, s, m, h ou d, et non '%s'.",
        "%s takes a duration with its unit: ms, s, m, h or d, not '%s'.":
            "%s attend une durée avec son unité : ms, s, m, h ou d, et non '%s'.",
        "Option %s takes a non-empty path, not '%s'.":
            "L'option %s attend un chemin non vide, et non '%s'.",
        "%s takes a non-empty path, not '%s'.":
            "%s attend un chemin non vide, et non '%s'.",
        "Option %s takes an absolute path, not '%s'.":
            "L'option %s attend un chemin absolu, et non '%s'.",
        "%s takes an absolute path, not '%s'.":
            "%s attend un chemin absolu, et non '%s'.",
        "Option %s takes %s, not '%s'.":
            "L'option %s attend l'une de ces valeurs : %s, et non '%s'.",
        "%s takes %s, not '%s'.":
            "%s attend l'une de ces valeurs : %s, et non '%s'.",
        "Option %s takes %s lowercase hexadecimal digits, not '%s'.":
            "L'option %s attend %s chiffres hexadécimaux en minuscules, et non '%s'.",
        "%s takes %s lowercase hexadecimal digits, not '%s'.":
            "%s attend %s chiffres hexadécimaux en minuscules, et non '%s'.",
        "Option %s takes %s or %s lowercase hexadecimal digits, not '%s'.":
            "L'option %s attend %s ou %s chiffres hexadécimaux en minuscules, et non '%s'.",
        "%s takes %s or %s lowercase hexadecimal digits, not '%s'.":
            "%s attend %s ou %s chiffres hexadécimaux en minuscules, et non '%s'.",
        "Option %s takes a SHA-256 digest of 64 lowercase hexadecimal characters, not '%s'.":
            "L'option %s attend une empreinte SHA-256 de 64 caractères hexadécimaux en minuscules, et non '%s'.",
        "%s takes a SHA-256 digest of 64 lowercase hexadecimal characters, not '%s'.":
            "%s attend une empreinte SHA-256 de 64 caractères hexadécimaux en minuscules, et non '%s'.",
        "Option %s takes ALGORITHM:HEX with %s, not '%s'.":
            "L'option %s attend ALGORITHME:HEX avec %s, et non '%s'.",
        "%s takes ALGORITHM:HEX with %s, not '%s'.":
            "%s attend ALGORITHME:HEX avec %s, et non '%s'.",
        "Option %s takes ALGORITHM:HEX with a declared algorithm, not '%s'.":
            "L'option %s attend ALGORITHME:HEX avec un algorithme déclaré, et non '%s'.",
        "%s takes ALGORITHM:HEX with a declared algorithm, not '%s'.":
            "%s attend ALGORITHME:HEX avec un algorithme déclaré, et non '%s'.",
        'Option %s must be between %s and %s, not %s.':
            "L'option %s doit être entre %s et %s, et non %s.",
        '%s must be between %s and %s, not %s.':
            '%s doit être entre %s et %s, et non %s.',
        'Language of the human text.':
            'Langue du texte humain.',
        'Hint:':
            'Conseil :',
        "Catalog language '%s' translates '%s' twice.":
            "La langue '%s' du catalogue traduit '%s' deux fois.",
        '%s Manual':
            'Manuel de %s',
        'NAME':
            'NOM',
        'SYNOPSIS':
            'SYNOPSIS',
        'DESCRIPTION':
            'DESCRIPTION',
        'EXIT STATUS':
            'CODE DE RETOUR',
        'ENVIRONMENT':
            'ENVIRONNEMENT',
        'NOTES':
            'NOTES',
        'SEE ALSO':
            'VOIR AUSSI',
        "%s %s, a program of the agent-cli/v2 contract: '%s help' shows the help, '%s describe --format json' the catalog this page is rendered from.":
            "%s %s, un programme du contrat agent-cli/v2 : '%s help' en montre l'aide, '%s describe --format json' le catalogue d'où cette page est rendue.",
        "%s [options] runs '%s', as %s %s [options] does: the options in front, other than --help and --version, are its, and its operands come after --.":
            "%s [options] lance '%s', comme %s %s [options] : les options placées devant, autres que --help et --version, sont les siennes, et ses opérandes viennent après --.",
        'Not available in this build: %s':
            'Indisponible dans cette compilation : %s',
        'Command completed.':
            'Commande terminée.',
        'Execution failed.':
            'Exécution échouée.',
        'Valid report with violations.':
            'Rapport valide, avec des violations.',
        'A stream command and a delegate exit with the status of what they run.':
            "Une commande de flux et un délégué sortent avec le code de retour de ce qu'ils lancent.",
        'The format when no option names one: text, json or jsonl.':
            "Le format quand aucune option n'en nomme : text, json ou jsonl.",
        'The language of the human text when no --lang names one; then LC_ALL, LC_MESSAGES, LANG.':
            "La langue du texte humain quand aucun --lang n'en nomme ; puis LC_ALL, LC_MESSAGES, LANG.",
        'Set and not empty: no color, unless --color always or CLICOLOR_FORCE.':
            'Définie et non vide : pas de couleur, sauf --color always ou CLICOLOR_FORCE.',
        'Set and neither empty nor 0: color even into a pipe, unless --color never.':
            'Définie, ni vide ni 0 : de la couleur même dans un tube, sauf --color never.',
        'Unset, empty or dumb: no color, unless --color always or CLICOLOR_FORCE.':
            'Non définie, vide ou dumb : pas de couleur, sauf --color always ou CLICOLOR_FORCE.',
        'The pager of a long text on a terminal; empty disables it.':
            "Le paginateur d'un long texte sur un terminal ; vide, il est désactivé.",
        "The width of the help on a terminal, read before the terminal's own.":
            "La largeur de l'aide sur un terminal, lue avant celle du terminal.",
        "Catalog canonical language '%s' is not a well-formed language tag.":
            "La langue canonique '%s' du catalogue n'est pas une étiquette de langue bien formée.",
        'Name the canonical language by a tag such as fr or fr-CA, or leave it NULL for English.':
            "Nommez la langue canonique par une étiquette comme fr ou fr-CA, ou laissez-la NULL pour l'anglais.",
    },
}
# END translations


# ---- the language of human text (C: src/language.c) -------------------------------
#
# A request is a language tag of the form agent-cli/v2 section 5 writes, language[-Script][-REGION], compared
# without regard to case. A locale (LC_ALL, LC_MESSAGES, LANG) is converted by its own rule, language[_territory][.codeset]
# [@modifier] giving language-territory; C, POSIX and C.* give the canonical language. The language a
# request resolves to is the Lookup of RFC 4647 section 3.4. Nothing reads the locales installed on the
# machine: the syntax is the RFCs', the same everywhere. tests/languages.tsv holds C and Python to the
# same answers.
_ASCII_ALPHA = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGIT = frozenset("0123456789")
_ASCII_ALNUM = _ASCII_ALPHA | _ASCII_DIGIT
# agent-cli/v2 section 5 ("Language"): the canonical language, which version names, and the form of a tag,
# language[-Script][-REGION], the pattern --lang declares.
CANONICAL_LANGUAGE = "en"
LANGUAGE_TAG_PATTERN = "^[A-Za-z]{2,3}(-[A-Za-z]{4})?(-([A-Za-z]{2}|[0-9]{3}))?$"
_LANGUAGE_TAG = re.compile(LANGUAGE_TAG_PATTERN[1:-1])
_CONVERSION = re.compile(r"%(?:[-+ #0]*(?:\*|[0-9]*)(?:\.(?:\*|[0-9]*))?[hljztL]*[diuoxXcspfFeEgGaA])")
MAX_LANGUAGES = 32


def _all_in(text: str, characters: frozenset) -> bool:
    return all(c in characters for c in text)


def _language_tag_is_well_formed(tag: Optional[str]) -> bool:
    """A tag of the form agent-cli/v2 writes, language[-Script][-REGION] (C:
    maelys_cli_language_tag_is_well_formed)."""
    return isinstance(tag, str) and tag.isascii() and _LANGUAGE_TAG.fullmatch(tag) is not None


def _language_from_locale(locale: Optional[str]) -> Optional[str]:
    """The tag a locale names (C: maelys_cli_language_from_locale): fr_FR.UTF-8 gives fr-FR; "" for the
    canonical language (C, POSIX, C.*, or a locale that does not convert); None for an empty value."""
    if not locale:
        return None
    if locale in ("C", "POSIX") or locale.startswith("C."):
        return ""
    match = re.match(r"([A-Za-z]{2,3})(?:_([A-Za-z0-9]{2,3}))?(?=[.@]|$)", locale)
    if match is None or not locale.isascii():
        return ""
    tag = match.group(1).lower() + (f"-{match.group(2).upper()}" if match.group(2) else "")
    return tag if _language_tag_is_well_formed(tag) else ""


def _language_lookup(tag: Optional[str], available: "list[str]") -> Optional[str]:
    """The available language a tag resolves to by the Lookup of RFC 4647 (C: maelys_cli_language_lookup):
    truncated from the right, a singleton left last with the subtag after it; None for the canonical one."""
    if not tag or len(tag) >= 64:
        return None
    subtags = tag.split("-")
    while subtags:
        wanted = "-".join(subtags).lower()
        for language in available:
            if language.lower() == wanted:
                return language
        subtags.pop()
        if subtags and len(subtags[-1]) == 1:
            subtags.pop()
    return None


def _language_resolve(option: Optional[str], variable: Optional[str], lc_all: Optional[str],
                      lc_messages: Optional[str], lang: Optional[str], available: "list[str]") -> Optional[str]:
    """The language in force, from the first source that is not empty (C: maelys_cli_language_resolve): the
    option, the implementation's variable, then LC_ALL, LC_MESSAGES and LANG. A request that names no
    available language falls back to the canonical one: a later source is never read."""
    if option:
        return _language_lookup(option, available)
    if variable:
        return _language_lookup(variable, available) if _language_tag_is_well_formed(variable) else None
    for locale in (lc_all, lc_messages, lang):
        if locale:
            tag = _language_from_locale(locale)
            return _language_lookup(tag, available) if tag else None
    return None


def _template_conversions(template: str) -> "list[str]":
    """The printf conversions of a template, "%%" left out (C: maelys_cli_template_conversions)."""
    found, at = [], 0
    while True:
        at = template.find("%", at)
        if at < 0:
            return found
        if template.startswith("%%", at):
            at += 2
            continue
        match = _CONVERSION.match(template, at)
        if match is None:
            raise ValueError(f"'%' that is not a conversion at {at}")
        found.append(match.group(0))
        at = match.end()


def _format_conversion(conversion: str, arguments: list) -> "tuple[str, int]":
    """One C conversion applied to the arguments in front, as printf would; the count it took."""
    spec = re.sub(r"[hljztL]", "", conversion)
    if spec[-1] == "u":
        spec = spec[:-1] + "d"
    taken = 1 + spec.count("*")
    if spec[-1] == "p":
        return f"{arguments[0]:#x}" if isinstance(arguments[0], int) else str(arguments[0]), 1
    return spec % tuple(arguments[:taken]), taken


def _format_template(template: str, arguments: tuple) -> "tuple[str, list[str]]":
    """A template with its arguments put in (C: maelys_cli_error_set), and each conversion's text, the way
    C keeps them so that a translated template takes them back."""
    out, texts, at, used = [], [], 0, 0
    for match in re.finditer(r"%%|" + _CONVERSION.pattern, template):
        out.append(template[at:match.start()])
        at = match.end()
        if match.group(0) == "%%":
            out.append("%")
            continue
        text, taken = _format_conversion(match.group(0), list(arguments[used:]))
        used += taken
        out.append(text)
        texts.append(text)
    out.append(template[at:])
    if used != len(arguments):
        raise TypeError(f"template {template!r} takes {used} arguments, not {len(arguments)}")
    return "".join(out), texts


def _substitute(template: str, texts: "list[str]") -> Optional[str]:
    """A translated template with the conversions' texts put back in order (C: maelys_cli_translate_error);
    None when it does not take them all."""
    out, at, taken = [], 0, 0
    for match in re.finditer(r"%%|" + _CONVERSION.pattern, template):
        out.append(template[at:match.start()])
        at = match.end()
        if match.group(0) == "%%":
            out.append("%")
            continue
        if taken >= len(texts):
            return None
        out.append(texts[taken])
        taken += 1
    out.append(template[at:])
    return "".join(out) if taken == len(texts) else None


def _translation_text_is_safe(text: Any) -> bool:
    """A text a table may hold: not empty, no control character but a newline (C: translation_text_is_safe)."""
    return isinstance(text, str) and bool(text) and all(_terminal_safe(line) == line for line in text.split("\n"))


# argument.pattern is written in a subset that ECMA-262 (u flag) and POSIX ERE
# read the same way (agent-cli/v2, section 3). C reads and matches it itself (src/pattern.c), and so does this
# module, with the same reader and the same program: a pattern is refused here
# exactly where C refuses it, and a value is searched by a Thompson simulation
# of the program, never by `re`, whose backtracking takes exponential time on
# a pattern the subset admits, (a+)+b against aaa...c. The work is
# O((len(value) + 1) x states) and the memory O(states), as in C.
# tests/patterns.tsv holds the two readers to the same answers, refusals
# included: their family and the byte where reading stopped.
_PATTERN_METACHARACTERS = "\\^$.[]()*+?{}|"
REGEX_MAX_BOUND = 255           # n of {m,n}: a rule of syntax (C: MAELYS_CLI_REGEX_MAX_BOUND)
REGEX_MAX_DEPTH = 32            # nested groups: a budget (C: MAELYS_CLI_REGEX_MAX_DEPTH)
REGEX_MAX_STATES = 65536        # the program once expanded: a budget (C: MAELYS_CLI_REGEX_MAX_STATES)
_PATTERN_MAX_BOUND = REGEX_MAX_BOUND
_PATTERN_MAX_DEPTH = REGEX_MAX_DEPTH
_PATTERN_MAX_STATES = REGEX_MAX_STATES

# The families of a refusal, the values of maelys_cli_regex_status_t in C, which never change.
_REGEX_STATUSES = {"invalid-argument": 1, "no-memory": 2, "empty": 3, "encoding": 4, "escape": 5,
                   "bracket": 6, "group": 7, "quantifier": 8, "bound": 9, "limit": 10}


class RegexError(ValueError):
    """Why a pattern is outside the subset of agent-cli/v2 section 3 (C: maelys_cli_regex_diagnostic_t).

    `code` names the family (`empty`, `encoding`, `escape`, `bracket`, `group`, `quantifier`, `bound`,
    `limit`) and `status` is its stable value in C; `offset` is the byte of the pattern, in UTF-8, where
    reading stopped; `message` is a sentence for a person, whose wording may change: a program compares
    `code` or `status`, never `message`."""

    def __init__(self, code: str, message: str, offset: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = _REGEX_STATUSES[code]
        self.message = message
        self.offset = offset

# The program, as src/pattern.c emits it.
_OP_CHAR, _OP_ANY, _OP_CLASS, _OP_SPLIT, _OP_JMP, _OP_BOL, _OP_EOL, _OP_MATCH = range(8)


def _pattern_tree(pattern: str) -> tuple:
    """Reads a pattern of the subset into its tree, or raises RegexError, the refusals, their
    families, their offsets and their order being C's: the first met from the left wins, and the
    state budget is met once the whole pattern is read."""
    if not isinstance(pattern, str):
        raise TypeError("a pattern is a str")
    text, length = pattern, len(pattern)
    position = [0]

    def refuse(code: str, message: str) -> RegexError:
        return RegexError(code, message, len(text[:position[0]].encode("utf-8", "surrogatepass")))

    if not pattern:
        raise refuse("empty", "an empty pattern")

    def peek() -> str:
        return text[position[0]] if position[0] < length else ""

    def parse_class() -> tuple:
        negated = peek() == "^"
        if negated:
            position[0] += 1
        mask, items, after_range = 0, 0, False
        while True:
            if position[0] >= length:
                raise refuse("bracket", "an unclosed bracket expression")
            c = text[position[0]]
            # POSIX reads '-' as itself first, last or as the end of a range only; after a range,
            # ECMA-262 takes it for itself and RE2 for the start of another: [a-z-9] has no common reading.
            if c == "-" and after_range and position[0] + 1 < length and text[position[0] + 1] != "]":
                raise refuse("bracket", "a '-' after a range that is not the last character of the bracket expression, "
                                 "which the two dialects read differently")
            if c == "]":
                if items == 0:
                    raise refuse("bracket", "an empty bracket expression, or ']' where the two dialects read it differently")
                position[0] += 1
                return ("class", mask, negated)
            if c == "[":
                raise refuse("bracket", "'[' inside a bracket expression, as a POSIX class such as [:alpha:] starts")
            if c == "\\":
                raise refuse("bracket", "'\\' inside a bracket expression, an escape for ECMA-262 and a character for POSIX ERE")
            if not " " <= c <= "~":
                raise refuse("encoding", "a character outside printable ASCII in a bracket expression")
            position[0] += 1
            low = high = ord(c)
            after_range = False
            if peek() == "-" and position[0] + 1 < length and text[position[0] + 1] != "]":
                end = text[position[0] + 1]
                if end in "[\\":
                    raise refuse("bracket", "a range that ends on '[' or '\\'")
                if not " " <= end <= "~":
                    raise refuse("encoding", "a character outside printable ASCII in a bracket expression")
                if end < c:
                    raise refuse("bracket", "a range whose ends are reversed")
                high = ord(end)
                position[0] += 2
                after_range = True
            mask |= ((1 << (high - low + 1)) - 1) << low
            items += 1

    def parse_bound() -> tuple:
        parts = [""]
        while True:
            if position[0] >= length:
                raise refuse("bound", "an unclosed bound: write {m}, {m,} or {m,n}")
            c = text[position[0]]
            position[0] += 1
            if c in "0123456789":
                # A number is written in its decimal form: RE2 refuses {0,054}, ECMA-262 reads it as 54.
                if parts[-1] == "0":
                    raise refuse("bound", "a bound written with a leading zero")
                parts[-1] += c
            elif c == "," and len(parts) == 1:
                parts.append("")
            elif c == "}":
                break
            else:
                raise refuse("bound", "'{' that does not open a bound {m}, {m,} or {m,n}")
        if not parts[0]:
            raise refuse("bound", "a bound without its minimum: write {m}, {m,} or {m,n}")
        low = int(parts[0])
        high = low if len(parts) == 1 else int(parts[1]) if parts[1] else None
        if low > _PATTERN_MAX_BOUND or (high is not None and high > _PATTERN_MAX_BOUND):
            raise refuse("limit", "a bound above 255, more than POSIX guarantees of an ERE engine")
        if high is not None and low > high:
            raise refuse("bound", "a bound {m,n} with m above n")
        return low, high

    def parse_atom(depth: int) -> tuple:
        c = peek()
        if c == "(":
            position[0] += 1
            if peek() == "?":
                raise refuse("group", "'(?', a group ECMA-262 names and POSIX ERE lacks")
            if depth + 1 > _PATTERN_MAX_DEPTH:
                raise refuse("limit", "more than 32 nested groups")
            inner = parse_alternation(depth + 1)
            if peek() != ")":
                raise refuse("group", "an unclosed group")
            position[0] += 1
            return inner
        if c == "[":
            position[0] += 1
            return parse_class()
        if c in (".", "^", "$"):
            position[0] += 1
            return {".": ("any",), "^": ("bol",), "$": ("eol",)}[c]
        if c == ")":
            raise refuse("group", "a ')' that closes no group")
        if c in ("]", "}"):
            raise refuse("group", "an unescaped ']' or '}', which the two dialects read differently: write \\] or \\}")
        if c in ("*", "+", "?", "{"):
            raise refuse("quantifier", "a quantifier that follows nothing")
        if c == "\\":
            position[0] += 1
            if position[0] >= length:
                raise refuse("escape", "a '\\' that ends the pattern")
            escaped = text[position[0]]
            if escaped not in _PATTERN_METACHARACTERS:
                raise refuse("escape", "an escape that is not of a metacharacter: '\\d', '\\n' and the like are not in the subset")
            position[0] += 1
            return ("char", ord(escaped))
        code = ord(c)
        if code < 0x20 or code == 0x7F:
            raise refuse("encoding", "a control character")
        if code > 0x7E:
            raise refuse("encoding", "a character outside printable ASCII, which a libc in the C locale reads byte by byte")
        position[0] += 1
        return ("char", code)

    def parse_piece(depth: int) -> tuple:
        atom = parse_atom(depth)
        c = peek()
        if c not in ("*", "+", "?", "{"):
            return atom
        if atom[0] in ("bol", "eol"):
            raise refuse("quantifier", "a quantified anchor")
        position[0] += 1
        if c == "{":
            low, high = parse_bound()
        else:
            low, high = {"*": (0, None), "+": (1, None), "?": (0, 1)}[c]
        if peek() in ("*", "+", "?", "{"):
            raise refuse("quantifier", "two quantifiers in a row, or a lazy quantifier such as '*?', which POSIX ERE lacks")
        return ("repeat", atom, low, high)

    # A sequence or an alternation is one node holding its items, as in C: the depth of the tree,
    # and of every recursion over it, is bounded by the nesting of groups, not by the length.
    def parse_sequence(depth: int) -> tuple:
        pieces = []
        while position[0] < length and text[position[0]] not in ("|", ")"):
            pieces.append(parse_piece(depth))
        if not pieces:
            raise refuse("empty", "an empty alternative or group")
        return pieces[0] if len(pieces) == 1 else ("cat", tuple(pieces))

    def parse_alternation(depth: int) -> tuple:
        alternatives = [parse_sequence(depth)]
        while peek() == "|":
            position[0] += 1
            alternatives.append(parse_sequence(depth))
        return alternatives[0] if len(alternatives) == 1 else ("alt", tuple(alternatives))

    tree = parse_alternation(0)
    if position[0] < length:
        raise refuse("group", "a ')' that closes no group")
    if _pattern_size(tree) + 1 > _PATTERN_MAX_STATES:
        position[0] = length
        raise refuse("limit", "a pattern that expands to more than 65536 states")
    return tree


def _pattern_size(node: tuple) -> int:
    """The instructions a node emits, saturating above the bound, as node_size in C."""
    cap = _PATTERN_MAX_STATES + 1
    kind = node[0]
    if kind in ("cat", "alt"):
        total = 2 * (len(node[1]) - 1) if kind == "alt" else 0
        for child in node[1]:
            if total > cap:
                break
            total += _pattern_size(child)
        return min(total, cap)
    if kind == "repeat":
        size, low, high = _pattern_size(node[1]), node[2], node[3]
        total = low * size + size + 2 if high is None else low * size + (high - low) * (size + 1)
        return min(total, cap)
    return 1


@functools.lru_cache(maxsize=256)
def _pattern_program(pattern: str) -> tuple:
    """The program of a pattern the subset admits, as src/pattern.c emits it: three
    tuples, the operation, its first and its second argument (a class keeps its mask
    and whether it is negated)."""
    tree = _pattern_tree(pattern)
    ops, xs, ys = [], [], []

    def emit(op: int, x: int = 0, y: int = 0) -> int:
        ops.append(op)
        xs.append(x)
        ys.append(y)
        return len(ops) - 1

    def emit_node(node: tuple) -> None:
        kind = node[0]
        if kind == "char":
            emit(_OP_CHAR, node[1])
        elif kind == "any":
            emit(_OP_ANY)
        elif kind == "bol":
            emit(_OP_BOL)
        elif kind == "eol":
            emit(_OP_EOL)
        elif kind == "class":
            emit(_OP_CLASS, node[1], 1 if node[2] else 0)
        elif kind == "cat":
            for child in node[1]:
                emit_node(child)
        elif kind == "alt":
            jumps = []
            for child in node[1][:-1]:
                split = emit(_OP_SPLIT, len(ops) + 1)
                emit_node(child)
                jumps.append(emit(_OP_JMP))
                ys[split] = len(ops)
            emit_node(node[1][-1])
            for jump in jumps:
                xs[jump] = len(ops)
        else:
            child, low, high = node[1], node[2], node[3]
            for _ in range(low):
                emit_node(child)
            if high is None:
                split = emit(_OP_SPLIT, len(ops) + 1)
                emit_node(child)
                emit(_OP_JMP, split)
                ys[split] = len(ops)
            else:
                splits = []
                for _ in range(high - low):
                    splits.append(emit(_OP_SPLIT, len(ops) + 1))
                    emit_node(child)
                for split in splits:
                    ys[split] = len(ops)

    emit_node(tree)
    emit(_OP_MATCH)
    return tuple(ops), tuple(xs), tuple(ys)


def _pattern_check(pattern: str) -> None:
    """Refuses, with the reason, a pattern outside the subset, as the catalog validation of C
    does: RegexError, a ValueError."""
    _pattern_tree(pattern)


def _utf8_characters(data: Union[bytes, bytearray, memoryview]) -> Iterator:
    """The characters of a value given as bytes, decoded as it is read, never copied whole: each a
    code point and whether it ends the value. A byte of no valid UTF-8 sequence (shortest form, at
    most U+10FFFF, no surrogate) is one character, U+DC00 + the byte, which is what surrogateescape
    makes of it; C reads it the same way."""
    view, length, at = memoryview(data), len(data), 0
    while at < length:
        first, need, value = view[at], 0, 0
        if first < 0x80:
            value = first
        elif 0xC2 <= first <= 0xDF:
            need, value, minimum = 1, first & 0x1F, 0x80
        elif 0xE0 <= first <= 0xEF:
            need, value, minimum = 2, first & 0x0F, 0x800
        elif 0xF0 <= first <= 0xF4:
            need, value, minimum = 3, first & 0x07, 0x10000
        else:
            need = -1
        if need > 0:
            # A sequence the value ends inside of is not one.
            if at + need >= length:
                need = -1
            else:
                for offset in range(1, need + 1):
                    following = view[at + offset]
                    if following & 0xC0 != 0x80:
                        need = -1
                        break
                    value = (value << 6) | (following & 0x3F)
            if need > 0 and (value < minimum or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF):
                need = -1
        if need < 0:
            value, need = 0xDC00 | first, 0
        at += need + 1
        yield value, at == length


def _str_characters(text: str) -> Iterator:
    """The characters of a value given as str: each code point, a lone surrogate included, which
    only `.` and a negated class match, as C matches a byte that is not UTF-8."""
    last = len(text) - 1
    for index, character in enumerate(text):
        yield ord(character), index == last


def _pattern_run(program: tuple, characters: Iterator, empty: bool, work: Optional[list] = None) -> bool:
    """A Thompson simulation of a pattern's program over a value's characters, as src/pattern.c
    runs it: O((len + 1) x states) in time, O(states) in memory. `work`, when a list, receives the
    positions popped and threads tried, which C counts alike."""
    ops, xs, ys = program
    mark = [0] * len(ops)
    generation = 0
    steps = 0

    def add(pc: int, at_start: bool, at_last: bool, out: list) -> bool:
        nonlocal steps
        stack = [pc]
        while stack:
            pc = stack.pop()
            steps += 1
            if mark[pc] == generation:
                continue
            mark[pc] = generation
            op = ops[pc]
            if op == _OP_JMP:
                stack.append(xs[pc])
            elif op == _OP_SPLIT:
                stack.append(ys[pc])
                stack.append(xs[pc])
            elif op == _OP_BOL:
                if at_start:
                    stack.append(pc + 1)
            elif op == _OP_EOL:
                if at_last:
                    stack.append(pc + 1)
            elif op == _OP_MATCH:
                return True
            else:
                out.append(pc)
        return False

    try:
        generation += 1
        current: list = []
        if add(0, True, empty, current):
            return True
        for code, at_last in characters:
            generation += 1
            following: list = []
            for pc in current:
                steps += 1
                op = ops[pc]
                if op == _OP_CHAR:
                    accepted = xs[pc] == code
                elif op == _OP_ANY:
                    # The four line terminators of ECMA-262 (agent-cli/v2, section 3).
                    accepted = code not in (0x0A, 0x0D, 0x2028, 0x2029)
                else:
                    inside = code < 0x80 and (xs[pc] >> code) & 1 == 1
                    accepted = not inside if ys[pc] else inside
                if accepted and add(pc + 1, False, at_last, following):
                    return True
            # A search: the pattern may also start here.
            if add(0, False, at_last, following):
                return True
            current = following
        return False
    finally:
        if work is not None:
            work.append(steps)


def _pattern_search(pattern: str, text: str, work: Optional[list] = None) -> bool:
    """Whether the value matches: a search, as regexec() and C's matcher make it."""
    return _pattern_run(_pattern_program(pattern), _str_characters(text), not text, work)


class Regex:
    """A pattern of the subset agent-cli/v2 section 3 writes, compiled (C: maelys_cli_regex_t).

    `Regex(pattern)` raises RegexError for a pattern outside the subset and TypeError when it is not
    a str. `search(value)` tells whether some part of the value matches, `^` and `$` holding at its
    two ends: a value is a str, each code point a character, or bytes, read as UTF-8 as they are
    searched and never copied whole, a byte of no valid sequence being one character. `.` matches
    every character but U+000A, U+000D, U+2028 and U+2029. The work is O((len + 1) x states), with no
    backtracking; `states` is the size of the program. A Regex is never changed once built."""

    __slots__ = ("pattern", "_program")

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._program = _pattern_program(pattern)

    @property
    def states(self) -> int:
        """The size of the compiled program, which bounds the work of a search."""
        return len(self._program[0])

    def search(self, value: Union[str, bytes]) -> bool:
        """Whether some part of the value matches."""
        if isinstance(value, str):
            return _pattern_run(self._program, _str_characters(value), not value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _pattern_run(self._program, _utf8_characters(value), len(value) == 0)
        raise TypeError("a value is a str or bytes")

    def __repr__(self) -> str:
        return f"Regex({self.pattern!r})"


class Failure(Exception):
    """A failure envelope: stable code, causal message, next safe action.

    `issues` are the causes under the code, each {"code", "message"} with an optional "path" (a string,
    or a list of strings and integers): where a product's domain detail goes under a code of the
    contract, such as {"code": "PANES_CONFIG", "path": "panes.conf:12", "message": ...} under
    VALIDATION_FAILED. `extensions` are members of the envelope's `error`, each named x- and then
    lowercase letters, digits and dashes: machine detail an agent may act on, such as the revisions of
    a REVISION_CONFLICT. The code is a stable one or a code of the product's own domain (CODE_GRAMMAR).
    Both go into JSON that agents log: no absolute path, environment or file content without a reason.
    JSON carries them; the text rendering is unchanged. A code, an issue or a member that breaks the
    rule is a defect of the command: the line is answered UNEXPECTED instead (C:
    maelys_cli_fail_error_extended)."""

    def __init__(self, code: str, message: str, hint: str = "", issues: Optional[list] = None,
                 extensions: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.issues = issues
        self.extensions = extensions
        # What failure() keeps for the text rendering in another language: the template and the text of
        # each of its conversions. A message built otherwise is translated only when a table names it
        # whole.
        self.template: Optional[str] = None
        self.arguments: "list[str]" = []
        self._hint_template: Optional[str] = None
        self._hint_arguments: "list[str]" = []


def failure(code: str, template: str, *arguments: Any, hint: str = "", issues: Optional[list] = None,
            extensions: Optional[dict] = None) -> Failure:
    """A Failure whose message is `template` with printf conversions -- %s, %d, %zu, %llu as C writes
    them -- filled by `arguments` (C: maelys_cli_fail). JSON carries the message in English; the text a
    person reads takes the template's translation when the language of the invocation has one, its
    arguments put back in order. Raise what it returns."""
    message, texts = _format_template(template, arguments)
    made = Failure(code, message, hint, issues, extensions)
    made.template, made.arguments = template, texts
    return made


def _fail(code: str, template: str, *arguments: Any, hint: str = "", hint_arguments: tuple = ()) -> Failure:
    """The framework's own failure(): its hint may be a template too, "Use '%s'." with the usage, which a
    table translates before the usage is put back."""
    made = failure(code, template, *arguments, hint=_format_template(hint, hint_arguments)[0] if hint_arguments
                   else hint)
    if hint_arguments:
        made._hint_template, made._hint_arguments = hint, _format_template(hint, hint_arguments)[1]
    return made


def _failure_problem(failure: Failure) -> Optional[str]:
    """What makes `failure` an envelope the contract refuses, or None (C: maelys_cli_fail_error_extended)."""
    if not isinstance(failure.code, str) or not CODE_GRAMMAR.fullmatch(failure.code):
        return "a code that is not capitals, digits and underscores after a capital"
    if failure.issues is not None:
        if not isinstance(failure.issues, list):
            return "issues that are not a JSON array"
        if len(failure.issues) > 64:
            return "more than 64 issues"
        for issue in failure.issues:
            if not isinstance(issue, dict):
                return "an issue that is not an object"
            path = issue.get("path", "")
            if set(issue) - {"code", "message", "path"} or not isinstance(issue.get("code", ""), str) \
                    or not isinstance(issue.get("message", ""), str) \
                    or not (isinstance(path, str) or (isinstance(path, list) and all(
                        isinstance(item, str) or (isinstance(item, int) and not isinstance(item, bool))
                        for item in path))):
                return "an issue whose members are not code, message and path as the schema has them"
            if "code" not in issue or "message" not in issue:
                return "an issue without a code or a message"
    if failure.extensions is not None:
        if not isinstance(failure.extensions, dict):
            return "extension members that are not a JSON object"
        if len(failure.extensions) > 64:
            return "more than 64 extension members"
        if any(not isinstance(name, str) or not EXTENSION_NAME.fullmatch(name) for name in failure.extensions):
            return "an extension member not named x- then lowercase letters, digits and dashes"
        try:
            json.dumps(failure.extensions, allow_nan=False)
        except (TypeError, ValueError):
            return "extension members that are not valid JSON"
    return None


def _checked_failure(failure: Failure, command_id: str) -> Failure:
    """`failure`, or the UNEXPECTED that says which of its parts the contract refuses."""
    problem = _failure_problem(failure)
    if problem is None:
        return failure
    return _fail("UNEXPECTED", "Command '%s' failed with %s.", command_id, problem,
                 hint="Report this defect to the command implementation.")


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
        # The required options first, then the optional ones, each spelled
        # as put_option() does in C (src/catalog.c).
        for item in sorted(options, key=lambda item: not item["required"]):
            if item.get("hidden"):
                continue
            piece = item["long"] + (f" {_value_placeholder(item['argument'], True)}" if "argument" in item else "")
            piece += "..." if item["repeatable"] else ""
            synopsis += " " + (piece if item["required"] else f"[{piece}]")
        if passthrough:
            synopsis += " [ARGUMENTS...]"
    elif not synopsis.startswith(pattern):
        raise ValueError(f"{identifier}: a synopsis starts with the pattern {pattern!r}, not {synopsis!r}")
    help_usage = _help_synopsis(synopsis, any(not item["required"] and not item.get("hidden") for item in options))
    return {"id": identifier, "pattern": words, "usage": synopsis, "helpUsage": help_usage, "purpose": purpose,
            "effect": effect,
            "outputMode": mode, "protocol": protocol, "external": external, "hidden": hidden,
            "unavailable": unavailable, "unavailableCode": unavailable_code,
            "operands": operands, "options": options, "passthrough": passthrough,
            "constraints": list(constraints), "examples": list(examples),
            "outputSchema": schema or {"type": "object"}, "handler": handler}


def _value_placeholder(value: dict, synopsis: bool = False) -> str:
    """What the reader replaces in an option's spelling. A declaration that
    names no value is Python's `VALUE`, the name describe gives when C
    declares no `.value_name`: a choice is then spelled by its choices joined
    by "|", in the synopsis and in help, and a digest `ALGORITHM:HEX` in the
    synopsis (C: put_option, option_label). Any other name is spelled as
    declared."""
    if value["name"] != "VALUE":
        return value["name"]
    if value["type"] == "choice" and value.get("choices"):
        return "|".join(value["choices"])
    if value["type"] == "digest" and synopsis:
        return "ALGORITHM:HEX"
    return value["name"]


def _help_synopsis(usage: str, optional: bool) -> str:
    """The synopsis help shows, one rule over the usage describe gives (C:
    maelys_cli_command_help_synopsis_alloc; spec, section 10): in each
    alternative a " | " separates, every optional option -- a bracketed group
    that starts with "--" -- leaves the line, and one "[options]" closes the
    alternative that lost any. The pattern, the operands with their arity or
    their choices, and the required options stay as usage writes them. A
    command whose usage names none of its optional options gets "[options]"
    at the end."""
    alternatives = []
    dropped_any = False
    for alternative in usage.split(" | "):
        units, unit, depth = [], "", 0
        for char in alternative + " ":
            if char == " " and depth == 0:
                if unit:
                    units.append(unit)
                unit = ""
                continue
            depth += 1 if char == "[" else -1 if char == "]" and depth > 0 else 0
            unit += char
        kept = [item for item in units if not (item.startswith("[--") and len(item) > 3)]
        dropped = len(kept) != len(units)
        dropped_any = dropped_any or dropped
        alternatives.append(" ".join(kept) + (" [options]" if dropped else ""))
    if optional and not dropped_any:
        alternatives[-1] += " [options]"
    return " | ".join(alternatives)


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
    options = list(options) + [flag("--apply", "Apply the reviewed plan; without it the command only plans.")]
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
    option("--lang", "Language of the human text.", argument("TAG", "string", pattern=LANGUAGE_TAG_PATTERN)),
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


HELP_PALETTE_ROLES = ("heading", "command", "option", "placeholder", "example")
_SGR_PARAMETERS = re.compile(r"[0-9;]{0,16}")
_HELP_PLAIN = {role: "" for role in HELP_PALETTE_ROLES}


def _help_styled(style: str, text: str) -> str:
    return f"{style}{text}\033[0m" if style and text else text


def _help_entry_parts(parts: list, text: str, label_width: int, width: int, groups: bool = False) -> str:
    """One entry of a list (C: help_entry_parts): the label, made of (text, style) parts measured plain,
    then the text beside it when the label fits its column, below it otherwise. A label wider than the
    line is wrapped between its groups, and then shown plain."""
    label = "".join(part for part, _ in parts)
    wide = display_width(label)
    if wide <= label_width or 2 + wide <= width:
        head = "  " + "".join(_help_styled(style, part) for part, style in parts)
    else:
        head = "  " + _help_wrap(label, 2, 8, width, groups)
    if wide <= label_width:
        column = label_width + 4
        return head + " " * (label_width - wide + 2) + _help_wrap(text, column, column, width) + "\n"
    return head + "\n" + " " * 6 + _help_wrap(text, 6, 6, width) + "\n"


def _help_entry(label: str, text: str, label_width: int, width: int, groups: bool = False, style: str = "") -> str:
    """One entry of a list: the text beside the label when the label fits its column, below it otherwise."""
    return _help_entry_parts([(label, style)], text, label_width, width, groups)


def _help_headings(text: str, heading: str) -> str:
    """Each heading of `text` -- a line of capitals at the margin -- in the `heading` sequence (C:
    help_headings). A character outside ASCII counts as a capital, so that a translated heading
    (OPÉRANDES) is one. The other roles are styled as they are written."""
    if not heading:
        return text
    return "\n".join(f"{heading}{line}\033[0m" if line and line[0] != " " and all(
        "A" <= c <= "Z" or c == " " or ord(c) >= 0x80 for c in line) else line for line in text.split("\n"))


def _help_names(lead: str, names: list, tail: str, width: int) -> str:
    """`lead`, then `names` joined by ", " and closed by a period, then `tail`, as one paragraph wrapped
    at the width (C: help_names). A name is never broken: `record stop` stays on one line."""
    units = lead.split() + [name + ("," if index + 1 < len(names) else ".") for index, name in enumerate(names)] \
        + tail.split()
    out, at, first = "  ", 2, True
    for unit in units:
        wide = display_width(unit)
        if not first and at + 1 + wide > width:
            out += "\n  "
            at = 2
        elif not first:
            out += " "
            at += 1
        out += unit
        at += wide
        first = False
    return out + "\n"


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
    # Each refusal is one sentence a table can translate whole: "Option %s" names an option, a bare name
    # an operand, and what the value takes is written in the template, its own arguments put in.
    subject, name = ("Option %s", where[len("Option "):]) if where.startswith("Option ") else ("%s", where)

    def refuse(what: str, *arguments: Any) -> Failure:
        return _fail("VALIDATION_FAILED", f"{subject} takes {what}, not '%s'.", name, *arguments, text,
                     hint="Use '%s'.", hint_arguments=(usage,))
    if kind == "boolean":
        if text not in ("true", "false"):
            raise refuse("true or false")
        return text == "true"
    if kind == "string":
        # The value MUST match the declared pattern (spec 2.3), written in the
        # common subset of ECMA-262 and POSIX ERE and read by _pattern_check;
        # search semantics, as ERE.
        if "pattern" in spec and not _pattern_search(spec["pattern"], text):
            raise refuse("a value matching %s", spec["pattern"])
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
        if "pattern" in spec and not _pattern_search(spec["pattern"], text):
            raise refuse("a value matching %s", spec["pattern"])
        return text
    elif kind == "absolute-path":
        if not text.startswith("/"):
            raise refuse("an absolute path")
        return text
    elif kind == "choice":
        if text not in spec.get("choices", []):
            raise refuse("%s", ", ".join(spec.get("choices", [])))
        return text
    elif kind == "hex":
        # The width is `digits`, one or two, as the C parser reads it.
        widths = spec["digits"] if isinstance(spec["digits"], list) else [spec["digits"]]
        if not re.fullmatch(r"[0-9a-f]+", text) or len(text) not in widths:
            raise refuse("%s lowercase hexadecimal digits", widths[0]) if len(widths) == 1 else \
                refuse("%s or %s lowercase hexadecimal digits", widths[0], widths[1])
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
            raise refuse("ALGORITHM:HEX with %s", ", ".join(spec["algorithms"])) if spec.get("algorithms") else \
                refuse("ALGORITHM:HEX with a declared algorithm")
        return text
    else:
        raise _fail("UNEXPECTED", "Value kind %s of %s is not known.", repr(kind), where, hint="Fix the catalog.")
    if ("minimum" in spec and value < spec["minimum"]) or ("maximum" in spec and value > spec["maximum"]):
        raise _fail("VALIDATION_FAILED", f"{subject} must be between %s and %s, not %s.", name,
                    spec.get("minimum", "-inf"), spec.get("maximum", "+inf"), text, hint="Use '%s'.",
                    hint_arguments=(usage,))
    return value


def _parse_flag(value: Optional[str], name: str, usage: str) -> bool:
    """Parse the explicit spellings accepted by the C flag parser."""
    if value is None:
        return True
    if value in ("true", "yes", "on", "1"):
        return True
    if value in ("false", "no", "off", "0"):
        return False
    raise _fail("VALIDATION_FAILED", "Option %s takes true, false, yes, no, on, off, 1 or 0, not '%s'.", name, value,
                hint="Use '%s'.", hint_arguments=(usage,))


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
                 color: str = "auto", field: Optional[str] = None, lang: Optional[str] = None) -> None:
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
        # --lang TAG (agent-cli/v2 section 5): the language this line asks for, None when absent; resolved
        # among the catalog's languages before the command runs.
        self.lang = lang
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

    def detail(self, message: str, *arguments: Any) -> None:
        """One detail line `PROGRAM: message` on stderr when --verbose is active in
        text mode (maelys_cli_detail); nothing in JSON; never a failure rendering.
        With `arguments`, `message` is a printf template, translated before they
        are put in; without, a text translated when a table names it whole."""
        if not self.verbose:
            return
        self.progress_done()
        message = self.program._human(message, *arguments)
        sys.stderr.write(f"{self.program.program}: {_terminal_safe(message)}\n")
        sys.stderr.flush()

    def show_progress(self, message: str, *arguments: Any) -> None:
        """Transient progress (maelys_cli_progress): rewritten in place on a terminal
        and erased by progress_done(); one line per call on another stderr. The
        message is translated as detail() translates it."""
        if not self.progress_wanted:
            return
        message = self.program._human(message, *arguments)
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
            raise _fail("PRECONDITION_FAILED",
                        "The plan --expect names is no longer the one '%s' would apply: the action or the state it "
                        "touches has changed. Nothing was written.",
                        self.command['id'],
                        hint="Plan again without --apply, review the new plan, then apply it with its fingerprint.")


class Program:
    def __init__(self, program: str, product: str, version: str, commands: list, guide: str = "",
                 text: Optional[dict] = None, framework: str = FRAMEWORK, static_completion: bool = True,
                 default_command: Optional[str] = None, help_palette: Optional[dict] = None,
                 translations: Optional[dict] = None, canonical_language: Optional[str] = None) -> None:
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
        # The built-ins are declared word for word as src/app.c declares them -- purposes, operands,
        # options and schemas -- so that describe says the same of them in C and in Python.
        self.catalog = [
            read("help", "help", "Show the help of the program, of one command or of one family.", self._help,
                 operands=[operand("COMMAND_ID", "Stable command identifier whose help is shown, or the family of "
                                   "commands under an identifier.", required=False)],
                 schema={"type": "object", "additionalProperties": False, "required": ["text", "commands"],
                         "properties": {"text": {"type": "string"},
                                        "commands": {"type": "array", "items": {"type": "string"}}}}),
            read("version", "version", "Show the product, program, version and contract.", self._version,
                 schema={"type": "object", "additionalProperties": False,
                         "required": ["product", "program", "version", "contract", "cliApi", "framework", "lang"],
                         "properties": {"product": {"type": "string"}, "program": {"type": "string"},
                                        "version": {"type": "string"}, "contract": {"const": CONTRACT},
                                        "cliApi": {"type": "integer"}, "framework": {"type": "string"},
                                        "lang": {"type": "string"}}}),
            read("describe", "describe", "Show the machine-readable catalog, its summary or one descriptor.",
                 self._describe,
                 operands=[operand("COMMAND_ID", "Stable command identifier returned by the catalog.", required=False)],
                 options=[flag("--summary", "Return the compact command inventory without output schemas."),
                          option("--prefix", "Restrict the summary to one command namespace: the command named PREFIX "
                                 "and every command whose identifier starts with PREFIX followed by a dot.",
                                 argument("PREFIX", "string", pattern=PREFIX_GRAMMAR.pattern),
                                 requires=("--summary",), conflicts_with=("COMMAND_ID",))],
                 schema={"type": "object", "description": "Command catalog, summary or single descriptor.",
                         "additionalProperties": True, "required": ["schemaVersion", "kind", "program", "commands"]}),
            read("completion", "completion", "Print the shell completion script generated from the catalog.",
                 self._completion, operands=[operand("SHELL", "Shell whose completion script is printed.",
                                                     choices=list(SHELLS))],
                 schema={"type": "object", "additionalProperties": False, "required": ["shell", "script"],
                         "properties": {"shell": {"enum": list(SHELLS)}, "script": {"type": "string"}}}),
            records("complete.candidates", "__complete", "Return completion candidates for a partial command line.",
                    self._complete,
                    operands=[operand("WORDS", "Command line words after the program name; the last one is the prefix "
                                      "being completed.", required=False, variadic=True)],
                    hidden=True,
                    schema={"type": "object", "additionalProperties": False, "required": ["count", "records"],
                            "properties": {"count": {"type": "integer"},
                                           "records": {"type": "array", "items": {"type": "object",
                                                                                  "required": ["word"]}}}}),
        ]
        self._builtin_count = len(self.catalog)
        self._family_words: Optional[list] = None
        seen = set()
        for command in commands:
            if command["id"] in seen or any(command["id"] == built["id"] for built in self.catalog):
                raise ValueError(f"duplicate command identifier {command['id']!r}")
            # A pattern outside the common subset is refused here, as C refuses
            # it at startup (src/pattern.c): the two read one motif.
            for item in command["options"]:
                pattern = item.get("argument", {}).get("pattern")
                if pattern is not None:
                    try:
                        _pattern_check(pattern)
                    except ValueError as error:
                        raise ValueError(f"{command['id']}: {item['long']} declares a pattern outside the common "
                                         f"subset of ECMA-262 and POSIX ERE: {error}") from None
            for item in command["operands"]:
                if "pattern" in item:
                    try:
                        _pattern_check(item["pattern"])
                    except ValueError as error:
                        raise ValueError(f"{command['id']}: operand {item['name']} declares a pattern outside the "
                                         f"common subset of ECMA-262 and POSIX ERE: {error}") from None
            seen.add(command["id"])
            self._check_references(command)
            self.catalog.append(command)
        # usage in help; describe uses the catalog's synopsis
        self.catalog[0]["usage"] = "help [COMMAND_ID] | --help"
        self.catalog[1]["usage"] = "version | --version"
        # The command a line that names none runs (spec, section 8; C: default_command and
        # validate_default_command), checked before the examples, which are parsed.
        self.default_command = default_command
        if default_command is not None:
            self._check_default_command(default_command)
        # The colors of help (C: maelys_cli_help_palette_t): SGR parameters per role, None or absent
        # for the framework's choice, "" for plain. Anything else could move a terminal's cursor.
        for role, value in (help_palette or {}).items():
            if role not in HELP_PALETTE_ROLES:
                raise ValueError(f"Help palette has no role {role!r}: the roles are {', '.join(HELP_PALETTE_ROLES)}.")
            if value is not None and not (isinstance(value, str) and _SGR_PARAMETERS.fullmatch(value)):
                raise ValueError(f"Help palette {role} {value!r} is not the parameters of an SGR sequence.")
        self.help_palette = dict(help_palette or {})
        self._style = _HELP_PLAIN
        # The language the catalog is written in (C: maelys_cli_app_t.canonical_language), English unless
        # the product names its own, and the languages it translates to: {tag: {canonical text: translation}}.
        if canonical_language is not None and not _language_tag_is_well_formed(canonical_language):
            raise ValueError(f"Catalog canonical language '{canonical_language}' is not a well-formed language tag.")
        self.canonical_language = canonical_language or CANONICAL_LANGUAGE
        self.translations = self._checked_translations(translations, self.canonical_language)
        # The tables human text is rendered with: the product's, then the framework's.
        self._tables: tuple = ()
        self._language: Optional[str] = None
        self._help_rendering: Optional[Callable[[], str]] = None
        # The examples last: parsing one needs the whole catalog.
        for command in self.catalog:
            self._check_examples(command)

    @staticmethod
    def _checked_translations(translations: Optional[dict], canonical: str = CANONICAL_LANGUAGE) -> dict:
        """The languages a product declares (C: validate_languages): each tag well formed and named once,
        the canonical language left out since it needs no table, every translation a text with the
        conversions of its source in the same order. The messages are those of the C catalog validation."""
        if translations is None:
            return {}
        if not isinstance(translations, dict) or not translations or len(translations) > MAX_LANGUAGES:
            raise ValueError(f"Catalog languages are declared incorrectly: a mapping of at most {MAX_LANGUAGES} "
                             "languages to their tables.")
        seen: set = set()
        checked = {}
        for tag, table in translations.items():
            if not _language_tag_is_well_formed(tag):
                raise ValueError(f"Catalog language '{tag}' is not a well-formed language tag.")
            if _language_lookup(tag, [canonical]):
                raise ValueError(f"Catalog language '{tag}' is the canonical language {canonical}, which has no "
                                 "table.")
            if tag.lower() in seen:
                raise ValueError(f"Catalog language '{tag}' is declared twice.")
            seen.add(tag.lower())
            if not isinstance(table, dict):
                raise ValueError(f"Catalog language '{tag}' is not a table of texts.")
            for index, (source, text) in enumerate(table.items()):
                if not _translation_text_is_safe(source) or not _translation_text_is_safe(text):
                    raise ValueError(f"Catalog language '{tag}' translation {index} is empty or holds a control "
                                     "character.")
                try:
                    same = _template_conversions(source) == _template_conversions(text)
                except ValueError:
                    same = False
                if not same:
                    raise ValueError(f"Catalog language '{tag}' translates '{source}' without its conversions, "
                                     "in order.")
            checked[tag] = dict(table)
        return checked

    def _human_tables(self) -> tuple:
        """The tables a person's text is translated with in this invocation: the product's for the language
        resolved, none in its canonical language, then the framework's own for the language in force, the
        canonical one included (C: resolve_language)."""
        product = (self.translations[self._language],) if self._language is not None else ()
        framework = _language_lookup(self._language or self.canonical_language, list(_FRAMEWORK_TRANSLATIONS))
        return product + ((_FRAMEWORK_TRANSLATIONS[framework],) if framework is not None else ())

    def _resolve_language(self, option: Optional[str] = None) -> None:
        """The language of human text for this invocation, among those the product declares (C:
        resolve_language; agent-cli/v2 section 5): the first source that is not empty decides alone --
        --lang, MAELYS_CLI_LANG, LC_ALL, LC_MESSAGES, LANG -- and a request the product cannot answer, C
        included, gives its canonical language: _language is then None."""
        found = _language_resolve(option, os.environ.get("MAELYS_CLI_LANG"), os.environ.get("LC_ALL"),
                                  os.environ.get("LC_MESSAGES"), os.environ.get("LANG"),
                                  [self.canonical_language, *self.translations])
        self._language = None if found == self.canonical_language else found

    def _human(self, template: str, *arguments: Any) -> str:
        """`template` in the language of the invocation, its printf arguments put in after the translation
        (C: human_vformat): the text of a detail, a progress line or a warning. Without arguments the text
        is not formatted, and is translated only when a table names it whole."""
        tables = self._human_tables()
        translated = next((table[template] for table in tables if template in table), template)
        return _format_template(translated, arguments)[0] if arguments else translated

    def _hint_label(self) -> str:
        """`Hint:` in the language of the invocation, its colon kept (agent-cli/v2 section 8)."""
        return next((table["Hint:"] for table in self._human_tables() if "Hint:" in table), "Hint:")

    def _human_failure(self, failure: Failure) -> "tuple[str, str]":
        """The message and hint of `failure` as a person reads them in the language of the invocation (C:
        maelys_cli_translate_error): the template translated and its arguments put back, or a message a
        table names whole; the hint translated whole. JSON keeps both in English."""
        tables = self._human_tables()
        message, hint = failure.message, failure.hint
        if not tables:
            return message, hint

        def lookup(source: str) -> Optional[str]:
            return next((table[source] for table in tables if source in table), None)

        translated = lookup(failure.template) if failure.template is not None else None
        if translated is not None:
            message = _substitute(translated, failure.arguments) or message
        else:
            message = lookup(message) or message
        if failure._hint_template is not None:
            translated = lookup(failure._hint_template)
            hint = (_substitute(translated, failure._hint_arguments) if translated else None) or hint
        elif hint:
            hint = lookup(hint) or hint
        return message, hint

    def _check_default_command(self, identifier: str) -> None:
        """A command of the product that a line naming nothing may run: not a built-in, not hidden or
        unavailable, not a delegate, and a read, a stream or a transaction -- a line that names nothing is
        no consent to an action. The messages are those of the C catalog validation."""
        target = next((item for item in self.catalog if item["id"] == identifier), None)
        problem = (
            "names no command of the catalog" if target is None else
            "names a command every program has" if self.catalog.index(target) < self._builtin_count else
            "names a command this build cannot run" if target["unavailable"] is not None else
            "names a hidden command" if target["hidden"] else
            "names a delegate, whose line this program does not read" if target["external"] or target["passthrough"] else
            "names a command that is neither a read, a stream nor a transaction: a line that names nothing is "
            "no consent to an action" if not (target["effect"] in ("read", "stream") or isinstance(target["effect"], dict))
            else None)
        if problem is not None:
            raise ValueError(f"Catalog default_command '{identifier}' {problem}.")

    def _default_forms(self, pattern: str) -> str:
        """The two ways to invoke the default command (C: default_forms), each a line to copy and so,
        as an example, never wrapped."""
        example = self._style["example"]
        return (f"  {_help_styled(example, f'{self.program} [options] [-- OPERANDS]')}\n"
                f"  {_help_styled(example, f'{self.program} {pattern} [options] [-- OPERANDS]')}\n")

    def _default(self) -> Optional[dict]:
        return self.command_by_id(self.default_command) if self.default_command is not None else None

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
        raise _fail("INVALID_COMMAND", "Unknown command identifier '%s'.", identifier,
                    hint="Run describe --summary to list the command identifiers.")

    def resolve(self, words: list) -> "tuple[Optional[dict], int]":
        best = None
        for command in self.catalog:
            pattern = command["pattern"]
            if words[:len(pattern)] == pattern and (best is None or len(pattern) > len(best["pattern"])):
                best = command
        return best, len(best["pattern"]) if best else 0

    def _help_style(self) -> dict:
        """The escape sequences of the product's palette, each role it leaves out taking the
        framework's choice: bold headings, everything else plain (C: help_style_init)."""
        chosen = {"heading": "1", **{role: value for role, value in self.help_palette.items() if value is not None}}
        return {role: f"\033[{chosen[role]}m" if chosen.get(role) else "" for role in HELP_PALETTE_ROLES}

    def _tr(self, source: str) -> str:
        """`source` in the language the help is rendered in for a person: the product's table, then the
        framework's, then `source` itself (C: tr). English while data.text is rendered."""
        for table in self._tables:
            if source in table:
                return table[source]
        return source

    def _trf(self, template: str, *arguments: Any) -> str:
        """A template of the help translated before its arguments are put in (C: trf)."""
        return _format_template(self._tr(template), arguments)[0]

    def _option_parts(self, item: dict) -> "tuple[str, str, str]":
        """An option's label in three parts: its name, what the reader replaces, and " (repeatable)"."""
        value = item.get("argument")
        return item["long"], _value_placeholder(value) if value else "", \
            " " + self._tr("(repeatable)") if item["repeatable"] else ""

    @staticmethod
    def _option_label(item: dict) -> str:
        value = item.get("argument")
        spelled = f"{item['long']} {_value_placeholder(value)}" if value else item["long"]
        return spelled + (" (repeatable)" if item["repeatable"] else "")

    def _option_text(self, item: dict) -> str:
        text = self._tr(item["summary"])
        if "default" in item:
            text += " " + self._trf("Default: %s.", item["default"])
        if item["required"]:
            text += " " + self._tr("Required.")
        # C names an option without its dashes in these two sentences: "Requires --%s.".
        text += "".join(" " + (self._trf("Requires --%s.", name[2:]) if name.startswith("--")
                               else self._trf("Requires %s.", name)) for name in item["requires"])
        text += "".join(" " + (self._trf("Conflicts with --%s.", name[2:]) if name.startswith("--")
                               else self._trf("Conflicts with %s.", name)) for name in item["conflictsWith"])
        return text

    def _options_help(self, options: list, width: int) -> str:
        shown = [(self._option_parts(item), self._option_text(item)) for item in options if not item.get("hidden")]
        labels = [name + (f" {value}" if value else "") + suffix for (name, value, suffix), _ in shown]
        column = max([display_width(label) for label in labels if display_width(label) <= HELP_LABEL_MAXIMUM] or [0])
        style = self._style
        return "".join(_help_entry_parts([(name, style["option"]), (" " if value else "", ""),
                                          (value, style["placeholder"]), (suffix, "")], text, column, width)
                       for (name, value, suffix), text in shown)

    def _effect_sentence(self, command: dict) -> Optional[str]:
        """What a command does that changes what its reader types, in words
        (C: effect_sentence): a transaction plans until --apply, an execute
        command acts at once, a stream and a delegate own their output."""
        effect = command["effect"]
        if command["external"]:
            return self._tr("The rest of the line goes to another program, word for word.")
        if isinstance(effect, dict):
            return self._tr("It shows a plan; --apply commits it." if effect["apply"] == "commit"
                            else "It shows a plan; --apply applies it.")
        if effect == "execute":
            return self._tr("It acts at once, without a plan.")
        if effect == "stream" and command["protocol"]:
            return self._trf("Its output is the %s protocol: --format and the other rendering options do not apply.",
                             command["protocol"])
        if effect == "stream":
            return self._tr("Its output is its own: --format and the other rendering options do not apply.")
        return None

    def command_help(self, command: dict, width: int = HELP_WIDTH) -> str:
        """The help of one command (C: command_help_text): its usage, what it
        is for, its examples, then its operands and options, laid out within
        `width` columns."""
        tr = self._tr
        text = tr("USAGE") + "\n" + _help_paragraph(f"{self.program} {command['helpUsage']}", width, groups=True)
        effect = self._effect_sentence(command)
        text += "\n" + _help_wrap(tr(command["purpose"]) + (f" {effect}" if effect else ""), 0, 0, width) + "\n"
        if command["unavailable"] is not None:
            text += f"\n{tr('UNAVAILABLE IN THIS BUILD')}\n" + _help_paragraph(tr(command["unavailable"]), width)
        if command["examples"]:
            # A line to copy: on one line whatever the width, the one place
            # the help passes it on purpose. Wrapped, the first half was a
            # command of its own and the second another. The sentence wraps.
            # And spelled for a shell: a word holding `$`, `*` or a quote,
            # copied as declared, would be another word once pasted.
            text += f"\n{tr('EXAMPLES')}\n" + "".join(
                "  " + _help_styled(self._style["example"],
                                    f"{self.program} {' '.join(_shell_word(word) for word in item['words'])}") + "\n"
                + " " * 6 + _help_wrap(tr(item["summary"]), 6, 6, width) + "\n"
                for item in command["examples"])
        if self.default_command == command["id"]:
            text += f"\n{tr('DEFAULT COMMAND')}\n" + _help_paragraph(tr(
                "A line that names no command runs this one: an empty line, or one that starts with an option "
                "other than --help and --version. Its operands come after --:"), width) \
                + self._default_forms(" ".join(command["pattern"]))
        if command["operands"]:
            column = max([display_width(item["name"]) for item in command["operands"]
                          if display_width(item["name"]) <= HELP_LABEL_MAXIMUM] or [0])
            text += f"\n{tr('OPERANDS')}\n" + "".join(
                _help_entry(item["name"], tr(item["summary"]) + ("" if item["required"] else " " + tr("Optional.")),
                            column, width,
                            style=self._style["placeholder"])
                for item in command["operands"])
        if any(not item.get("hidden") for item in command["options"]):
            text += f"\n{tr('OPTIONS')}\n" + self._options_help(command["options"], width)
        text += f"\n{tr('GLOBAL OPTIONS')}\n" + _help_paragraph(self._trf(
            "Run '%s --help' for --format, --json, --compact, --non-interactive, --color and the others.",
            self.program), width)
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
        tr = self._tr
        text = self._trf("%s - commands", f"{self.program} {name}") + f"\n\n{tr('COMMANDS')}\n"
        for command in commands:
            purpose = tr(command["purpose"]) + (" " + tr("(unavailable in this build)")
                                                if command["unavailable"] is not None else "")
            text += _help_entry(command["helpUsage"], purpose, 0, width, groups=True, style=self._style["command"])
        return text + "\n" + _help_wrap(self._trf(
            "Run '%s help COMMAND_ID' or '%s COMMAND --help' for the operands and options of one command.",
            self.program, self.program), 0, 0, width) + "\n"

    def warn(self, message: str, *arguments: Any) -> None:
        """A diagnostic on stderr, `program: warning: message`, as maelys_cli_warn(); never stdout. The
        message is translated as Invocation.detail() translates it."""
        message = self._human(message, *arguments)
        sys.stderr.write(f"{self.program}: warning: {_terminal_safe(message)}\n")
        sys.stderr.flush()

    def guide(self, width: int = HELP_WIDTH) -> str:
        """The general help, a screen for a human (C: catalog_help_text): what
        the program is, how a line is written, the product's commands by their
        pattern and purpose. What every program has is named in a line: the
        built-in commands, the global options, the agent contract. A command
        this build cannot run is named at the end, never mixed with the rest."""
        tr = self._tr
        text = _help_wrap(f"{self.program} {self.version} - {tr(self.guide_line)}", 0, 0, width) + "\n\n"
        text += f"{tr('USAGE')}\n  {self.program} COMMAND [OPERANDS] [options]\n"
        column = display_width(f"{self.program} help COMMAND")
        text += _help_entry(f"{self.program} help COMMAND", tr("one command, its options and examples"), column, width)
        text += _help_entry(f"{self.program} help FAMILY", tr("the commands of one family"), column, width)
        # The default command in one sentence that says the three things the
        # spec (section 8) asks of help; where its operands go is in its help.
        default = self._default()
        if default is not None:
            pattern = " ".join(default["pattern"])
            text += "\n" + _help_paragraph(self._trf(
                "%s [options] runs '%s', as %s %s [options] does: the options in front, other than --help and "
                "--version, are its.", self.program, pattern, self.program, pattern), width)
        visible = [(index, command) for index, command in enumerate(self.catalog) if not command["hidden"]]
        own = [command for index, command in visible if index >= self._builtin_count and command["unavailable"] is None]
        built_in = [command for index, command in visible if index < self._builtin_count]
        unavailable = [command for index, command in visible
                       if index >= self._builtin_count and command["unavailable"] is not None]
        column = max([display_width(" ".join(command["pattern"])) for command in own
                      if display_width(" ".join(command["pattern"])) <= HELP_LABEL_MAXIMUM] or [0])
        text += f"\n{tr('COMMANDS')}\n" + "".join(
            _help_entry(" ".join(command["pattern"]), tr(command["purpose"]), column, width,
                        style=self._style["command"])
            for command in own)
        if built_in or unavailable:
            text += "\n"
        if built_in:
            text += _help_names(tr("Also:"), [" ".join(command["pattern"]) for command in built_in], "", width)
        if unavailable:
            text += _help_names(tr("Not available in this build:"),
                                [" ".join(command["pattern"]) for command in unavailable],
                                self._trf("'%s help COMMAND' says why.", self.program), width)
        # The options every command takes, whole: a human reads them here and
        # nowhere else. The contract an agent relies on is describe's
        # (`invariants`, `output`) and the manual's; one line points there.
        text += f"\n{tr('GLOBAL OPTIONS')}\n" + self._options_help(GLOBAL_OPTIONS, width)
        text += f"\n{tr('AGENTS')}\n" + _help_paragraph(self._trf(
            "Run '%s describe --summary --format json' first, and build every line from its input, never from "
            "this text.", self.program), width)
        return text

    # ---- built-in handlers ----

    def _version(self, invocation: Invocation) -> "tuple[dict, int]":
        return {"product": self.product, "program": self.program, "version": self.version, "contract": CONTRACT,
                "cliApi": CLI_API, "framework": self.framework, "lang": self.canonical_language}, EXIT_OK

    def _help(self, invocation: Invocation) -> "tuple[dict, int]":
        width = help_width(invocation.format)
        words, self._family_words = self._family_words, None
        render: Callable[[], str]
        if words:
            # `PROGRAM note --help`: the words named no command, but a family.
            family = self._family(words=words)
            commands = [command["id"] for command in family]
            render = lambda: self.family_help(" ".join(words), family, width)  # noqa: E731
        elif invocation.operands:
            query = invocation.operands[0]
            command = next((item for item in self.catalog if item["id"] == query), None)
            if command is not None:
                commands = [command["id"]]
                render = lambda: self.command_help(command, width)  # noqa: E731
            else:
                # Not a command: a family, the namespace describe --prefix selects.
                family = self._family(prefix=query)
                if not family:
                    raise _fail("INVALID_COMMAND", "Unknown command identifier or family: %s.", query,
                                hint="Run 'help' without operands to list the commands and their families.")
                commands = [item["id"] for item in family]
                render = lambda: self.family_help(query, family, width)  # noqa: E731
        else:
            commands = [c["id"] for c in self.catalog if not c["hidden"]]
            render = lambda: self.guide(width)  # noqa: E731
        # data.text is the plain rendering in the language of the invocation; a terminal with color is shown
        # the palette's (C: help_reply).
        self._help_rendering = render
        self._tables = self._human_tables()
        try:
            text = render()
        finally:
            self._tables = ()
        return {"text": text, "commands": commands}, EXIT_OK

    def _describe(self, invocation: Invocation) -> "tuple[dict, int]":
        data: dict = {"schemaVersion": CATALOG_SCHEMA, "program": self.program, "product": self.product,
                      "version": self.version, "contract": CONTRACT, "cliApi": CLI_API, "framework": self.framework}
        if self.default_command is not None:
            # In every form: it says how the program reads a line, not which commands are shown.
            data["defaultCommand"] = self.default_command
        prefix = invocation.option("--prefix")
        if prefix is not None:
            if not PREFIX_GRAMMAR.fullmatch(prefix):
                raise _fail("VALIDATION_FAILED", "Option --prefix takes a command identifier prefix, not '%s'.",
                            prefix,
                            hint="Use lowercase letters, digits, '.' and '-', starting with a letter.")
            selected = [self.descriptor(c, summary=True) for c in self.catalog
                        if c["id"] == prefix or c["id"].startswith(f"{prefix}.")]
            if not selected:
                raise _fail("INVALID_COMMAND", "No command in namespace: %s.", prefix,
                            hint="Run describe --summary --format json and select a returned command identifier.")
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
            # What a manual page needs and the contract does not carry (C: x-maelys-summary), an extension
            # member a generic agent ignores; the catalog form alone, which a manual reads.
            data["x-maelys-summary"] = self.guide_line
            # The product's languages, for a manual page in each (C: describe_translations).
            if self.translations or self.canonical_language != CANONICAL_LANGUAGE:
                data["x-maelys-translations"] = {"version": 1, "canonical": self.canonical_language,
                                                 "languages": self.translations}
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
        # A catalog with a default command gets the script that calls __complete: the static table
        # resolves a command from its pattern words, and a line of the default command has none.
        rows = self._static_rows() if (self.static_completion if static is None else static) \
            and self.default_command is None else None
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
        consumed = len(command["pattern"]) if command is not None else 0
        # A first word that is an option selects the default command, whose words the rest are.
        default = self._default()
        if command is None and default is not None and previous and previous[0].startswith("-") \
                and previous[0] not in ("--help", "--version"):
            command, consumed = default, 0
        candidates: list = []
        if command is None:
            # The next pattern word of every command that starts with the words given so far.
            candidates = [other["pattern"][len(previous)] for other in shown
                          if len(other["pattern"]) > len(previous) and other["pattern"][:len(previous)] == previous]
            if default is not None and not previous and current.startswith("-"):
                # At the root, a word with a dash may begin the line of the default command.
                shared = [] if default["outputMode"] == "protocol-stream" else GLOBAL_OPTIONS
                candidates += [item["long"] for item in default["options"] + shared if not item.get("hidden")]
        elif command["external"]:
            # The words after a delegate's pattern are the delegate's, which this catalog does not hold:
            # none, and never this program's own options (section 9).
            pass
        else:
            after = previous[consumed:]
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
        # With a default command, the first word of the line, and it only, says whether the line names a
        # command (spec, section 8): an option, `--`, a word with one dash or an empty line select the
        # default command, whose words the rest are; a bare word further on is no command and no operand.
        default = self._default()
        implicit = default is not None and (not argv or (argv[0].startswith("-") and argv[0] not in ("--help", "--version")))
        while index < len(argv):
            word = argv[index]
            command, consumed = (default, 0) if implicit else self.resolve(words)
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
            if implicit:
                # Refused below at its turn, in the name of the default command.
                raw.append((word, _BARE_WORD))
                index += 1
                continue
            words.append(word)
            index += 1
        # A word that starts with one dash names no command and selects none:
        # `-x --help` resolves nothing, as in C.
        if implicit:
            words = list(default["pattern"])
            consumed = len(words)
        elif not words and any(name in ("--help", "--version") for name, _ in raw) \
                and not any(value is _DASH_WORD for _, value in raw):
            words = ["help"] if any(name == "--help" for name, _ in raw) else ["version"]
            raw = [(name, value) for name, value in raw if name not in ("--help", "--version")]
        if not words:
            raise _fail("INVALID_COMMAND", "No command given.", hint="Run '%s help' or 'describe --summary'.",
                        hint_arguments=(self.program,))
        command, consumed = self.resolve(words)
        if command is None and any(name == "--help" for name, _ in raw) and self._family(words=words):
            # `PROGRAM note --help`: the words name no command, but a family of
            # them, and help was asked for: the help of that family.
            self._family_words = list(words)
            words = ["help"]
            raw = [(name, value) for name, value in raw if name != "--help"]
            command, consumed = self.resolve(words)
        if command is None:
            raise _fail("INVALID_COMMAND", "Unknown command '%s'.", ' '.join(words),
                        hint="Run describe --summary and use one of the listed command identifiers.")
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
        lang = None
        format_requested = False
        rendering: list = []
        help_requested = False
        options: dict = {}
        seen: set = set()
        known = {item["long"]: item for item in command["options"]}
        for name, value in raw:
            if name in seen and not (name in known and known[name]["repeatable"]):
                raise _fail("VALIDATION_FAILED", "Option %s is given twice.", name, hint="Give each option once.")
            if value is _DASH_WORD:
                raise _fail("VALIDATION_FAILED", "Option %s is not supported: options are spelled --name.", name,
                            hint="Spell an option --name; write an operand that starts with a dash after --.")
            if value is _BARE_WORD:
                raise _fail("VALIDATION_FAILED",
                            "'%s' is neither an option nor the value of one: the line names no command and runs the "
                            "default command '%s', whose operands are written after --.",
                            name, command['id'],
                            hint="Write the command's pattern first, or the operands of the default command after --.")
            seen.add(name)
            declared = known.get(name) or next((item for item in GLOBAL_OPTIONS if item["long"] == name), None)
            if declared is not None and "argument" in declared and value is None:
                raise _fail("VALIDATION_FAILED", "Option %s needs a value %s.", name, declared['argument']['name'],
                            hint="Run describe for this command and pass the option's argument.")
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
            elif name == "--lang":
                lang = parse_value("string", value or "", {"pattern": LANGUAGE_TAG_PATTERN}, "Option --lang", usage)
            elif name == "--help":
                help_requested = _parse_flag(value, name, usage)
            elif name in ("--dry-run", "--plan") and isinstance(command["effect"], dict):
                raise _fail("VALIDATION_FAILED", "Option %s is not supported by '%s': it plans by default.", name,
                            command['id'],
                            hint="Run without --apply to plan, then add --apply to write.")
            elif name not in known:
                raise _fail("VALIDATION_FAILED", "Option %s is not supported by '%s'. Use '%s'.", name, command['id'],
                            usage,
                            hint="Run describe for this command and use only its declared options.")
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
                    raise _fail("VALIDATION_FAILED", "Option %s requires %s.", name, other, hint="Use '%s'.",
                                hint_arguments=(usage,))
            for other in definition["conflictsWith"]:
                if other.startswith("--"):
                    if enabled(other):
                        raise _fail("VALIDATION_FAILED", "Option %s conflicts with %s.", name, other,
                                    hint="Use '%s'.", hint_arguments=(usage,))
                else:
                    position = next((i for i, item in enumerate(command["operands"]) if item["name"] == other), None)
                    if position is not None and len(raw_operands) > position:
                        raise _fail("VALIDATION_FAILED", "Option %s conflicts with the %s operand.", name, other,
                                    hint="Use '%s'.", hint_arguments=(usage,))
        groups: dict = {}
        for definition in command["options"]:
            if "group" in definition:
                groups.setdefault(definition["group"], []).append(definition["long"])
        for members in groups.values():
            present = [name for name in members if enabled(name)]
            if present and len(present) != len(members):
                raise _fail("VALIDATION_FAILED", "Options %s are given together or not at all.", ', '.join(members),
                            hint="Use '%s'.", hint_arguments=(usage,))
        # The rules the command states itself (spec 2.5), in the same causal
        # slot as the dependencies they extend; exactly-one refuses zero as
        # it refuses two.
        for rule in command["constraints"]:
            present = [name for name in rule["options"] if enabled(name)]
            listed = ", ".join(rule["options"])
            if rule["kind"] == "requires" and enabled(rule["options"][0]):
                for other in rule["options"][1:]:
                    if not enabled(other):
                        raise _fail("VALIDATION_FAILED", "Option %s requires %s.", rule['options'][0], other,
                                    hint="Use '%s'.", hint_arguments=(usage,))
            elif rule["kind"] == "at-most-one" and len(present) > 1:
                raise _fail("VALIDATION_FAILED", "At most one of %s may be given.", listed, hint="Use '%s'.",
                            hint_arguments=(usage,))
            elif rule["kind"] == "exactly-one" and len(present) != 1:
                raise _fail("VALIDATION_FAILED", "Exactly one of %s must be given.", listed, hint="Use '%s'.",
                            hint_arguments=(usage,))
        for definition in command["options"]:
            if definition["required"] and definition["long"] not in options:
                raise _fail("VALIDATION_FAILED", "Option %s is required by '%s'.", definition['long'], command['id'],
                            hint="Use '%s'.", hint_arguments=(usage,))
        operands: list = []
        if not command["passthrough"]:
            required = sum(1 for item in command["operands"] if item["required"])
            variadic = any(item["variadic"] for item in command["operands"])
            if len(raw_operands) < required or (not variadic and len(raw_operands) > len(command["operands"])):
                raise _fail("VALIDATION_FAILED", "Operands do not match '%s'. Use '%s'.", command['id'], usage,
                            hint="Use the synopsis returned by describe and retry.")
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
            raise _fail(command.get("unavailableCode") or "UNSUPPORTED",
                        "Command '%s' is not available in this build: %s", command['id'],
                        command['unavailable'],
                        hint="Use another build of the product, or another command.")
        if command["outputMode"] == "protocol-stream" and rendering:
            raise _fail("VALIDATION_FAILED", "Command '%s' owns its stdout and refuses %s.", command['id'],
                        rendering[0],
                        hint="Set MAELYS_CLI_FORMAT=json in the environment to receive its failure envelope as JSON.")
        if fmt == "jsonl" and field is None and command["outputMode"] != "json-records":
            raise _fail("VALIDATION_FAILED",
                        "--format jsonl is accepted only by json-records commands, not '%s'; add --field to render "
                        "one member in jsonl.",
                        command['id'], hint="Use --format json.")
        # data is governed by outputSchema; a filtered envelope would not validate
        # against it (spec 2.4). An environment MAELYS_CLI_FORMAT=json applies after
        # parsing and cannot be caught here; main() refuses it too, defensively.
        if field is not None and fmt == "json" and format_requested:
            raise Failure("VALIDATION_FAILED",
                          "--field conflicts with --format json: a filtered envelope would not "
                          "validate against the command's outputSchema.",
                          "Use --format text or --format jsonl with --field.")
        return Invocation(self, command, operands, options, fmt, compact, non_interactive, raw_operands,
                          verbose, progress, pager, color, field, lang), command

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
        # The --lang of a line the parser may refuse, so that its refusal is written in that language.
        self._resolve_language(_prescan_language(argv))
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
            # The line as the parser read it: a delegate's words are its own.
            self._resolve_language(invocation.lang)
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
                        raise _fail("VALIDATION_FAILED",
                                    "Option --field is not accepted by '%s': the command can write and its output "
                                    "schema requires no member.",
                                    command_id,
                                    hint="Run the command without --field and read the member from its result.")
                    if invocation.field not in required:
                        raise _fail("VALIDATION_FAILED",
                                    "Option --field names '%s', which '%s' does not always return: a command that "
                                    "can write accepts only a member its output schema requires.",
                                    invocation.field, command_id,
                                    hint="Use a member the command's output schema requires; describe lists them.")
            data, exit_code = command["handler"](invocation)
            invocation.progress_done()
            if invocation.options.get("--expect") is not None and not invocation._expect_checked \
                    and isinstance(command["effect"], dict):
                # --expect was given and the handler answers without having
                # asked invocation.expect(): the caller would believe a
                # binding nobody checked (C: succeed_with).
                raise _fail("UNEXPECTED",
                            "Command '%s' answered without checking --expect: what it did is not known to be the "
                            "plan the fingerprint names.",
                            command_id,
                            hint="Report this defect to the command implementation.")
            if invocation.field is not None:
                # A name absent from data is discoverable only now, once the
                # handler has run.
                if invocation.field not in data:
                    raise _fail("VALIDATION_FAILED", "Option --field names '%s', which '%s' does not have.",
                                invocation.field, command_id,
                                hint="Use a top-level member of the command's data.")
                value = data[invocation.field]
                if fmt == "jsonl":
                    sys.stdout.write(field_jsonl(value))
                else:
                    text = field_text(value)
                    if not page_text(text, invocation):
                        sys.stdout.write(text)
            elif fmt == "text":
                text = self.render_text(command, data)
                # What a person reads on a terminal that takes color: colored (C: help_reply).
                color = invocation.color_stdout
                if command_id == "help" and self._help_rendering is not None and color:
                    style = self._help_style()
                    self._style, self._tables = style, self._human_tables()
                    try:
                        text = _help_headings(self._help_rendering(), style["heading"])
                    finally:
                        self._style, self._tables = _HELP_PLAIN, ()
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
            failure = _checked_failure(failure, command_id or self.resolved_command_id or "unknown")
            error: dict = {"code": failure.code, "message": failure.message}
            if failure.hint:
                error["hint"] = failure.hint
            if failure.issues:
                error["issues"] = failure.issues
            error.update(failure.extensions or {})
            if fmt == "text":
                # A person reads it in the language of the invocation; the "Hint:" label stays, as the
                # contract writes it (section 8).
                message, hint = self._human_failure(failure)
                sys.stderr.write(self._colored(
                    f"{self.program}: [{failure.code}] {_terminal_safe(message)}",
                    self._failure_color(invocation, argv)) + "\n")
                if hint:
                    sys.stderr.write(f"{self._hint_label()} {_terminal_safe(hint)}\n")
            else:
                sys.stderr.write(self.envelope(command_id or self.resolved_command_id or "unknown", False,
                                               EXIT_FAILURE, error, compact))
            return EXIT_FAILURE
        except OSError as error:
            # An OSError a handler did not convert: same table as maelys_cli_fail_file().
            failure = file_failure(error)
            payload = {"code": failure.code, "message": failure.message, "hint": failure.hint}
            if fmt == "text":
                message, hint = self._human_failure(failure)
                sys.stderr.write(self._colored(
                    f"{self.program}: [{failure.code}] {_terminal_safe(message)}",
                    self._failure_color(invocation, argv)) + "\n")
                sys.stderr.write(f"{self._hint_label()} {_terminal_safe(hint)}\n")
            else:
                sys.stderr.write(self.envelope(command_id or self.resolved_command_id or "unknown", False,
                                               EXIT_FAILURE, payload, compact))
            return EXIT_FAILURE
