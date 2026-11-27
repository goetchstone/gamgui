"""The GAM-native fast path for a signature run (roadmap 5.4): one GAM call reads everyone's current signature, one sets them all.

The per-user loop stays the default: it is what draws the live ✓/✗ feed and honours Stop. This path is opt-in and has
neither, and the preview says so. It still keeps each person's current signature first (roadmap 4.1), so Put Back works.

- **Backup read:** `gam file - print signature`, the addresses on standard input. A person is read only when their one row
  came back whole and GAM said "Getting Signature for" them; anyone else is unread, never "no signature" (invariant 10),
  and isn't written.
- **Write:** `gam csv - … gam user ~User signature ~signature html`, the rows on standard input. GAM 7.52.05's `csv` reads
  them with csv.DictReader after `sys.stdin.read()` (text mode: `\\r\\n` and `\\r` arrive as `\\n`, decoded in GAM's own
  locale), so each body has its line breaks made `\\n` and every non-ASCII character written as an HTML character
  reference: the bytes GAM reads are ASCII whatever its locale, and an HTML signature shows `&#233;` as é. The body
  kept as "what the run set" is that same text, so Put Back compares like with like.
- **Results:** GAM's parent doesn't take its children's exit codes, so a row that failed can still exit 0. Only GAM's
  own "User: <u>, Signature: <u>, Set" line says a person was set (setSignature → _processSendAs → entityActionPerformed
  with Ent.SIGNATURE and Act.SET, at the 7.52.05 tag; not yet seen live); anyone without one is listed as failed
  ("may not have happened"), and Retry runs them through the per-user loop.

SwiftGamGui ports this with the same argv, the same CSV bytes (tests/fixtures/signatures_fast.csv) and the same parse.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .gam.commands import signature_read_as_file
from .gam.parser import parse_records

# Python's csv.field_size_limit default, which GAM 7.52.05 never raises: one longer cell ends its whole csv read before any
# row runs (review finding), so a body that long, as sent, is refused for that person alone.
FIELD_LIMIT = 131072

# GAM's own word for a refused row when there is no line to quote.
NOT_SAID = "GAM didn't say it set this one, so it may not have happened."


def as_sent(body: str) -> str:
    """``body`` as the fast path sends it: `\\n` line breaks, non-ASCII as HTML character references."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def refusal(email: str, body: str) -> str:
    """Why one person's row can't go through `gam csv`, or ""."""
    if not body.strip():
        return "An empty signature: GAM's csv would clear theirs. Use the per-person run to clear one."
    if signature_read_as_file(body):
        return "GAM would read this signature as a file to load. Change the text."
    if len(as_sent(body)) >= FIELD_LIMIT:
        return "Too long for GAM's csv in one go. Set this one with the per-person run."
    if "\0" in body or not email.isascii() or any(c in email for c in ',"\r\n'):
        return "This can't be written as one row of GAM's csv."
    return ""


def rows_csv(items: Sequence[Tuple[str, str]]) -> str:
    """The rows for `set_signatures_csv`: a `User,signature` header, then each ``(email, body)`` as sent (``as_sent``),
    every field quoted, `"` doubled, `\\n` between rows. Read back by csv.DictReader(delimiter=',', quotechar='"',
    escapechar=None), GAM's reader with the options the argv names, each row is exactly ``(email, as_sent(body))``."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", quotechar='"', doublequote=True, escapechar=None,
                        quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(["User", "signature"])
    for email, body in items:
        writer.writerow([email, as_sent(body)])
    return buf.getvalue()


def emails_stdin(emails: Iterable[str]) -> str:
    """The addresses for `print_signatures_from_stdin`, one a line."""
    return "".join(f"{e}\n" for e in emails)


def _said_getting(stderr: str, email: str) -> bool:
    pattern = re.compile(r"^Getting Signature for " + re.escape(email) + r"(?: \(\d+/\d+\))?\s*$", re.I | re.M)
    return bool(pattern.search(stderr or ""))


def read_signatures(stdout: str, stderr: str, emails: Sequence[str]) -> Dict[str, str]:
    """Each person's stored signature from one `file - print signature` run, only for those read whole: their one row,
    every cell present, and GAM's "Getting Signature for" line. Missing from the result is unread, never "" (the
    per-person read's rule, GAMConnector.signature_exact)."""
    rows: Dict[str, List[dict]] = {}
    for r in parse_records(stdout or ""):
        rows.setdefault(str(r.get("User") or "").strip().lower(), []).append(r)
    read: Dict[str, str] = {}
    for email in emails:
        mine = rows.get(email.strip().lower(), [])
        if len(mine) != 1 or any(v is None for v in mine[0].values()) or not _said_getting(stderr, email):
            continue
        read[email] = str(mine[0].get("signature", ""))
    return read


def updated(stdout: str, emails: Sequence[str]) -> Set[str]:
    """The people GAM's own line says it set: "User: <u>, Signature: <u>, Set" (entityActionPerformed with Ent.SIGNATURE and
    Act.SET), with or without its "(i/n)". Matched on the whole address, without case."""
    said: Set[str] = set()
    for m in re.finditer(r"^\s*User: ([^,\s]+), Signature: ([^,\s]+), Set(?: \(\d+/\d+\))?\s*$", stdout or "", re.M):
        if m.group(1).lower() == m.group(2).lower():
            said.add(m.group(1).lower())
    return {e for e in emails if e.lower() in said}
