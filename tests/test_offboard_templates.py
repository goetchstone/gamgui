"""Saved offboarding routines (back-port plan C5): a template fills the form and nothing else, the lock can't be left out, and a
file that can't be read is moved aside, never saved over. The cases are SwiftGamGui's. Local files only: no GAM call."""

from __future__ import annotations

import json
import stat
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.lifecycle import DEFAULT_MESSAGE, OPTIONAL_STEPS, ROUTINE
from gamgui.core.offboard_templates import STARTER_DEFAULT, OffboardTemplate, OffboardTemplateStore
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.mark.parametrize("template,says", [
    (OffboardTemplate(leave_out=frozenset({"password"})), "always runs"),
    (OffboardTemplate(leave_out=frozenset({"revoke"})), "always runs"),
    (OffboardTemplate(leave_out=frozenset({"forward"})), "always runs"),
    (OffboardTemplate(leave_out=frozenset({"imap_off"})), "runs only when added"),
    (OffboardTemplate(add=frozenset({"delegate"})), "isn't added"),
    (OffboardTemplate(add=frozenset({"nonsense"})), "isn't an offboarding step"),
    (OffboardTemplate(days=0), "between"),
    (OffboardTemplate(days=36_501), "between"),
    (OffboardTemplate(invitee="not an address"), "isn't one email address"),
])
def test_a_template_that_couldn_t_run_says_why(template, says):
    assert says in template.problem


def test_the_steps_it_runs_in_order():
    assert OffboardTemplate().runs[0] == "Reset password" and len(OffboardTemplate().runs) == len(ROUTINE)
    lock = OffboardTemplate(leave_out=frozenset(OPTIONAL_STEPS))
    assert lock.runs == ["Reset password", "Revoke access & sign out", "Turn off forwarding"]
    added = OffboardTemplate(add=frozenset({"clear_recovery"}))
    assert added.runs[:2] == ["Reset password", "Clear recovery email & phone"]


def test_the_starters_show_on_first_use_and_can_all_run(tmp_path):
    store = OffboardTemplateStore(tmp_path / "t.json")
    assert store.default == STARTER_DEFAULT and set(store.names()) == {"All eight steps", "Hand-over", "Lock only"}
    assert all(not store.get(n).problem for n in store.names())
    assert not (tmp_path / "t.json").exists()     # nothing written until the operator saves


def test_a_save_round_trips_and_is_private(tmp_path):
    path = tmp_path / "app" / "t.json"
    store = OffboardTemplateStore(path)
    template = OffboardTemplate(leave_out=frozenset({"calacls"}), add=frozenset({"imap_off"}), subject="S", message="M", days=7, invitee="it@example.com")
    store.save("Mine", template)
    store.set_default("Mine")
    again = OffboardTemplateStore(path)
    assert again.get("Mine") == template and again.default == "Mine" and again.opening == template
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["version"] == 2
    assert [p.name for p in path.parent.iterdir()] == ["t.json"]     # no temp file left


def test_version_1_reads_as_adding_nothing(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": 1, "default": "Old", "templates": {"Old": {"leave_out": ["calacls"], "subject": "S", "message": "M", "days": 3, "invitee": ""}}}))
    store = OffboardTemplateStore(path)
    assert store.get("Old").add == frozenset() and store.set_aside is None and path.exists()


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 99, "templates": {}}', '{"version": 2, "templates": {}}',
                                     '{"version": 2, "default": "x", "templates": {"x": {"leave_out": ["password"]}}}',
                                     '{"version": 2, "default": "x", "templates": {"x": {"days": "soon"}}}',
                                     '{"version": 2, "default": "x", "templates": {"x": "text"}}'])
def test_a_file_it_can_t_read_is_moved_aside_and_never_saved_over(tmp_path, content):
    path = tmp_path / "t.json"
    path.write_text(content)
    store = OffboardTemplateStore(path)
    assert store.default == STARTER_DEFAULT and store.set_aside is not None and store.set_aside.read_text() == content
    assert not path.exists()
    store.save("New", OffboardTemplate())
    assert store.set_aside.read_text() == content      # the operator's old file is still whole


def test_a_save_refuses_what_couldn_t_run_and_a_blank_name(tmp_path):
    store = OffboardTemplateStore(tmp_path / "t.json")
    with pytest.raises(ValueError, match="always runs"):
        store.save("Bad", OffboardTemplate(leave_out=frozenset({"password"})))
    with pytest.raises(ValueError, match="name"):
        store.save("  ", OffboardTemplate())
    assert not (tmp_path / "t.json").exists()


def test_deleting_the_default_moves_it_and_the_last_stays(tmp_path):
    store = OffboardTemplateStore(tmp_path / "t.json")
    store.delete(STARTER_DEFAULT)
    assert store.default in store.names() and STARTER_DEFAULT not in store.names()
    store.delete(store.names()[0])
    with pytest.raises(ValueError, match="last template"):
        store.delete(store.names()[0])


# --- the screen -----------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     offboard_templates=OffboardTemplateStore(tmp_path / "templates.json"))
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _form(**extra):
    return {"user": "", "manager": "", "subject": "S", "message": "M", "days": "30", "notify": "", **extra}


def test_the_page_opens_as_all_eight_steps(client):
    page = client.get("/lifecycle").text
    assert "All eight steps" in page and "opens by default" in page and "Hand-over" in page
    assert not [c for c in page.split('name="na"')[1:] if c.split(">")[0].strip().endswith("checked")]
    assert unescape(DEFAULT_MESSAGE) in unescape(page)


def test_picking_one_fills_the_form_and_keeps_the_addresses(client, gam_calls):
    before = len(gam_calls())
    fields = client.get("/lifecycle/template", params={"name": "Lock only", "user": "a@example.com", "manager": "b@example.com"}).text
    assert 'value="a@example.com"' in fields and 'value="b@example.com"' in fields
    assert fields.count("checked") == len(OPTIONAL_STEPS)
    assert len(gam_calls()) == before       # a template runs nothing


def test_an_unknown_template_leaves_a_form(client):
    assert 'name="manager"' in client.get("/lifecycle/template", params={"name": "Nope"}).text


def test_saving_the_form_and_opening_with_it(client):
    panel = client.post("/lifecycle/templates/save", data=_form(template_name="Mine", na=["calacls"], add=["imap_off"], days="7")).text
    assert "Saved “Mine”" in unescape(panel)
    assert "opens by default" in client.post("/lifecycle/templates/default", data={"name": "Mine"}).text
    page = client.get("/lifecycle").text
    assert 'value="7"' in page and 'value="imap_off" checked' in page and 'value="calacls" checked' in page


def test_saving_over_a_name_asks(client):
    panel = client.post("/lifecycle/templates/save", data=_form(template_name="Hand-over", days="9")).text
    assert "Replace “Hand-over”" in unescape(panel)
    store = client.app.state.gamgui.offboard_templates
    assert store.get("Hand-over").days == 30
    client.post("/lifecycle/templates/save", data=_form(template_name="Hand-over", days="9", replace="1"))
    assert store.get("Hand-over").days == 9


def test_a_form_that_couldn_t_run_can_t_be_saved(client):
    panel = client.post("/lifecycle/templates/save", data=_form(template_name="Bad", notify="not an address")).text
    assert "isn&#39;t one email address" in panel and client.app.state.gamgui.offboard_templates.get("Bad") is None


def test_deleting_one(client):
    client.post("/lifecycle/templates/delete", data={"name": "Hand-over"})
    assert client.app.state.gamgui.offboard_templates.get("Hand-over") is None


def test_a_template_s_words_are_only_ever_text(client):
    client.post("/lifecycle/templates/save", data=_form(template_name="<img src=x onerror=alert(1)>", subject="</textarea><script>x</script>"))
    panel = client.get("/lifecycle").text
    assert "<img src=x" not in panel


def test_a_template_can_t_reach_the_run(client):
    # The plan is the form's: a template only fills it, and Preview and Run still hold it to their token.
    assert client.post("/lifecycle/offboard/run", data=_form(user="carol@example.com", manager="alice@example.com", template="Lock only", confirmed="1")).text.count("preview") >= 1
