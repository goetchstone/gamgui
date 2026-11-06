"""Respond (back-port plan E5): where each account stands, what's still to do with a button on each item, and what's done, from the logs, the audit log or the operator's word. Reads and this Mac only.
Ported from SwiftGamGui's KnownSignInStoreTests, NobodyHasStoreTests and PhishingChangesTests' audit reading."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident import known, logs, pull, reader, runs
from gamgui.core.incident.known import KnownSignIns, NobodyHasChecks
from gamgui.core.incident.store import IncidentStore, Problem
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import TEMPLATES, AppState, create_app
from gamgui.web.routes import incident_logs

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
ATTACKER = "198.51.100.23"
PHISH = "phish-1@mail.example.com"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


# --- the sign-ins marked as theirs ---------------------------------------------------------------------------------------------


@pytest.fixture
def signins(tmp_path) -> KnownSignIns:
    return KnownSignIns(tmp_path / "kept")


def test_a_mark_survives_a_restart_privately(signins, tmp_path):
    mark = signins.mark(DOMAIN, " Carol@Example.com ", ATTACKER, "  Her phone  ", NOW)
    assert (mark.account, mark.note) == (CAROL, "Her phone")
    again = KnownSignIns(tmp_path / "kept")
    assert again.marks == [mark] and again.for_domain("EXAMPLE.com") == [mark] and again.for_domain("other.example") == []
    assert stat.S_IMODE((tmp_path / "kept" / known.MARKS_FILE).stat().st_mode) == 0o600 and stat.S_IMODE((tmp_path / "kept").stat().st_mode) == 0o700


@pytest.mark.parametrize("account,address,note,says", [(CAROL, ATTACKER, "  ", "Say how you know"), (CAROL, ATTACKER, "x" * 501, "500 characters"), ("carol", ATTACKER, "n", "isn't one account"),
                                                       (f"{CAROL},b@example.com", ATTACKER, "n", "isn't one account"), (CAROL, "not an address", "n", "isn't one account"), (CAROL, "1.2.3.4 5.6.7.8", "n", "isn't one account")])
def test_a_mark_is_one_account_one_address_and_a_note(signins, account, address, note, says):
    with pytest.raises(Problem, match=says):
        signins.mark(DOMAIN, account, address, note)
    assert signins.marks == []


def test_marking_again_replaces_and_undo_removes(signins):
    signins.mark(DOMAIN, CAROL, ATTACKER, "first", NOW)
    second = signins.mark(DOMAIN, CAROL, ATTACKER, "second", NOW + timedelta(hours=1))
    assert signins.marks == [second]
    signins.unmark(second.id)
    assert signins.marks == []


def test_at_most_so_many_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(known, "MARKS_CAP", 1)
    signins = KnownSignIns(tmp_path / "kept")
    signins.mark(DOMAIN, CAROL, ATTACKER, "n")
    with pytest.raises(Problem, match="undo some"):
        signins.mark(DOMAIN, "b@example.com", ATTACKER, "n")


@pytest.mark.parametrize("data", ["not json", '{"version": 2, "marks": []}', '{"version": 1, "marks": [{"domain": "example.com"}]}', "[" * 200_000,
                                  '{"version": 1, "marks": [{"domain": "example.com", "account": "carol@example.com", "address": "nope", "note": "n", "at": "2026-10-20T12:00:00+00:00"}]}'])
def test_a_file_holding_something_else_is_moved_aside_and_said(tmp_path, data):
    (tmp_path / "kept").mkdir()
    path = tmp_path / "kept" / known.MARKS_FILE
    path.write_text(data)
    again = KnownSignIns(tmp_path / "kept")
    assert again.marks == [] and again.moved_aside and not path.exists() and (tmp_path / "kept" / again.moved_aside).read_text() == data


def test_a_linked_file_is_refused_rather_than_followed(tmp_path):
    (tmp_path / "kept").mkdir()
    secret = tmp_path / "secret"
    secret.write_text('{"version": 1, "marks": []}')
    (tmp_path / "kept" / known.MARKS_FILE).symlink_to(secret)
    with pytest.raises(Problem):
        KnownSignIns(tmp_path / "kept")
    assert secret.exists()


# --- what Find showed gone -----------------------------------------------------------------------------------------------------


def test_what_nobody_has_survives_a_restart_and_the_newest_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(known, "CHECKS_CAP", 2)
    checks = NobodyHasChecks(tmp_path / "kept")
    checks.record(DOMAIN, ["a@mail.example.com"], "admin@example.com", NOW)
    checks.record(DOMAIN, ["b@mail.example.com"], "admin@example.com", NOW + timedelta(hours=1))
    checks.record(DOMAIN, ["c@mail.example.com", "bad id with spaces"], "", NOW + timedelta(hours=2))
    again = NobodyHasChecks(tmp_path / "kept")
    assert [c.message_id for c in again.for_domain(DOMAIN)] == ["c@mail.example.com", "b@mail.example.com"] and again.for_domain("other.example") == []


def test_checking_again_keeps_the_latest(tmp_path):
    checks = NobodyHasChecks(tmp_path / "kept")
    checks.record(DOMAIN, ["a@mail.example.com"], "", NOW)
    checks.record(DOMAIN, ["a@mail.example.com"], "admin@example.com", NOW + timedelta(hours=1))
    assert [(c.at, c.by) for c in checks.checks] == [(NOW + timedelta(hours=1), "admin@example.com")]


# --- Phishing's runs, from the audit log ---------------------------------------------------------------------------------------


def _record(audit: AuditLog, ok=True, target="a@example.com", message=PHISH, run="r1", steps=2, searched=True, domain=DOMAIN, by="admin@example.com", action="trash_message"):
    audit.record(action, target=target, ok=ok, extra=runs.about(message, run, domain, steps, searched, by, "mailbox"))


def test_the_audit_shows_each_email_s_latest_run(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    _record(audit, run="old", ok=False)
    _record(audit, run="new")
    _record(audit, run="new", target="b@example.com", action="trash_spam_messages")
    _record(audit, message="other@mail.example.com", run="x", steps=1, searched=False)
    found = {r.message_id: r for r in runs.latest_runs(audit.path, DOMAIN)}
    assert found[PHISH].is_done and found[PHISH].mailboxes == 2 and found[PHISH].by == "admin@example.com" and found[PHISH].trashed == 2 and found[PHISH].failed == 0
    assert not found["other@mail.example.com"].is_done and "couldn't search every mailbox" in found["other@mail.example.com"].left


def test_the_audit_says_a_failed_and_an_unfinished_step(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    _record(audit, steps=3)
    _record(audit, steps=3, ok=False, target="b@example.com")
    run = runs.latest_runs(audit.path, DOMAIN)[0]
    assert not run.is_done and "1 failed" in run.left and "1 didn't finish" in run.left


@pytest.mark.parametrize("extra", [{}, {"message_id": PHISH, "domain": DOMAIN, "steps": "1"}, {"message_id": PHISH, "run": "r", "domain": DOMAIN, "steps": "0"},
                                   {"message_id": PHISH, "run": "r", "domain": DOMAIN, "steps": "many"}, {"message_id": PHISH, "run": "r", "domain": "other.example", "steps": "1"}])
def test_a_record_that_can_t_say_its_run_is_passed_over(tmp_path, extra):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("trash_message", target="a@example.com", ok=True, extra=extra)
    audit.record("reset_password", target="a@example.com", ok=True, extra={"message_id": PHISH, "run": "r", "domain": DOMAIN, "steps": "1"})
    assert runs.latest_runs(audit.path, DOMAIN) == []


# --- narrowing the logs --------------------------------------------------------------------------------------------------------


def _row(who=CAROL, ip=ATTACKER, app="", agent="", application="login"):
    return logs.Row(f"{who}{ip}{app}", application, "2026-10-19T14:00:00Z", NOW, who, "login_success", ip, (), False, agent, app)


def test_a_filter_keeps_only_what_it_names():
    assert pull.Filter((CAROL.upper(),), (ATTACKER,)).keeps(_row())
    assert not pull.Filter(("b@example.com",)).keeps(_row()) and not pull.Filter((), ("192.0.2.1",)).keeps(_row())
    assert not pull.Filter(apps=("Mail Sync",)).keeps(_row()) and pull.Filter(apps=("Mail Sync",)).keeps(_row(app="Mail Sync"))
    assert not pull.Filter(agent="H").keeps(_row()) and not pull.Filter(applications=("token",)).applied(pull.Log.read("login", [_row()], 72))


def test_a_filter_says_itself_and_a_log_it_leaves():
    words = pull.Filter((CAROL,), (ATTACKER, "192.0.2.1"), ("gmail",), ("Mail Sync",), "the AI agent H").words
    assert words == f"{CAROL}'s events from {ATTACKER} and 192.0.2.1, in Gmail, through Mail Sync, by the AI agent H"
    assert pull.Filter().words == "Every account's events" and not pull.Filter()
    assert pull.Filter((CAROL,)).applied(pull.Log.read("login", [_row(who="b@example.com")], 72)) is None
    unread = pull.Log("login", None, "refused")
    assert pull.Filter((CAROL,)).applied(unread) is unread, "a log that wasn't read stays said"


# --- through the screens, on the mock ------------------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    st = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    st.incident_store = IncidentStore(tmp_path / "incidents")
    st.incident_known = KnownSignIns(tmp_path / "incidents")
    st.incident_nobody = NobodyHasChecks(tmp_path / "incidents")
    return st


@pytest.fixture
def client(state):
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


ALL = ("login", "token", "admin", "gmail", "contacts", "mobile")
ON_INCIDENT = {"HX-Current-URL": "http://testserver/incident"}


def _pull(client, state, *wanted, accounts=CAROL):
    client.post("/incident/logs/pull", data={"accounts": accounts, "hours": 72, "log": list(wanted or ALL)})
    wait_for_job(client, state.jobs[state.incident_pull_job])


def _read(client, part="respond") -> str:
    return unescape(client.get(f"/incident/logs/reading?part={part}").text)


def _reading(client, state) -> reader.Reading:
    return client.portal.call(incident_logs._current_reading, state)


def _task(client, state, kind) -> reader.Task:
    return next(t for t in _reading(client, state).to_do if t.kind == kind)


def _open_new(client, state, title="Carol's account"):
    client.post("/incident/kept/new", data={"title": title})
    job = state.jobs.get(state.incident_open_job)
    if job:
        wait_for_job(client, job)
    return state.incident_store.get(state.incident_open)


def test_each_incident_screen_says_where_it_is_and_what_is_open(client, state):
    for path in ("/incident/kept", "/incident/logs", "/incident"):
        page = client.get(path).text
        assert f'<a href="{path}" aria-current="page"' in page and 'aria-label="Incident"' in page, path
    _open_new(client, state, "<b>Carol's</b>")
    for path in ("/incident/kept", "/incident/logs", "/incident"):
        page = client.get(path).text
        assert "Open: <a" in page and "&lt;b&gt;Carol" in page and "<b>Carol" not in page, path


def test_respond_reads_the_pull_only_once_there_is_one(client, state):
    page = client.get("/incident").text
    assert "Pull the logs</a> to see where each account stands" in page and "part=respond" not in page
    _pull(client, state)
    assert 'hx-get="/incident/logs/reading?part=respond"' in client.get("/incident").text


def test_each_part_shows_its_own(client, state):
    _pull(client, state)
    respond, findings = _read(client), _read(client, "findings")
    assert "Where each account stands" in respond and "Still to do" in respond and "What the logs show" not in respond and "data-focus" not in respond
    assert "What the logs show" in findings and "Still to do" not in findings and "Respond</a> has where each account stands" in findings and "data-focus" in findings
    every = _read(client, "")
    assert "Still to do" in every and "What the logs show" in every


def test_the_reading_is_worked_out_once_until_what_it_knows_changes(client, state, monkeypatch):
    _pull(client, state)
    real = reader.read
    calls = []
    monkeypatch.setattr(reader, "read", lambda events, context: (calls.append(1), real(events, context))[1])
    _read(client)
    _read(client, "findings")
    assert len(calls) == 1
    state.incident_known.mark(DOMAIN, CAROL, ATTACKER, "n")
    _read(client)
    assert len(calls) == 2, "a mark changes what it says"
    _record(state.connector.audit)
    _read(client)
    assert len(calls) == 3, "so does a run, back from Phishing"
    assert client.portal.call(incident_logs._current_reading, state) is not None and len(calls) == 3


# That was them


def test_that_was_them_is_offered_where_google_marked_it_and_makes_no_suspect(client, state):
    _pull(client, state)
    findings = _read(client, "findings")
    assert "That was them" in findings and f"How do you know it was {CAROL} from {ATTACKER}?" in findings
    assert any(t.kind == "sweep" for t in _reading(client, state).to_do)
    page = unescape(client.post("/incident/logs/theirs", data={"account": CAROL, "address": ATTACKER, "note": "Her new phone", "part": "findings"}).text)
    assert "Marked as theirs" in page and "Her new phone" in page and "Undo" in page
    assert reader.key(CAROL, ATTACKER) in state.incident_reading[0][2].theirs
    assert not any(ATTACKER in f.addresses and f.kind == "suspectAddress" for f in _reading(client, state).findings)
    mark = state.incident_known.marks[0]
    page = unescape(client.post("/incident/logs/unmark", data={"mark": mark.id, "part": "findings"}).text)
    assert "Marked as theirs" not in page and state.incident_known.marks == []
    assert any(ATTACKER in f.addresses and f.kind == "suspectAddress" for f in _reading(client, state).findings)


def test_a_mark_that_can_t_be_kept_is_said_and_another_domain_s_is_left(client, state):
    _pull(client, state)
    assert "Say how you know" in _read_post(client, "/incident/logs/theirs", {"account": CAROL, "address": ATTACKER, "note": " "})
    assert "isn't one the logs ask about" in _read_post(client, "/incident/logs/theirs", {"account": CAROL, "address": "x", "note": "n"})
    theirs = state.incident_known.mark("other.example", "b@other.example", ATTACKER, "n")
    _read_post(client, "/incident/logs/unmark", {"mark": theirs.id})
    assert state.incident_known.marks == [theirs]
    assert "b@other.example" not in _read(client, "findings")


def _read_post(client, path, data) -> str:
    return unescape(client.post(path, data=data).text)


def test_what_a_person_typed_is_only_ever_text(client, state):
    _pull(client, state)
    page = client.post("/incident/logs/theirs", data={"account": CAROL, "address": ATTACKER, "note": "<script>x</script>", "part": "findings"}).text
    assert "<script>x" not in page and "&lt;script&gt;x" in page


def test_a_list_that_won_t_open_is_said_and_the_reading_still_shows(client, state, monkeypatch):
    from gamgui.web.routes import _common

    def refuses(*args):
        raise Problem("its folder can't be read")

    state.incident_known = None
    monkeypatch.setattr(_common, "KnownSignIns", refuses)
    _pull(client, state)
    page = _read(client)
    assert "Sign-ins can't be marked as theirs: its folder can't be read" in page and "Still to do" in page
    assert "Sign-ins can't be marked" in _read_post(client, "/incident/logs/theirs", {"account": CAROL, "address": ATTACKER, "note": "n"})


def test_a_list_moved_aside_is_said(client, state, tmp_path):
    (tmp_path / "incidents" / known.MARKS_FILE).write_text("not json")
    state.incident_known = KnownSignIns(tmp_path / "incidents")
    _pull(client, state)
    assert "was moved aside as known-sign-ins.json.unreadable-" in _read(client)


# Marked done by hand


def _tick_form(task, how="By phone with her"):
    return {"kind": task.kind, "words": task.words, "basis": task.basis, "how": how, "part": "respond"}


def test_mark_done_needs_an_incident_open(client, state):
    _pull(client, state)
    page = _read(client)
    assert "Mark done" not in page and "Keep an incident</a> to mark an item done" in page
    assert "Open an incident first" in _read_post(client, "/incident/logs/tick", _tick_form(_task(client, state, "removeAdded")))


def test_marked_done_moves_it_to_done_with_how_when_and_who_and_undo_brings_it_back(client, state, monkeypatch):
    monkeypatch.setattr(state.vault, "oauth_admin_email", lambda domain: "admin@example.com")
    record = _open_new(client, state)
    _pull(client, state)
    task = _task(client, state, "removeAdded")
    assert "Mark done" in _read(client)
    page = _read_post(client, "/incident/logs/tick", _tick_form(task))
    assert "Done by hand: By phone with her." in page and "by admin@example.com" in page and "kept with the incident" in page
    held = state.incident_store.get(record.id)
    assert [(t.kind, t.how, t.by) for t in held.ticks] == [("removeAdded", "By phone with her", "admin@example.com")]
    assert not any(t.kind == "removeAdded" for t in incident_logs._ticked(state, _reading(client, state), held).to_do)
    page = _read_post(client, "/incident/logs/untick", {"kind": task.kind, "words": task.words, "basis": task.basis, "part": "respond"})
    assert "Done by hand" not in page and state.incident_store.get(record.id).ticks == ()


def test_a_lock_a_read_or_what_the_logs_changed_can_t_be_marked_done(client, state):
    record = _open_new(client, state)
    _pull(client, state)
    sweep = _task(client, state, "sweep")
    assert not sweep.tickable
    assert "can't be marked done" in _read_post(client, "/incident/logs/tick", _tick_form(sweep))
    assert "A lock, recovery details cleared, a computer" in _read(client)
    changed = _task(client, state, "removeAdded")
    assert "can't be marked done" in _read_post(client, "/incident/logs/tick", {**_tick_form(changed), "basis": "another"})
    assert "Say how it was done" in _read_post(client, "/incident/logs/tick", _tick_form(changed, how="  "))
    assert state.incident_store.get(record.id).ticks == ()


# Done from Phishing


def test_an_email_phishing_pulled_shows_done_with_when_and_who(client, state):
    _pull(client, state)
    assert PHISH in " ".join(_reading(client, state).emails_to_pull)
    _record(state.connector.audit)
    _record(state.connector.audit, target="b@example.com")
    assert not any(PHISH in e for e in _reading(client, state).emails_to_pull)
    page = _read(client)
    assert "as this app's audit log shows" in page and "by admin@example.com" in page


def test_a_run_that_didn_t_finish_stays_to_do_and_says_so(client, state):
    _pull(client, state)
    _record(state.connector.audit)
    _record(state.connector.audit, ok=False, target="b@example.com")
    task = _task(client, state, "pullEmail")
    assert "1 failed" in task.words


def test_a_phishing_run_records_what_makes_it_done(client, state, tmp_path):
    from .test_phishing import PHISH as ONE
    from .test_phishing import _find, _token

    _find(client, state, ONE)
    count = state.phishing_found.step_count
    confirm = client.post("/phishing/trash/preview").text
    client.post("/phishing/trash/apply", data={"confirmed": "1", "preview": _token(confirm), "confirm_count": str(count)})
    wait_for_job(client, next(j for j in state.jobs.values() if j.kind == "phishing"))
    run = next(r for r in runs.latest_runs(state.connector.audit.path, DOMAIN) if r.message_id == ONE)
    assert run.is_done and run.steps == count and run.trashed == count and run.failed == 0


def test_find_showing_nobody_has_it_is_kept_and_shown_done(client, state):
    from .test_phishing import _find

    _pull(client, state)
    assert _task(client, state, "pullEmail")
    _find(client, state, PHISH)
    assert [c.message_id for c in state.incident_nobody.for_domain(DOMAIN)] == [PHISH]
    assert not _reading(client, state).emails_to_pull
    assert "as Phishing's Find showed" in _read(client)


def test_find_that_can_t_be_kept_is_said_on_respond(client, state, monkeypatch):
    from .test_phishing import _find

    def refuses(*args, **kwargs):
        raise Problem("no room")

    monkeypatch.setattr(NobodyHasChecks, "record", refuses)
    _pull(client, state)
    _find(client, state, PHISH)
    assert "What Phishing's Find showed gone couldn't be kept: no room" in _read(client)


def test_find_found_something_is_never_kept_as_gone(client, state):
    from .test_phishing import PHISH as ONE
    from .test_phishing import _find

    _find(client, state, ONE)
    assert state.phishing_found.with_copies and [c.gone for c in state.incident_nobody.checks] == [False]


# Do It Here


def _checked(html: str) -> set:
    import re

    return {m.group(1) for m in re.finditer(r'name="switch" value="(\w+)"[^>]* checked', html)}


def _check(client, state, person=CAROL, query=""):
    client.post(f"/incident/check{query}", data={"person": person})
    wait_for_job(client, state.jobs[state.incident_check_job])
    return client.get(f"/incident/check/status?job={state.incident_check_job}", headers=ON_INCIDENT).text


def test_do_it_here_links_to_that_one_fix():
    def link(draft):
        task = reader.Task("1", "clearRecovery", "Do it", drafts=(draft,))
        reading = reader.Reading((), (), (task,), (), 1, (), None)
        return unescape(TEMPLATES.env.get_template("_incident_reading.html").render(reading=reading, part="respond", when=str, store_problems=[], marks=[], shows_query=str))

    assert "/incident?person=carol%40example.com&fix=clear_recovery" in link(reader.Draft("undo", account=CAROL, pick="clearRecovery"))
    assert "Do It Here" not in link(reader.Draft("undo", account=CAROL, pick="computer")), "it would open the whole lock ticked, the opposite of what it says"
    assert "/incident?person=carol%40example.com" in link(reader.secure_draft(CAROL))


def test_do_it_here_asks_its_check_with_only_that_fix_picked(client, state):
    assert "/incident/check?pick=clear_recovery" in client.get(f"/incident?person={CAROL}&fix=clear_recovery").text
    assert _checked(_check(client, state, query="?pick=clear_recovery")) == {"clear_recovery"}


def test_do_it_here_on_the_check_shown_picks_it_without_reading_again(client, state, gam_calls):
    assert "clear_recovery" not in _checked(_check(client, state))
    before = len(gam_calls())
    page = client.get(f"/incident?person={CAROL}&fix=clear_recovery").text
    assert _checked(page) == {"clear_recovery"} and len(gam_calls()) == before


@pytest.mark.parametrize("then", ["by hand", "another person", "no fix asked"])
def test_a_pick_waits_only_for_its_own_check(client, state, then):
    _check(client, state, query="?pick=clear_recovery")
    if then == "by hand":
        page = _check(client, state)
    elif then == "another person":
        page = _check(client, state, person="alice@example.com")
    else:
        page = client.get(f"/incident?person={CAROL}").text
    assert "clear_recovery" not in _checked(page) and "password" in _checked(page)


def test_a_pick_that_isn_t_a_fix_is_passed_over(client, state):
    assert "?pick=" not in client.get(f"/incident?person={CAROL}&fix=rm%20-rf").text
    assert "password" in _checked(_check(client, state, query="?pick=nothing"))


# Show the events


def test_show_the_events_narrows_the_logs_and_says_so(client, state):
    _pull(client, state)
    task = _task(client, state, "removeAdded")
    query = pull.Filter(task.shows.accounts, task.shows.addresses, task.shows.logs, task.shows.apps, task.shows.agent).query
    assert f"/incident/logs?{query}" in _read(client).replace("&amp;", "&")
    page = unescape(client.get(f"/incident/logs?{query}").text)
    assert f"Showing only {CAROL}'s events from {ATTACKER}" in page and 'href="/incident/logs"' in page and "Show all" in page
    everything = client.get("/incident/logs").text
    assert "Showing only" not in everything


def test_each_log_s_csv_then_saves_just_those_events(client, state):
    _pull(client, state)
    whole = client.get("/incident/logs/log.csv?application=login").text
    narrowed = client.get(f"/incident/logs/log.csv?application=login&from={ATTACKER}").text
    assert len(narrowed.splitlines()) < len(whole.splitlines()) and all(ATTACKER in line for line in narrowed.splitlines()[1:])
    assert f"application=login&amp;from={ATTACKER}" in client.get(f"/incident/logs?from={ATTACKER}").text
    assert client.get("/incident/logs/log.csv?application=login&from=192.0.2.200").status_code == 404, "never an empty CSV that passes as no events"


def test_events_no_log_holds_are_said(client, state):
    _pull(client, state, "login")
    assert "None of the logs shown hold those events" in client.get("/incident/logs?from=192.0.2.200").text


def test_the_logs_text_in_the_address_stays_text(client, state):
    _pull(client, state, "login")
    page = client.get('/incident/logs?app="><script>x</script>').text
    assert "<script>x" not in page and "&lt;script&gt;x" in page


# Pull again


def _again(client, state, accounts="alice@example.com bob@example.com", log=("gmail",)):
    r = client.post("/incident/logs/again", data={"accounts": accounts, "log": list(log), "part": "respond"})
    if r.status_code == 204:
        wait_for_job(client, state.jobs[state.incident_pull_job])
    return r


def test_pull_again_is_offered_and_opens_the_logs(client, state):
    _pull(client, state)
    assert "Pull again" in _read(client) and _task(client, state, "pullAgain").pulls_again == ("gmail",)
    r = _again(client, state)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/incident/logs"


def test_pull_again_adds_to_what_s_shown(client, state, gam_calls):
    _pull(client, state)
    before = {log.application: len(log.rows) for log in state.incident_pull.logs}
    _again(client, state)
    assert not state.incident_pull.from_evidence
    assert {log.application for log in state.incident_pull.logs} >= set(before), "a narrower read never takes away what the wider one found"
    assert len(state.incident_pull.log("login").rows) == before["login"] and len(state.incident_pull.log("gmail").rows) >= before["gmail"]
    read = [a for a in gam_calls() if a[:2] == ["report", "gmail"]][-1]
    assert "alice@example.com,bob@example.com" in read


def test_pull_again_with_an_incident_open_is_saved_to_it(client, state):
    record = _open_new(client, state)
    _pull(client, state)
    _again(client, state)
    assert len(state.incident_store.names(record.id)) == 2 and state.incident_pull.from_evidence and state.incident_pull.log("login").rows


def test_pull_again_reads_only_those_in_the_directory_and_says_when_none_are(client, state, gam_calls):
    _pull(client, state)
    assert "None of those accounts" in _again(client, state, accounts="nobody@example.com").text
    _again(client, state, accounts="nobody@example.com alice@example.com")
    assert "alice@example.com" in [a for a in gam_calls() if a[:2] == ["report", "gmail"]][-1]
    assert not [a for a in gam_calls() if "nobody@example.com" in " ".join(a)]


def test_pull_again_needs_a_pull_and_waits_for_one_going_on(client, state):
    from gamgui.web.jobs import start_job

    assert "pull the logs first" in _again(client, state).text
    _pull(client, state)
    assert "Pull the logs first" in _again(client, state, log=("not a log",)).text
    state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
    assert "A pull is going on" in _again(client, state).text
    state.jobs[state.incident_pull_job].finish()


# Across a restart


def _restart(state, tmp_path):
    state.incident_open = ""
    state.incident_pull = None
    state.incident_reading = None
    state.incident_store = IncidentStore(tmp_path / "incidents")


def test_respond_after_a_restart_reads_what_the_incident_saved(client, state, tmp_path, gam_calls):
    _open_new(client, state)
    _pull(client, state)
    _restart(state, tmp_path)
    before = len(gam_calls())
    page = client.get("/incident").text
    assert 'hx-get="/incident/logs/reading?part=respond"' in page, "its saved logs are on their way"
    assert "Still to do" in _read(client)
    assert not [a for a in gam_calls()[before:] if a[0] == "report"]


def test_the_logs_screen_after_a_restart_says_its_saved_logs_are_being_read(client, state, tmp_path, monkeypatch):
    from gamgui.web.jobs import start_job

    _open_new(client, state)
    _pull(client, state, "login")
    state.incident_open_job = start_job(state.jobs, 1, kind="incident_open", title="Read the incident's saved logs").id
    page = client.get("/incident/logs").text
    assert "Read the incident&#39;s saved logs" in page or "Read the incident's saved logs" in page
    job = state.jobs[state.incident_open_job]
    assert f"/incident/logs/status?job={job.id}" in page
    job.finish()
    assert "Saved in the incident" in client.get(f"/incident/logs/status?job={job.id}").text


def test_pull_all_asks_phishing_for_every_email_left(client, state):
    _pull(client, state)
    page = client.get("/phishing?message=%3Ca%40mail.example.com%3E%0A%3Cb%40mail.example.com%3E").text
    assert "a@mail.example.com" in page and "b@mail.example.com" in page


def test_pull_all_asks_for_as_many_as_phishing_takes_at_once():
    def rendered(most):
        tasks = tuple(reader.Task(str(i), "pullEmail", f"Pull {i}", drafts=(reader.pull_draft(f"m{i}@mail.example.com", "s"),)) for i in range(3))
        reading = reader.Reading((), (), tasks, (), 1, (), None)
        return unescape(TEMPLATES.env.get_template("_incident_reading.html").render(reading=reading, part="respond", when=str, store_problems=[], marks=[], shows_query=str, most_emails=most))

    assert "Pull all 3 emails" in rendered(100)
    two = rendered(2)
    assert "Pull the first 2 emails" in two and "m1%40mail.example.com" in two.split("Pull the first")[0] and "m2%40mail.example.com" not in two.split("Pull the first")[0].split("/phishing?message=")[-1]


# What the review of this slice proved: an incident looking further along than it is


def test_a_later_find_showing_a_copy_takes_back_that_it_was_gone(client, state):
    _pull(client, state)
    state.incident_nobody.record(DOMAIN, [PHISH], "", NOW)
    assert not _reading(client, state).emails_to_pull
    state.incident_nobody.record(DOMAIN, [], "", NOW + timedelta(days=30), not_gone=[PHISH])
    assert "Phishing's Find last showed a mailbox with it" in _task(client, state, "pullEmail").words
    assert PHISH in " ".join(_reading(client, state).emails_to_pull) and "Pull" in _read(client)


def test_a_later_find_takes_back_a_run_and_a_later_run_settles_it(client, state):
    _pull(client, state)
    _record(state.connector.audit, steps=1)     # the audit stamps now, later than any fixture's event
    assert not _reading(client, state).emails_to_pull
    state.incident_nobody.record(DOMAIN, [], "", datetime.now(timezone.utc) + timedelta(hours=1), not_gone=[PHISH])
    assert _reading(client, state).emails_to_pull, "stopped before its first step, a later run writes nothing: the Find is the latest word"
    state.incident_nobody.record(DOMAIN, [PHISH], "", datetime.now(timezone.utc) + timedelta(hours=2))
    assert not _reading(client, state).emails_to_pull


def test_the_log_showing_it_after_the_last_look_puts_it_back(client, state, monkeypatch):
    before_it = datetime(2026, 10, 1, tzinfo=timezone.utc)
    _pull(client, state)
    state.incident_nobody.record(DOMAIN, [PHISH], "", before_it)
    assert "The log shows it after Phishing last looked" in _task(client, state, "pullEmail").words
    state.incident_nobody.record(DOMAIN, [PHISH], "", NOW)
    assert not _reading(client, state).emails_to_pull


def test_a_run_older_than_the_log_doesn_t_count_either(client, state, monkeypatch):
    before_it = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(runs, "latest_runs", lambda *args: [reader.Pulled(PHISH, before_it, "a", 1, 1, 0, 1, True)])
    _pull(client, state)
    assert "The log shows it after Phishing last looked" in _task(client, state, "pullEmail").words


def test_do_it_here_s_pick_never_reaches_the_form_a_person_checks_with(client, state):
    import re

    page = client.get(f"/incident?person={CAROL}&fix=clear_recovery").text
    form = re.search(r'<form[^>]*hx-post="([^"]*)"', page[page.index('name="person"') - 400:]).group(1)
    assert form == "/incident/check"
    assert 'hx-post="/incident/check?pick=clear_recovery"' in page and 'hx-trigger="load"' in page
    page = _check(client, state, query="?pick=clear_recovery")
    assert _checked(page) == {"clear_recovery"} and "The rest of the lock is off" in page
    page = _check(client, state, person="alice@example.com", query=form[len("/incident/check"):])
    assert "password" in _checked(page) and "The rest of the lock is off" not in page


def test_a_run_going_on_drops_the_pick(client, state):
    from gamgui.web.jobs import start_job

    _check(client, state)
    state.incident_run_job = start_job(state.jobs, 1, kind="incident_secure", title="x").id
    assert "?pick=" not in client.get("/incident?person=alice@example.com&fix=clear_recovery").text
    assert "clear_recovery" not in _checked(client.get(f"/incident?person={CAROL}&fix=clear_recovery").text)
    client.post("/incident/check?pick=clear_recovery", data={"person": CAROL})
    assert state.incident_pick is None
    state.jobs[state.incident_run_job].finish()


def test_pull_again_keeps_what_was_on_screen_when_the_evidence_doesn_t_hold_it(client, state):
    _pull(client, state, accounts="")
    wide = {log.application: len(log.rows) for log in state.incident_pull.logs}
    empty = state.incident_store.create(DOMAIN, "Empty", NOW)
    client.post("/incident/kept/open", data={"incident": empty.id})
    wait_for_job(client, state.jobs[state.incident_open_job])
    assert not state.incident_pull.from_evidence, "left on screen"
    _again(client, state, accounts="alice@example.com")
    assert {a for a in wide if state.incident_pull.log(a) and state.incident_pull.log(a).rows} == {a for a, n in wide.items() if n}
    assert len(state.incident_store.names(empty.id)) == 2, "saved with it"


def test_pull_again_keeps_it_when_it_can_t_be_saved_either(client, state, monkeypatch):
    _pull(client, state, accounts="")
    empty = state.incident_store.create(DOMAIN, "Empty", NOW)
    client.post("/incident/kept/open", data={"incident": empty.id})
    wait_for_job(client, state.jobs[state.incident_open_job])
    wide = {log.application for log in state.incident_pull.logs if log.rows}

    def full(*args, **kwargs):
        raise Problem("no room")

    monkeypatch.setattr(IncidentStore, "keep", full)
    _again(client, state, accounts="alice@example.com")
    assert {log.application for log in state.incident_pull.logs if log.rows} >= wide
    assert "wasn't saved to the incident: no room" in state.jobs[state.incident_pull_job].error


def test_pull_again_reads_the_window_shown_and_names_who_it_left_out(client, state, gam_calls):
    _pull(client, state)
    end = state.incident_pull.at
    _again(client, state, accounts="nobody@example.com alice@example.com")
    read = [a for a in gam_calls() if a[:2] == ["report", "gmail"]][-1]
    assert read[read.index("end") + 1] == logs.report_time(end) and read[read.index("start") + 1] == logs.report_time(end - timedelta(hours=72))
    assert "Not in the directory, so not read: nobody@example.com." in state.jobs[state.incident_pull_job].error
    assert state.incident_pull.at == end


def _with_cap(state, covered=()):
    held = state.incident_pull
    login = held.log("login")
    held.logs = [pull.Log.read("login", login.rows, login.hours, True, None, covered) if log.application == "login" else log for log in held.logs]
    state.incident_reading = None


def test_the_pull_again_item_stays_for_those_a_narrow_read_didn_t_cover(client, state):
    _pull(client, state)
    context_capped = datetime(2026, 11, 1, tzinfo=timezone.utc)
    events = [reader.event_from_row(r) for log in state.incident_pull.logs for r in log.rows or []]
    def to_do(covered):
        return [t for t in reader.read(events, reader.Context(capped={"login": context_capped}, covered=covered)).to_do if t.kind == "pullAgain"]
    assert to_do({})[0].accounts == (CAROL,) and to_do({"login": {"other@example.com"}})
    assert to_do({"login": {CAROL}}) == [], "every account that matters read past the cap: it clears"


def test_nothing_reads_or_exports_a_pull_still_being_saved(client, state):
    from gamgui.web.jobs import start_job

    _pull(client, state)
    assert client.get("/incident/logs/export.zip").status_code == 200
    state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
    assert "A pull of the logs is going on" in _read(client)
    assert client.get("/incident/logs/export.zip").status_code == 404
    state.jobs[state.incident_pull_job].finish()
    assert "Still to do" in _read(client)


def test_a_narrowed_csv_says_so_in_its_name(client, state):
    _pull(client, state)
    whole = client.get("/incident/logs/log.csv?application=login").headers["Content-Disposition"]
    narrowed = client.get(f"/incident/logs/log.csv?application=login&from={ATTACKER}").headers["Content-Disposition"]
    assert whole.endswith("filename=login-log.csv") and narrowed.endswith("filename=login-log-narrowed.csv")


def test_narrowing_keeps_saying_a_log_stopped_short(client, state):
    _pull(client, state, "login")
    _with_cap(state)
    page = client.get("/incident/logs?from=192.0.2.200").text
    assert "None of the logs shown hold those events" not in page and "Sign-ins" in page and "past the cap" in page
    assert client.get("/incident/logs/log.csv?application=login&from=192.0.2.200").status_code == 404, "never an empty CSV that passes as no events"


def test_that_was_them_takes_only_a_pair_the_logs_ask_about(client, state):
    _pull(client, state)
    for account, address in ((CAROL, "192.0.2.77"), ("bob@example.com", ATTACKER)):
        assert "isn't one the logs ask about" in _read_post(client, "/incident/logs/theirs", {"account": account, "address": address, "note": "n"})
    assert state.incident_known.marks == []
    assert "Marked as theirs" in _read_post(client, "/incident/logs/theirs", {"account": CAROL.upper(), "address": ATTACKER, "note": "n", "part": "findings"})


def test_respond_never_says_nothing_is_suspect_beside_a_finding():
    finding = reader.Finding("1", "suspectAddress", "Google called it suspicious", (CAROL,), (ATTACKER,), 1, ())
    page = TEMPLATES.env.get_template("_incident_reading.html").render(reading=reader.Reading((finding,), (), (), (), 1, (), None), part="respond", when=str, store_problems=[], marks=[], shows_query=str)
    assert "Nothing here is suspect" not in page
