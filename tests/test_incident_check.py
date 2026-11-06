"""Incident (back-port plan E1): Check Account and Secure. Ported from SwiftGamGui's CompromisedTests and CompromisedChangesTests. carol@ is the
mock's taken-over account: forwarding on, three filters, a delegate, a send-as address, a recovery email and phone, three drafts and one
scheduled phish."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands, is_one_address
from gamgui.core.gam.models import Vacation
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident import secure
from gamgui.core.incident.check import (
    Account,
    AdminRole,
    Draft,
    Filter,
    Forwarding,
    ScheduledSend,
    SendAs,
    drafts_matching,
    findings,
)
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
ALICE = "alice@example.com"
ON_INCIDENT = {"HX-Current-URL": "http://testserver/incident"}


# --- the reads in words ---------------------------------------------------------------------------------------------------


def test_a_filter_says_what_it_matches_and_does():
    f = Filter.from_record({"id": "F1", "criteria": {"from": "billing@vendor.example", "hasAttachment": True},
                            "action": {"forward": "x@example.org", "removeLabelIds": ["INBOX", "UNREAD"], "addLabelIds": ["TRASH", "Label_7"]}})
    assert f.hides_or_forwards
    assert f.words == ("From billing@vendor.example, has an attachment: forwards it to x@example.org, deletes it (to Trash), adds the label "
                       "Label_7, archives it (skips the inbox), marks it read")
    plain = Filter.from_record({"id": "F2", "criteria": {}, "action": {"addLabelIds": ["STARRED"]}})
    assert not plain.hides_or_forwards and plain.words == "Every message: stars it"


def test_forwarding_in_words():
    assert Forwarding.from_records([{"forwardEnabled": "True", "forwardTo": "x@y.z", "disposition": "archive"}]).words == \
        "On: every message goes to x@y.z, and the copy is archived"
    assert Forwarding.from_records([{"forwardEnabled": "False"}]).words == "Off"
    assert Forwarding.from_records([]) is None       # Gmail off prints the titles alone: unknown, never "off"


def test_send_as_and_roles_in_words():
    s = SendAs.from_record({"sendAsEmail": "b@x.y", "displayName": "B", "isPrimary": "False", "isDefault": "True", "replyToAddress": "r@x.y",
                            "verificationStatus": "accepted", "signature": "True"})
    assert s.words == "B <b@x.y> · sent from by default · replies go to r@x.y · verification accepted · has a signature"
    assert AdminRole.from_record({"role": "_SEED_ADMIN_ROLE", "scopeType": "CUSTOMER"}).words == "Super Admin, for the whole domain"
    assert AdminRole.from_record({"role": "Help", "scopeType": "ORG_UNIT", "orgUnit": "/Staff"}).words == "Help, for the org unit /Staff"


def test_an_answer_that_is_not_the_person_is_refused():
    with pytest.raises(ValueError):
        Account.from_record({"notice": "x"})


@pytest.mark.parametrize("row,expected", [
    ({"id": "a", "Labels": "SCHEDULED", "LabelsCount": "1"}, True),
    ({"id": "b", "Labels": "SCHEDULED SENT", "LabelsCount": "2"}, False),      # its copy holds the only whole Bcc list
    ({"id": "c", "Labels": "Not SCHEDULED", "LabelsCount": "1"}, False),       # a user label's name can hold the word
    ({"id": "d", "Labels": "INBOX", "LabelsCount": "1"}, False),
])
def test_only_a_message_plainly_labelled_scheduled_counts(row, expected):
    assert ScheduledSend.from_record(row).is_scheduled is expected


def test_a_scheduled_email_names_each_recipient_once():
    send = ScheduledSend.from_record({"id": "a", "Labels": "SCHEDULED", "LabelsCount": "1", "To": "A <a@x.y>, b@x.y", "Cc": "a@x.y", "Bcc 1": "c@x.y"})
    assert send.recipients == ("a@x.y", "b@x.y", "c@x.y") or set(send.recipients) == {"a@x.y", "b@x.y", "c@x.y"}


def test_drafts_by_subject_ignore_case_and_never_match_nothing():
    drafts = [Draft("1", subject="Shared file: Q4"), Draft("2", subject="budget")]
    assert [d.id for d in drafts_matching(drafts, " shared FILE ")] == ["1"]
    assert drafts_matching(drafts, "   ") == []


def test_findings_put_what_goes_out_on_its_own_first():
    send = ScheduledSend("a", "x", "", ("p@x.y",), True)
    said = findings(Account(CAROL, recovery_email="r@x.y"), forwarding=Forwarding(True, "t@x.y"), scheduled=[send], delegates=["d@x.y"],
                    auto_reply=Vacation(enabled=True, subject="Out"), drafts=[Draft("1")])
    assert "scheduled to send" in said[0] and "Forwarding is on, to t@x.y" in said[1]
    assert any("auto-reply is on (“Out”)" in s for s in said) and any("Recovery email r@x.y" in s for s in said)
    assert any("2-Step Verification is off" in s for s in said)


def test_a_read_that_failed_finds_nothing_rather_than_none():
    # None is a read that failed: it's said beside its list, never here as if it were clear.
    said = findings(Account(CAROL, enrolled_2sv=True))
    assert said == []


def test_a_super_admin_and_a_role_are_said():
    assert "super admin" in findings(Account(CAROL, is_admin=True, enrolled_2sv=True))[0]
    assert "holds an admin role" in findings(Account(CAROL, enrolled_2sv=True), roles=[AdminRole("Help", "the whole domain")])[0]


# --- the steps ------------------------------------------------------------------------------------------------------------


def _steps(**kw):
    kw.setdefault("switched_on", frozenset(secure.ON_BY_DEFAULT))
    return secure.steps(CAROL, **kw)


def test_the_lock_runs_in_order_and_every_step_needs_the_reset():
    steps = _steps()
    assert [s.key for s in steps] == ["password", "revoke", "forward", "imap_off", "pop_off"]
    assert steps[0].requires == () and all(s.requires == ("password",) for s in steps[1:])
    assert GAMCommands.deprovision_user(CAROL) in steps[1].commands


def test_without_the_reset_nothing_stops_anything():
    # A refused sign-out, the admin's scope, mustn't stop forwarding off, the service account's.
    steps = _steps(switched_on=frozenset({"revoke", "forward"}))
    assert [s.requires for s in steps] == [(), ()]


def test_each_pick_is_its_own_step_in_run_order():
    send = ScheduledSend("s1", "Later", "", ("p@x.y",), True)
    steps = _steps(switched_on=frozenset({"password", "clear_recovery"}), filters=[Filter("F1", ("a",), ("b",), True)],
                   delegates=["d@x.y"], send_as=["s@x.y"], auto_reply_off=True, scheduled=[send], drafts=[Draft("d1", subject="Hi")],
                   recovery=("r@x.y", ""))
    assert [s.key for s in steps] == ["password", "scheduled", "clear_recovery", "filters", "delegate", "send_as", "auto_reply", "drafts"]
    assert "now email r@x.y" in steps[2].summary and "“Hi”" in steps[-1].summary


@pytest.mark.parametrize("bad", ["Name <a@b.c>", "id:123", "a@b.c,d@e.f", "a b@c.d", "nobody", ""])
def test_a_removal_is_one_address_gam_acts_on_as_given(bad):
    assert not is_one_address(bad)
    with pytest.raises(ValueError):
        _steps(delegates=[bad])
    with pytest.raises(ValueError):
        GAMCommands.delete_send_as(CAROL, bad)


def test_only_a_scheduled_message_is_deleted_as_one():
    with pytest.raises(ValueError):
        _steps(scheduled=[ScheduledSend("s1", is_scheduled=False)])


def test_message_ids_are_one_list_never_a_selector():
    assert GAMCommands.delete_messages(CAROL, ["a", "b"]) == ["user", CAROL, "delete", "messages", "ids", "a,b"]
    for bad in ([], ["a,b"], ["file"], ["a b"]):
        with pytest.raises(ValueError):
            GAMCommands.delete_messages(CAROL, bad)


def test_a_switch_outside_the_list_is_refused():
    with pytest.raises(ValueError):
        secure.steps(CAROL, frozenset({"delete_everything"}))


# --- through the page and the mock ----------------------------------------------------------------------------------------


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


def _check(client, state, person=CAROL) -> str:
    r = client.post("/incident/check", data={"person": person})
    job = state.jobs.get(state.incident_check_job)
    if job is not None:
        wait_for_job(client, job)
    return client.get(f"/incident/check/status?job={state.incident_check_job}", headers=ON_INCIDENT).text if job is not None else r.text


def _preview(client, state, **picks) -> str:
    data = {"run": state.incident_check.run, "switch": sorted(secure.ON_BY_DEFAULT), **picks}
    return client.post("/incident/secure/preview", data=data).text


def _token(html: str) -> str:
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html))
    return m.group(1) if m else ""


def _apply(client, state, token, typed=CAROL, **extra) -> str:
    r = client.post("/incident/secure/apply", data={"confirmed": "1", "preview": token, "confirm_email": typed, **extra})
    job = state.jobs.get(state.incident_run_job)
    if job is not None:
        wait_for_job(client, job)
    return r.text


def _writes(gam_calls) -> list:
    return [a for a in gam_calls() if GAMCommands.print_forward(CAROL) != a and (a[:2] == ["update", "user"] or (
        a[:2] == ["user", CAROL] and a[2] in ("deprovision", "forward", "imap", "pop", "delete", "vacation")))]


def test_a_run_s_end_reads_the_account_again_and_keeps_its_result(client, state):
    # The result once sat inside what the check replaces, so it vanished the moment the run ended.
    _check(client, state)
    _apply(client, state, _token(_preview(client, state)))
    panel = client.get(f"/incident/secure/status?job={state.incident_run_job}", headers=ON_INCIDENT).text
    assert 'hx-post="/incident/check"' in panel and "after_run" in panel and "complete" in panel
    page = client.get("/incident").text
    assert page.index('id="incident-check"') < page.index('id="incident-plan"')
    assert '<div id="incident-plan"></div>' in page
    assert 'hx-swap-oob' not in client.post("/incident/check", data={"person": CAROL, "after_run": "1"}).text
    wait_for_job(client, state.jobs[state.incident_check_job])


def test_a_new_check_clears_the_plan_on_screen(client, state):
    assert 'id="incident-plan" hx-swap-oob="innerHTML"' in client.post("/incident/check", data={"person": CAROL}).text
    wait_for_job(client, state.jobs[state.incident_check_job])


def test_the_jobs_page_links_to_the_check_rather_than_offering_a_plan_it_can_t_show(client, state):
    _check(client, state)
    panel = client.get(f"/incident/check/status?job={state.incident_check_job}").text
    assert 'hx-post="/incident/secure/preview"' not in panel and 'href="/incident"' in panel
    run = client.post("/incident/check", data={"person": CAROL})
    assert run.status_code == 200
    wait_for_job(client, state.jobs[state.incident_check_job])


def test_the_page_and_nav_offer_it(client):
    page = client.get("/incident").text
    assert 'href="/incident"' in page and "Check account" in page


def test_check_reads_and_changes_nothing(client, state, gam_calls):
    page = unescape(_check(client, state))
    assert "carol.home@example.org" in page and "billing.desk@example.net" in page and "helpdesk@example.com" in page
    assert "carol.recovery@example.net" in page and "2-Step Verification is off" in page
    assert "scheduled to send" in page and "3 drafts" in page
    assert _writes(gam_calls) == []


def test_an_alias_reads_as_its_owner(client, state):
    _check(client, state, "a.anders@example.com")
    assert state.incident_check.account.primary_email == ALICE


def test_an_account_that_can_t_be_read_stops_the_check(client, state):
    page = unescape(_check(client, state, "nobody-here@example.com"))
    assert "Couldn't read nobody-here@example.com" in page and state.incident_check is None


def test_a_read_that_failed_is_said_never_none(client, state, gam_state):
    (gam_state / "nogmail").mkdir()
    (gam_state / "nogmail" / CAROL).write_text("")
    page = unescape(_check(client, state))
    assert "Couldn't read filters" in page or "Couldn't read drafts" in page
    assert "No drafts." not in page and "No filters." not in page and "No delegates." not in page


def test_nothing_is_ticked_to_undo_at_the_start(client, state):
    page = _check(client, state)
    for name in ("filter", "delegate", "send_as", "delete_scheduled", "auto_reply"):
        assert not re.search(rf'name="{name}"[^>]*checked', page), name
    assert re.search(r'value="none" checked', page)
    assert not re.search(r'value="clear_recovery"[^>]*checked', page)    # a loss when the values are the person's own
    assert re.search(r'value="revoke"[^>]*checked', page)


def test_the_lock_runs_from_its_preview_with_the_address_typed(client, state, gam_calls):
    _check(client, state)
    page = unescape(_preview(client, state))
    assert "Securing carol@example.com: 5 steps" in page and "deprovision signout" in page and _writes(gam_calls) == []
    _apply(client, state, _token(page))
    sent = _writes(gam_calls)
    assert sent[0] == GAMCommands.reset_password(CAROL) and GAMCommands.deprovision_user(CAROL) in sent
    assert GAMCommands.forward_off(CAROL) in sent and GAMCommands.imap_off(CAROL) in sent and GAMCommands.pop_off(CAROL) in sent


@pytest.mark.parametrize("typed", ["", "carol", ALICE])
def test_the_wrong_address_runs_nothing(client, state, gam_calls, typed):
    _check(client, state)
    refused = unescape(_apply(client, state, _token(_preview(client, state)), typed=typed))
    assert "Type the exact email address" in refused and _writes(gam_calls) == []


def test_picks_are_what_the_check_listed_whatever_was_posted(client, state):
    _check(client, state)
    page = unescape(_preview(client, state, filter=["ANe1Bmj001", "NOT-LISTED"], delegate=["helpdesk@example.com", "boss@example.com"],
                             send_as=["billing.desk@example.net", CAROL, "other@example.net"]))
    assert "delete filters ANe1Bmj001" in page and "NOT-LISTED" not in page
    assert "delete delegate helpdesk@example.com" in page and "boss@example.com" not in page
    assert "delete sendas billing.desk@example.net" in page and "other@example.net" not in page
    assert "delete sendas carol@example.com" not in page       # never the primary


def test_a_full_run_removes_what_was_picked_and_the_check_again_says_so(client, state, gam_state, gam_calls):
    _check(client, state)
    page = _preview(client, state, filter=["ANe1Bmj001"], delegate=["helpdesk@example.com"], send_as=["billing.desk@example.net"],
                    delete_scheduled="1", drafts="all", auto_reply="1")
    done = _apply(client, state, _token(page), confirm="confirm")
    assert done
    sent = _writes(gam_calls)
    assert sent[0] == GAMCommands.reset_password(CAROL)
    assert sent[1] == GAMCommands.delete_messages(CAROL, ["19a0c0ffee000014"])     # right after the reset: one comes due during the run
    assert GAMCommands.delete_send_as(CAROL, "billing.desk@example.net") in sent
    assert state.incident_done[CAROL] == frozenset(secure.ON_BY_DEFAULT)
    again = unescape(_check(client, state))
    assert "billing.desk@example.net" not in again and "No drafts." in again and "No email is scheduled" in again
    assert "(done in an earlier run)" in again and not re.search(r'value="revoke"[^>]*checked', again)


def test_what_a_run_finished_belongs_to_that_person(client, state, gam_state):
    # Those ticks carried to the next person typed once, whose plan then skipped the reset and the sign-out.
    _check(client, state)
    _apply(client, state, _token(_preview(client, state)))
    page = _check(client, state, ALICE)      # alice@ isn't the mock's connected admin: only this tenant's check
    assert "(done in an earlier run)" not in page and re.search(r'value="revoke"[^>]*checked', page)


def test_a_second_run_keeps_what_the_first_finished(client, state, gam_state):
    # Replaced rather than added to, the next check switched the reset back on: a second random password after one was handed out.
    _check(client, state)
    _apply(client, state, _token(_preview(client, state)))
    _check(client, state)
    page = client.post("/incident/secure/preview", data={"run": state.incident_check.run, "switch": ["forward"]}).text
    _apply(client, state, _token(page))
    assert "password" in state.incident_done[CAROL] and "forward" in state.incident_done[CAROL]
    assert not re.search(r'value="password"[^>]*checked', _check(client, state))


def test_a_silently_skipped_token_is_never_none(client, state, gam_state):
    # GAM skips a refused token without a word and exits 0: read as an answer, this said "No filters." about an account nobody read.
    (gam_state / "silent_token").mkdir()
    (gam_state / "silent_token" / CAROL).write_text("")
    page = unescape(_check(client, state))
    for none in ("No filters.", "No delegates.", "No other send-as", "No auto-reply is on.", "No drafts.", "No email is scheduled"):
        assert none not in page, none
    for said in ("Couldn't read filters", "Couldn't read delegates", "Couldn't read send-as", "Couldn't read the auto-reply", "Couldn't read drafts",
                 "Couldn't read scheduled emails"):
        assert said in page, said


def test_a_later_enabled_line_can_t_hide_an_auto_reply():
    text = "User: x@y.z, Vacation:\n  Enabled: True\n  Subject: Hi\n  Enabled: False\n  Message:\n    Click here\n"
    assert Vacation.from_show_text(text).enabled


def test_a_delete_gam_said_nothing_about_fails_the_step(client, state, gam_state, tmp_path):
    # GAM skips a batch on serviceNotAvailable without a word, exit 0, and a scheduled email in it still goes out.
    _check(client, state)
    page = client.post("/incident/secure/preview", data={"run": state.incident_check.run, "delete_scheduled": "1"}).text
    (gam_state / "gmail_unavailable").write_text("")
    _apply(client, state, _token(page))
    job = state.jobs[state.incident_run_job]
    assert job.failed_total == 1 and any("Delete 1 scheduled email" in item for item in job.failed_items)
    assert "said nothing about the rest" in (tmp_path / "audit.jsonl").read_text()


def test_a_run_refuses_while_a_check_is_going(client, state, gam_calls):
    _check(client, state)
    token = _token(_preview(client, state))
    from gamgui.web.jobs import start_job
    state.incident_check_job = start_job(state.jobs, 1, kind="incident_check", title="Check").id
    refused = unescape(_apply(client, state, token))
    assert "A check or a run is going on" in refused and _writes(gam_calls) == []
    state.jobs[state.incident_check_job].finish()


def test_a_connection_changed_during_the_reads_runs_nothing(client, state, gam_calls, monkeypatch):
    _check(client, state)
    token = _token(_preview(client, state))
    conn = state.connector
    real = conn.account_to_check

    async def switch_during(email):
        account = await real(email)
        state.connector = None
        return account

    monkeypatch.setattr(conn, "account_to_check", switch_during)
    refused = unescape(_apply(client, state, token))
    state.connector = conn
    assert "connection changed" in refused and _writes(gam_calls) == []


def test_an_audit_log_that_can_t_be_written_runs_nothing(client, state, gam_calls, monkeypatch):
    _check(client, state)
    token = _token(_preview(client, state))

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state.connector.audit, "record", full)
    refused = unescape(_apply(client, state, token))
    assert "so nothing ran" in refused and _writes(gam_calls) == [] and not state.incident_run_job


def test_a_run_s_panel_names_its_own_person(client, state, gam_state):
    _check(client, state)
    _apply(client, state, _token(_preview(client, state)))
    first = state.incident_run_job
    _check(client, state, ALICE)
    _apply(client, state, _token(_preview(client, state)), typed=ALICE)
    assert "give carol@example.com a new password" in client.get(f"/incident/secure/status?job={first}").text


def test_a_failed_reset_stops_the_rest(client, state, gam_calls, monkeypatch):
    _check(client, state)
    page = _preview(client, state)
    conn = state.connector
    real = conn.reset_password

    async def refused(email):
        result = await real(email)
        result.ok = False
        result.detail = "no"
        return result

    monkeypatch.setattr(conn, "reset_password", refused)
    _apply(client, state, _token(page))
    job = state.jobs[state.incident_run_job]
    assert job.skipped and GAMCommands.forward_off(CAROL) not in gam_calls()
    assert "The attacker may still be signed in" in client.get(f"/incident/secure/status?job={job.id}").text


def test_an_account_changed_since_the_check_runs_nothing(client, state, gam_calls, monkeypatch):
    _check(client, state)
    page = _preview(client, state)
    now = Account(CAROL, suspended=True)

    async def changed(email):
        return now

    monkeypatch.setattr(state.connector, "account_to_check", changed)
    refused = unescape(_apply(client, state, _token(page)))
    assert "changed after the check" in refused and _writes(gam_calls) == [] and state.incident_check is None


def test_a_scheduled_email_sent_since_the_check_runs_nothing(client, state, gam_calls, monkeypatch):
    # One Gmail sent since may be its Sent copy under the same ID, the only one with the whole Bcc list.
    _check(client, state)
    page = _preview(client, state, delete_scheduled="1")

    async def gone(email):
        return []

    monkeypatch.setattr(state.connector, "scheduled_sends_of", gone)
    refused = unescape(_apply(client, state, _token(page)))
    assert "sent or cancelled after the check" in refused and _writes(gam_calls) == [] and state.incident_check is None


def test_a_preview_from_an_older_check_is_refused(client, state, gam_calls):
    _check(client, state)
    old = _token(_preview(client, state))
    _check(client, state)
    refused = unescape(_apply(client, state, old))
    assert "check the account again" in refused and _writes(gam_calls) == []


def test_a_preview_of_a_form_from_an_older_check_is_refused(client, state):
    _check(client, state)
    run = state.incident_check.run
    _check(client, state)
    page = unescape(client.post("/incident/secure/preview", data={"run": run, "switch": ["forward"]}).text)
    assert "Check the account first" in page and not _token(page)


def test_a_token_runs_once(client, state, gam_calls):
    _check(client, state)
    token = _token(_preview(client, state))
    _apply(client, state, token)
    before = len(_writes(gam_calls))
    _apply(client, state, token)
    assert len(_writes(gam_calls)) == before


def test_nothing_switched_or_picked_is_refused_in_words(client, state):
    _check(client, state)
    page = unescape(client.post("/incident/secure/preview", data={"run": state.incident_check.run}).text)
    assert "there is nothing to run" in page and not _token(page)


def test_a_subject_no_draft_has_is_refused(client, state):
    _check(client, state)
    page = unescape(_preview(client, state, drafts="subject", drafts_subject="zzz"))
    assert "No draft's subject contains “zzz”" in page and not _token(page)


def test_drafts_by_subject_hold_only_those(client, state):
    _check(client, state)
    drafts = state.incident_check.drafts.value
    wanted = drafts[0].subject
    page = unescape(_preview(client, state, drafts="subject", drafts_subject=wanted))
    picked = [d.id for d in drafts if wanted.lower() in d.subject.lower()]
    assert f"delete messages ids {','.join(picked)}" in page


def test_the_admin_gamgui_signs_in_as_can_t_be_secured_here(client, state, gam_calls, monkeypatch):
    async def cut_off(commands, *, previewing=False):
        return "This is the admin GamGUI signs in as."

    monkeypatch.setattr(state.connector, "cut_off_refusal", cut_off)
    page = unescape(_check(client, state))
    assert "This is the admin GamGUI signs in as." in page and re.search(r'<button type="submit" disabled', page)
    refused = unescape(_preview(client, state))
    assert "This is the admin GamGUI signs in as." in refused and not _token(refused)


def test_the_check_goes_with_the_connection(client, state):
    _check(client, state)
    assert state.incident_check is not None
    state.activate(state.connector)
    assert state.incident_check is None


def test_a_person_s_page_hands_the_address_over(client, state):
    assert "/incident?person=" in client.get(f"/users/detail?email={CAROL}").text
    page = client.get(f"/incident?person={CAROL}").text
    assert CAROL in page and 'hx-trigger="load, submit"' in page


def test_a_subject_the_attacker_wrote_is_only_ever_text(client, state):
    _check(client, state)
    state.incident_check.drafts = type(state.incident_check.drafts)([Draft("d1", subject="<script>x</script>")])
    page = client.get("/incident").text
    assert "<script>x</script>" not in page and "&lt;script&gt;x" in page
