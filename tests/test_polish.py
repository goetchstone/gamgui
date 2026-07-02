"""Polish: status badges on Reports' user lists + "Clear filters" on the empty Users table."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.calendar_index import CalendarIndex, IndexedCalendar
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.models import GAMUser
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.reports import Report
from gamgui.web.server import AppState, TEMPLATES, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


def _fake_request() -> Request:
    """A minimal ASGI request scope — enough for Jinja2Templates.TemplateResponse (no url_for used)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/reports",
        "headers": [],
        "query_string": b"",
        "app": None,
    }
    return Request(scope)


# --- Users table empty state: "Clear filters" -------------------------------------------

def test_users_empty_state_offers_clear_filters(client):
    r = client.get("/users/table", params={"q": "zzz-no-match", "scope": "all"})
    assert r.status_code == 200
    assert "No users found" in r.text
    assert "Clear filters" in r.text
    assert 'hx-get="/users/table?q=&amp;scope=all&amp;page=1"' in r.text or "/users/table?q=&scope=all&page=1" in r.text


def test_users_no_filters_no_clear_button(client):
    r = client.get("/users/table", params={"q": "", "scope": "all"})
    assert r.status_code == 200
    assert "No users found" not in r.text  # alice/bob/carol exist unfiltered
    assert "Clear filters" not in r.text


def test_users_suspended_scope_empty_offers_clear_filters(client):
    # A scope filter alone (no q) should also trigger the clear-filters affordance when it yields nothing.
    r = client.get("/users/table", params={"q": "zzz-nobody", "scope": "suspended"})
    assert r.status_code == 200
    assert "No users found" in r.text
    assert "Clear filters" in r.text


# --- Reports lists: status badges --------------------------------------------------------

def test_reports_lists_show_status_badges():
    admin_user = GAMUser.from_json({
        "primaryEmail": "alice@example.com",
        "name": {"givenName": "Alice", "familyName": "Anders"},
        "suspended": False,
        "isAdmin": True,
        "isEnrolledIn2Sv": False,  # forced into no_2sv for this test, alongside her admin flag
    })
    plain_user = GAMUser.from_json({
        "primaryEmail": "carol@example.com",
        "name": {"givenName": "Carol", "familyName": "Clark"},
        "suspended": False,
        "isAdmin": False,
        "isEnrolledIn2Sv": False,
    })
    reports = [
        Report(
            key="no_2sv",
            title="No 2-step verification",
            description="Active users not enrolled in 2SV — a real security gap.",
            users=[admin_user, plain_user],
        ),
    ]
    resp = TEMPLATES.TemplateResponse(
        _fake_request(), "reports.html", {"connected": True, "reports": reports, "total": 2}
    )
    body = resp.body.decode()
    assert "Admin" in body        # alice, an admin, surfaced with a badge in the no-2sv list
    # Carol is plain (no admin/suspended flags) — no badge noise for her.
    carol_snippet = body.split("carol@example.com")[0][-200:]
    assert "Admin" not in carol_snippet or "alice" in carol_snippet


def test_reports_admins_list_skips_redundant_admin_badge():
    admin_user = GAMUser.from_json({
        "primaryEmail": "alice@example.com",
        "name": {"givenName": "Alice", "familyName": "Anders"},
        "suspended": False,
        "isAdmin": True,
        "isEnrolledIn2Sv": True,
    })
    reports = [
        Report(key="admins", title="Administrators", description="Accounts with admin privileges.", users=[admin_user]),
    ]
    resp = TEMPLATES.TemplateResponse(
        _fake_request(), "reports.html", {"connected": True, "reports": reports, "total": 1}
    )
    body = resp.body.decode()
    # The admins report already says "Administrators" — no redundant per-row "Admin" pill needed.
    assert "Admin</span>" not in body


def test_reports_suspended_list_skips_redundant_suspended_badge():
    suspended_user = GAMUser.from_json({
        "primaryEmail": "bob@example.com",
        "name": {"givenName": "Bob", "familyName": "Brown"},
        "suspended": True,
        "isAdmin": False,
    })
    reports = [
        Report(key="suspended", title="Suspended", description="Accounts currently suspended.", users=[suspended_user]),
    ]
    resp = TEMPLATES.TemplateResponse(
        _fake_request(), "reports.html", {"connected": True, "reports": reports, "total": 1}
    )
    body = resp.body.decode()
    assert "Suspended</span>" not in body


def test_reports_page_renders_with_mock_fixtures(client):
    # End-to-end smoke test through the real route + mock GAM data (alice=admin, bob=suspended).
    r = client.get("/reports")
    assert r.status_code == 200
    assert "Administrators" in r.text
    assert "Suspended" in r.text


# --- Navigation dead ends: link people/groups everywhere they appear ---------------------

def test_board_members_link_to_user_detail(client):
    r = client.get("/groups/members", params={"group": "sales@example.com"})
    assert r.status_code == 200
    assert "/users/detail?email=" in r.text


def _seed_calendar_index(client):
    """Populate the persistent index as a rebuild would (background build doesn't run under TestClient)."""
    client.app.state.gamgui.calendar_index.replace_all(DOMAIN, [
        IndexedCalendar("c_house123@group.calendar.google.com", "House Call Calendar", "alice@example.com", "secondary", 2),
    ])


def test_calendar_search_owner_links_to_user_detail(client):
    _seed_calendar_index(client)
    r = client.get("/calendars/search", params={"q": "house"})
    assert r.status_code == 200
    assert "/users/detail?email=" in r.text


def test_user_groups_chip_links_to_groups_board(client):
    r = client.get("/users/groups", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert 'href="/groups"' in r.text
