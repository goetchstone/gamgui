"""Switching domains from the header (plan F6): the domain menu, the switch it posts, and what a switch does to what is
running and held.

The menu shows only with another domain in the Keychain, names itself "Domain, <domain>", and switches through Setup's
own route and check (`check serviceaccount`, through the mock's real handler here): only a pass switches. A switch to
another domain while a run is going says first what it stops, then stops each run after its current step, as Stop does,
and the records written during and after it name the domain each was made on. Templates and roles stay shared.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from html import unescape
from html.parser import HTMLParser

import pytest
from keyring.errors import KeyringLocked

from gamgui.core.secrets.vault import SecretsVault, _KeyringBackend
from gamgui.web.jobs import ONE_STEP, BatchJob, register_job, retry_of

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture (example.com)

OTHER = "example.org"
BOSS = f"boss@{OTHER}"


def _add_other(st, admin: str = BOSS) -> None:
    oauth2 = json.dumps({"token": "tok", "decoded_id_token": {"email": admin}})
    st.vault.set_all(OTHER, {"client_secrets": "{}", "oauth2": oauth2, "oauth2service": json.dumps({"client_id": "x"})})


def _head(client, path: str = "/users") -> str:  # noqa: F811
    return client.get(path).text.split("</header>")[0]


def _menu_switch(client, tenant: str = OTHER, back: str = "/users", **extra):  # noqa: F811
    return client.post("/setup/switch", data={"tenant": tenant, "where": "menu", "back": back, **extra})


class _Text(HTMLParser):
    """The text of some markup, leaving out what is aria-hidden (a button's name from its content)."""

    def __init__(self, hidden_ok: bool) -> None:
        super().__init__()
        self.hidden_ok, self.depth, self.hidden_at, self.parts = hidden_ok, 0, None, []

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        if self.hidden_at is None and not self.hidden_ok and ("aria-hidden", "true") in attrs:
            self.hidden_at = self.depth

    def handle_endtag(self, tag):
        if self.hidden_at == self.depth:
            self.hidden_at = None
        self.depth -= 1

    def handle_data(self, data):
        if self.hidden_at is None:
            self.parts.append(data)


def _name_of(markup: str, hidden_ok: bool = False) -> str:
    parser = _Text(hidden_ok)
    parser.feed(markup)
    return " ".join("".join(parser.parts).split())


def _records(st) -> list:
    return [json.loads(line) for line in st.connector.audit.path.read_text().splitlines() if line.strip()]


# --- the menu --------------------------------------------------------------------------------------------------------

def test_one_domain_keeps_the_plain_tenant_link(client):  # noqa: F811
    head = _head(client)
    assert 'id="domain-toggle"' not in head and '<a id="tenant" href="/setup"' in head


def test_a_second_domain_makes_the_header_a_domain_menu_named_for_the_active_one(client):  # noqa: F811
    _add_other(client.app.state.gamgui)
    head = _head(client, "/users/detail?email=alice%40example.com")
    toggle = head.split('id="domain-toggle"')[1].split("</button>")[0]
    assert 'type="button"' in toggle and 'aria-expanded="false"' in toggle and 'aria-controls="domain-panel"' in toggle
    assert _name_of(toggle.split(">", 1)[1]) == "Domain, example.com"       # what a screen reader says it is
    described = re.search(r'id="tenant-admin"[^>]*>(.*?)</span>\s*</span>', toggle, flags=re.S).group(1)
    assert 'aria-describedby="tenant-admin"' in toggle                       # and, after it, who acts
    assert _name_of(described, hidden_ok=True) == "Signed in as admin@example.com"
    panel = head.split('id="domain-panel"')[1]
    assert " hidden " in panel.split(">")[0] + " " and 'role="region"' in panel.split(">")[0]
    buttons = re.findall(r'<button [^>]*hx-post="/setup/switch"[^>]*>([^<]*)</button>', panel)
    assert buttons == [OTHER]                                       # the others only: the active one isn't a switch
    # htmx 1.9.12 shares one counter between hx-disabled-elt="this" and the request class: a button its answer doesn't
    # replace kept .htmx-request (the a11y run waited on it forever). hx-sync drops a second click instead.
    assert "hx-disabled-elt" not in panel.split('id="domain-result"')[0] and 'hx-sync="#domain-panel:drop"' in panel
    vals = json.loads(re.search(r"hx-vals='([^']*)'", panel).group(1))
    assert vals == {"tenant": OTHER, "where": "menu", "back": "/users"}   # back to the screen's top, not alice's page


def test_a_spelling_from_before_lowercasing_is_not_another_domain(client):  # noqa: F811
    st = client.app.state.gamgui
    st.vault.set_all("Example.com", st.vault.get_all("example.com"))
    assert 'id="domain-toggle"' not in _head(client)
    _add_other(st)
    st.vault.set_all("Example.org", st.vault.get_all(OTHER))
    panel = _head(client).split('id="domain-panel"')[1]
    assert re.findall(r'hx-post="/setup/switch"[^>]*>([^<]*)</button>', panel) == [OTHER]


def test_a_keychain_that_wont_list_its_domains_leaves_the_link(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)

    def locked():
        raise KeyringLocked("Can't get password from keychain")
    monkeypatch.setattr(st.vault, "list_domains", locked)
    r = client.get("/users")
    assert r.status_code == 200 and '<a id="tenant" href="/setup"' in r.text and "example.com" in r.text


# --- the switch it posts ---------------------------------------------------------------------------------------------

def test_a_menu_switch_runs_setups_check_and_reloads_the_screen_for_the_new_domain(client, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    st.user_cache._items = ["example.com's people"]
    r = _menu_switch(client, back="/users")
    assert r.headers.get("HX-Redirect") == "/users" and r.text == ""
    assert st.connector.domain == OTHER and st.audit_domain == OTHER and st.user_cache._items is None
    assert ["user", BOSS, "check", "serviceaccount"] == gam_calls()[-1][:4]   # Setup's own check, as its own admin
    assert gam_writes(gam_calls()) == []


@pytest.mark.parametrize("back, lands", [("/reports", "/reports"), ("/setup", "/setup"), ("/", "/"), ("/jobs", "/"),
                                         ("//evil.example", "/"), ("https://evil.example/", "/"), ("/users/detail", "/")])
def test_a_pass_reloads_only_one_of_the_headers_screens(client, back, lands):  # noqa: F811
    _add_other(client.app.state.gamgui)
    assert _menu_switch(client, back=back).headers.get("HX-Redirect") == lands


def test_a_failed_check_keeps_the_active_domain_and_says_why_in_the_menu(client):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st, admin=f"partialdwd@{OTHER}")                 # the mock's tenant missing two scopes
    r = _menu_switch(client)
    page = unescape(r.text)
    assert "HX-Redirect" not in r.headers and st.connector.domain == "example.com"
    assert f"{OTHER} didn't pass the access check, so example.com stays active." in page
    assert "Authorize Domain-Wide Delegation" in page and "data-focus" in page


def test_a_domain_whose_token_names_no_admin_is_sent_to_setup(client):  # noqa: F811
    st = client.app.state.gamgui
    st.vault.set_all(OTHER, {"oauth2": "tok", "oauth2service": json.dumps({"client_id": "x"})})
    r = _menu_switch(client)
    assert "switch to it on Setup" in unescape(r.text) and st.connector.domain == "example.com"


def test_a_domain_not_in_the_keychain_is_refused(client):  # noqa: F811
    r = _menu_switch(client, tenant="example.net")
    assert "No credentials" in r.text and client.app.state.gamgui.connector.domain == "example.com"


# --- the Keychain asks, as Setup's verify does; a refusal is an answer -----------------------------------------------

@pytest.mark.parametrize("where", ["menu", ""])
def test_a_keychain_that_refuses_the_switch_keeps_the_domain_and_says_so(client, monkeypatch, where):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    real = st.vault.get

    def locked(domain, name):
        if domain == OTHER and name == "oauth2service":       # the prompt for the switch's own read, cancelled
            raise KeyringLocked("Can't get password from keychain: (-128, 'Keychain Access Denied')")
        return real(domain, name)
    monkeypatch.setattr(st.vault, "get", locked)
    r = client.post("/setup/switch", data={"tenant": OTHER, "where": where, "back": "/users"})
    page = unescape(r.text)
    assert r.status_code == 200 and "HX-Redirect" not in r.headers and st.connector.domain == "example.com"
    assert f"The Keychain didn't hand over {OTHER}'s credentials" in page and "Nothing changed" in page
    assert "-128" not in page and "Fix the delegation" not in page


@pytest.mark.skipif(sys.platform != "darwin", reason="keyring's macOS backend loads only on macOS")
@pytest.mark.parametrize("status", [-128, -25308, -25293])   # Deny clicked; locked with no UI; auth failed
def test_the_real_macos_backend_refuses_with_what_the_switch_catches(client, monkeypatch, status):  # noqa: F811
    """The stand-in above raises what keyring's macOS backend raises when the Keychain's prompt is refused: drive the
    real backend over a refused OS call (no Keychain touched) and the switch answers in words, not a 500."""
    from keyring.backends import macOS

    st = client.app.state.gamgui
    _add_other(st)
    monkeypatch.setattr(macOS.api, "SecItemCopyMatching", lambda query, data: status)   # the OS call: no Keychain
    backend = _KeyringBackend()
    backend._keyring = macOS.Keyring()
    st.vault = SecretsVault(backend=backend, cache_ttl=0)
    r = _menu_switch(client)
    assert r.status_code == 200 and f"The Keychain didn't hand over {OTHER}'s credentials" in unescape(r.text)
    assert st.connector.domain == "example.com"


# --- a run in progress -----------------------------------------------------------------------------------------------

def _running(st, title: str, kind: str = "signatures") -> BatchJob:
    return register_job(st.jobs, BatchJob(total=3, kind=kind, title=title))


def test_a_switch_while_a_run_goes_says_first_what_it_stops_and_checks_nothing(client, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    run = _running(st, "Signature for the whole company — 3 users")
    read = _running(st, "Reading connected apps", kind="connected_apps")
    register_job(st.jobs, BatchJob(total=1, kind=ONE_STEP, title="Set a signature: alice@example.com"))
    before = len(gam_calls())
    page = unescape(_menu_switch(client).text)
    assert len(gam_calls()) == before and st.connector.domain == "example.com" and not run.cancel_requested
    assert f"Switching to {OTHER} stops what's running on example.com after its current step:" in page
    assert "Signature for the whole company — 3 users</li>" in page
    assert f"Reading connected apps — one read, which can't stop: it finishes, and nothing it read shows on {OTHER}" in page
    assert "Set a signature" not in page                          # a one-step change finishes: it isn't a run
    vals = json.loads(re.search(r"hx-vals='([^']*)'", page).group(1))
    assert vals == {"tenant": OTHER, "where": "menu", "back": "/users", "confirmed": f"{run.id},{read.id}"}
    assert 'data-clear="domain-result"' in page and "Switch anyway" in page

    assert "Switch anyway" in _menu_switch(client, confirmed="1").text   # a bare "yes" names no run: warned again
    r = _menu_switch(client, confirmed=vals["confirmed"])
    assert r.headers.get("HX-Redirect") == "/users" and st.connector.domain == OTHER
    assert run.cancel_requested and read.cancel_requested and run.stop_why == f"Stopped: you switched to {OTHER}"


def test_a_run_started_after_the_warning_is_warned_about_before_it_is_stopped(client):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    first = _running(st, "Signature for the whole company — 3 users")
    warned = _confirmed(_menu_switch(client).text)
    later = _running(st, "Offboarding carol@example.com", kind="offboard")   # from another window, meanwhile
    page = unescape(_menu_switch(client, confirmed=warned).text)
    assert st.connector.domain == "example.com" and not first.cancel_requested and not later.cancel_requested
    assert "Offboarding carol@example.com — stopped part-way, it is left half done" in page
    assert _menu_switch(client, confirmed=_confirmed(page)).headers.get("HX-Redirect") and later.cancel_requested


def _confirmed(page: str) -> str:
    return json.loads(re.search(r"hx-vals='([^']*)'", unescape(page)).group(1))["confirmed"]


def test_setups_switcher_warns_the_same_way(client):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    _running(st, "Offboarding carol@example.com", kind="offboard")
    page = unescape(client.post("/setup/switch", data={"tenant": OTHER}).text)
    assert "stops what's running on example.com" in page and 'hx-target="#tenant-result"' in page
    assert "hx-include=\"[name='admin']\"" in page and st.connector.domain == "example.com"
    assert "Offboarding carol@example.com — stopped part-way, it is left half done" in page


def test_checking_the_active_domain_again_stops_nothing(client):  # noqa: F811
    st = client.app.state.gamgui
    run = _running(st, "Signature for the whole company — 3 users")
    r = client.post("/setup/switch", data={"tenant": "example.com"})
    assert "All required scopes authorized." in r.text and not run.cancel_requested


def test_a_switch_drops_every_runs_retry_and_the_report_export_waiting(client, tmp_path):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    done = _running(st, "Department for 3 users", kind="department")
    done.retry = "Sales"
    done.record("bob@example.com", False, "failed")
    done.finish()
    assert retry_of(st.jobs, done.id, "department")[0] is done
    export = tmp_path / "gamgui-report-x.zip"
    export.write_bytes(b"example.com's report")
    st.incident_export = ("tok", str(export), "INC-1")
    assert _menu_switch(client).headers.get("HX-Redirect")
    job, why = retry_of(st.jobs, done.id, "department")              # its Retry would preview example.com's people here
    assert job is None and "before the switch" in why and not done.can_retry
    assert done.retry == "Sales"                                      # kept: a panel names its target with it
    assert st.incident_export is None and not export.exists()


def test_a_running_panel_still_names_its_target_after_a_switch(client):  # noqa: F811
    # Review: clearing retry on running jobs drew "Removing from None", and its finished panel then read group "None".
    st = client.app.state.gamgui
    _add_other(st)
    run = _running(st, "Removing 3 from staff@example.com", kind="group_remove")
    run.retry = "staff@example.com"
    assert _menu_switch(client, confirmed=run.id).headers.get("HX-Redirect")
    run.finish()
    panel = client.get(f"/groups/members/remove-many/status?job={run.id}").text
    assert "staff@example.com" in panel and "None" not in panel


@pytest.mark.parametrize("where", ["menu", ""])
def test_a_check_that_cant_run_keeps_the_domain_and_says_so(client, monkeypatch, where):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)

    async def no_room(*args, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(st.runner, "run_authenticated", no_room)
    r = client.post("/setup/switch", data={"tenant": OTHER, "where": where, "back": "/users"})
    page = unescape(r.text)
    assert r.status_code == 200 and "HX-Redirect" not in r.headers and st.connector.domain == "example.com"
    assert f"{OTHER}'s access check couldn't run, so nothing changed." in page and "No space" not in page


def test_templates_and_roles_stay_shared_across_domains(client):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    st.runbooks.set_role("Sales Associate", ["Order a laptop"])
    st.sig_templates.save("Classic", "<b>{name}</b>")
    runbooks, templates, catalog = st.runbooks, st.sig_templates, st.catalog
    _menu_switch(client)
    assert st.connector.domain == OTHER
    assert st.runbooks is runbooks and "Sales Associate" in st.runbooks.role_names()
    assert st.sig_templates is templates and "Classic" in st.sig_templates.names()
    assert st.catalog is catalog


# --- records during and after a switch name the right domain ---------------------------------------------------------

def test_records_written_during_and_after_a_switch_name_the_domain_each_ran_on(client, gam_calls):  # noqa: F811
    """A signature run on example.com has its first write in flight when the menu switches to example.org. That write
    finishes and is recorded under example.com; the run stops before the next person; a read after the switch is
    recorded under example.org. Nothing of the run reaches example.org."""
    st = client.app.state.gamgui
    _add_other(st)
    first = st.connector
    gate, held = asyncio.Event(), []
    real = st.runner.run_authenticated

    async def write_waits(domain, argv, **kw):
        if argv[2:3] == ["signature"] and not held:     # `user <email> signature <body> html`: the write
            held.append(domain)
            await gate.wait()                     # the write is in flight while the switch lands
        return await real(domain, argv, **kw)
    st.runner.run_authenticated = write_waits

    body = {"template": "<b>{name}</b>", "scope_type": "company", "scope_value": ""}
    token = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', client.post("/signatures/preview", data=body).text).group(1)
    client.post("/signatures/apply", data={**body, "confirmed": "1", "preview": token})
    job = next(j for j in st.jobs.values() if j.kind == "signatures")
    for _ in range(200):
        if held:
            break
        client.portal.call(asyncio.sleep, 0.01)
    assert held == ["example.com"] and job.total > 1

    warned = _confirmed(_menu_switch(client, back="/signatures").text)
    assert warned == job.id
    assert _menu_switch(client, back="/signatures", confirmed=warned).headers.get("HX-Redirect") == "/signatures"
    assert st.connector.domain == OTHER and st.connector is not first
    client.portal.call(gate.set)
    wait_for_job(client, job)

    assert job.error == f"Stopped: you switched to {OTHER} — {job.total - 1} not attempted."
    assert len(gam_writes(gam_calls())) == 1                         # the write in hand, and none after it
    client.post("/builder/run", data={"cid": "build.print_delegates", "email": f"alice@{OTHER}"})
    records = _records(st)
    writes = [r for r in records if r["action"] == "set_signature"]
    reads = [r for r in records if r["action"] in ("read", "sensitive_read")]
    assert len(writes) == 1 and writes[0]["extra"]["domain"] == "example.com"
    assert reads and all(r["extra"]["domain"] == OTHER for r in reads)
    assert all(r["extra"].get("domain") for r in records)               # nothing unnamed


# --- review of #173: two checks at once, a run started during the check, records across an await --------------------

def _switch_now(st) -> None:
    """A switch to example.org landing inside a request's await (on the app's own loop, where no request can be made):
    what the menu's passing switch does to the state."""
    from gamgui.core.connectors.gam_connector import GAMConnector

    st.activate(GAMConnector(runner=st.runner, domain=OTHER, audit=st.connector.audit))

def _hold_the_check(client, st):  # noqa: F811
    """Hold the first `check serviceaccount` in flight until released, the way a 5-15 s check runs: (reached, gate)."""
    reached, gate, held = threading.Event(), client.portal.call(_event), []
    real = st.runner.run_authenticated

    async def waits(domain, argv, **kw):
        if "serviceaccount" in argv and not held:
            held.append(domain)
            reached.set()
            await gate.wait()
        return await real(domain, argv, **kw)
    st.runner.run_authenticated = waits
    return reached, gate


async def _event() -> asyncio.Event:
    return asyncio.Event()


def _in_thread(fn):
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_a_second_switch_is_refused_while_a_check_runs(client):  # noqa: F811
    """Two switches in flight once ended on whichever check finished last, after the menu had already sent the window to
    the other domain. One check at a time: the second is refused in words and changes nothing."""
    st = client.app.state.gamgui
    _add_other(st)
    third = "example.net"
    st.vault.set_all(third, {"client_secrets": "{}", "oauth2": json.dumps({"decoded_id_token": {"email": f"boss@{third}"}}),
                             "oauth2service": json.dumps({"client_id": "x"})})
    reached, gate = _hold_the_check(client, st)
    t, out = _in_thread(lambda: _menu_switch(client))
    try:
        assert reached.wait(10)
        answers = [_menu_switch(client, tenant=third), client.post("/setup/switch", data={"tenant": third}),
                   client.post("/setup/verify", data={"domain": third, "admin": f"boss@{third}"})]
        meanwhile = st.connector.domain
    finally:                                  # the held check always ends: a failed assert mustn't hang the client
        client.portal.call(gate.set)
        t.join(10)
    for again in answers:
        assert f"{OTHER}'s access is being checked. Wait for its answer" in unescape(again.text)
        assert "HX-Redirect" not in again.headers
    assert meanwhile == "example.com"
    assert out["r"].headers.get("HX-Redirect") == "/users" and st.connector.domain == OTHER and st.switching == ""
    assert _menu_switch(client, tenant=third).headers.get("HX-Redirect")       # free again once it answered


def test_switch_anyway_drops_a_second_click_in_the_menu():
    from gamgui.web.server import TEMPLATES
    from gamgui.web.routes.setup import _Warn

    warn = _Warn("/setup/switch", {"tenant": OTHER, "where": "menu", "back": "/"}, zone="domain-result", sync="#domain-panel")
    html = TEMPLATES.env.get_template("_switch_warning.html").render(
        tenant=OTHER, active="example.com", running=[], unstoppable=(), warn=warn, sync=warn.sync,
        vals={**warn.vals, "confirmed": ""})
    assert 'hx-sync="#domain-panel:drop"' in html                      # the domain buttons' own: one request at a time


def test_a_run_started_during_the_check_is_warned_about_not_stopped(client):  # noqa: F811
    """Nothing ran when the domain was clicked; an offboarding was started while its check ran. The pass once stopped it
    after its first step, half done and never named. Now the pass answers with the warning and switches nothing."""
    st = client.app.state.gamgui
    _add_other(st)
    reached, gate = _hold_the_check(client, st)
    t, out = _in_thread(lambda: _menu_switch(client))
    try:
        assert reached.wait(10)
        late = register_job(st.jobs, BatchJob(total=10, kind="offboard", title="Offboarding carol@example.com"))
    finally:
        client.portal.call(gate.set)
        t.join(10)
    page = unescape(out["r"].text)
    assert "HX-Redirect" not in out["r"].headers and st.connector.domain == "example.com" and not late.cancel_requested
    assert "Offboarding carol@example.com — stopped part-way, it is left half done" in page
    assert _confirmed(page) == late.id
    assert _menu_switch(client, confirmed=late.id).headers.get("HX-Redirect") and late.cancel_requested


def test_an_imports_verify_that_would_stop_a_run_warns_first(client):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    run = _running(st, "Signature for the whole company — 3 users")
    page = unescape(client.post("/setup/verify", data={"domain": OTHER, "admin": BOSS}).text)
    assert st.connector.domain == "example.com" and not run.cancel_requested and 'hx-post="/setup/verify"' in page
    assert 'hx-target="#wizard-next"' in page and _confirmed(page) == run.id
    r = client.post("/setup/verify", data={"domain": OTHER, "admin": BOSS, "confirmed": run.id})
    assert "All required scopes authorized." in r.text and st.connector.domain == OTHER and run.cancel_requested


def test_a_sensitive_export_a_switch_lands_during_is_recorded_under_the_rows_domain(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    st.builder_last_result = {"id": "r1", "records": [{"User": "alice@example.com", "verificationCodes": "11112222"}],
                              "sensitive": {"command": "print_backupcodes", "argv": ["user", "alice@example.com",
                                                                                     "print", "backupcodes"],
                                            "target": "alice@example.com"}}
    real = st.tenant_domains

    async def read_then_switch():
        domains = await real()
        _switch_now(st)                                     # lands while External-only reads the domains
        return domains
    monkeypatch.setattr(st, "tenant_domains", read_then_switch)
    client.get("/builder/export.csv?external=1")
    exports = [r for r in _records(st) if r["action"] == "sensitive_csv_export"]
    assert [(r["target"], r["extra"]["domain"]) for r in exports] == [("alice@example.com", "example.com")]


def test_a_department_run_a_switch_lands_before_starts_nothing(client, monkeypatch, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    _add_other(st)
    form = {"store": "Marketing", "group": "", "emails": "alice@example.com"}
    token = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', client.post("/users/bulk/preview", data=form).text).group(1)
    real = st.users

    async def read_then_switch(**kw):
        users = await real(**kw)                       # read on example.com, as previewed ...
        monkeypatch.setattr(st, "users", real)
        _switch_now(st)                                # ... and nothing was running to warn about
        return users
    monkeypatch.setattr(st, "users", read_then_switch)
    before = len(gam_writes(gam_calls()))
    r = client.post("/users/bulk/apply", data={**form, "confirmed": "1", "preview": token})
    assert "The connection changed, so nothing ran." in r.text
    assert not [j for j in st.jobs.values() if j.kind == "department"] and len(gam_writes(gam_calls())) == before


def test_an_offboarding_a_switch_lands_before_starts_nothing(client, monkeypatch, gam_calls):  # noqa: F811
    from gamgui.web.routes import lifecycle as lifecycle_route

    st = client.app.state.gamgui
    _add_other(st)
    body = {"user": "carol@example.com", "manager": "alice@example.com", "subject": "s", "message": "m",
            "days": "30", "notify": ""}
    token = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', client.post("/lifecycle/offboard/preview", data=body).text).group(1)
    real = lifecycle_route._check

    async def check_then_switch(*args, **kw):
        found = await real(*args, **kw)
        _switch_now(st)
        return found
    monkeypatch.setattr(lifecycle_route, "_check", check_then_switch)
    before = len(gam_writes(gam_calls()))
    r = client.post("/lifecycle/offboard/run", data={**body, "confirmed": "1", "preview": token})
    assert "The connection changed, so nothing ran." in r.text
    assert not [j for j in st.jobs.values() if j.kind == "offboard"] and len(gam_writes(gam_calls())) == before


