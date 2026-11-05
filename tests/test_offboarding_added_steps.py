"""Offboarding's added steps (back-port plan C3): off by default, each one GAM command, in SwiftGamGui's order and with its
argv. Four are argv-identical to its live runs; handing over calendars and removing filters haven't run live in either app."""

from __future__ import annotations

import re
from datetime import date
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import lifecycle
from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL, ALICE = "carol@example.com", "alice@example.com"
FORM = {"user": CAROL, "manager": ALICE, "subject": "s", "message": "m", "days": "30", "notify": ""}


def test_each_argv_is_swift_gamgui_s():
    c = GAMCommands
    assert c.clear_recovery(CAROL) == ["update", "user", CAROL, "recoveryemail", "", "recoveryphone", ""]
    assert c.hide_from_directory(CAROL) == ["update", "user", CAROL, "gal", "off"]
    assert c.imap_off(CAROL) == ["user", CAROL, "imap", "off"]
    assert c.pop_off(CAROL) == ["user", CAROL, "pop", "off"]
    assert c.transfer_calendar_ownership(["c@group.calendar.google.com", "d@group.calendar.google.com"], ALICE) == [
        "calendars", "c@group.calendar.google.com,d@group.calendar.google.com", "transfer", "ownership", ALICE]
    assert c.delete_filters(CAROL, ["a-1", "b_2"]) == ["user", CAROL, "delete", "filters", "a-1,b_2"]
    assert c.print_owned_calendars(CAROL) == ["user", CAROL, "print", "calendars", "ownedsecondary", "showhidden", "fields",
                                              "id,summary", "formatjson"]
    assert c.print_vault_holds(CAROL) == ["user", CAROL, "print", "vaultholds"]


@pytest.mark.parametrize("bad", [[], ["a@group.calendar.google.com,b@group.calendar.google.com"], ["team"],
                                 ["a @group.calendar.google.com"], ["file"], ["c_a\u0600,c_victim@group.calendar.google.com"],
                                 ["c_a\x1fx@group.calendar.google.com"], ["boss@example.com"], ["a@b@group.calendar.google.com"]])
def test_each_calendar_handed_over_is_one_id(bad):
    with pytest.raises(ValueError):
        GAMCommands.transfer_calendar_ownership(bad, ALICE)


@pytest.mark.parametrize("bad", [[], [""], ["a,b"], ["a b"], ["a\u0600"], ["ok", "x y"], ["file"], ["CSV_File"], ["csv-kmd"], ["csvdata"]])
def test_each_filter_deleted_is_one_id(bad):
    with pytest.raises(ValueError):
        GAMCommands.delete_filters(CAROL, bad)


def test_a_selector_word_beside_another_id_is_just_an_id():
    assert GAMCommands.delete_filters(CAROL, ["file", "ANe1Bmj2"])[-1] == "file,ANe1Bmj2"


def _steps(**kwargs):
    return lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, date(2026, 11, 5), **kwargs)


def test_with_nothing_added_it_is_the_eight():
    assert [s.key for s in _steps()] == list(lifecycle.ROUTINE)


def test_added_steps_run_in_their_order_and_need_only_the_reset():
    steps = _steps(added=frozenset(lifecycle.ADDABLE_STEPS), calendars=[lifecycle.OwnedCalendar("c@group.calendar.google.com", "C")],
                   filters=[lifecycle.ForwardingFilter("F1", "x@example.org")])
    assert [s.key for s in steps] == list(lifecycle.RUN_ORDER) and len(steps) == 14
    for step in steps:
        if step.key in lifecycle.ADDABLE_STEPS:
            assert step.requires == ("password",), step.key


def test_only_an_addable_step_can_be_added():
    with pytest.raises(ValueError):
        _steps(added=frozenset({"delegate"}))


def test_only_a_filter_that_forwards_is_read_as_one():
    found = lifecycle.forwarding_filters_from([{"id": "F1", "action": {"forward": "x@e.org"}}, {"id": "F2", "action": {"label": "L"}},
                                               {"id": "F3"}, {"action": {"forward": "y@e.org"}}])
    assert found == [lifecycle.ForwardingFilter("F1", "x@e.org")]


def test_the_warnings_say_what_is_at_stake():
    many = [lifecycle.OwnedCalendar(f"c{i}", f"Cal {i}") for i in range(7)]
    assert "Cal 4" in lifecycle.secondary_calendars_warning(CAROL, many) and "and 2 more" in lifecycle.secondary_calendars_warning(CAROL, many)
    assert lifecycle.secondary_calendars_warning(CAROL, []) == "" and lifecycle.vault_holds_warning(CAROL, [], True) == ""
    held = lifecycle.vault_holds_warning(CAROL, [lifecycle.VaultHold("M", "H", "/Staff")], True)
    assert "“H” in “M” (through the org unit /Staff)" in held and "may move nothing" in held
    assert "may move nothing" not in lifecycle.vault_holds_warning(CAROL, [lifecycle.VaultHold("M", "H")], False)


# --- through the page and the mock --------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    return AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")


@pytest.fixture
def client(state):
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _preview(client, *added, **extra):
    data = {**FORM, **extra, "add": list(added)}
    return client.post("/lifecycle/offboard/preview", data=data).text


def _token(html):
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html))
    return m.group(1) if m else ""


def _run(client, state, token, *added, **extra):
    r = client.post("/lifecycle/offboard/run", data={**FORM, "add": list(added), "confirmed": "1", "preview": token, **extra})
    job = next((j for j in state.jobs.values() if j.kind == "offboard"), None)
    if job is not None:
        wait_for_job(client, job)
    return r.text


def test_the_form_offers_them_off(client):
    page = client.get("/lifecycle").text
    assert page.count('name="add"') == len(lifecycle.ADDABLE_STEPS) and "Remove filters that forward" in page
    assert not re.search(r'name="add"[^>]*checked', page)


def test_a_preview_warns_of_owned_calendars_whatever_the_steps(client):
    page = unescape(_preview(client))
    assert "owns 2 secondary calendars" in page and "Operations" in page and "Hand over secondary calendars" in page


def test_adding_the_handover_replaces_the_warning_and_plans_hidden_calendars_too(client):
    page = unescape(_preview(client, "secondary_calendars"))
    assert "owns 2 secondary calendars" not in page and "transfer ownership alice@example.com" in page
    assert "c_ops@group.calendar.google.com" in page


def test_someone_with_no_calendars_or_filters_plans_neither(client):
    page = unescape(_preview(client, "secondary_calendars", "forwarding_filters", user="bob@example.com"))
    assert "transfer ownership" not in page and "delete filters" not in page


def test_a_refused_calendar_read_is_said_never_none(client, gam_state):
    (gam_state / "nocalendar").mkdir()
    (gam_state / "nocalendar" / CAROL).write_text("")
    page = unescape(_preview(client, "secondary_calendars"))
    assert "Couldn't check whether carol@example.com owns secondary calendars" in page and "transfer ownership" not in page


def test_a_refused_vault_read_is_said_never_none(client, gam_state):
    (gam_state / "novault").write_text("")
    assert "Couldn't check carol@example.com for Vault holds" in unescape(_preview(client))


def test_only_filters_that_forward_are_planned(client):
    page = unescape(_preview(client, "forwarding_filters"))
    assert "delete filters ANe1Bmj001" in page and "carol.home@example.org" in page


def test_the_added_steps_run_after_the_reset(client, state, gam_calls):
    added = ("clear_recovery", "imap_off", "pop_off", "hide_from_directory")
    token = _token(_preview(client, *added))
    _run(client, state, token, *added, confirm="confirm")
    sent = [argv for argv in gam_calls() if argv[:2] == ["user", CAROL] or argv[:2] == ["update", "user"]]
    assert GAMCommands.clear_recovery(CAROL) in sent and GAMCommands.imap_off(CAROL) in sent
    assert GAMCommands.pop_off(CAROL) in sent and GAMCommands.hide_from_directory(CAROL) in sent
    reset = sent.index(GAMCommands.reset_password(CAROL))
    assert reset < sent.index(GAMCommands.clear_recovery(CAROL))


def test_ten_steps_need_the_word_typed(client, state, gam_calls):
    added = ("clear_recovery", "imap_off")
    page = _preview(client, *added)
    assert 'id="offboard-typed"' in page
    assert "confirm" in _run(client, state, _token(page), *added).lower()
    assert not [a for a in gam_calls() if a[:2] == ["update", "user"]]      # refused: nothing ran
    done = _run(client, state, _token(_preview(client, *added)), *added, confirm="confirm")
    assert not [j for j in state.jobs.values() if j.kind == "offboard" and not j.finished] and done


def test_eight_steps_don_t(client):
    assert 'id="offboard-typed"' not in _preview(client)


def test_a_new_forwarding_filter_since_the_preview_refuses_the_run(client, state, monkeypatch, gam_calls):
    token = _token(_preview(client, "forwarding_filters"))
    conn = state.connector
    real = conn.user_filters

    async def one_more(email):
        return [*await real(email), {"id": "NEW", "action": {"forward": "evil@example.org"}}]

    monkeypatch.setattr(conn, "user_filters", one_more)
    refused = _run(client, state, token, "forwarding_filters")
    assert "changed after the preview" in unescape(refused) and not [a for a in gam_calls() if a[:2] == ["update", "user"]]


def test_filters_are_read_again_when_the_preview_found_none(client, state, monkeypatch, gam_calls):
    # The leaver is still signed in: one made since would keep forwarding after the offboarding says it's done.
    conn = state.connector

    async def none(email):
        return []

    monkeypatch.setattr(conn, "user_filters", none)
    page = unescape(_preview(client, "forwarding_filters"))
    assert "has nothing to do: none of carol@example.com's filters forward" in page and "delete filters" not in page

    async def one(email):
        return [{"id": "NEW", "action": {"forward": "evil@example.org"}}]

    monkeypatch.setattr(conn, "user_filters", one)
    refused = _run(client, state, _token(page), "forwarding_filters")
    assert "changed after the preview" in unescape(refused) and not [a for a in gam_calls() if a[:2] == ["update", "user"]]


@pytest.mark.parametrize("read", ["owned_calendars", "vault_holds", "user_filters"])
def test_a_read_that_fails_without_a_gam_error_is_said(client, state, monkeypatch, read):
    async def locked(email):
        raise PermissionError("the Keychain said no")

    monkeypatch.setattr(state.connector, read, locked)
    r = client.post("/lifecycle/offboard/preview", data={**FORM, "add": ["forwarding_filters"]})
    assert r.status_code == 200 and "Couldn&#39;t" in r.text and _token(r.text)


def test_the_filters_read_again_failing_runs_nothing(client, state, monkeypatch, gam_calls):
    token = _token(_preview(client, "forwarding_filters"))

    async def locked(email):
        raise PermissionError("the Keychain said no")

    monkeypatch.setattr(state.connector, "user_filters", locked)
    r = _run(client, state, token, "forwarding_filters")
    assert "so nothing ran" in unescape(r) and not [a for a in gam_calls() if a[:2] == ["update", "user"]]


def test_a_preview_with_nothing_to_run_offers_no_run(client):
    # Every routine step already done, and the added step found nothing: "complete" would be a claim about nothing.
    done = {"done": list(lifecycle.ROUTINE)}
    page = unescape(_preview(client, "forwarding_filters", user="bob@example.com", **done))
    assert "there is nothing to run" in page and not _token(page) and "has nothing to do" in page


def test_a_requested_step_that_can_t_run_says_so(client, gam_state):
    page = unescape(_preview(client, "secondary_calendars", user="bob@example.com"))
    assert "has nothing to do: bob@example.com owns no secondary calendars" in page
    (gam_state / "nocalendar").mkdir()
    (gam_state / "nocalendar" / CAROL).write_text("")
    assert "so none will be handed over" in unescape(_preview(client, "secondary_calendars"))


def test_a_calendar_not_shared_with_the_admin_fails_and_says_so(client, state, gam_state, gam_calls):
    # GAM's validateCalendar reads it as the admin: carol's hidden calendar isn't shared with them, her team's is.
    token = _token(_preview(client, "secondary_calendars"))
    _run(client, state, token, "secondary_calendars")
    job = next(j for j in state.jobs.values() if j.kind == "offboard")
    assert any("Hand over secondary calendars" in item for item in job.failed_items)
    assert ["user", CAROL, "print", "calendars", "ownedsecondary", "showhidden", "fields", "id,summary", "formatjson"] in gam_calls()
    assert "c_archive@group.calendar.google.com" in " ".join(next(a for a in gam_calls() if a[:1] == ["calendars"]))
    # The one that went has left her list: a second preview plans only what's left.
    assert "c_ops@group.calendar.google.com" not in unescape(_preview(client, "secondary_calendars"))


def test_a_run_with_added_steps_says_so_in_the_audit(client, state, tmp_path):
    token = _token(_preview(client, "imap_off"))
    _run(client, state, token, "imap_off")
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"offboard_plan"' in log and "imap_off" in log
