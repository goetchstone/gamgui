"""A Builder read exported to a new Google Sheet (plan F4): what GAM needs, what it prints, and what that means in words.

Read from GAM's own source at the pin (7.52.04: ``CSVPrintFile.GetTodriveParameters`` and ``writeCSVfile``'s
``writeCSVToDrive``, to the end of the function). GamGUI hands GAM a fresh gam.cfg each call, so ``todrive_clientaccess`` is
false and every Drive and Sheets call is made by the SERVICE ACCOUNT acting as the admin (``buildGAPIServiceObject``):
the admin token's scopes never matter to an export. The read runs as it would without ``todrive``; then, instead of
printing its CSV, GAM:

1. makes the file in the admin's Drive with ``files.create`` (the CSV, converted to a Sheet): the delegated Drive scope.
   Refused, it writes ``User: <admin>, Drive API v3 Service/App not enabled`` to stderr and exits 73
   (SERVICE_NOT_APPLICABLE_RC): nothing made. Drive turned off for the admin reads ``User: <admin>, Drive Service/App not
   enabled`` (73); ``forbidden``/``insufficientPermissions`` reads ``WARNING: Insufficient permissions to perform this
   task`` (67); a missing folder, a full Drive or Google's internal error reads ``Drive Folder: root, Failed, <why>`` (50).
2. finishes it as a Sheet through the Sheets API (``spreadsheets.get`` + ``batchUpdate``, naming the tab): the delegated
   Sheets scope. Refused, the file is ALREADY made, but GAM returns before printing its link:
   ``User: <admin>, Sheets API Service/App not enabled``, exit 73.
3. prints ``Data uploaded to Drive File:`` and the file's ``webViewLink`` on the next line, on stdout. The exit is 0, or a
   warning code the read itself set: GAM uploads the table it has all the same.

A result too large for a Sheet is uploaded as a plain CSV file after a ``WARNING`` line on stdout (its link is a
drive.google.com one). No rows is still uploaded (``todrive_upload_nodata`` defaults true; 77 only with it off).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional
from urllib.parse import urlsplit

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
# The two delegated scopes an export needs, with what each does for it, in the order GAM uses them.
SCOPES = (
    (DRIVE_SCOPE, "Drive: GAM makes the file in the admin's Drive"),
    (SHEETS_SCOPE, "Sheets: GAM finishes it as a Sheet and prints its link"),
)

_UPLOADED = re.compile(r"^\s*Data uploaded to Drive File:\s*\n\s*(\S+)\s*$", re.M)
# The hosts a Drive file's webViewLink is on: a Sheet's docs.google.com/spreadsheets/d/…, a plain CSV's drive.google.com/file/d/….
_LINK_HOSTS = frozenset({"docs.google.com", "drive.google.com"})
_SA_DRIVE_REFUSED = re.compile(r"Drive API v3[^,\n]*Service/App not enabled", re.I)
_SA_SHEETS_REFUSED = re.compile(r"Sheets API[^,\n]*Service/App not enabled", re.I)
_DRIVE_OFF = re.compile(r"\bDrive Service/App not enabled", re.I)
_NOT_PERMITTED = re.compile(r"Insufficient permissions to perform this task", re.I)
_PROBABLY_UPLOADED = re.compile(r"the file was probably uploaded", re.I)
_CREATE_FAILED = re.compile(r"Drive Folder: [^,\n]+, Failed", re.I)   # files.create refused: no folder, Drive full, Google's error


def is_google_link(url: str) -> bool:
    """A Drive file's link as Google gives it: https, on docs.google.com or drive.google.com. Anything else is shown as text,
    never as a link, whatever printed it."""
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):   # a browser strips or splits on these
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:   # a malformed or non-numeric port
        return False
    return (parts.scheme == "https" and (parts.hostname or "") in _LINK_HOSTS and port is None
            and not parts.username and not parts.password)


def sheet_url(stdout: str) -> str:
    """The link GAM printed for the file it made (the line after ``Data uploaded to Drive File:``), or "" when it printed
    none or something that isn't a Google file link."""
    m = _UPLOADED.search(stdout or "")
    return m.group(1) if m and is_google_link(m.group(1)) else ""


def noted(stdout: str) -> Dict[str, str]:
    """What an export's record keeps from GAM's output: the file's link, when GAM printed one (``_run_write``'s ``noted``)."""
    url = sheet_url(stdout)
    return {"url": url} if url else {}


def verify(stdout: str) -> str:
    """An exit 0 that printed no link is no answer: whether a file was made can't be told (invariant 10)."""
    return "" if sheet_url(stdout) else ("GAM finished without printing the file's link, so whether a Sheet was made "
                                         "can't be told. Look in the admin's Drive before exporting again.")


def title(domain: str, read: str) -> str:
    """The new file's name: the domain and the read, as GAM names one without ``tdtitle`` (``<domain> - <list>``)."""
    return f"{domain} - {read}" if domain else read


def scope_state(checked: Optional[Mapping[str, bool]]) -> str:
    """The scope check, in words, for the preview: "" when both are authorized; what's missing otherwise; or that it
    couldn't be told (``checked`` None). An unknown is said as unknown, never read as authorized."""
    if checked is None:
        return ("Whether the service account may use Drive and Sheets for the admin couldn't be checked, so GAM may stop "
                "part way: with Drive refused nothing is made, with Sheets refused the file is made and its link isn't printed.")
    missing = [scope for scope, _ in SCOPES if not checked.get(scope)]
    if not missing:
        return ""
    if missing == [SHEETS_SCOPE]:
        return ("The service account isn't allowed Google Sheets (" + SHEETS_SCOPE + ") for the admin. GAM would make the "
                "file in the admin's Drive, then stop before finishing it as a Sheet or printing its link.")
    return ("The service account isn't allowed " + " or ".join(missing) + " for the admin, so GAM can't make the file.")


@dataclass(frozen=True)
class Outcome:
    """What an export did, for the result panel: the link (when GAM printed one), whether a file was made (``yes``, ``no`` or
    ``unknown``), and what happened and what to do, in words."""

    url: str
    made: str
    said: str
    todo: str = ""


_ADD_SCOPE = ("Authorize it in the Admin console's Domain-Wide Delegation (Setup's link has every scope GamGUI uses), "
              "then export again.")


def outcome(ok: bool, url: str, error: str, owner: str) -> Outcome:
    """Read an export's result: ``ok`` and ``error`` are the write's (``ChangeResult.ok`` and ``.detail``, GAM's own
    line), ``url`` the link its record kept. Only a printed link is a made file; a failure that can't be placed is unknown."""
    who = owner or "the admin"
    if ok and url and "/spreadsheets/" not in url:   # too large for a Sheet: GAM uploaded the CSV as a plain file
        return Outcome(url, "yes", f"Exported to a new file in {who}'s Drive, shared with nobody: a plain CSV file, "
                       "since the result was too large for a Google Sheet.")
    if ok:
        return Outcome(url, "yes", f"Exported to a new Google Sheet in {who}'s Drive, shared with nobody." if url else
                       f"Exported to {who}'s Drive. No link was kept: the export was made before its link was recorded.")
    if url:   # printed, then a warning exit the read itself set: the file is there, its rows maybe short
        return Outcome(url, "yes", f"GAM made the Sheet in {who}'s Drive, then reported a problem, so it may not hold "
                       "every row.", "Check it against the table, or export again.")
    if _SA_SHEETS_REFUSED.search(error):
        return Outcome("", "yes", f"GAM made the file in {who}'s Drive, then stopped before finishing it as a Sheet or "
                       "printing its link: the service account isn't allowed Google Sheets (" + SHEETS_SCOPE + ").",
                       "Find the file in that Drive by its name. " + _ADD_SCOPE)
    if _SA_DRIVE_REFUSED.search(error):
        return Outcome("", "no", "Nothing was made: the service account isn't allowed Drive (" + DRIVE_SCOPE + ") for "
                       f"{who}.", _ADD_SCOPE)
    if _DRIVE_OFF.search(error):
        return Outcome("", "no", f"Nothing was made: Google Drive is turned off for {who}.",
                       "Turn Drive on for the admin's organizational unit in the Admin console, then export again.")
    if _NOT_PERMITTED.search(error):
        return Outcome("", "no", f"Nothing was made: Google refused to create a file in {who}'s Drive.",
                       "Check that the admin's Drive accepts new files, then export again.")
    if _PROBABLY_UPLOADED.search(error):   # before _CREATE_FAILED: GAM says this one in the same "Failed" line
        return Outcome("", "unknown", "Google reported an error, but says the file was probably made.",
                       f"Look in {who}'s Drive before exporting again.")
    if _CREATE_FAILED.search(error):
        return Outcome("", "no", f"Nothing was made: Google refused to create the file in {who}'s Drive (it may be full).",
                       "See GAM's line below, then export again.")
    return Outcome("", "unknown", "The export failed, and whether a file was made can't be told.",
                   f"Look in {who}'s Drive before exporting again.")
