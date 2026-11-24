"""Directory insight reports — the read-only "stuff the Admin Console buries" view.

Pure functions: given a list of :class:`GAMUser`, bucket them into security/activity findings.
No GAM calls here (kept testable); the connector fetches the users with the right fields.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .gam.models import GAMUser

# Fields the report needs from `gam print users`.
REPORT_FIELDS = (
    "primaryEmail", "name", "suspended", "isAdmin", "isDelegatedAdmin",
    "isEnrolledIn2Sv", "lastLoginTime", "orgUnitPath", "recoveryEmail",
)

INACTIVE_DAYS = 90

# Usage-report parameters (Admin SDK reports API). Data lags ~2-3 days.
USAGE_PARAMS = (
    "accounts:used_quota_in_mb",
    "gmail:num_emails_received",
    "gmail:num_emails_sent",
    "drive:num_items_created",
)


@dataclass
class UsageRow:
    email: str
    quota_mb: int
    storage_gb: float
    received: int
    sent: int
    drive_created: int


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_usage(rows: List[dict]) -> List[UsageRow]:
    """Turn raw `gam report users` CSV rows into UsageRows, sorted by storage (desc)."""
    out = []
    for r in rows:
        mb = _int(r.get("accounts:used_quota_in_mb"))
        out.append(UsageRow(
            email=str(r.get("email", "")),
            quota_mb=mb,
            storage_gb=round(mb / 1024, 1),
            received=_int(r.get("gmail:num_emails_received")),
            sent=_int(r.get("gmail:num_emails_sent")),
            drive_created=_int(r.get("drive:num_items_created")),
        ))
    out.sort(key=lambda u: u.quota_mb, reverse=True)
    return out


@dataclass
class Report:
    key: str
    title: str
    description: str
    users: List[GAMUser]

    @property
    def count(self) -> int:
        return len(self.users)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def build_reports(users: List[GAMUser], now: Optional[datetime] = None, inactive_days: int = INACTIVE_DAYS) -> List[Report]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)

    no_2sv, admins, suspended, inactive, no_recovery = [], [], [], [], []
    no_title, no_dept, no_phone, no_location = [], [], [], []
    for u in users:
        if u.suspended:
            suspended.append(u)
            continue  # the buckets below describe *active* accounts
        if u.is_admin or u.is_delegated_admin:
            admins.append(u)
        if not u.enrolled_2sv:
            no_2sv.append(u)
        if not u.recovery_email:
            no_recovery.append(u)
        if not (u.title or "").strip():
            no_title.append(u)
        if not (u.department or "").strip():
            no_dept.append(u)
        if not (u.phone or "").strip():
            no_phone.append(u)
        if not (u.location or "").strip():
            no_location.append(u)
        last = _parse_dt(u.last_login_time)
        if last is None or last < cutoff:
            inactive.append(u)

    return [
        Report("no_2sv", "No 2-step verification", "Active users not enrolled in 2SV — a real security gap.", no_2sv),
        Report("inactive", f"Inactive ({inactive_days}+ days)", "Active users with no recent (or any) login.", inactive),
        Report("admins", "Administrators", "Accounts with super or delegated admin privileges.", admins),
        Report("no_recovery", "No recovery info", "Active users without a recovery email set.", no_recovery),
        Report("suspended", "Suspended", "Accounts currently suspended (sign-in blocked).", suspended),
        # Directory completeness — the worklist for filling profile data (e.g. before a signature rollout).
        Report("no_title", "No job title", "Active users with no title set — needed for role-based signatures.", no_title),
        Report("no_department", "No department", "Active users with no department set.", no_dept),
        Report("no_phone", "No phone", "Active users with no work phone set.", no_phone),
        Report("no_location", "No location", "Active users with no location set.", no_location),
    ]


# --- named reports (roadmap 3.1) ------------------------------------------------------------------------------------
# Domain-wide reads the Builder already runs, named so they can be found. No new parser: each opens its catalog command.
# A command is named by its syntax, not its `raw.N` id, which moves with every GAM bump; test_every_named_report_is_one_read
# fails if a bump renames one or makes it ambiguous. Each read's token and scope are GAM's own (gamlib glapi
# `_CLIENT_SCOPES` for the admin token, `_SVCACCT_SCOPES` for the service account), read from the vendored 7.52.03 build.
ADMIN_TOKEN, SERVICE_ACCOUNT = "admin", "service"


@dataclass(frozen=True)
class NamedReport:
    key: str
    title: str
    what: str
    syntax: str     # the catalog command's syntax up to its first option
    token: str      # ADMIN_TOKEN (oauth2.txt, picked in `gam oauth create`) or SERVICE_ACCOUNT (domain-wide delegation)
    scope: str


NAMED_REPORTS = (
    NamedReport("licenses", "Licenses", "Who holds which Google Workspace licence.", "gam print licenses",
                ADMIN_TOKEN, "https://www.googleapis.com/auth/apps.licensing"),
    NamedReport("shared_drives", "Shared drives", "Every shared drive in the domain.", "gam print shareddrives",
                SERVICE_ACCOUNT, "https://www.googleapis.com/auth/drive"),
    NamedReport("shared_drive_acls", "Shared drive members", "Who has access to each shared drive, and how.",
                "gam print shareddriveacls", SERVICE_ACCOUNT, "https://www.googleapis.com/auth/drive"),
    NamedReport("shared_drive_organizers", "Shared drive organizers", "Who can manage each shared drive.",
                "gam print shareddriveorganizers", SERVICE_ACCOUNT, "https://www.googleapis.com/auth/drive"),
    NamedReport("org_units", "Org units", "The organizational units and their paths.", "gam print orgs|ous",
                ADMIN_TOKEN, "https://www.googleapis.com/auth/admin.directory.orgunit"),
    NamedReport("mobile_devices", "Mobile devices", "Phones and tablets that sync with the domain.", "gam print mobile",
                ADMIN_TOKEN, "https://www.googleapis.com/auth/admin.directory.device.mobile"),
    NamedReport("group_members", "Group memberships", "Every group's members and their roles.", "gam print group-members",
                ADMIN_TOKEN, "https://www.googleapis.com/auth/admin.directory.group"),
    NamedReport("data_transfers", "Data transfers", "Drive and Calendar ownership transfers and how far each got.",
                "gam print datatransfers|transfers", ADMIN_TOKEN, "https://www.googleapis.com/auth/admin.datatransfer"),
    NamedReport("alerts", "Google's alerts", "The Alert Center: suspicious sign-ins, phishing, device issues.",
                "gam print alerts", SERVICE_ACCOUNT, "https://www.googleapis.com/auth/apps.alerts"),
)


def named_report_command(catalog, report: NamedReport):
    """The one runnable catalog read ``report`` names, else ``None`` (a bump renamed it, or it matches more than one)."""
    hits = [c for c in catalog.commands
            if (c.raw_syntax == report.syntax or c.raw_syntax.startswith(report.syntax + " ")) and c.buildable]
    return hits[0] if len(hits) == 1 else None


def admin_scope_held(report: NamedReport, granted) -> Optional[bool]:
    """Whether the admin token holds ``report``'s scope (or its read-only form): ``None`` for a service-account read, whose
    delegation only `check serviceaccount` can see, and when the token's scopes can't be read. Unknown is never "held"."""
    if report.token != ADMIN_TOKEN or not isinstance(granted, list):
        return None
    return report.scope in granted or f"{report.scope}.readonly" in granted


# --- Inactive → suspend (roadmap 3.3, D3) -----------------------------------------------------------------------------
@dataclass(frozen=True)
class InactiveTarget:
    email: str
    name: str
    last_login: str      # as the directory gave it; "" for never
    new: bool            # made inside the window, or when isn't known: a new hire who hasn't signed in yet looks inactive


def inactive_to_suspend(users: List[GAMUser], admin_email: str, admin_id: str, now: Optional[datetime] = None,
                        inactive_days: int = INACTIVE_DAYS) -> "tuple[List[InactiveTarget], List[str]]":
    """The Inactive bucket as the people a suspend would act on, from a directory read just now: ``(targets, left_out)``.
    The admin GamGUI signs in as is left out (suspending it would sign GamGUI out, and the chokepoint refuses it anyway),
    matched by Google's account ID as well as by address. Each target says whether it was made inside the window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)
    bucket = next(r for r in build_reports(users, now=now, inactive_days=inactive_days) if r.key == "inactive")
    targets, left_out = [], []
    for u in sorted(bucket.users, key=lambda u: u.primary_email.lower()):
        if u.primary_email.strip().lower() == admin_email.strip().lower() or (admin_id and str(u.raw.get("id") or "") == admin_id):
            left_out.append(u.primary_email)
            continue
        made = _parse_dt(u.creation_time)
        targets.append(InactiveTarget(u.primary_email, u.full_name, u.last_login_time or "", made is None or made >= cutoff))
    return targets, left_out


# --- No 2SV / No recovery → a reminder email (roadmap 3.3, D3) ---------------------------------------------------------
# The fix is the person's to make, so the action is an email asking them to. Each bucket has its own starting text, which
# the operator can change before the preview; the preview holds the people a fresh directory read found and that exact text.
SECURITY_PAGE = "https://myaccount.google.com/security"
REMINDERS = {
    "no_2sv": ("Please turn on 2-Step Verification",
               "Hello,\n\nYour Google account doesn't have 2-Step Verification turned on yet. It keeps your account safe "
               "even if someone learns your password. Please turn it on from the Security page of your Google account: "
               f"{SECURITY_PAGE}\n\nThank you."),
    "no_recovery": ("Please add a recovery email or phone",
                    "Hello,\n\nYour Google account has no recovery email or phone. Without one, getting back into your "
                    "account after a lost password takes much longer. Please add one from the Security page of your "
                    f"Google account: {SECURITY_PAGE}\n\nThank you."),
}


def reminder_html(text: str) -> str:
    """The operator's plain text as the email's HTML: escaped, its line breaks kept, nothing else of theirs as markup."""
    return "<br>".join(html.escape(line) for line in text.strip().splitlines())


def to_remind(users: List[GAMUser], key: str, now: Optional[datetime] = None) -> List[GAMUser]:
    """The bucket ``key`` (a REMINDERS key) from a directory read just now, sorted by address."""
    bucket = next(r for r in build_reports(users, now=now) if r.key == key)
    return sorted(bucket.users, key=lambda u: u.primary_email.lower())


# --- the completeness buckets as a sheet to fill in (roadmap 3.3, D3) -------------------------------------------------
# No title, department, phone or location: the fix is data only the organisation has, so each of these buckets downloads as
# a spreadsheet to fill in. Its columns are the ones bulk apply from CSV (roadmap 4.6) will read back, by name: `email`
# says who, `name` is there for the person filling it in, and each other column is the profile field it sets.
FILL_IN_REPORTS = ("no_title", "no_department", "no_phone", "no_location")
FILL_IN_COLUMNS = ("email", "name", "title", "department", "phone", "location", "org_unit")


def fill_in_rows(users: List[GAMUser]) -> List[tuple]:
    """One row per person, in FILL_IN_COLUMNS order, sorted by address: the values the directory holds now."""
    return [(u.primary_email, u.full_name, u.title, u.department, u.phone, u.location, u.org_unit_path)
            for u in sorted(users, key=lambda u: u.primary_email.lower())]
