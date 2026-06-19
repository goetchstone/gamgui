from __future__ import annotations

from datetime import datetime, timezone

from gamgui.core.gam.models import GAMUser
from gamgui.core.reports import build_reports


def _u(email, **kw):
    return GAMUser.from_json({"primaryEmail": email, **kw})


def test_build_reports_buckets():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    users = [
        _u("a@e.com", isAdmin=True, isEnrolledIn2Sv=True, lastLoginTime="2026-06-18T08:00:00Z", recoveryEmail="r@e.com"),
        _u("b@e.com", suspended=True, isEnrolledIn2Sv=False, lastLoginTime="2025-12-01T00:00:00Z"),
        _u("c@e.com", isEnrolledIn2Sv=False, lastLoginTime="2026-01-01T00:00:00Z"),
    ]
    reports = {r.key: r for r in build_reports(users, now=now)}
    # suspended accounts are excluded from the active-user findings
    assert [u.primary_email for u in reports["no_2sv"].users] == ["c@e.com"]
    assert [u.primary_email for u in reports["admins"].users] == ["a@e.com"]
    assert [u.primary_email for u in reports["suspended"].users] == ["b@e.com"]
    assert [u.primary_email for u in reports["inactive"].users] == ["c@e.com"]   # a logged in yesterday
    assert "c@e.com" in [u.primary_email for u in reports["no_recovery"].users]


def test_never_logged_in_counts_as_inactive():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    reports = {r.key: r for r in build_reports([_u("n@e.com", isEnrolledIn2Sv=True, recoveryEmail="r@e.com")], now=now)}
    assert [u.primary_email for u in reports["inactive"].users] == ["n@e.com"]
