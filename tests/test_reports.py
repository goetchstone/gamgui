from __future__ import annotations

from datetime import datetime, timezone

from gamgui.core.gam.models import GAMUser
from gamgui.core.reports import build_reports, parse_usage


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


def test_directory_completeness_buckets():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    users = [
        _u("full@e.com", organizations=[{"title": "Director", "department": "IT", "primary": True}],
           phones=[{"value": "555-1212", "primary": True}], locations=[{"buildingName": "Glastonbury", "primary": True}]),
        _u("bare@e.com"),  # no title, department, phone, or location
        _u("gone@e.com", suspended=True),  # suspended -> excluded from active completeness buckets
    ]
    reports = {r.key: r for r in build_reports(users, now=now)}
    assert [u.primary_email for u in reports["no_title"].users] == ["bare@e.com"]
    assert [u.primary_email for u in reports["no_department"].users] == ["bare@e.com"]
    assert [u.primary_email for u in reports["no_phone"].users] == ["bare@e.com"]
    assert [u.primary_email for u in reports["no_location"].users] == ["bare@e.com"]


def test_never_logged_in_counts_as_inactive():
    now = datetime(2026, 6, 19, tzinfo=timezone.utc)
    reports = {r.key: r for r in build_reports([_u("n@e.com", isEnrolledIn2Sv=True, recoveryEmail="r@e.com")], now=now)}
    assert [u.primary_email for u in reports["inactive"].users] == ["n@e.com"]


def test_parse_usage_sorts_by_storage_and_converts_gb():
    rows = [
        {"email": "small@e.com", "accounts:used_quota_in_mb": "2048", "gmail:num_emails_received": "40", "gmail:num_emails_sent": "3"},
        {"email": "big@e.com", "accounts:used_quota_in_mb": "1048576", "gmail:num_emails_received": "5", "gmail:num_emails_sent": "0"},
    ]
    u = parse_usage(rows)
    assert u[0].email == "big@e.com" and u[0].storage_gb == 1024.0
    assert u[1].email == "small@e.com" and u[1].storage_gb == 2.0
    assert u[1].received == 40 and u[1].sent == 3
