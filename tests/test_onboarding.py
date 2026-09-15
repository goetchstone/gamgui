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
from gamgui.core.signatures import SignatureStore
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
    state.sig_templates = SignatureStore(tmp_path / "signatures.json")   # isolated; seeds "Classic"/"Modern accent"/"Minimal"
    with TestClient(create_app(state)) as c:
        c.get("/?token=t")
        yield c


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


# --- account creation: temp password, argv builder, per-role config ---

def test_temp_password_shape_and_alphabet():
    pw = onboarding.generate_temp_password()
    groups = pw.split("-")
    assert len(groups) == 3 and all(len(g) == 4 for g in groups)     # default is three groups of four
    assert set(pw) <= set(onboarding._PW_ALPHABET) | {"-"}
    assert not (set("0O1lI") & set(onboarding._PW_ALPHABET))         # no glyphs misread off a printed sheet
    assert len(onboarding.generate_temp_password(groups=4, size=5).replace("-", "")) == 20   # configurable
    assert len({onboarding.generate_temp_password() for _ in range(200)}) == 200             # effectively unique


def test_create_user_argv():
    assert GAMCommands.create_user("new@x.com", "Ada", "Byte", "s3cr-et", change_password=True, org_unit="/Sales") == \
        ["create", "user", "new@x.com", "firstname", "Ada", "lastname", "Byte",
         "password", "s3cr-et", "changepassword", "on", "org", "/Sales"]
    # no OU -> no org tokens; changepassword off is honoured
    assert GAMCommands.create_user("n@x.com", "A", "B", "p", change_password=False) == \
        ["create", "user", "n@x.com", "firstname", "A", "lastname", "B", "password", "p", "changepassword", "off"]
    # a poisoned name lands as ONE argv element (injection-safe)
    assert GAMCommands.create_user("n@x.com", "A; rm -rf /", "B", "p")[4] == "A; rm -rf /"


def test_role_config_persists_and_migrates(tmp_path):
    p = tmp_path / "ob.json"
    RunbookStore(p).set_role("Sales", ["Do a thing"], signature="Classic", org_unit="/Sales")
    r = RunbookStore(p).role("Sales")   # reload from disk
    assert r.steps == ["Do a thing"] and r.signature == "Classic" and r.org_unit == "/Sales"
    # an older file stored a role as a bare list of steps -> migrated to the dict form on load
    import json
    p.write_text(json.dumps({"roles": {"Legacy": ["Step A", "Step B"]}}))
    lr = RunbookStore(p).role("Legacy")
    assert lr.steps == ["Step A", "Step B"] and lr.signature == "" and lr.org_unit == ""


# --- connector: the temp password is never audited or surfaced ---

@pytest.mark.asyncio
async def test_create_user_redacts_password(connector, tmp_path):
    secret = "Xk7m-Qp9r-2Tzv"
    res = await connector.create_user("brand-new@example.com", "Ada", "Byte", secret, org_unit="/Sales")
    assert res.ok
    audit = (tmp_path / "audit.jsonl").read_text()
    assert secret not in audit and "create_user" in audit        # never written to the audit log
    assert secret not in " ".join(res.preview.argv or [])         # nor surfaced in the change preview
    assert "********" in (res.preview.argv or [])                 # the masked copy is what's shown/audited


@pytest.mark.asyncio
async def test_create_user_fails_on_duplicate(connector):
    res = await connector.create_user("exists@example.com", "Al", "Ready", "pw")
    assert not res.ok   # the mock mirrors GAM's 409 on an account that already exists


# --- web flow: create the account, print the sheet, keep the password out of the log ---

def test_preview_shows_create_account_block(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "signature": "Classic", "org_unit": "/Sales"})
    r = client.post("/onboard/preview", data={"role": "Sales", "name": "Ada Byte",
                                              "email": "ada@example.com", "create_account": "1"})
    assert r.status_code == 200
    assert "Creates the account" in r.text and "ada@example.com" in r.text
    assert "/Sales" in r.text and "Classic" in r.text        # role's OU + signature surfaced
    assert "Ada" in r.text and "Byte" in r.text              # first/last derived from the display name


def test_run_refuses_account_without_confirmation(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte",
                                          "email": "ada@example.com", "create_account": "1"})   # no confirm=1
    assert "confirm" in r.text.lower()   # gated behind the preview's confirmation


def test_run_creates_account_and_returns_sheet(client, tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "SENTINELpw-1234-5678")
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "signature": "Classic", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte",
                                          "email": "ada@example.com", "assignee": "it@example.com",
                                          "create_account": "1", "confirm": "1"})
    assert r.status_code == 200
    assert "Account created" in r.text and "SENTINELpw-1234-5678" in r.text   # the printable sheet shows the temp pw
    assert "Classic applied" in r.text                                        # the role's signature was applied
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "SENTINELpw-1234-5678" not in audit and "create_user" in audit     # ...but it never reaches the audit log


def test_run_account_duplicate_fails_gracefully(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Al Ready",
                                          "email": "exists@example.com", "assignee": "it@example.com",
                                          "create_account": "1", "confirm": "1"})
    assert "create the account" in r.text and "409" in r.text   # the 409 is surfaced, not swallowed


# --- per-role group membership + shared calendars ---

def test_role_groups_calendars_persist_and_migrate(tmp_path):
    p = tmp_path / "ob.json"
    RunbookStore(p).set_role("Sales", ["Step"], groups=["sales@example.com", ""], calendars=["cal@x", "  "])
    r = RunbookStore(p).role("Sales")   # reload from disk
    assert r.groups == ["sales@example.com"] and r.calendars == ["cal@x"]   # blanks dropped, persisted
    # a dict written before groups/calendars existed migrates to empty lists (keeps other fields)
    import json
    p.write_text(json.dumps({"roles": {"Old": {"steps": ["s"], "signature": "", "org_unit": "/O"}}}))
    old = RunbookStore(p).role("Old")
    assert old.groups == [] and old.calendars == [] and old.org_unit == "/O"


def test_preview_shows_groups_and_calendars(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "groups": "sales@example.com\nstaff@example.com",
                                        "calendars": "team@group.calendar.google.com"})
    r = client.post("/onboard/preview", data={"role": "Sales", "name": "Ada", "email": "ada@example.com"})
    assert r.status_code == 200
    assert "2 groups" in r.text and "sales@example.com" in r.text and "staff@example.com" in r.text
    assert "1 shared calendar" in r.text and "team@group.calendar.google.com" in r.text


def test_run_adds_groups_and_subscribes_calendars(client):
    # No account creation — groups/calendars apply to an existing hire's email.
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "groups": "sales@example.com\nstaff@example.com",
                                        "calendars": "team@group.calendar.google.com"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada",
                                          "email": "ada@example.com", "assignee": "it@example.com"})
    assert r.status_code == 200
    assert "2 of 2 groups" in r.text and "1 of 1 shared calendar" in r.text
    assert "Created" in r.text   # the tasklist still ran


def test_run_reports_failed_group_and_calendar_non_fatal(client):
    # A *missing* group 404s in the mock; a SUBFAIL calendar is refused — both reported, neither fatal.
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "groups": "sales@example.com\nmissing-group@example.com",
                                        "calendars": "SUBFAIL-cal@x"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada",
                                          "email": "ada@example.com", "assignee": "it@example.com"})
    assert r.status_code == 200
    assert "1 of 2 groups" in r.text and "missing-group@example.com" in r.text   # bad group reported
    assert "0 of 1 shared calendar" in r.text and "SUBFAIL-cal@x" in r.text      # bad calendar reported
    assert "Created" in r.text   # memberships are best-effort; the runbook still ran
