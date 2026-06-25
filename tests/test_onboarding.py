"""Onboarding runbooks: GAM argv, the role/welcome template store, and the /onboard web flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core import onboarding
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.onboarding import RunbookStore
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    state.runbooks = RunbookStore(tmp_path / "onboarding.json")   # isolated store, not the real ~/Library file
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


# --- GAM argv (injection-safe single elements) ---

def test_runbook_command_argv():
    assert GAMCommands.create_tasklist("a@x.com", "Onboard Jo") == \
        ["user", "a@x.com", "create", "tasklist", "title", "Onboard Jo", "returnidonly"]
    assert GAMCommands.create_task("a@x.com", "TL1", "Set up Brite", "note") == \
        ["user", "a@x.com", "create", "task", "TL1", "title", "Set up Brite", "notes", "note"]
    assert GAMCommands.send_email("new@x.com", "Hi", "Welcome") == \
        ["sendemail", "to", "new@x.com", "subject", "Hi", "message", "Welcome", "html"]
    # a poisoned step lands as ONE argv element
    argv = GAMCommands.create_task("a@x.com", "TL1", "evil; rm -rf /")
    assert argv[-1] == "evil; rm -rf /"


# --- the template store ---

def test_store_round_trip(tmp_path):
    s = RunbookStore(tmp_path / "ob.json")
    s.set_role("Cashier", ["Set up POS", "Issue badge", ""])   # blanks dropped
    assert s.steps_for("Cashier") == ["Set up POS", "Issue badge"]
    s.set_welcome("Welcome {name}", "Hi {name}, you are a {role}.")
    # reload from disk → persisted
    s2 = RunbookStore(tmp_path / "ob.json")
    assert "Cashier" in s2.role_names() and s2.welcome()["subject"] == "Welcome {name}"
    s2.delete_role("Cashier")
    assert "Cashier" not in RunbookStore(tmp_path / "ob.json").role_names()


def test_store_rejects_blank_role(tmp_path):
    with pytest.raises(ValueError):
        RunbookStore(tmp_path / "ob.json").set_role("  ", ["x"])


def test_render_substitutes_known_vars_only():
    out = onboarding.render("Hi {name}, {role} at {email}. {unknown}",
                            {"name": "Jo", "role": "Sales", "email": "jo@x.com"})
    assert out == "Hi Jo, Sales at jo@x.com. {unknown}"   # unknown left untouched, no crash


# --- web flow ---

def test_onboard_page_lists_seed_role(client):
    r = client.get("/onboard")
    assert r.status_code == 200 and "Onboard a new hire" in r.text and "Salesperson" in r.text


def test_add_and_delete_role(client):
    r = client.post("/onboard/role", data={"name": "Cashier", "steps": "Set up POS\nIssue badge"})
    assert "Cashier" in r.text and "Set up POS" in r.text
    r2 = client.post("/onboard/role/delete", data={"name": "Cashier"})
    assert "Cashier" not in r2.text


def test_welcome_template_saves(client):
    r = client.post("/onboard/welcome", data={"subject": "Hi {name}", "body": "Welcome {name}"})
    assert "Saved" in r.text and "Hi {name}" in r.text


def test_preview_renders_steps_and_email(client):
    r = client.post("/onboard/preview", data={"role": "Salesperson", "name": "Jordan",
                                              "email": "jordan@example.com", "manager": "mgr@example.com",
                                              "send_welcome": "1"})
    assert r.status_code == 200 and "Set up Brite for the employee" in r.text
    assert "Jordan" in r.text   # welcome email rendered with the name


def test_run_creates_google_tasks_list(client):
    r = client.post("/onboard/run", data={"role": "Salesperson", "name": "Jordan",
                                          "email": "jordan@example.com", "assignee": "it@example.com"})
    assert r.status_code == 200
    assert "Created" in r.text and "it@example.com" in r.text   # tasklist made on the assignee


def test_run_needs_an_assignee_or_email(client):
    r = client.post("/onboard/run", data={"role": "Salesperson", "name": "Jordan"})
    assert "assignee" in r.text.lower()
