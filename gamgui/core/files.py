"""Files (plan F4): what this app made, listed read-only. Runs no gam and writes nothing of its own:
the one write it can cause is the incident store's own, on first opening it (an unreadable incident record is moved aside, as
every incident screen does).

Three sources, each bounded (invariant 9):

- **Google Sheets** the Builder exported: each ``export_to_sheet`` / ``sensitive_export`` record in the audit log, newest
  first, with the link GAM printed read back from the record (``extra.url``, core/sheet_export), its domain (F1), the read
  (``extra.command`` and the argv) and whose Drive (the target). At most ``SHEETS_SHOWN``, from the newest
  ``RECORDS_SCANNED`` records; the page says when it stopped at either.
- **Incident evidence**: each kept incident's ``evidence`` folder (core/incident/store.py) and how many saved reads it
  holds; an incident whose record couldn't be read is named as such.
- **Files saved from the app window**: GamGUI keeps no list of them. A CSV or report download is handed to the window
  (WKWebView, or a browser), which saves it where its downloads go; the app never learns the path. The page says so.

Never a run folder (the per-call GAMCFGDIR, wiped after each call) or the setup folder (credentials): nothing here lists a
directory except the incident store's own evidence folders, through ``IncidentStore``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import sheet_export
from .audit import DOMAIN_KEY, iter_records

SHEETS_SHOWN = 50
RECORDS_SCANNED = 50_000
EXPORT_ACTIONS = frozenset({"export_to_sheet", "sensitive_export"})


@dataclass(frozen=True)
class SheetMade:
    when: str              # the record's ``ts``
    domain: str            # "" for a record that names none
    command: str           # the catalog id of the read
    argv: Tuple[str, ...]  # what ran: the read and its todrive options
    owner: str             # whose Drive: the record's target
    sensitive: bool
    outcome: sheet_export.Outcome   # the link, and whether a file was made: yes, no, or unknown

    @property
    def gam(self) -> str:
        """What ran, quoted as a shell would: a title with spaces is one argument."""
        return "gam " + shlex.join(self.argv)


@dataclass(frozen=True)
class Sheets:
    made: Tuple[SheetMade, ...]
    more: bool          # there are more than SHEETS_SHOWN: the oldest aren't listed
    scanned_all: bool   # False: stopped after RECORDS_SCANNED records; older exports weren't looked for
    problem: str = ""   # the log couldn't be read: unknown, never "none" (invariant 10)


def _extra(record: Dict[str, Any]) -> Dict[str, Any]:
    extra = record.get("extra")
    return extra if isinstance(extra, dict) else {}


def sheets_made(path: Path) -> Sheets:
    """The Sheets the audit log at ``path`` says this app exported, newest first. No log yet is nothing made; a log that
    can't be opened is said, never read as empty."""
    try:
        with open(path, "rb"):
            pass
    except FileNotFoundError:
        return Sheets((), False, True)
    except OSError as exc:
        return Sheets((), False, False, f"The audit log couldn't be read ({exc.strerror}), so what was exported can't be told.")
    made: List[SheetMade] = []
    scanned = 0
    more = False
    for record in iter_records(path, limit=RECORDS_SCANNED):
        scanned += 1
        if record.get("action") not in EXPORT_ACTIONS:
            continue
        if len(made) >= SHEETS_SHOWN:
            more = True
            break
        extra = _extra(record)
        url = str(extra.get("url") or "")
        url = url if sheet_export.is_google_link(url) else ""
        owner = str(record.get("target") or "")
        ok = record.get("ok") is True
        made.append(SheetMade(
            when=str(record.get("ts") or ""), domain=str(extra.get(DOMAIN_KEY) or "").lower(),
            command=str(extra.get("command") or ""),
            argv=tuple(str(a) for a in record.get("argv") or ()) if isinstance(record.get("argv"), list) else (),
            owner=owner, sensitive=record.get("action") == "sensitive_export",
            # What became of the file is what the record kept from the upload's own lines, never its error summary.
            outcome=sheet_export.outcome(ok, url, upload if (upload := str(extra.get("upload") or "")) in sheet_export.UPLOAD_CODES
                                         else "", owner, "" if ok else str(extra.get("error") or ""))))
    return Sheets(tuple(made), more, more or scanned < RECORDS_SCANNED)


@dataclass(frozen=True)
class EvidenceFolder:
    incident: str           # its title ("" for one whose record couldn't be read)
    id: str
    domain: str
    opened: Optional[datetime]
    path: str
    reads: Optional[int]    # saved reads in it; None when the folder couldn't be read


def evidence_folders(store) -> List[EvidenceFolder]:
    """Each kept incident's evidence folder that holds a saved read (or couldn't be read), newest opened first, and each
    incident whose record couldn't be read. ``store`` is an ``IncidentStore``, already bounded to its CAP."""
    found: List[EvidenceFolder] = []
    for record in store.records:
        path = str(Path(store.root) / record.id / "evidence")
        try:
            reads: Optional[int] = len(store.names(record.id))
        except OSError:   # said as unread on the page, never as empty
            reads = None
        if reads != 0:
            found.append(EvidenceFolder(record.title, record.id, record.domain, record.opened, path, reads))
    for record_id in store.unreadable:
        found.append(EvidenceFolder("", record_id, "", None, str(Path(store.root) / record_id / "evidence"), None))
    return found
