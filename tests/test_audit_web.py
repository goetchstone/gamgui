from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog, read_records
from gamgui.core.calendar_index import CalendarIndex
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Mirrors tests/test_users_web.py's client fixture, but keeps the audit path handy so tests
    can seed records the /audit routes will actually read (the route resolves the path from
    ``connector.audit.path``, same object this fixture constructs)."""
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(audit_path))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    c = TestClient(create_app(state))
    c.get("/?token=t")
    c.audit_path = audit_path  # type: ignore[attr-defined]
    return c


def _seed(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


# --- unit: read_records ------------------------------------------------------------------

def test_read_records_newest_first_and_skips_malformed(tmp_path):
    path = tmp_path / "audit.jsonl"
    lines = [
        json.dumps({"ts": "2026-06-23T15:00:00+00:00", "action": "set_vacation", "target": "a@example.com", "ok": True}),
        "not json at all",
        "",
        json.dumps({"ts": "2026-06-23T15:01:00+00:00", "action": "suspend", "target": "b@example.com", "ok": False, "extra": {"error": "boom"}}),
        json.dumps({"ts": "2026-06-23T15:02:00+00:00", "action": "delete_user", "target": "c@example.com", "ok": True}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = read_records(path)
    assert len(records) == 3
    # most-recent-first
    assert [r["action"] for r in records] == ["delete_user", "suspend", "set_vacation"]
    failures = [r for r in records if r.get("ok") is False]
    assert len(failures) == 1
    assert failures[0]["extra"]["error"] == "boom"


def test_read_records_missing_file_returns_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []


def test_read_records_respects_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path, [{"ts": str(i), "action": "noop", "ok": True} for i in range(10)])
    records = read_records(path, limit=3)
    assert len(records) == 3
    assert records[0]["ts"] == "9"  # newest first


# --- routes -------------------------------------------------------------------------------

def test_audit_page_renders_seeded_records(client):
    _seed(client.audit_path, [
        {"ts": "2026-06-23T15:02:51+00:00", "connector": "google_workspace", "action": "set_vacation",
         "target": "alice@example.com", "argv": ["user", "alice@example.com"], "exit_code": 0, "ok": True, "actor": None},
        {"ts": "2026-06-23T15:03:00+00:00", "connector": "google_workspace", "action": "suspend",
         "target": "bob@example.com", "argv": ["update", "user", "bob@example.com"], "exit_code": 1, "ok": False,
         "actor": None, "extra": {"error": "Permission denied", "tolerated": False}},
    ])
    r = client.get("/audit")
    assert r.status_code == 200
    assert "Audit log" in r.text
    assert "2 actions logged" in r.text
    assert "1 failure" in r.text
    assert "alice@example.com" in r.text
    assert "bob@example.com" in r.text
    assert "Failed" in r.text


def test_audit_rows_failed_filter(client):
    _seed(client.audit_path, [
        {"ts": "t1", "action": "set_vacation", "target": "alice@example.com", "ok": True},
        {"ts": "t2", "action": "suspend", "target": "bob@example.com", "ok": False, "extra": {"error": "boom"}},
    ])
    r = client.get("/audit/rows", params={"failed": 1})
    assert r.status_code == 200
    assert "bob@example.com" in r.text
    assert "alice@example.com" not in r.text


def test_audit_rows_query_filter(client):
    _seed(client.audit_path, [
        {"ts": "t1", "action": "set_vacation", "target": "alice@example.com", "ok": True},
        {"ts": "t2", "action": "suspend", "target": "bob@example.com", "ok": False, "extra": {"error": "boom"}},
    ])
    r = client.get("/audit/rows", params={"q": "vacation"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "bob@example.com" not in r.text


def test_audit_rows_empty_state(client):
    r = client.get("/audit/rows")
    assert r.status_code == 200
    assert "No audited actions yet." in r.text


def test_audit_export_csv(client):
    _seed(client.audit_path, [
        {"ts": "2026-06-23T15:02:51+00:00", "action": "set_vacation", "target": "alice@example.com",
         "argv": ["user", "alice@example.com"], "exit_code": 0, "ok": True},
    ])
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "audit-export.csv" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "ts,action,target,ok,exit_code,error,argv"
    assert "alice@example.com" in lines[1]


def test_audit_export_csv_no_records_still_200(client):
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "ts,action,target,ok,exit_code,error,argv"


# --- unconnected: routes shouldn't 500 even with no connector -----------------------------

@pytest.fixture
def unconnected_client(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


def test_audit_page_without_connector(unconnected_client):
    r = unconnected_client.get("/audit")
    assert r.status_code == 200


def test_audit_rows_without_connector(unconnected_client):
    r = unconnected_client.get("/audit/rows")
    assert r.status_code == 200


def test_audit_export_without_connector(unconnected_client):
    r = unconnected_client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
