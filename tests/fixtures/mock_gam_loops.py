"""The mock's two GAM loops that read standard input (roadmap 5.4's signature fast path), called by mock_gam.sh.

Each runs the mock once per row or person, the way GAM runs a command per item, so the strict one-person handlers decide
every answer and these only do GAM's looping, from GAM 7.52.05's source:

- `csv - columndelimiter , quotechar " noescapechar true gam user ~User signature ~signature html` (doCSV): the rows read
  with csv.DictReader(delimiter=',', quotechar='"', escapechar=None) after sys.stdin.read() (universal newlines); a
  `~Field` the header lacks ends it (csvFieldErrorExit); an empty cell substitutes "" (processSubFields). Each row is its
  own child; its output is passed on whole. GAM's parent takes no child's exit code, so this exits 0 when rows failed.
- `file - print signature` (getEntitiesFromFile, printShowSignature): the addresses one a line, read in one process;
  "Getting Signatures for <u> (i/n)" per person on stderr, one CSV of every row (titles User then sorted, the `signature`
  column only when a row has one), written in GAM's dialect (doubled quotes and a backslash escape).

Only these exact argv shapes are modelled: anything else is refused, as the mock refuses what it doesn't model.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys

CSV_SIGNATURES = ["csv", "-", "columndelimiter", ",", "quotechar", '"', "noescapechar", "true",
                  "gam", "user", "~User", "signature", "~signature", "html"]
FILE_PRINT_SIGNATURE = ["file", "-", "print", "signature"]


def child(mock: str, argv: list) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GAM_MOCK_ARGV_LOG"}   # one logged call: the loop's own
    res = subprocess.run([mock, *argv], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # GAM says it initialized gam.cfg once, in the parent (the mock's prologue already did): not again per item.
    res.stdout = re.sub(r"\A(?:Created: [^\n]*\n)?(?:Config File: [^\n]*, Initialized\n)?", "", res.stdout)
    return res


def csv_loop(mock: str) -> int:
    reader = csv.DictReader(io.StringIO(sys.stdin.read()), delimiter=",", quotechar='"', escapechar=None)
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
    if argv == CSV_SIGNATURES:
        return csv_loop(mock)
    if argv == FILE_PRINT_SIGNATURE:
        return print_loop(mock)
    sys.stderr.write(f"ERROR: mock: {' '.join(argv[:4])} … isn't modelled\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
