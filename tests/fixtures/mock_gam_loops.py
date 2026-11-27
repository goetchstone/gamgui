"""The mock's two GAM loops that read standard input (roadmap 5.4's signature fast path), called by mock_gam.sh.

Each runs the mock once per row or person, the way GAM runs a command per item, so the strict one-person handlers decide
every answer and these only do GAM's looping, from GAM 7.52.05's source:

- `csv - columndelimiter , noescapechar true quotechar " gam user ~User signature ~signature html` (doCSV): its options
  read in GAM's own order, once each (`csv_options`), then the rows read with csv.DictReader with those options after
  sys.stdin.read() (universal newlines); a
  `~Field` the header lacks ends it (csvFieldErrorExit); an empty cell substitutes "" (processSubFields). Each row is its
  own child; its output is passed on whole. GAM's parent takes no child's exit code, so this exits 0 when rows failed.
- `file - print signature` (getEntitiesFromFile, printShowSignature): the addresses one a line, read in one process;
  "Getting Signatures for <u> (i/n)" per person on stderr, one CSV of every row (titles User then sorted, the `signature`
  column only when a row has one), written in GAM's dialect (doubled quotes and a backslash escape).

Only these shapes are modelled: anything else is refused, as the mock refuses what it doesn't model, and an option GAM
would leave over is GAM's usage error.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys

INNER_SIGNATURE = ["user", "~User", "signature", "~signature", "html"]
FILE_PRINT_SIGNATURE = ["file", "-", "print", "signature"]
TRUE, FALSE = {"true", "on", "yes", "enabled", "1"}, {"false", "off", "no", "disabled", "0"}


class Usage(Exception):
    """GAM's usageErrorExit: the command, the reason, exit 2."""


def csv_options(argv: list) -> tuple:
    """`csv - …` read as GAM 7.52.05 reads it: doCSV → openCSVFileReader checks `charset`, `warnifnodata`,
    `columndelimiter`, `noescapechar`, `quotechar`, `fields`, each ONCE and in that order (checkArgumentPresent only looks
    at the current argument), then getMatchSkipFields, `showcmds`, `skiprows`, `maxrows`, then `gam` is required
    (invalidChoiceExit). An option out of that order is left over and ends it at `gam`: what `columndelimiter , quotechar "
    noescapechar true gam …` did (failure-log 2026-11-27). Returns (delimiter, quotechar, escapechar, the inner argv)."""
    i = 2

    def present(word: str) -> bool:
        nonlocal i
        if i < len(argv) and argv[i].strip().lower().replace("_", "") == word:
            i += 1
            return True
        return False

    def value() -> str:
        nonlocal i
        if i >= len(argv):
            raise Usage("Missing argument")
        i += 1
        return argv[i - 1]

    if argv[1] != "-":
        raise NotImplementedError("a csv file other than -")
    if present("charset"):
        value()
    present("warnifnodata")
    delimiter, no_escape, quote = ",", False, '"'
    if present("columndelimiter"):
        delimiter = value()
    if present("noescapechar"):
        word = value().lower()
        if word not in TRUE | FALSE:
            raise Usage(f"Invalid choice ({word}): Expected <true|false>")
        no_escape = word in TRUE
    if present("quotechar"):
        quote = value()
    if len(delimiter) != 1 or len(quote) != 1:
        raise Usage("Invalid argument: Expected <Character>")
    if i < len(argv) and argv[i].lower() in {"fields", "matchfield", "skipfield", "showcmds", "skiprows", "maxrows"}:
        raise NotImplementedError(argv[i])
    if not present("gam"):
        raise Usage(f"Invalid choice ({argv[i] if i < len(argv) else ''}): Expected <gam>")
    return delimiter, quote, (None if no_escape else "\\"), argv[i:]


def child(mock: str, argv: list) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GAM_MOCK_ARGV_LOG"}   # one logged call: the loop's own
    res = subprocess.run([mock, *argv], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # GAM says it initialized gam.cfg once, in the parent (the mock's prologue already did): not again per item.
    res.stdout = re.sub(r"\A(?:Created: [^\n]*\n)?(?:Config File: [^\n]*, Initialized\n)?", "", res.stdout)
    return res


def csv_loop(mock: str, argv: list) -> int:
    try:
        delimiter, quote, escape, inner = csv_options(argv)
    except Usage as exc:
        sys.stderr.write(f"Command: gam {' '.join(argv)}\n\nERROR: {exc}\n")
        return 2
    except NotImplementedError as exc:
        sys.stderr.write(f"ERROR: mock: csv with {exc} isn't modelled\n")
        return 2
    if inner != INNER_SIGNATURE:
        sys.stderr.write(f"ERROR: mock: csv running gam {' '.join(inner[:4])} … isn't modelled\n")
        return 2
    # GAM's reader with the options as read: no `noescapechar true` and a backslash in a cell is eaten, as live.
    reader = csv.DictReader(io.StringIO(sys.stdin.read()), delimiter=delimiter, quotechar=quote, escapechar=escape)
    fields = reader.fieldnames or []
    for name in ("User", "signature"):
        if name not in fields:
            sys.stderr.write(f'ERROR: Header "{name}" not found in CSV headers of "{",".join(fields)}".\n')
            return 2
    for row in reader:
        res = child(mock, ["user", row.get("User") or "", "signature", row.get("signature") or "", "html"])
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
    return 0


def print_loop(mock: str) -> int:
    users = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    rows, rc = [], 0
    for i, user in enumerate(users, 1):
        res = child(mock, ["user", user, "print", "signature"])
        count = f" ({i}/{len(users)})" if len(users) > 1 else ""
        sys.stderr.write(re.sub(r"^(Getting Signatures for \S+)$", r"\1" + count.replace("\\", r"\\"), res.stderr, flags=re.M))
        if res.returncode:
            rc = res.returncode
        if res.stdout.strip():
            rows += list(csv.DictReader(io.StringIO(res.stdout), escapechar="\\", doublequote=True))
    titles = ["User"] + sorted({k for r in rows for k in r} - {"User"})
    if not rows:
        titles = ["User", "displayName", "isDefault", "isPrimary", "replyToAddress", "sendAsEmail", "treatAsAlias",
                  "verificationStatus"]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=titles, escapechar="\\", doublequote=True, quoting=csv.QUOTE_MINIMAL,
                            lineterminator="\n", restval="")
    writer.writeheader()
    writer.writerows(rows)
    sys.stdout.write(out.getvalue())
    return rc


def main() -> int:
    mock, argv = sys.argv[1], sys.argv[2:]
    if argv[:1] == ["csv"] and len(argv) > 1:
        return csv_loop(mock, argv)
    if argv == FILE_PRINT_SIGNATURE:
        return print_loop(mock)
    sys.stderr.write(f"ERROR: mock: {' '.join(argv[:4])} … isn't modelled\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
