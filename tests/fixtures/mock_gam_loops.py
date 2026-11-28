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
# The parallel reads (docs/scale.md): each person's own read, `~primaryEmail` from the stdin CSV.
INNER_READS = (["user", "~primaryEmail", "print", "forward", "enabledonly"],
               ["user", "~primaryEmail", "print", "delegates", "shownames"])
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
    if i < len(argv) and argv[i].lower() in {"fields", "matchfield", "skipfield", "skiprows", "maxrows"}:
        raise NotImplementedError(argv[i])
    show = False
    if present("showcmds"):   # _getShowCommands: `showcmds [<Boolean>]`
        show = True
        if i < len(argv) and argv[i].lower() in TRUE | FALSE:
            show = argv[i].lower() in TRUE
            i += 1
    if i < len(argv) and argv[i].lower() in {"skiprows", "maxrows"}:
        raise NotImplementedError(argv[i])
    if not present("gam"):
        raise Usage(f"Invalid choice ({argv[i] if i < len(argv) else ''}): Expected <gam>")
    return delimiter, quote, (None if no_escape else "\\"), argv[i:], show


def child(mock: str, argv: list) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GAM_MOCK_ARGV_LOG"}   # one logged call: the loop's own
    res = subprocess.run([mock, *argv], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # GAM says it initialized gam.cfg once, in the parent (the mock's prologue already did): not again per item.
    res.stdout = re.sub(r"\A(?:Created: [^\n]*\n)?(?:Config File: [^\n]*, Initialized\n)?", "", res.stdout)
    return res


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


def read_loop(mock: str, inner: list, rows: list, show: bool, merge: bool) -> int:
    """GAM's MultiprocessGAMCommands over one read a person: with `showcmds`, "<ts>,<i>/<N>,Start,0,<cmd>" and
    "<ts>,<i>/<N>,End,<rc>,<cmd>" on stderr per item (poolCallback, 10590 and 10706 at the tag), each worker's own stderr
    as it runs; with `redirect csv - multiprocess` every worker's rows merged into one CSV (titles merged in the order seen,
    rows in the order workers finish, here the input order) written once at the end. The parent exits 0 whatever its
    workers did."""
    n = len(rows)
    titles, merged = [], []
    for i, row in enumerate(rows, 1):
        argv = [row.get("primaryEmail") or "" if a == "~primaryEmail" else a for a in inner]
        cmd = " ".join(["gam", *argv])
        if show:
            sys.stderr.write(f"{_stamp()},{i}/{n},Start,0,{cmd}\n")
        res = child(mock, argv)
        sys.stderr.write(res.stderr)
        if show:
            sys.stderr.write(f"{_stamp()},{i}/{n},End,{res.returncode},{cmd}\n")
        if merge:
            if res.stdout.strip():
                reader = csv.DictReader(io.StringIO(res.stdout), escapechar="\\", doublequote=True)
                titles += [t for t in (reader.fieldnames or []) if t not in titles]
                merged += list(reader)
        else:
            sys.stdout.write(res.stdout)
    if merge and titles:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=titles, escapechar="\\", doublequote=True, quoting=csv.QUOTE_MINIMAL,
                                lineterminator="\n", restval="")
        writer.writeheader()
        writer.writerows(merged)
        sys.stdout.write(out.getvalue())
    return 0


def prefixed(mock: str, argv: list) -> int:
    """`config num_threads <n> [redirect csv - multiprocess] csv - …`: GAM reads `config` (4134) before `redirect` (4286),
    and `config`'s loop ends at the first word that isn't a variable. Only these shapes are modelled."""
    i = 1
    if argv[i:i + 1] != ["num_threads"] or len(argv) < 3 or not argv[2].isdigit() or int(argv[2]) < 1:
        sys.stderr.write(f"ERROR: mock: config {' '.join(argv[1:3])} isn't modelled\n")
        return 2
    i = 3
    merge = argv[i:i + 4] == ["redirect", "csv", "-", "multiprocess"]
    if merge:
        i += 4
    elif argv[i:i + 1] == ["redirect"]:
        sys.stderr.write(f"ERROR: mock: redirect {' '.join(argv[i + 1:i + 4])} isn't modelled\n")
        return 2
    if argv[i:i + 1] != ["csv"]:
        sys.stderr.write(f"ERROR: mock: config … {' '.join(argv[i:i + 2])} isn't modelled\n")
        return 2
    return csv_loop(mock, argv[i:], merge=merge)


def csv_loop(mock: str, argv: list, merge: bool = False) -> int:
    try:
        delimiter, quote, escape, inner, show = csv_options(argv)
    except Usage as exc:
        sys.stderr.write(f"Command: gam {' '.join(argv)}\n\nERROR: {exc}\n")
        return 2
    except NotImplementedError as exc:
        sys.stderr.write(f"ERROR: mock: csv with {exc} isn't modelled\n")
        return 2
    if inner not in (INNER_SIGNATURE, *INNER_READS):
        sys.stderr.write(f"ERROR: mock: csv running gam {' '.join(inner[:4])} … isn't modelled\n")
        return 2
    # GAM's reader with the options as read: no `noescapechar true` and a backslash in a cell is eaten, as live.
    reader = csv.DictReader(io.StringIO(sys.stdin.read()), delimiter=delimiter, quotechar=quote, escapechar=escape)
    fields = reader.fieldnames or []
    if inner in INNER_READS:
        if "primaryEmail" not in fields:
            sys.stderr.write(f'ERROR: Header "primaryEmail" not found in CSV headers of "{",".join(fields)}".\n')
            return 2
        return read_loop(mock, inner, list(reader), show, merge)
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
    if argv[:1] == ["config"]:
        return prefixed(mock, argv)
    if argv == FILE_PRINT_SIGNATURE:
        return print_loop(mock)
    sys.stderr.write(f"ERROR: mock: {' '.join(argv[:4])} … isn't modelled\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
