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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .connected_apps import Reach, describe, grants_from, looks_like_ai
from .gam.commands import CACHE_FIELDS, COURSE_STATES, GAMCommands, is_one_address
from .gam.models import GAMUser
from .reports import INACTIVE_DAYS, _parse_dt, build_reports

ROWS_SHOWN = 100                           # rows a finished table draws; its count and its CSV are the whole of it
INACTIVE_WINDOWS = (30, 60, 90, 180, 365)  # the window picker's choices, in days

# A step's state once the run is over. PARTIAL: GAM read all but some accounts it named (an account-level warning exit);
# its rows stand, and what it couldn't read is unknown, never "none".
READ, PARTIAL, UNKNOWN, NOT_RUN = "read", "partial", "unknown", "not_run"
NO_ENTITIES_FOUND_RC = 60                 # GAM's own "none", the titles written (e.g. no shared drive at all)
ACCOUNT_WARNING_RCS = (56, 73)            # entityUnknownWarning: an account GAM couldn't read, in the domain or outside it


@dataclass(frozen=True)
class Step:
    key: str
    title: str      # what the step reads, as the run's progress names it
    key_column: str  # the column the step's joins read: a table without it isn't the table asked for
    ok_rcs: Tuple[int, ...] = (0,)          # exits that are the step's whole answer
    partial_rcs: Tuple[int, ...] = ()       # exits whose rows stand, with accounts GAM named as unread
    empty_rcs: Optional[Tuple[int, ...]] = None   # exits at which no row at all is an answer; None: any of ok_rcs


@dataclass(frozen=True)
class Run:
    key: str
    title: str
    what: str
    steps: Tuple[Step, ...]


DIRECTORY = Step("directory", "The directory, suspended accounts included", "primaryEmail")
LICENSES = Step("licenses", "Every license seat", "userId")
# No row is "no drive" only with GAM's 60: a service-account token Google refuses silently (REFRESH_PERM_ERRORS) makes both
# reads skip the admin, titles alone, exit 0 (buildGAPIServiceObject -> handleOAuthTokenError, then `continue` before the
# none check, printShowSharedDrives / printShowSharedDriveACLs at the 7.52.04 tag).
DRIVES = Step("drives", "Every shared drive", "id", ok_rcs=(0, NO_ENTITIES_FOUND_RC), empty_rcs=(NO_ENTITIES_FOUND_RC,))
DRIVE_ACLS = Step("drive_acls", "Every shared drive's members", "id", ok_rcs=(0, NO_ENTITIES_FOUND_RC),
                  empty_rcs=(NO_ENTITIES_FOUND_RC,))
DOMAINS = Step("domains", "The domain's own domains and aliases", "domainName")
COURSES = Step("courses", "Every Classroom class, with its counts", "id")
ADMINS = Step("admins", "Every admin role, a group's members included", "roleAssignmentId")
ADMIN_TOKENS = Step("admin_tokens", "Each admin's connected apps, suspended admins included", "user",
                    partial_rcs=ACCOUNT_WARNING_RCS)

INACTIVE_USERS = Run("inactive_users", "Inactive users",
                     "Active accounts with no sign-in inside a window you pick; accounts made inside it are flagged.",
                     (DIRECTORY,))
SHARED_DRIVES = Run("shared_drive_permissions", "Shared drive permissions",
                    "Every shared drive's members, outside members first, and drives no active organizer looks after.",
                    (DRIVES, DRIVE_ACLS, DOMAINS, DIRECTORY))
LICENSE_USAGE = Run("license_usage", "License usage",
                    "Every license seat against the directory: seats per license, and seats on suspended accounts.",
                    (LICENSES, DIRECTORY))
CLASSROOM = Run("classroom_courses", "Classroom",
                "Every class with its owner, state and counts; classes owned by suspended accounts; stale classes.",
                (COURSES, DIRECTORY))
ADMIN_TOKEN_REVIEW = Run("admin_tokens", "Admin token review",
                         "Every admin's connected apps, suspended admins included, AI and broad access flagged.",
                         (ADMINS, DIRECTORY, ADMIN_TOKENS))
RUNS = (INACTIVE_USERS, SHARED_DRIVES, LICENSE_USAGE, CLASSROOM, ADMIN_TOKEN_REVIEW)


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
    unread: Tuple[str, ...] = ()   # PARTIAL: the accounts GAM named as not read


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
    partial: bool = False  # known, but holding something no read could tell (an admin GAM couldn't name or read)
    # What a change since an earlier run can't tell, said beside it (plan F7a): a read GAM can cut short without a word
    # makes a row that's gone, or a count that fell, look the same as one it couldn't read this time.
    change_note: str = ""


@dataclass
class RunResult:
    run: Run
    reads: List[StepRead]
    findings: List[Finding]
    tables: Dict[str, list]   # each step's records, kept for the run (the directory as GAMUser, the rest as rows)
    when: float
    days: int = 0             # Inactive users: the window it was read with
    # A scheduled run (plan F7a): each table against the last kept run that read it (core/schedules.Change, by finding
    # key), and what happened to this run's keep, in words (``kept_problem``: that it went wrong).
    changes: Dict[str, Any] = field(default_factory=dict)
    kept_note: str = ""
    kept_problem: bool = False

    @property
    def complete(self) -> bool:
        return all(r.state == READ for r in self.reads) and not any(f.partial for f in self.findings)

    @property
    def answered(self) -> bool:
        """Every step has an answer, at most with accounts GAM named as unread: the run counts for its schedule (F7a)."""
        return all(r.state in (READ, PARTIAL) for r in self.reads)

    def finding(self, key: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.key == key), None)


def _unnamed(row: dict) -> bool:
    """A role holder GAM couldn't name: its lookup of `assignedTo` failed (convertUIDtoEmailAddressWithType returns the bare
    ID as type `unknown`, getUserEmailFromID having passed over forbidden, badRequest, backendError, systemError and
    serviceNotAvailable), so the row has `assignedToUnknown` True and no address, and GAM exits 0 (doPrintShowAdmins,
    _setNamesFromIds, at the 7.52.04 tag). A group's member expanded by `recursive` can be one too."""
    return str(row.get("assignedToUnknown") or "").strip().lower() == "true"


def holder_of(row: dict, users: Optional[Sequence[GAMUser]]) -> str:
    """Who holds an admin row: its `assignedToUser`; for a holder GAM couldn't name, the directory account whose `id` is its
    `assignedTo` ("The unique ID of the entity this role is assigned to"); else "" (a service account, a group with no
    member, or a holder neither GAM nor the directory read can name)."""
    address = str(row.get("assignedToUser") or "").strip().lower()
    if is_one_address(address):
        return address
    if _unnamed(row) and users:
        uid = str(row.get("assignedTo") or "").strip()
        for u in users:
            if uid and str(u.raw.get("id") or "") == uid:
                return u.primary_email.strip().lower()
    return ""


def unnamed_holders(admin_rows: Sequence[dict], users: Optional[Sequence[GAMUser]]) -> List[str]:
    """The IDs of role holders neither GAM nor the directory read could name: unknown admins, never left out."""
    return sorted({str(r.get("assignedTo") or "") for r in admin_rows if _unnamed(r) and not holder_of(r, users)} - {""})


def admin_addresses(admin_rows: Sequence[dict], users: Optional[Sequence[GAMUser]] = None) -> List[str]:
    """Every person who holds an admin role, from `print admins recursive` (a group's members each get a row of their own),
    a holder GAM couldn't name joined to the directory by ID. A service account's row, or a group with no member, names no
    person."""
    return sorted({a for a in (holder_of(r, users) for r in admin_rows) if a})


def step_argv(step: Step, tables: Optional[Dict[str, list]] = None) -> Optional[List[str]]:
    """The GAM read a step runs, from its GAMCommands builder. The directory is the user cache's own read (CACHE_FIELDS,
    suspended accounts included); the seats are `print licenses` with no product named. The admins' apps are read for the
    people the admins step found, so None when that step has no table: the step can't run, and is unknown."""
    if step is DIRECTORY:
        return GAMCommands.print_users(fields=CACHE_FIELDS)
    if step is LICENSES:
        return GAMCommands.print_licenses()
    if step is DRIVES:
        return GAMCommands.print_shared_drives()
    if step is DRIVE_ACLS:
        return GAMCommands.print_shared_drive_acls()
    if step is DOMAINS:
        return GAMCommands.print_domains()
    if step is COURSES:
        return GAMCommands.print_courses(states=COURSE_STATES, owner_email=False, counts=True)
    if step is ADMINS:
        return GAMCommands.print_all_admins()
    if step is ADMIN_TOKENS:
        admins = admin_addresses((tables or {}).get(ADMINS.key) or [], (tables or {}).get(DIRECTORY.key))
        return GAMCommands.print_tokens_of(admins) if admins else None
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
    if step is DOMAINS:
        return "GAM listed no domain at all, and a Workspace has at least one, so its domains are unknown."
    if step is ADMINS:
        return "GAM listed no admin role at all, and a Workspace always has a super admin, so its admins are unknown."
    return ""


def unknown_because(reads: Sequence[StepRead], *needed: Step) -> str:
    """Why a finding needing ``needed`` is unknown, in words, or "" when every one was read (a partial read counts: what
    it couldn't read is said where it's shown)."""
    for read in reads:
        if read.step in needed and read.state not in (READ, PARTIAL):
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
    # Every table built on the seats says so: with no product named GAM passes over one Google refuses, exit 0, so even
    # a clean run may be missing a product's seats, and an all-clear covers only the seats read.
    skipped = "GAM passes over a product Google won't let it read without saying so, so this covers only the seats read."
    gam_note = ("GAM passes over a product Google won't let it read without saying so: a product missing here may be one "
                "it couldn't read. Check the counts against the Admin console (Billing → Licenses).")
    seats_word = f"{total} seat{'' if total == 1 else 's'}"
    unsure = ("GAM passes over a product it can't read without saying so, so a seat gone since then may be one it couldn't "
              "read this time.")
    return [
        Finding("per_sku", "Seats per license",
                f"{seats_word} across {len(per_sku)} license{'' if len(per_sku) == 1 else 's'}.",
                PER_SKU_COLUMNS, sku_rows, known=not seats_why, why=seats_why, change_note=unsure,
                note=gam_note + ("" if joined else " The directory couldn't be read, so the last two columns are unknown.")),
        Finding("on_suspended", "Seats on suspended accounts",
                (f"{n_susp} seat{'' if n_susp == 1 else 's'} {'is' if n_susp == 1 else 'are'} held by a suspended account."
                 if n_susp else f"No suspended account holds any of the {seats_word} read."),
                SUSPENDED_SEAT_COLUMNS, suspended_rows, known=not join_why, why=join_why, links=True, change_note=unsure,
                note="A suspended account keeps its seat until the license is removed. " + skipped),
        Finding("not_in_directory", "Seats on an address the directory read doesn't list",
                (f"{n_stray} seat{'' if n_stray == 1 else 's'} {'is' if n_stray == 1 else 'are'} on an address the directory "
                 "read didn't list." if n_stray else "Every seat read is on an account the directory read listed."),
                STRAY_SEAT_COLUMNS, stray_rows, known=not join_why, why=join_why, change_note=unsure,
                note="An account made, renamed or deleted between the two reads shows here too. " + skipped),
    ]


# --- 2. Shared drive permissions --------------------------------------------------------------------------------------
# `print shareddriveacls` reads each shared drive's OWN permissions: its members. Google's sharing guide
# (developers.google.com/workspace/drive/api/guides/manage-sharing, the Membership row): "Applies to `user` or `group` only
# (not domains)." So a drive's members are users and groups; "anyone with the link" and a whole domain are sharing of the
# files inside a drive, which this run doesn't read, and no flag here claims to. Drive's permission fields
# (…/drive/api/reference/rest/v3/permissions): `type` "The type of the grantee."; `role` (organizer, fileOrganizer, writer,
# commenter, reader); `emailAddress` "The email address of the user or group to which this permission refers."; `domain`
# "The domain to which this permission refers." (GAM also sets it from emailAddress, _mapDrivePermissionNames).
PERMISSION_COLUMNS = ("Drive", "Member", "Type", "Role", "Flags")
ORPHAN_COLUMNS = ("Drive", "Organizers", "Why")
UNREAD_DRIVE_COLUMNS = ("Drive", "Drive ID")
EXTERNAL = "outside the domain"


def domains_from(records: Sequence[dict]) -> List[str]:
    """Every domain the tenant's addresses can end in, from `print domains formatjson` (as GAMConnector.list_domains)."""
    found: List[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key.rsplit(".", 1)[-1] in ("domainName", "domainAliasName", "parentDomainName") and isinstance(val, str):
                    found.append(val.strip().lower())
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(list(records))
    return sorted({d for d in found if d})


def _permission_flag(p: dict, domains: Sequence[str]) -> str:
    """A member whose address isn't in one of the domain's own domains or aliases is from outside it."""
    domain = str(p.get("domain") or str(p.get("emailAddress") or "").rpartition("@")[2]).strip().lower()
    return EXTERNAL if domain and domain not in domains else ""


def _who(p: dict) -> str:
    return str(p.get("emailAddress") or p.get("displayName") or "")


def shared_drive_findings(drives: List[dict], acl_rows: List[dict], domains: Optional[List[str]],
                          users: Optional[List[GAMUser]], leaving: frozenset, reads: Sequence[StepRead]) -> List[Finding]:
    """Every drive's members (outside members first), drives no active organizer looks after, and drives whose members GAM
    didn't read: it drops a drive whose permissions Google refused without a word or a code, so a listed drive missing from
    the members table is unknown, never "shared with no one"."""
    perms_why = unknown_because(reads, DRIVE_ACLS, DOMAINS)
    orphan_why = unknown_because(reads, DRIVE_ACLS, DIRECTORY)
    unread_why = unknown_because(reads, DRIVES, DRIVE_ACLS)
    rows: List[Tuple[str, ...]] = []
    by_drive: Dict[str, List[dict]] = {}
    names: Dict[str, str] = {}
    for r in acl_rows:
        drive_id, name = str(r.get("id") or ""), str(r.get("name") or "")
        names[drive_id] = name
        perms = by_drive.setdefault(drive_id, [])
        p = r.get("permission")
        if not isinstance(p, dict):
            continue        # shownopermissionsdrives: the drive was read and has none
        perms.append(p)
        flag = _permission_flag(p, domains or [])
        rows.append((name, _who(p), str(p.get("type") or ""), str(p.get("role") or ""), flag))
    rows.sort(key=lambda r: (not r[4], r[0].lower(), r[1].lower()))
    outside = sum(1 for r in rows if r[4])
    status = {u.primary_email.strip().lower(): u for u in users or []}
    orphans = []
    for drive_id, perms in by_drive.items():
        organizers = [p for p in perms if str(p.get("role") or "") == "organizer"]
        if any(str(p.get("type") or "") != "user" for p in organizers):
            continue        # a group organizer: its members can't be told from here, so it isn't flagged
        whys = []
        for p in organizers:
            address = str(p.get("emailAddress") or "").strip().lower()
            holder = status.get(address)
            if p.get("deleted") is True or (holder is None and address):
                whys.append(f"{address or 'an account'}: not in the directory read")
            elif holder is not None and holder.suspended:
                whys.append(f"{address}: suspended")
            elif address in leaving:
                whys.append(f"{address}: being offboarded")
            else:
                break
        else:
            orphans.append((names[drive_id], ", ".join(str(p.get("emailAddress") or "") for p in organizers) or "none",
                            "; ".join(whys) or "no organizer at all"))
    orphans.sort(key=lambda r: r[0].lower())
    read_ids = set(by_drive)
    unread = sorted(((str(d.get("name") or ""), str(d.get("id") or "")) for d in drives if str(d.get("id") or "") not in read_ids),
                    key=lambda r: r[0].lower())
    n, n_orphans, n_unread = len(rows), len(orphans), len(unread)
    # A drive whose members weren't read (or whose listing is unknown) is missing from both tables below: each holds
    # something no read could tell, so neither is whole, and neither is compared with an earlier run as if it were.
    left_out = bool(unread) or bool(unread_why)
    left_out_note = (f" It leaves out the {n_unread} drive{'' if n_unread == 1 else 's'} whose members weren't read, listed "
                     "below." if unread else " Whether GAM passed over a drive can't be told: the list of drives wasn't read."
                     if unread_why else "")
    return [
        Finding("permissions", "Every drive's members", (f"{n} member{'' if n == 1 else 's'} across {len(by_drive)} drive"
                f"{'' if len(by_drive) == 1 else 's'}; {outside} {'is' if outside == 1 else 'are'} from outside the domain, "
                "listed first."),
                PERMISSION_COLUMNS, rows, known=not perms_why, why=perms_why, partial=left_out,
                note=("A shared drive's members are users and groups only. Outside the domain means an address not in one of "
                      "the domain's own domains or aliases. Files inside a drive can be shared further (with anyone with the "
                      "link, say): this run reads each drive's members, not its files' sharing." + left_out_note)),
        Finding("orphaned", "Drives no active organizer looks after",
                (f"{n_orphans} drive{'' if n_orphans == 1 else 's'} {'has' if n_orphans == 1 else 'have'} no organizer who is "
                 "active here." if n_orphans else "Every drive read has an active organizer, or a group as one."),
                ORPHAN_COLUMNS, orphans, known=not orphan_why, why=orphan_why, partial=left_out,
                note=("Being offboarded means an offboarding started in GamGUI since it opened. A group organizer isn't "
                      "flagged: its members can't be told from here." + left_out_note)),
        Finding("unread", "Drives whose members weren't read",
                (f"GAM listed {n_unread} drive{'' if n_unread == 1 else 's'} but didn't read {'its' if n_unread == 1 else 'their'} "
                 "members: unknown, not shared with no one." if n_unread else "GAM read every listed drive's members."),
                UNREAD_DRIVE_COLUMNS, unread, known=not unread_why, why=unread_why,
                note="GAM passes over a drive whose permissions Google won't return without a word or a code."),
    ]


# --- 4. Classroom (read only) -----------------------------------------------------------------------------------------
# Classroom's course fields (developers.google.com/workspace/classroom/reference/rest/v1/courses): `ownerId` "The identifier
# of the owner of a course." (joined to the directory's `id`, "The unique ID for the user."); `courseState` ACTIVE, ARCHIVED,
# PROVISIONED ("created, but not yet activated"), DECLINED, SUSPENDED; `updateTime` "Time of the most recent update to this
# course." GAM adds `teachers` and `students` counts with `show all countsonly`, and counts a roster it couldn't read as 0.
STALE_DAYS = 365
COURSE_COLUMNS = ("Class", "Section", "Owner", "State", "Teachers", "Students", "Updated")
OWNED_COLUMNS = ("Owner", "Class", "State", "Updated")
STRAY_OWNER_COLUMNS = ("Class", "Owner ID", "State")


def classroom_findings(courses: List[dict], users: Optional[List[GAMUser]], reads: Sequence[StepRead], now: datetime) -> List[Finding]:
    courses_why = unknown_because(reads, COURSES)
    join_why = unknown_because(reads, COURSES, DIRECTORY)
    by_id = {str(u.raw.get("id") or ""): u for u in users or [] if u.raw.get("id")}
    cutoff = now - timedelta(days=STALE_DAYS)
    every, owned, strays, stale = [], [], [], []
    for c in courses:
        owner = by_id.get(str(c.get("ownerId") or ""))
        owner_shown = owner.primary_email if owner else (f"ID {c.get('ownerId')}" if users is not None else str(c.get("ownerId") or ""))
        state = str(c.get("courseState") or "")
        updated = _shown_time(str(c.get("updateTime") or ""))
        name = str(c.get("name") or "")
        every.append((name, str(c.get("section") or ""), owner_shown, state, str(c.get("teachers", "")), str(c.get("students", "")),
                      updated))
        if users is not None and owner is None:
            strays.append((name, str(c.get("ownerId") or ""), state))
        elif owner is not None and owner.suspended:
            owned.append((owner.primary_email, name, state, updated))
        made = _parse_dt(str(c.get("updateTime") or ""))
        if state in ("ACTIVE", "PROVISIONED") and made is not None and made < cutoff:
            stale.append((name, owner_shown, state, updated))
    every.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    owned.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    strays.sort(key=lambda r: r[0].lower())
    stale.sort(key=lambda r: (r[3], r[0].lower()))
    n = len(every)
    return [
        Finding("courses", "Every class", f"{n} class{'' if n == 1 else 'es'} in every state.", COURSE_COLUMNS, every,
                known=not courses_why, why=courses_why,
                change_note=("GAM counts a roster Google won't return as 0 without a word, so a class whose count fell to 0 "
                             "since then may be one it couldn't read this time."),
                note=("GAM counts a class whose roster Google won't return as 0 teachers and 0 students, without a word, so a "
                      "0 isn't an empty class. Not yet checked on a school domain.")),
        Finding("owned_by_suspended", "Classes owned by a suspended account",
                (f"{len(owned)} class{'' if len(owned) == 1 else 'es'} {'is' if len(owned) == 1 else 'are'} owned by a suspended "
                 "account." if owned else "No class read is owned by a suspended account."),
                OWNED_COLUMNS, owned, known=not join_why, why=join_why, links=True,
                note="GamGUI changes nothing in Classroom: hand a class over in Classroom itself."),
        Finding("owner_not_in_directory", "Classes whose owner the directory read doesn't list",
                (f"{len(strays)} class{'' if len(strays) == 1 else 'es'} {'has' if len(strays) == 1 else 'have'} an owner the "
                 "directory read didn't list." if strays else "Every class read has an owner the directory read listed."),
                STRAY_OWNER_COLUMNS, strays, known=not join_why, why=join_why,
                note="A deleted account, or one outside the domain."),
        Finding("stale", f"Active or provisioned classes not updated in {STALE_DAYS} days",
                (f"{len(stale)} class{'' if len(stale) == 1 else 'es'} {'has' if len(stale) == 1 else 'have'}n't changed in a year."
                 if stale else f"Every active or provisioned class read has changed within {STALE_DAYS} days."),
                ("Class", "Owner", "State", "Updated"), stale, known=not courses_why, why=courses_why),
    ]


# --- 5. Admin token review --------------------------------------------------------------------------------------------
# Directory roleAssignments (developers.google.com/workspace/admin/directory/reference/rest/v1/roleAssignments): `assignedTo`
# "The unique ID of the entity this role is assigned to"; GAM adds `assignedToUser` / `assignedToGroup` (the address) and
# `role` (the role's name). Directory tokens (…/tokens): `clientId`; `displayText` "The displayable name of the application
# the token is issued to."; `scopes` "A list of authorization scopes the application is granted." (GAM joins them with spaces).
ADMIN_COLUMNS = ("Admin", "Roles", "Suspended", "Apps")
ADMIN_APP_COLUMNS = ("Admin", "App", "What it may do", "Flags")
AI_NAME, BROAD, SUSPENDED_ADMIN = "AI, by its name", "broad access", "suspended admin"


def admin_token_findings(admin_rows: List[dict], users: Optional[List[GAMUser]], token_rows: List[dict],
                         reads: Sequence[StepRead]) -> List[Finding]:
    admins_why = unknown_because(reads, ADMINS)
    apps_why = unknown_because(reads, ADMINS, ADMIN_TOKENS)
    tokens_read = next((r for r in reads if r.step is ADMIN_TOKENS), None)
    unread = set(tokens_read.unread) if tokens_read else set()
    roles: Dict[str, List[str]] = {}
    for r in admin_rows:
        via = str(r.get("assignedToGroup") or "")
        role = str(r.get("role") or r.get("roleId") or "") + (f" (through {via})" if via else "")
        address = holder_of(r, users)
        if address:
            roles.setdefault(address, []).append(role)
        elif _unnamed(r) and r.get("assignedTo"):
            roles.setdefault(f"ID {r.get('assignedTo')}", []).append(role)   # a holder no read could name: unknown
    nameless = unnamed_holders(admin_rows, users)
    status = {u.primary_email.strip().lower(): u for u in users or []}
    grants = grants_from(token_rows) if not apps_why else []
    apps_of: Counter = Counter(g.user.strip().lower() for g in grants)
    admin_rows_out = []
    for address in sorted(roles):
        holder = status.get(address)
        if address.startswith("ID "):
            admin_rows_out.append((address, "; ".join(sorted(set(roles[address]))), "unknown", "unknown"))
            continue
        suspended = "unknown" if users is None else ("not in the directory read" if holder is None
                                                      else ("yes" if holder.suspended else "no"))
        apps = "unknown" if apps_why or address in unread else str(apps_of[address])
        admin_rows_out.append((address, "; ".join(sorted(set(roles[address]))), suspended, apps))
    app_rows = []
    for g in grants:
        address = g.user.strip().lower()
        described = [describe(s) for s in g.scopes]
        flags = ([SUSPENDED_ADMIN] if (status.get(address) is not None and status[address].suspended) else []) \
            + ([AI_NAME] if looks_like_ai(g.name) else []) + ([BROAD] if any(d[1] == Reach.BROAD for d in described) else [])
        words = [d[0] for d in sorted(described, key=lambda d: d[1])]
        app_rows.append((address, g.name, ", ".join(dict.fromkeys(words)), ", ".join(flags)))
    app_rows.sort(key=lambda r: (not r[3], r[0], r[1].lower()))
    n_admins, n_flagged = len(admin_rows_out), sum(1 for r in app_rows if r[3])
    partial = (f" GAM couldn't read the apps of {', '.join(sorted(unread))}: unknown, not none." if unread else "")
    if nameless:
        partial += (f" GAM couldn't name {len(nameless)} role holder{'' if len(nameless) == 1 else 's'} and the directory read "
                    f"has no account with {'that ID' if len(nameless) == 1 else 'those IDs'} ({', '.join(nameless)}): "
                    "who they are, and their apps, are unknown.")
    some_unknown = bool(unread or nameless)
    return [
        Finding("admins", "Every admin", f"{n_admins} role holder{'' if n_admins == 1 else 's'}, suspended ones included.",
                ADMIN_COLUMNS, admin_rows_out, known=not admins_why, why=admins_why, links=True, partial=some_unknown,
                note=("A role held through a group counts each member. A holder GAM couldn't name is matched to the directory "
                      "by ID. A service account's role names no person, so it isn't listed." + partial)),
        Finding("admin_apps", "Admins' connected apps", (f"{len(app_rows)} grant{'' if len(app_rows) == 1 else 's'} across the "
                f"admins; {n_flagged} flagged, listed first." if app_rows else "No admin read has given an app access."),
                ADMIN_APP_COLUMNS, app_rows, known=not apps_why, why=apps_why, links=True, partial=some_unknown,
                note=("An app is called AI by its name alone. To revoke one, open Incident for that admin: its access is "
                      "taken away there, after a preview." + partial)),
    ]


def findings_for(run: Run, reads: Sequence[StepRead], tables: Dict[str, list], now: Optional[datetime] = None,
                 days: int = INACTIVE_DAYS, leaving: frozenset = frozenset()) -> List[Finding]:
    """The run's tables, from its step reads and their kept tables. A finding whose step is unknown says so."""
    now = now or datetime.now(timezone.utc)
    if run is INACTIVE_USERS:
        why = unknown_because(reads, DIRECTORY)
        if why:
            return [Finding("inactive", f"Inactive {days}+ days", why, INACTIVE_COLUMNS, known=False, why=why)]
        return [inactive_finding(tables[DIRECTORY.key], days, now)]
    users = tables.get(DIRECTORY.key) if not unknown_because(reads, DIRECTORY) else None
    if run is LICENSE_USAGE:
        return license_findings(tables.get(LICENSES.key, []), users, reads)
    if run is SHARED_DRIVES:
        domains = domains_from(tables.get(DOMAINS.key, [])) if not unknown_because(reads, DOMAINS) else None
        return shared_drive_findings(tables.get(DRIVES.key, []), tables.get(DRIVE_ACLS.key, []), domains, users,
                                     frozenset(a.strip().lower() for a in leaving), reads)
    if run is CLASSROOM:
        return classroom_findings(tables.get(COURSES.key, []), users, reads, now)
    if run is ADMIN_TOKEN_REVIEW:
        return admin_token_findings(tables.get(ADMINS.key, []), users, tables.get(ADMIN_TOKENS.key, []), reads)
    raise ValueError(f"no findings for run {run.key!r}")
