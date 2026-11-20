"""Audit runs (plan F3): ready-made lists of reads, started by the operator, with Stop between steps.

Pure: what each run reads and what its tables say. The connector runs each step's read (``GAMConnector.audit_read``,
which leaves a ``read`` record of origin ``audit_run``); ``web/routes/audit_runs.py`` runs the steps as a job and keeps
every step's table for the run. A run never writes: Inactive users' "Suspend these…" is the Reports preview, a write
of its own through the chokepoint, which reads the directory again.

A step that failed, stopped short or never ran is unknown, never "none" (invariant 10), and so is every finding built
on it. A step that read nothing where something must be (no account, no seat) is unknown too.

Google's fields each join or flag reads, from Google's reference:
- Directory `users` (developers.google.com/workspace/admin/directory/reference/rest/v1/users): `primaryEmail` "The
  user's primary email address."; `suspended` "Indicates if user is suspended."; `lastLoginTime` "The last time the user
  logged into the user's account."; `creationTime` "The time the user's account was created."; `isAdmin` "Indicates a
  user with super administrator privileges."; `isDelegatedAdmin` "Indicates if the user is a delegated administrator."
  GAM prints a time as YYYY-MM-DDThh:mm:ssZ and Google's never (1970-01-01T00:00:00.000Z) as `Never` (formatLocalTime,
  NEVER_TIME, the 7.52.04 tag).
- Licensing `licenseAssignments` (developers.google.com/workspace/admin/licensing/reference/rest/v1/licenseAssignments):
  `userId` "The user's current primary email address." (GAM lowercases it); `productId`, `skuId` the product's and
  SKU's identifiers. GAM adds `productDisplay` and `skuDisplay` from its own table, the ID itself for one it doesn't know.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .gam.commands import CACHE_FIELDS, GAMCommands
from .gam.models import GAMUser
from .reports import INACTIVE_DAYS, _parse_dt, build_reports

ROWS_SHOWN = 100                           # rows a finished table draws; its count and its CSV are the whole of it
INACTIVE_WINDOWS = (30, 60, 90, 180, 365)  # the window picker's choices, in days

# A step's state once the run is over.
READ, UNKNOWN, NOT_RUN = "read", "unknown", "not_run"


@dataclass(frozen=True)
class Step:
    key: str
    title: str      # what the step reads, as the run's progress names it
    key_column: str  # the column the step's joins read: a table without it isn't the table asked for


@dataclass(frozen=True)
class Run:
    key: str
    title: str
    what: str
    steps: Tuple[Step, ...]


DIRECTORY = Step("directory", "The directory, suspended accounts included", "primaryEmail")
LICENSES = Step("licenses", "Every license seat", "userId")

INACTIVE_USERS = Run("inactive_users", "Inactive users",
                     "Active accounts with no sign-in inside a window you pick; accounts made inside it are flagged.",
                     (DIRECTORY,))
LICENSE_USAGE = Run("license_usage", "License usage",
                    "Every license seat against the directory: seats per license, and seats on suspended accounts.",
                    (LICENSES, DIRECTORY))
RUNS = (INACTIVE_USERS, LICENSE_USAGE)


def run_named(key: str) -> Optional[Run]:
    return next((r for r in RUNS if r.key == key), None)


def step_command(run: Run, step: Step) -> str:
    """What a step's read record names as its ``command``: the run and the step, e.g. ``license_usage/licenses``."""
    return f"{run.key}/{step.key}"


@dataclass
class StepRead:
    """One step, once the run is over: read (its table is kept), unknown (and why, in words) or not run (Stopped)."""

    step: Step
    state: str
    why: str = ""
    rows: int = 0
    detail: str = ""   # GAM's own error, for a collapsed "GAM's error"


@dataclass
class Finding:
    """A table the run's reads add up to. ``known`` False: a step it needs is unknown or never ran, said in ``why``."""

    key: str
    title: str
    summary: str
    columns: Tuple[str, ...]
    rows: Sequence[Tuple[str, ...]] = field(default_factory=list)
    known: bool = True
    why: str = ""
    note: str = ""
    links: bool = False   # the first column is an account's address in the directory: it opens the person


@dataclass
class RunResult:
    run: Run
    reads: List[StepRead]
    findings: List[Finding]
    tables: Dict[str, list]   # each step's records, kept for the run (the directory as GAMUser, the rest as rows)
    when: float
    days: int = 0             # Inactive users: the window it was read with

    @property
    def complete(self) -> bool:
        return all(r.state == READ for r in self.reads)

    def finding(self, key: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.key == key), None)


def step_argv(step: Step) -> List[str]:
    """The GAM read a step runs, from its GAMCommands builder. The directory is the user cache's own read (CACHE_FIELDS,
    suspended accounts included); the seats are `print licenses` with no product named."""
    if step is DIRECTORY:
        return GAMCommands.print_users(fields=CACHE_FIELDS)
    if step is LICENSES:
        return GAMCommands.print_licenses()
    raise ValueError(f"no read for step {step.key!r}")


def implausibly_empty(step: Step, rows: int) -> str:
    """Why a step that read no row at all can't be an answer, else "". A domain has at least one account, and that account
    holds a seat; GAM also passes over a product it can't read without a word, so no seat at all is unknown."""
    if rows:
        return ""
    if step is DIRECTORY:
        return "GAM listed no account at all, and a domain always has one, so the directory is unknown."
    if step is LICENSES:
        return ("GAM listed no license seat at all. It passes over a product Google won't let it read without saying so, "
                "so the seats are unknown, not none. Check that the admin token holds the License Manager scope (Setup).")
    return ""


def unknown_because(reads: Sequence[StepRead], *needed: Step) -> str:
    """Why a finding needing ``needed`` is unknown, in words, or "" when every one was read."""
    for read in reads:
        if read.step in needed and read.state != READ:
            if read.state == NOT_RUN:
                return f"Not known: the run was stopped before “{read.step.title}”."
            return f"Not known: “{read.step.title}” couldn't be read. {read.why}".strip()
    return ""


def _shown_time(value: Optional[str]) -> str:
    """A time as GAM printed it, cut to the day; "never" for Google's never (GAM's `Never`), "" when not there."""
    if not value:
        return ""
    if str(value).strip().lower() == "never" or str(value).startswith("1970-01-01"):
        return "never"
    return str(value)[:10]


# --- 1. Inactive users ------------------------------------------------------------------------------------------------
NEW, ADMIN = "new account", "admin"
INACTIVE_COLUMNS = ("Email", "Name", "Last sign-in", "Created", "Org unit", "Flags")


def inactive_finding(users: List[GAMUser], days: int, now: datetime) -> Finding:
    """The Inactive bucket (Reports' own, for the window) as a table: never-signed-in first, then the longest gone. An account
    made inside the window, or when isn't known, is flagged new (a new hire who hasn't signed in yet looks inactive); an
    administrator is flagged, since suspending one takes their admin work with it."""
    cutoff = now - timedelta(days=days)
    bucket = next(r for r in build_reports(users, now=now, inactive_days=days) if r.key == "inactive")
    rows, new = [], 0
    for u in bucket.users:
        made = _parse_dt(u.creation_time)
        flags = []
        if made is None or made >= cutoff:
            flags.append(NEW)
            new += 1
        if u.is_admin or u.is_delegated_admin:
            flags.append(ADMIN)
        rows.append((u.primary_email, u.full_name, _shown_time(u.last_login_time) or "never", _shown_time(u.creation_time),
                     u.org_unit_path, ", ".join(flags)))

    def order(row: Tuple[str, ...]) -> tuple:
        return (row[2] != "never", row[2], row[0].lower())
    rows.sort(key=order)
    n = len(rows)
    summary = (f"{n} active account{'' if n == 1 else 's'} {'has' if n == 1 else 'have'} not signed in within {days} days"
               + (f"; {new} of them {'was' if new == 1 else 'were'} made inside the window, or when isn't known." if new else "."))
    if not n:
        summary = f"Every active account has signed in within {days} days."
    return Finding("inactive", f"Inactive {days}+ days", summary, INACTIVE_COLUMNS, rows, links=True,
                   note="Suspended accounts are left out. Suspend these… reads the directory again before anything is suspended.")


# --- 3. License usage -------------------------------------------------------------------------------------------------
PER_SKU_COLUMNS = ("Product", "License", "SKU ID", "Seats", "On suspended accounts", "Not in the directory read")
SUSPENDED_SEAT_COLUMNS = ("Email", "Name", "License", "Org unit", "Last sign-in")
STRAY_SEAT_COLUMNS = ("Email", "License")


def license_findings(seats: List[dict], users: Optional[List[GAMUser]], reads: Sequence[StepRead]) -> List[Finding]:
    """Seats per license, seats on suspended accounts, and seats on an address the directory read doesn't list. ``users``
    is None when the directory is unknown: the counts per license still stand, the joins don't."""
    seats_why = unknown_because(reads, LICENSES)
    join_why = unknown_because(reads, LICENSES, DIRECTORY)
    by_email = {u.primary_email.strip().lower(): u for u in users} if users is not None else {}
    per_sku: Counter = Counter()
    on_suspended: Counter = Counter()
    stray: Counter = Counter()
    names: Dict[Tuple[str, str], Tuple[str, str]] = {}
    suspended_rows, stray_rows = [], []
    for seat in seats:
        email = str(seat.get("userId") or "").strip().lower()
        sku = (str(seat.get("productId") or ""), str(seat.get("skuId") or ""))
        names.setdefault(sku, (str(seat.get("productDisplay") or sku[0]), str(seat.get("skuDisplay") or sku[1])))
        per_sku[sku] += 1
        if users is None or not email:
            continue
        holder = by_email.get(email)
        if holder is None:
            stray[sku] += 1
            stray_rows.append((email, names[sku][1]))
        elif holder.suspended:
            on_suspended[sku] += 1
            suspended_rows.append((holder.primary_email, holder.full_name, names[sku][1], holder.org_unit_path,
                                   _shown_time(holder.last_login_time) or "never"))
    joined = users is not None
    sku_rows = [(names[sku][0], names[sku][1], sku[1], str(n),
                 str(on_suspended[sku]) if joined else "unknown", str(stray[sku]) if joined else "unknown")
                for sku, n in sorted(per_sku.items(), key=lambda kv: (-kv[1], names[kv[0]][1].lower()))]
    suspended_rows.sort(key=lambda r: (r[2].lower(), r[0].lower()))
    stray_rows.sort(key=lambda r: (r[1].lower(), r[0].lower()))
    total, n_susp, n_stray = sum(per_sku.values()), len(suspended_rows), len(stray_rows)
    gam_note = ("GAM passes over a product Google won't let it read without saying so: a product missing here may be one "
                "it couldn't read. Check the counts against the Admin console (Billing → Licenses).")
    return [
        Finding("per_sku", "Seats per license",
                f"{total} seat{'' if total == 1 else 's'} across {len(per_sku)} license{'' if len(per_sku) == 1 else 's'}.",
                PER_SKU_COLUMNS, sku_rows, known=not seats_why, why=seats_why,
                note=gam_note + ("" if joined else " The directory couldn't be read, so the last two columns are unknown.")),
        Finding("on_suspended", "Seats on suspended accounts",
                (f"{n_susp} seat{'' if n_susp == 1 else 's'} {'is' if n_susp == 1 else 'are'} held by a suspended account."
                 if n_susp else f"None of the {total} seats read is held by a suspended account."),
                SUSPENDED_SEAT_COLUMNS, suspended_rows, known=not join_why, why=join_why, links=True,
                note="A suspended account keeps its seat until the license is removed."),
        Finding("not_in_directory", "Seats on an address the directory read doesn't list",
                (f"{n_stray} seat{'' if n_stray == 1 else 's'} {'is' if n_stray == 1 else 'are'} on an address the directory "
                 "read didn't list." if n_stray else "Every seat read is on an account the directory read listed."),
                STRAY_SEAT_COLUMNS, stray_rows, known=not join_why, why=join_why,
                note="An account made, renamed or deleted between the two reads shows here too."),
    ]


def findings_for(run: Run, reads: Sequence[StepRead], tables: Dict[str, list], now: Optional[datetime] = None,
                 days: int = INACTIVE_DAYS) -> List[Finding]:
    """The run's tables, from its step reads and their kept tables. A finding whose step is unknown says so."""
    now = now or datetime.now(timezone.utc)
    if run is INACTIVE_USERS:
        why = unknown_because(reads, DIRECTORY)
        if why:
            return [Finding("inactive", f"Inactive {days}+ days", why, INACTIVE_COLUMNS, known=False, why=why)]
        return [inactive_finding(tables[DIRECTORY.key], days, now)]
    if run is LICENSE_USAGE:
        users = tables.get(DIRECTORY.key) if not unknown_because(reads, DIRECTORY) else None
        return license_findings(tables.get(LICENSES.key, []), users, reads)
    raise ValueError(f"no findings for run {run.key!r}")
