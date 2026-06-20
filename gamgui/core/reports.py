"""Directory insight reports — the read-only "stuff the Admin Console buries" view.

Pure functions: given a list of :class:`GAMUser`, bucket them into security/activity findings.
No GAM calls here (kept testable); the connector fetches the users with the right fields.
"""

from __future__ import annotations

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
        Report("no_location", "No location", "Active users with no location/store set.", no_location),
    ]
