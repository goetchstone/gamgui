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

from .helpers import TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


def _connector(tmp_path, gam_binary):
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=gam_binary, base_dir=tmp_path)
    return GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))


def _client(tmp_path, gam_binary):
    conn = _connector(tmp_path, gam_binary)
    state = AppState(vault=conn.runner.vault, runner=conn.runner, audit_domain=DOMAIN, connector=conn, token="t")
    state.runbooks = RunbookStore(tmp_path / "onboarding.json")   # isolated store, not the real ~/Library file
    state.sig_templates = SignatureStore(tmp_path / "signatures.json")   # isolated; seeds "Classic"/"Modern accent"/"Minimal"
    return TestClient(create_app(state, allowed_hosts=TEST_HOSTS))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    with _client(tmp_path, FIXTURES / "mock_gam.sh") as c:
        c.get("/?token=t")
        yield c


@pytest.fixture
def echoing_gam(tmp_path):
    """A gam that fails every call by echoing its command line, as GAM does on a usage error — here as
    the LAST stderr line, the one GAMError.message (so the audit's extra.error and the UI) carries."""
    path = tmp_path / "echoing_gam.sh"
    path.write_text("#!/bin/sh\nprintf 'ERROR: Invalid argument\\nCommand: gam %s\\n' \"$*\" 1>&2\nexit 2\n")
    path.chmod(0o755)
    return path


# --- GAM argv (injection-safe single elements) ---

def test_runbook_command_argv():
    assert GAMCommands.create_tasklist("a@x.com", "Onboard Jo") == \
        ["user", "a@x.com", "create", "tasklist", "title", "Onboard Jo", "returnidonly"]
    assert GAMCommands.create_task("a@x.com", "TL1", "Set up the CRM login", "note") == \
        ["user", "a@x.com", "create", "task", "TL1", "title", "Set up the CRM login", "notes", "note"]
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
    assert r.status_code == 200 and "Set up the CRM login" in r.text
    assert "Jordan" in r.text   # welcome email rendered with the name


def test_run_creates_google_tasks_list(client):
    r = client.post("/onboard/run", data={"role": "Salesperson", "name": "Jordan",
                                          "email": "jordan@example.com", "assignee": "it@example.com",
                                          "confirmed": "1"})
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
    # notify clause is appended after the attributes; notifypassword carries the same temp password
    assert GAMCommands.create_user("n@x.com", "A", "B", "pw", org_unit="/S", notify="p@x.com")[-4:] == \
        ["notify", "p@x.com", "notifypassword", "pw"]
    # a poisoned notify cell stays ONE argv element (GAM rejects it; never a second flag)
    assert GAMCommands.create_user("n@x.com", "A", "B", "pw", notify="e@x.com notifypassword hijack")[-3] == \
        "e@x.com notifypassword hijack"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("surname", ["Password", "NotifyPassword"])
async def test_create_user_failure_redacts_password_by_value(tmp_path, echoing_gam, surname):
    # A surname that IS a sensitive key shifts the positional mask onto the `password` keyword, so the
    # echoed temp password survived it into extra.error and the result detail. Redaction by value holds.
    secret = "Xk7m-Qp9r-2Tzv"
    conn = _connector(tmp_path, echoing_gam)
    res = await conn.create_user("new@example.com", "Ada", surname, secret, notify="it@example.com")
    assert not res.ok
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "create_user" in audit and secret not in audit
    assert secret not in (res.detail or "") and "***redacted***" in res.detail   # echoed, then masked
    assert secret not in " ".join(res.preview.argv or [])


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
                                          "email": "ada@example.com", "create_account": "1"})   # no confirmed=1
    assert "confirm" in r.text.lower()   # gated behind the preview's confirmation


def test_run_creates_account_and_returns_sheet(client, tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "SENTINELpw-1234-5678")
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "signature": "Classic", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte",
                                          "email": "ada@example.com", "assignee": "it@example.com",
                                          "create_account": "1", "confirmed": "1"})
    assert r.status_code == 200
    assert "Account created" in r.text and "SENTINELpw-1234-5678" in r.text   # the printable sheet shows the temp pw
    assert "Classic applied" in r.text                                        # the role's signature was applied
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "SENTINELpw-1234-5678" not in audit and "create_user" in audit     # ...but it never reaches the audit log
    # U11: a Copy button beside Print (WKWebView may not print), copying a plain-text version of the sheet
    assert 'onclick="copyEl(this)"' in r.text and "obPrintSheet('ob-creds-sheet')" in r.text
    assert "Temp password: SENTINELpw-1234-5678" in r.text


def test_credentials_sheets_share_one_print_helper_and_escape_the_copy_text(client):
    # F#23: both one-time sheets use the shared macro + obPrintSheet, not their own copy of a print function.
    tpl = Path(__file__).parent.parent / "gamgui" / "web" / "templates"
    for name in ("_onboard_run.html", "_onboard_bulk_status.html"):
        src = (tpl / name).read_text()
        assert "sheet_buttons(" in src and "function " not in src and "<script" not in src, name
    import time as _t
    from gamgui.web.routes.onboarding import OnboardJob
    client.app.state.gamgui.jobs["j"] = OnboardJob(
        id="j", total=1, done=1, ok=1, account_created=1, finished=True, finished_at=_t.monotonic(),
        credentials=[{"name": "Ada </textarea><b>x", "email": "ada@example.com",
                      "password": "COPYpw-1", "org_unit": "/Sales"}])
    r = client.get("/onboard/bulk/status?job=j")
    assert "obPrintSheet('ob-bulk-creds-sheet')" in r.text and "Temp password: COPYpw-1" in r.text
    assert "</textarea><b>" not in r.text and "&lt;/textarea&gt;&lt;b&gt;x" in r.text   # a name can't break out


def test_run_account_duplicate_fails_gracefully(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Al Ready",
                                          "email": "exists@example.com", "assignee": "it@example.com",
                                          "create_account": "1", "confirmed": "1"})
    assert "create the account" in r.text and "409" in r.text   # the 409 is surfaced, not swallowed


def test_run_account_failure_error_partial_never_shows_password(tmp_path, echoing_gam, monkeypatch):
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "SENTINELpw-1234-5678")
    with _client(tmp_path, echoing_gam) as c:
        c.get("/?token=t")
        c.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
        r = c.post("/onboard/run", data={"role": "Sales", "name": "Ada Password",
                                         "email": "ada@example.com", "assignee": "it@example.com",
                                         "create_account": "1", "confirmed": "1"})
    assert "Couldn&#39;t create the account" in r.text or "Couldn't create the account" in r.text
    assert "SENTINELpw-1234-5678" not in r.text and "***redacted***" in r.text
    assert "SENTINELpw-1234-5678" not in (tmp_path / "audit.jsonl").read_text()


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
                                          "email": "ada@example.com", "assignee": "it@example.com", "confirmed": "1"})
    assert r.status_code == 200
    assert "2 of 2 groups" in r.text and "1 of 1 shared calendar" in r.text
    assert "Created" in r.text   # the tasklist still ran


def test_run_reports_failed_group_and_calendar_non_fatal(client):
    # A *missing* group 404s in the mock; a SUBFAIL calendar is refused — both reported, neither fatal.
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS",
                                        "groups": "sales@example.com\nmissing-group@example.com",
                                        "calendars": "SUBFAIL-cal@x"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada",
                                          "email": "ada@example.com", "assignee": "it@example.com", "confirmed": "1"})
    assert r.status_code == 200
    assert "1 of 2 groups" in r.text and "missing-group@example.com" in r.text   # bad group reported
    assert "0 of 1 shared calendar" in r.text and "SUBFAIL-cal@x" in r.text      # bad calendar reported
    assert "Created" in r.text   # memberships are best-effort; the runbook still ran


# --- bulk CSV import ---

def _hire(**over):
    base = {"role": "Sales", "name": "Ada Byte", "email": "ada@example.com", "first": "", "last": "",
            "manager": "", "assignee": "it@example.com", "create_account": False, "send_welcome": False,
            "notify": ""}
    base.update(over)
    return base


def test_parse_hire_csv():
    rows, errors = onboarding.parse_hire_csv(onboarding.HIRE_CSV_TEMPLATE)
    assert len(rows) == 2 and not errors
    assert rows[0]["create_account"] is True and rows[0]["notify"] == "jordan.personal@gmail.com"
    assert rows[1]["notify"] == "" and rows[1]["send_welcome"] is False
    # header case-insensitive, blank lines skipped, per-row validation
    rows2, errors2 = onboarding.parse_hire_csv("ROLE,Email\nSales,a@x.com\n\n,b@x.com\nSales,\n")
    assert [r["email"] for r in rows2] == ["a@x.com"]     # role-less and blank rows dropped
    assert any("Row 4" in e for e in errors2)              # ",b@x.com" -> missing role
    assert any("Row 5" in e for e in errors2)              # "Sales," -> no email or assignee
    # a CSV with no role column is a hard error
    assert onboarding.parse_hire_csv("name,email\nx,y@x.com\n")[1] == \
        ["The CSV needs a 'role' column — that's what picks the template."]


@pytest.mark.asyncio
async def test_provision_hire_notify_vs_sheet(connector, tmp_path):
    from gamgui.web.routes.onboarding import _provision_hire
    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], signature="Classic", org_unit="/Sales",
                   groups=["sales@example.com"], calendars=["team@x"])
    sig_store = SignatureStore(tmp_path / "sig.json")
    cfg = store.role("Sales")
    # notify address -> GAM emails it, nothing on the printable sheet
    r1 = await _provision_hire(connector, sig_store, store, cfg,
                               _hire(name="Ada Byte", email="ada@example.com", create_account=True,
                                     notify="ada.personal@gmail.com"))
    assert r1["ok"] and r1["account_created"] and r1["notified"] and r1["credential"] is None
    assert r1["signature"] == "Classic" and r1["groups"]["added"] == 1
    # blank notify -> a credential for the printable sheet, not notified
    r2 = await _provision_hire(connector, sig_store, store, cfg,
                               _hire(name="Sam Rivers", email="sam@example.com", create_account=True))
    assert r2["credential"] and r2["credential"]["password"] and not r2["notified"]
    # the temp passwords never reach the audit log
    audit = (tmp_path / "audit.jsonl").read_text()
    assert r2["credential"]["password"] not in audit and "create_user" in audit


@pytest.mark.asyncio
async def test_provision_hire_account_failure_is_fatal_for_that_row(connector, tmp_path):
    from gamgui.web.routes.onboarding import _provision_hire
    store = RunbookStore(tmp_path / "ob.json"); store.set_role("Sales", ["Set up POS"])
    cfg = store.role("Sales")
    r = await _provision_hire(connector, SignatureStore(tmp_path / "sig.json"), store, cfg,
                              _hire(name="Al Ready", email="exists@example.com", first="Al", last="Ready",
                                    create_account=True))
    assert not r["ok"] and any("create" in e for e in r["errors"])


@pytest.mark.asyncio
async def test_run_bulk_onboard_executor(connector, tmp_path):
    # Drive the executor directly (never under TestClient — the bg task + mock-gam can deadlock).
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard, _RECENT_WINDOW
    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], groups=["sales@example.com"])
    sig_store = SignatureStore(tmp_path / "sig.json")
    rows = [_hire(name="Ada", email="ada@example.com"),                                  # existing acct: ok
            _hire(name="Bad", email="exists@example.com", first="Bad", last="Row", create_account=True),  # 409
            _hire(role="Nope", name="X", email="x@example.com")]                          # unknown role
    cfgs = {"Sales": store.role("Sales"), "Nope": None}
    job = OnboardJob(id="t", total=3)
    await _run_bulk_onboard(job, connector, sig_store, store, rows, cfgs)
    assert job.finished and job.done == 3
    assert job.ok == 1 and job.failed_total == 2 and len(job.failed) == 2


@pytest.mark.asyncio
async def test_bulk_job_feed_is_bounded_at_scale(connector, tmp_path):
    # #9 — the live feed keeps a fixed rolling window no matter how many hires the CSV holds.
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard, _RECENT_WINDOW
    store = RunbookStore(tmp_path / "ob.json"); store.set_role("Sales", ["Set up POS"])
    rows = [_hire(name=f"H{i}", email=f"h{i}@example.com") for i in range(50)]
    job = OnboardJob(id="t", total=50)
    await _run_bulk_onboard(job, connector, SignatureStore(tmp_path / "sig.json"), store,
                            rows, {"Sales": store.role("Sales")})
    assert job.done == 50 and len(job.recent) == _RECENT_WINDOW


def test_bulk_template_download(client):
    r = client.get("/onboard/bulk/template.csv")
    assert r.status_code == 200 and "role,name,email" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_bulk_preview_summarizes_and_flags_bad_rows(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    csv = ("role,name,email,create_account,notify\n"
           "Sales,Ada,ada@example.com,yes,ada.p@gmail.com\n"
           "Bogus,X,x@example.com,no,\n")
    r = client.post("/onboard/bulk/preview", files={"csv_file": ("hires.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert "1 hire" in r.text and "1</strong> account" in r.text and "emailed by GAM" in r.text
    assert "unknown role" in r.text.lower()   # the Bogus row is skipped and flagged


def test_bulk_run_needs_confirmation(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    r = client.post("/onboard/bulk/run", data={"csv_text": "role,name,email\nSales,Ada,ada@example.com\n"})
    assert "confirm" in r.text.lower()


def test_bulk_run_starts_a_job(client):
    # Only assert the polling panel started; the executor is covered by the direct tests above.
    import re
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    csv = "role,name,email,assignee\nSales,Ada,ada@example.com,it@example.com\n"
    r = client.post("/onboard/bulk/run", data={"csv_text": csv, "confirmed": "1"})
    assert r.status_code == 200
    assert re.search(r"/onboard/bulk/status\?job=[A-Za-z0-9_\-]+", r.text), r.text[:200]


def test_bulk_status_credentials_ttl_no_store_and_done(client):
    # The status GET is bookmarkable/re-fetchable, so the sheet must be no-store, kept only within the
    # TTL (a refresh mustn't lose every password), and dropped by an explicit Done or once the TTL
    # lapses (invariant 4 — no stranded plaintext).
    import time as _t
    from gamgui.web.routes.onboarding import OnboardJob
    st = client.app.state.gamgui
    st.jobs["j1"] = OnboardJob(id="j1", total=1, done=1, ok=1, account_created=1, finished=True,
                               finished_at=_t.monotonic(),
                               credentials=[{"name": "Ada", "email": "ada@example.com",
                                             "password": "SHEETpw-1234-5678", "org_unit": "/Sales"}])
    r1 = client.get("/onboard/bulk/status?job=j1")
    assert "SHEETpw-1234-5678" in r1.text and r1.headers.get("cache-control") == "no-store"
    r2 = client.get("/onboard/bulk/status?job=j1")
    assert "SHEETpw-1234-5678" in r2.text                       # a refresh within the TTL still shows it
    r3 = client.post("/onboard/bulk/done", data={"job": "j1"})
    assert "SHEETpw-1234-5678" not in r3.text and st.jobs["j1"].credentials == []   # Done drops it
    # a job finished longer ago than the TTL: sheet gone and cleared
    st.jobs["j2"] = OnboardJob(id="j2", total=1, finished=True, finished_at=_t.monotonic() - 10_000,
                               credentials=[{"name": "B", "email": "b@x.com", "password": "OLDpw", "org_unit": "/"}])
    r4 = client.get("/onboard/bulk/status?job=j2")
    assert "OLDpw" not in r4.text and st.jobs["j2"].credentials == []


# --- group + calendar pickers (search while editing a role) ---

def test_search_groups_filters_by_email_and_name(client):
    r = client.get("/onboard/search/groups?q=sales")     # mock groups: Sales/Staff/IT
    assert r.status_code == 200
    assert "sales@example.com" in r.text and "staff@example.com" not in r.text
    assert client.get("/onboard/search/groups?q=IT").text.count("it@example.com") >= 1   # name match
    both = client.get("/onboard/search/groups").text     # empty query -> all
    assert "sales@example.com" in both and "it@example.com" in both


def test_search_calendars_needs_a_built_index(client):
    r = client.get("/onboard/search/calendars?q=team")
    assert r.status_code == 200 and "No calendar index yet" in r.text


def test_search_calendars_uses_the_index(client, tmp_path):
    from gamgui.core.calendar_index import CalendarIndex, IndexedCalendar
    idx = CalendarIndex(tmp_path / "cal.db")
    idx.replace_all("example.com", [
        IndexedCalendar("team@group.calendar.google.com", "Team Calendar", "o@example.com", "secondary", 3),
        IndexedCalendar("room@resource.calendar.google.com", "Aspen Room", "", "room", 0),
    ])
    client.app.state.gamgui.calendar_index = idx
    r = client.get("/onboard/search/calendars?q=team")
    assert "Team Calendar" in r.text and "team@group.calendar.google.com" in r.text
    assert "Aspen" not in r.text   # filtered by the query
    # an index built for another tenant is not served
    idx.replace_all("other.com", [IndexedCalendar("x@g.com", "X", "", "secondary", 0)])
    assert "No calendar index yet" in client.get("/onboard/search/calendars?q=x").text


@pytest.mark.asyncio
async def test_provision_hire_skips_signature_for_existing_account(connector, tmp_path):
    # Bulk must match the single flow: the role signature is applied only to an account this run
    # created — never clobbering an existing user's signature (create_account=False).
    from gamgui.web.routes.onboarding import _provision_hire
    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], signature="Classic")
    cfg = store.role("Sales")
    r = await _provision_hire(connector, SignatureStore(tmp_path / "sig.json"), store, cfg,
                              _hire(name="Ada Byte", email="ada@example.com", create_account=False))
    assert r["ok"] and r["account_created"] is False and r["signature"] is None


@pytest.mark.asyncio
async def test_run_keeps_credentials_when_tasklist_fails(client, monkeypatch):
    # After the account is created, a task-list failure must NOT strand the one-time password.
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "KEEPpw-1234-5678")
    async def boom(self, assignee, title, steps):
        raise RuntimeError("insufficientPermissions: Tasks scope not granted")
    monkeypatch.setattr(
        "gamgui.core.connectors.gam_connector.GAMConnector.create_onboarding_runbook", boom)
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte", "email": "ada@example.com",
                                          "assignee": "it@example.com", "create_account": "1", "confirmed": "1"})
    assert r.status_code == 200
    assert "KEEPpw-1234-5678" in r.text                 # temp password still shown
    assert "Couldn't create the task list" in r.text    # failure surfaced, not swallowed


def test_setup_verify_busts_group_cache(client, monkeypatch):
    # A tenant switch (any successful /setup/verify) must invalidate the non-domain-tagged group
    # cache, or the onboarding group picker serves the previous tenant's groups for the TTL window.
    from gamgui.core.setup import VerifyResult
    st = client.app.state.gamgui
    client.get("/onboard/search/groups")                       # prime group_cache from the mock
    assert st.group_cache._items is not None
    async def ok_verify(self, domain, admin):
        return VerifyResult(ok=True, summary="verified")
    monkeypatch.setattr("gamgui.core.setup.SetupService.verify", ok_verify)
    client.post("/setup/verify", data={"domain": "example.com", "admin": "admin@example.com"})
    assert st.group_cache._items is None                       # busted on switch


@pytest.mark.asyncio
async def test_provision_hire_partial_failure_counts_as_failed(connector, tmp_path):
    # A hire whose group add fails is NOT "ok" — the failure reaches res['errors'] and the job feed.
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard, _provision_hire
    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], groups=["missing-group@example.com"])  # 404s in the mock
    sig_store = SignatureStore(tmp_path / "sig.json")
    r = await _provision_hire(connector, sig_store, store, store.role("Sales"), _hire(email="ada@example.com"))
    assert r["ok"] is False and any("groups" in e for e in r["errors"])
    job = OnboardJob(id="t", total=1)
    await _run_bulk_onboard(job, connector, sig_store, store, [_hire(email="ada@example.com")],
                            {"Sales": store.role("Sales")})
    assert job.failed_total == 1 and job.ok == 0


@pytest.mark.asyncio
async def test_failed_tasklist_create_is_audited(connector, tmp_path, monkeypatch):
    from gamgui.core.gam.errors import GAMError, GAMErrorKind
    orig = connector.runner.run_authenticated
    async def fail_tasklist(domain, argv, **kw):
        if "tasklist" in argv:
            raise GAMError(kind=GAMErrorKind.SCOPE_MISSING, exit_code=1, stderr="insufficient scope")
        return await orig(domain, argv, **kw)
    monkeypatch.setattr(connector.runner, "run_authenticated", fail_tasklist)
    with pytest.raises(GAMError):
        await connector.create_onboarding_runbook("it@example.com", "Onboard Ada", ["Step"])
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"action": "onboard_runbook"' in audit and '"ok": false' in audit   # attempt recorded


def test_split_name_single_word_has_no_fabricated_surname():
    from gamgui.web.routes.onboarding import _split_name
    assert _split_name("Ada", "", "") == ("Ada", "")
    assert _split_name("Ada Byte", "", "") == ("Ada", "Byte")


def test_run_single_word_name_requires_last(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada", "email": "ada@example.com",
                                          "assignee": "it@example.com", "create_account": "1", "confirmed": "1"})
    assert "first and last name" in r.text.lower()   # single-word name no longer becomes "Ada Ada"


def test_bulk_status_ignores_foreign_job(client):
    from gamgui.web.jobs import BatchJob
    client.app.state.gamgui.jobs["foreign"] = BatchJob(id="foreign", total=3, finished=True)
    r = client.get("/onboard/bulk/status?job=foreign")
    assert r.status_code == 200 and "no longer available" in r.text   # not a 500


def test_gam_error_scrubs_password_from_stderr_and_argv():
    # GAM echoes the command line (incl. the password) on a usage error — the exception must not carry it.
    from gamgui.core.gam.errors import GAMError
    exc = GAMError.from_run(
        2, "ERROR: usage: Command: gam create user @bad.com password SEKRETpw-9999 firstname A",
        ["create", "user", "@bad.com", "password", "SEKRETpw-9999"])
    for surface in (str(exc), exc.stderr or "", " ".join(exc.argv or [])):
        assert "SEKRETpw-9999" not in surface


def test_parse_hire_csv_flags_duplicate_emails():
    rows, errors = onboarding.parse_hire_csv("role,email\nSales,a@x.com\nSales,A@X.com\nSales,b@x.com\n")
    assert [r["email"] for r in rows] == ["a@x.com", "b@x.com"]           # case-insensitive dup dropped
    assert any("duplicate email" in e and "Row 3" in e for e in errors)


def test_run_reports_actual_task_count(client):
    # The mock now 404s create-task on a wrong tasklist id, so "Created N of N" proves real creation.
    client.post("/onboard/role", data={"name": "Cashier", "steps": "A\nB\nC"})
    r = client.post("/onboard/run", data={"role": "Cashier", "name": "Jo",
                                          "email": "jo@example.com", "assignee": "it@example.com", "confirmed": "1"})
    assert "<strong>3</strong> of 3 tasks" in r.text


def test_run_welcome_email_sent_and_failed(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    ok = client.post("/onboard/run", data={"role": "Sales", "name": "Jo", "email": "jo@example.com",
                                           "assignee": "it@example.com", "send_welcome": "1", "confirmed": "1"})
    assert "sent" in ok.text and "✓" in ok.text                     # sent ✓
    bad = client.post("/onboard/run", data={"role": "Sales", "name": "Jo", "email": "jo-SENDFAIL@example.com",
                                            "assignee": "it@example.com", "send_welcome": "1", "confirmed": "1"})
    assert "failed to send" in bad.text


def test_run_create_account_with_groups_calendars_and_signature(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "signature": "Classic",
                                        "org_unit": "/Sales", "groups": "sales@example.com\nstaff@example.com",
                                        "calendars": "team@group.calendar.google.com"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte", "email": "ada@example.com",
                                          "assignee": "it@example.com", "create_account": "1", "confirmed": "1"})
    assert "Account created" in r.text and "Classic applied" in r.text
    assert "2 of 2 group" in r.text and "1 of 1 shared calendar" in r.text and "of 1 task" in r.text


@pytest.mark.asyncio
async def test_provision_hire_notify_never_audits_password(connector, tmp_path, monkeypatch):
    from gamgui.web.routes.onboarding import _provision_hire
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "NOTIFYpw-1234-5678")
    store = RunbookStore(tmp_path / "ob.json"); store.set_role("Sales", ["Set up POS"])
    r = await _provision_hire(connector, SignatureStore(tmp_path / "sig.json"), store, store.role("Sales"),
                              _hire(name="Ada Byte", email="ada@example.com", create_account=True,
                                    first="Ada", last="Byte", notify="ada.personal@gmail.com"))
    assert r["notified"] is True and r["credential"] is None
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "NOTIFYpw-1234-5678" not in audit and "create_user" in audit   # notifypassword redacted too


def test_bulk_status_does_not_drain_credentials_before_finish(client):
    from gamgui.web.routes.onboarding import OnboardJob
    st = client.app.state.gamgui
    st.jobs["run"] = OnboardJob(id="run", total=2, done=1, ok=1, account_created=1, finished=False,
                                credentials=[{"name": "Ada", "email": "ada@example.com",
                                              "password": "PENDINGpw-1", "org_unit": "/Sales"}])
    r = client.get("/onboard/bulk/status?job=run")
    assert r.status_code == 200 and "PENDINGpw-1" not in r.text          # not served while running
    assert st.jobs["run"].credentials != []                             # not drained early


def test_bulk_preview_accepts_excel_utf8_bom(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    csv = "role,name,email\nSales,Ada,ada@example.com\n".encode("utf-8-sig")   # Excel "CSV UTF-8" BOM
    r = client.post("/onboard/bulk/preview", files={"csv_file": ("hires.csv", csv, "text/csv")})
    assert r.status_code == 200 and "1 hire" in r.text                   # BOM didn't break the header


@pytest.mark.asyncio
async def test_bulk_job_failed_sample_is_capped(tmp_path):
    from gamgui.web.routes.onboarding import OnboardJob, _FAILED_SAMPLE_CAP
    job = OnboardJob(id="t", total=_FAILED_SAMPLE_CAP + 50)
    for n in range(_FAILED_SAMPLE_CAP + 50):
        job.record({"email": "h{}@x.com".format(n), "ok": False, "errors": ["boom"]})
    assert job.failed_total == _FAILED_SAMPLE_CAP + 50 and len(job.failed) == _FAILED_SAMPLE_CAP  # #9


@pytest.mark.asyncio
async def test_bulk_recent_feed_holds_no_plaintext_password(connector, tmp_path, monkeypatch):
    import json
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "RECENTpw-9999")
    store = RunbookStore(tmp_path / "ob.json"); store.set_role("Sales", ["Set up POS"])
    job = OnboardJob(id="t", total=1)
    await _run_bulk_onboard(job, connector, SignatureStore(tmp_path / "sig.json"), store,
                            [_hire(name="Ada Byte", email="ada@example.com", first="Ada", last="Byte",
                                   create_account=True)], {"Sales": store.role("Sales")})
    assert "RECENTpw-9999" not in json.dumps(job.recent)                    # feed never retains plaintext
    assert any(c["password"] == "RECENTpw-9999" for c in job.credentials)   # only the sheet holds it


def test_parse_hire_csv_rejects_invalid_email_targets():
    rows, errors = onboarding.parse_hire_csv(
        "role,email\nSales,oauthuser\nSales,@example.com\nSales,ok@example.com\n")
    assert [r["email"] for r in rows] == ["ok@example.com"]                 # the two traps dropped
    assert sum("not a valid email" in e for e in errors) == 2


def test_parse_hire_csv_refuses_an_unreadable_file_instead_of_raising():
    # B1: a cell over csv's 131,072-char field limit raised csv.Error out of the row loop (a 500).
    text = "role,email,name\nSales,a@x.com,ok\nSales,b@x.com," + "x" * 140000 + "\n"
    rows, errors = onboarding.parse_hire_csv(text)
    assert rows == [] and len(errors) == 1                      # refused whole, never half-imported
    assert errors[0].startswith("Row 3: couldn't read the CSV") and "Nothing was imported" in errors[0]


def test_bulk_preview_unreadable_or_oversized_csv_is_a_friendly_error(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    long_cell = "role,email,name\nSales,a@x.com," + "x" * 140000 + "\n"
    r = client.post("/onboard/bulk/preview", files={"csv_file": ("hires.csv", long_cell, "text/csv")})
    assert r.status_code == 200 and "couldn&#39;t read the CSV" in r.text and "Run onboarding" not in r.text
    big = "role,email\n" + "Sales,a@x.com\n" * 80000                # ~1.1 MB of otherwise-valid rows
    r = client.post("/onboard/bulk/preview", files={"csv_file": ("hires.csv", big, "text/csv")})
    assert r.status_code == 200 and "over 1 MB" in r.text and "Run onboarding" not in r.text


def test_run_rejects_invalid_email(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "groups": "sales@example.com"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "X", "email": "oauthuser",
                                          "assignee": "it@example.com"})
    assert "valid email" in r.text.lower()   # rejected before any group-add targets the admin


def test_preview_rejects_invalid_email(client):
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS"})
    r = client.post("/onboard/preview", data={"role": "Sales", "name": "X", "email": "oauthuser"})
    assert "valid email" in r.text.lower()   # caught at preview, before Run
