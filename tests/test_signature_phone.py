"""The phone a signature shows (back-port plan C2): a change sends every other phone back as it is, and one added since the
read refuses the run rather than be deleted by a stale list. The cases are SwiftGamGui's. Setting one is argv-identical to
its live run (2026-10-24); changing or removing one is offline only in both apps."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands, Phone
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.phones import phones_from, plan_phone_change, shown_phone
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
ALICE = "alice@example.com"


def _phone(type_, value="1", primary=False, custom=""):
    return Phone(type=type_, custom_type=custom, value=value, primary=primary)


@pytest.mark.parametrize("bad", [
    [_phone("work", primary=True), _phone("home", primary=True)],    # GAM's usage error
    [_phone("google_voice")],                                        # GAM would keep it as a custom type
    [_phone("custom")], [_phone("custom", custom="  ")], [_phone("custom", custom=" Desk")],
    [_phone("work", custom="Desk")], [_phone("", custom="Desk")],    # GAM keeps no name on these
    [_phone("work", "")], [_phone("work", "1\n2")], [_phone("work", "1\x00")],
])
def test_a_phone_gam_would_not_send_back_as_it_is_never_reaches_it(bad):
    with pytest.raises(ValueError):
        GAMCommands.update_phones("c@example.com", bad)


def test_the_argv_is_swift_gamgui_s_and_each_number_is_one_element():
    assert GAMCommands.update_phones("c@example.com", [_phone("work", "x primary phone clear", primary=True)]) == [
        "update", "user", "c@example.com", "phone", "type", "work", "value", "x primary phone clear", "primary"]
    assert GAMCommands.update_phones("c@example.com", []) == ["update", "user", "c@example.com", "phone", "clear"]
    assert GAMCommands.update_phones("c@example.com", [_phone("custom", "100", custom="Desk")]) == [
        "update", "user", "c@example.com", "phone", "type", "custom", "Desk", "value", "100", "notprimary"]
    assert GAMCommands.update_phones("c@example.com", [_phone("", "100")]) == [
        "update", "user", "c@example.com", "phone", "value", "100", "notprimary"]


def test_the_shown_phone_is_the_primary_else_the_first():
    assert shown_phone([_phone("home", "1"), _phone("mobile", "2", primary=True)]) == "2"
    assert shown_phone([_phone("home", "1"), _phone("mobile", "2")]) == "1"
    assert shown_phone([]) == ""


def test_phones_read_from_a_record():
    assert phones_from({"primaryEmail": ALICE, "phones": [{"type": "custom", "customType": "Desk", "value": "100", "primary": True}]}) == [
        Phone("custom", "Desk", "100", True)]
    assert phones_from({"primaryEmail": ALICE}) == []


@pytest.mark.parametrize("record", [{}, {"notice": "x"}, {"phones": [{"value": "1"}]}, {"primaryEmail": ALICE, "phones": "x"},
                                    {"primaryEmail": ALICE, "phones": [{"value": "1"}, "junk"]}])
def test_an_answer_that_isn_t_their_phones_is_never_no_phones(record):
    # Read as none, the change would send one phone, which deletes the rest.
    with pytest.raises(ValueError):
        phones_from(record)


def test_a_change_in_words():
    both = [_phone("home", "1"), _phone("mobile", "2", primary=True), _phone("custom", "3", custom="Desk")]
    change = plan_phone_change(both, " 9 ", "Alice")
    assert [p.value for p in change.phones] == ["1", "9", "3"] and change.phones[1].type == "mobile" and not change.removes
    assert change.summary == "Change Alice's phone from “2” to “9”, keeping their other 2 phones as they are"
    change = plan_phone_change(both, "", "Alice")
    assert [p.value for p in change.phones] == ["1", "3"] and change.removes
    assert change.summary == "Remove Alice's phone “2”, keeping their other 2 phones as they are"
    change = plan_phone_change([], "5", "Alice")
    assert change.phones == [Phone("work", "", "5", True)] and change.summary == "Set Alice's work phone to “5”"
    with pytest.raises(ValueError, match="Nothing to remove"):
        plan_phone_change([], "  ", "Alice")
    with pytest.raises(ValueError, match="Nothing to change"):
        plan_phone_change([_phone("work", "100", primary=True)], " 100", "Alice")


# --- through the page and the mock --------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _google_has(gam_state: Path, phones: list, who: str = ALICE) -> None:
    (gam_state / "phones").mkdir(exist_ok=True)
    (gam_state / "phones" / who).write_text(json.dumps(phones))


def _writes(gam_calls):
    return [argv for argv in gam_calls() if argv[:2] == ["update", "user"]]


def _token(html: str) -> str:
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html))
    return m.group(1) if m else ""


def _preview(client, phone, email=ALICE) -> str:
    return client.post("/users/phone/preview", data={"email": email, "phone": phone}).text


def _apply(client, token, phone, email=ALICE, confirmed="1"):
    return client.post("/users/phone/apply", data={"email": email, "phone": phone, "confirmed": confirmed, "preview": token}).text


def test_a_phone_is_set_and_the_directory_shows_it(client, gam_state, gam_calls):
    client.get("/users")
    page = _preview(client, " +1 555 0100 ")
    assert "Set alice@example.com&#39;s work phone to “+1 555 0100”" in page and _writes(gam_calls) == []
    assert "+1 555 0100" in page and "phone type work value" in page
    done = _apply(client, _token(page), "+1 555 0100")
    assert _writes(gam_calls) == [["update", "user", ALICE, "phone", "type", "work", "value", "+1 555 0100", "primary"]]
    assert "Saved" in done and "+1 555 0100" in done
    st = client.app.state.gamgui
    assert [u.phone for u in st.user_cache.cached if u.primary_email == ALICE] == ["+1 555 0100"]
    again = _preview(client, "+1 555 0123")
    assert "Change" in again and "+1 555 0100" in again   # Google has it now, so it's no longer a first phone


def test_a_stray_line_on_stdout_never_reads_as_no_phones(client, gam_state, gam_calls):
    # GAM has written lines that aren't the record to stdout: read as none, the change would delete every phone.
    _google_has(gam_state, [{"type": "work", "value": "+1 555 0111", "primary": True}, {"type": "home", "value": "+1 555 0122"}])
    (gam_state / "stray_stdout").mkdir()
    (gam_state / "stray_stdout" / ALICE).write_text("")
    page = _preview(client, "+1 555 0100")
    assert not _token(page) and _writes(gam_calls) == [] and "aren&#39;t known" in page


def test_a_refused_change_keeps_the_form(client, gam_calls):
    page = _preview(client, "")      # nothing to change
    assert "/users/phone/preview" in page and 'name="phone"' in page
    page = _apply(client, "not-a-token", "+1 555 0100")
    assert "/users/phone/preview" in page and "+1 555 0100" in page and _writes(gam_calls) == []


@pytest.mark.hand_built_argv   # as many `phone` clauses as they have: the builder contract tries one
def test_changing_the_phone_keeps_the_others(client, gam_state, gam_calls):
    _google_has(gam_state, [{"type": "home", "value": "+1 555 0111"}, {"primary": True, "type": "mobile", "value": "+1 555 0199"},
                            {"customType": "Desk", "type": "custom", "value": "100"}])
    page = _preview(client, "+1 555 0100")
    assert "keeping their other 2 phones" in page
    _apply(client, _token(page), "+1 555 0100")
    assert _writes(gam_calls) == [["update", "user", ALICE,
                                   "phone", "type", "home", "value", "+1 555 0111", "notprimary",
                                   "phone", "type", "mobile", "value", "+1 555 0100", "primary",
                                   "phone", "type", "custom", "Desk", "value", "100", "notprimary"]]


def test_removing_the_last_phone_is_a_clear_and_needs_the_confirm_click(client, gam_state, gam_calls):
    _google_has(gam_state, [{"primary": True, "type": "work", "value": "+1 555 0100"}])
    token = _token(_preview(client, "  "))
    assert "needs confirmation" in _apply(client, token, "", confirmed="") and _writes(gam_calls) == []
    _apply(client, _token(_preview(client, "")), "")
    assert _writes(gam_calls) == [["update", "user", ALICE, "phone", "clear"]]


def test_a_phone_added_since_the_read_refuses_the_run(client, gam_state, gam_calls):
    token = _token(_preview(client, "+1 555 0100"))
    _google_has(gam_state, [{"primary": True, "type": "mobile", "value": "+1 555 0199"}])
    refused = _apply(client, token, "+1 555 0100")
    assert "preview again" in refused.lower() and _writes(gam_calls) == []


def test_a_phone_gam_can_t_send_back_refuses_the_change(client, gam_state, gam_calls):
    _google_has(gam_state, [{"primary": True, "type": "google_voice", "value": "+1 555 0123"}])
    page = _preview(client, "+1 555 0100")
    assert "google_voice" in page and not _token(page) and _writes(gam_calls) == []


def test_nothing_to_change_is_no_write(client, gam_calls):
    page = _preview(client, "  ")
    assert "Nothing to remove" in page and not _token(page) and _writes(gam_calls) == []


def test_it_runs_only_what_was_previewed(client, gam_calls):
    token = _token(_preview(client, "+1 555 0100"))
    assert "changed after the preview" in _apply(client, token, "+1 555 0199") and _writes(gam_calls) == []
    assert "expired or was already run" in _apply(client, token, "+1 555 0100") and _writes(gam_calls) == []


def test_a_failed_read_says_so_and_runs_nothing(client, gam_calls):
    page = _preview(client, "+1 555 0100", email="nobody-here@example.com")
    assert not _token(page) and _writes(gam_calls) == []


def test_the_overview_offers_it(client):
    page = client.get("/users/detail", params={"email": ALICE}).text
    assert 'hx-post="/users/phone/preview"' in page


def test_a_number_is_only_ever_text(client):
    page = _preview(client, "<img src=x onerror=alert(1)>")
    assert "<img" not in page and "&lt;img" in page
