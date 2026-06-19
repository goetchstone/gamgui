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
        last = _parse_dt(u.last_login_time)
        if last is None or last < cutoff:
            inactive.append(u)

    return [
        Report("no_2sv", "No 2-step verification", "Active users not enrolled in 2SV — a real security gap.", no_2sv),
        Report("inactive", f"Inactive ({inactive_days}+ days)", "Active users with no recent (or any) login.", inactive),
        Report("admins", "Administrators", "Accounts with super or delegated admin privileges.", admins),
        Report("no_recovery", "No recovery info", "Active users without a recovery email set.", no_recovery),
        Report("suspended", "Suspended", "Accounts currently suspended (sign-in blocked).", suspended),
    ]
