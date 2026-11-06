"""Incident logs (back-port plan E2): the reports in one row shape, a log read whole, oldest first, and the screen. Ported from SwiftGamGui's ActivityLogsTests
and IncidentLogsTests. The mock's logs: carol@'s sign-ins include a flagged one from 198.51.100.23, and the same address granted an app, read a message and
sent the phish."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands, is_report_time
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident import logs, pull
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
ATTACKER = "198.51.100.23"
UTC = timezone.utc


def _row(when: datetime, who: str = CAROL, n: int = 0) -> logs.Row:
    time = logs.report_time(when)
    return logs.Row(f"{time}|{who}|{n}", "login", time, when, who, "login_success", "", (), False)


# --- the builder ----------------------------------------------------------------------------------------------------------


def test_the_one_two_and_every_account_shapes():
    c = GAMCommands
    assert c.report_activity("login", hours=72) == ["report", "login", "user", "all", "start", "-72h", "end", "now", "maxactivities", "1000"]
    assert c.report_activity("login", [CAROL], 72)[2:4] == ["user", CAROL]
    assert c.report_activity("login", [CAROL, "a@b.c"], 72)[2:5] == ["select", "users", f"{CAROL},a@b.c"]


def test_a_window_is_two_times_or_neither():
    argv = GAMCommands.report_activity("login", hours=24, start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert argv[argv.index("start") + 1] == "2026-10-19T00:00:00Z" and argv[argv.index("end") + 1] == "2026-10-20T00:00:00Z"
    for start, end in (("2026-10-19T00:00:00Z", ""), ("", "2026-10-19T00:00:00Z"), ("2026-10-20T00:00:00Z", "2026-10-19T00:00:00Z"),
                       ("2026-13-19T00:00:00Z", "2026-13-20T00:00:00Z"), ("yesterday", "today")):
        with pytest.raises(ValueError):
            GAMCommands.report_activity("login", start=start, end=end)
    assert is_report_time("2026-10-19T00:00:00Z") and not is_report_time("2026-02-30T00:00:00Z")


def test_chrome_reads_one_account_either_way():
    assert GAMCommands.report_activity("chrome", [CAROL.upper()], 72, chrome="profile")[2:6] == ["user", "all", "filter", f"PROFILE_USER_NAME=={CAROL}"]
    assert GAMCommands.report_activity("chrome", [CAROL], 72, chrome="device")[2:4] == ["user", CAROL]
    with pytest.raises(ValueError):
        GAMCommands.report_activity("chrome", [CAROL, "a@b.c"], 72)


def test_the_admin_log_reads_an_account_as_actor_or_as_affected():
    assert "userisactor" in GAMCommands.report_activity("admin", [CAROL], 72, admin="actor")
    assert "userisactor" not in GAMCommands.report_activity("admin", [CAROL], 72, admin="affected")
    assert "userisactor" not in GAMCommands.report_activity("admin", (), 72)


@pytest.mark.parametrize("kw", [{"application": "gplus"}, {"application": "gmail", "hours": 721}, {"application": "login", "hours": 0},
                                {"application": "login", "hours": 4321}, {"application": "login", "users": ["a b@c.d"]},
                                {"application": "login", "users": ["Name <a@b.c>"]}, {"application": "login", "users": [f"u{i}@e.com" for i in range(21)]},
                                {"application": "login", "ip": "not-an-ip"}, {"application": "login", "events": ["a,b"]}, {"application": "login", "max_activities": 0}])
def test_what_gam_would_misread_never_reaches_it(kw):
    with pytest.raises(ValueError):
        GAMCommands.report_activity(**kw)


# --- the rows -------------------------------------------------------------------------------------------------------------


def _fixture(application: str):
    return logs.rows_from((FIXTURES / f"report_{application}.csv").read_text(), application)


def test_a_sign_in_row_in_the_one_shape():
    rows = _fixture("login")
    flagged = [r for r in rows if r.flagged and r.ip == ATTACKER]
    assert flagged and all(r.who for r in rows)
    success = next(r for r in rows if r.event == "login_success")
    assert success.words == "Signed in" and success.date is not None and "by password" in success.details[0]


@pytest.mark.parametrize("application", ["accessevaluation", "admin", "chat", "chrome", "contacts", "drive", "geminiinworkspaceapps", "gmail", "login", "mobile",
                                         "saml", "takeout", "token", "useraccounts"])
def test_every_fixture_log_reads_with_words_for_each_event(application):
    rows = _fixture(application)
    assert rows and all(r.words and r.time and r.date for r in rows)
    assert len({r.identity for r in rows}) == len(rows)


def test_a_gmail_event_is_named_by_its_nested_type():
    # Every Gmail event is `delivery`: what happened is event_info.mail_event_type, flattened under parameters.N.
    rows = _fixture("gmail")
    assert {r.event for r in rows} == {"delivery"}
    words = {r.words for r in rows}
    assert "Sent a message" in words and "Viewed a message" in words and "Received a message" in words
    sent = next(r for r in rows if r.words == "Sent a message")
    assert sent.value("event_info.mail_event_type") == "1"


def test_an_unknown_gmail_type_is_said_never_a_guess():
    assert logs.gmail_words("99") == "A Gmail event (type 99)" and logs.gmail_words("") == "A Gmail event"


def test_nested_parameters_go_back_in_numeric_order_and_a_count_is_never_one():
    # GAM sorts its titles as text, so `.10` comes before `.2`.
    header = "name,id.time,parameters,parameters.0.name,parameters.0.value,parameters.10.name,parameters.10.value,parameters.2.name,parameters.2.multiValue,parameters.2.multiValue.0,parameters.2.multiValue.1"
    row = "delivery,2026-10-19T00:00:00Z,3,first,a,last,c,list,2,x,y"
    parsed = logs.rows_from(f"{header}\n{row}\n", "drive")[0]
    assert parsed.parameters == (("first", "a"), ("list", "x y"), ("last", "c"))


def test_an_event_with_no_actor_is_about_the_account_it_names():
    # Google's own account warnings have no actor, and where there is one, the same parameter names someone else (a blocked sender).
    no_actor = logs.rows_from("name,id.time,actor.email,affected_email_address\naccount_disabled_hijacked,2026-10-19T00:00:00Z,,c@e.com\n", "login")[0]
    assert no_actor.who == "c@e.com" and no_actor.flagged
    blocked = logs.rows_from("name,id.time,actor.email,affected_email_address\nblocked_sender,2026-10-19T00:00:00Z,a@e.com,x@y.z\n", "login")[0]
    assert blocked.who == "a@e.com" and "blocked: x@y.z" in blocked.details


def test_who_acted_besides_the_person_is_said():
    row = logs.rows_from("name,id.time,actor.email,isAgenticAction,actor.agentAttributionInfo.agentName,actor.applicationInfo.applicationName,actor.applicationInfo.impersonation\n"
                         "x,2026-10-19T00:00:00Z,a@e.com,True,Helper,Mail Sync,True\n", "drive")[0]
    assert row.via == "by the AI agent Helper; through Mail Sync (acting as the person)"
    device = logs.rows_from("name,id.time,actor.email,actor.applicationInfo.applicationName\nx,2026-10-19T00:00:00Z,a@e.com,Device\n", "mobile")[0]
    assert device.app == ""     # the Devices log names the device itself "Device": no app acted


def test_addresses_put_the_flagged_first_then_by_how_often():
    t = datetime(2026, 10, 19, tzinfo=UTC)
    plain = logs.Row("1", "login", "", t, CAROL, "x", "203.0.113.1", (), False)
    bad = logs.Row("2", "login", "", t, CAROL, "x", ATTACKER, (), True)
    assert logs.addresses([plain, plain, plain, bad]) == [ATTACKER, "203.0.113.1"]


def test_accounts_as_typed_split_and_keep_each_once():
    assert logs.accounts_typed(" a@e.com, A@E.com\nb@e.com  c@e.com,") == ["a@e.com", "b@e.com", "c@e.com"]


def test_the_export_table_has_a_row_per_event_in_the_one_shape():
    rows = _fixture("token")
    table = logs.table(rows)
    assert table[0] == list(logs.TABLE_HEADER) and len(table) == len(rows) + 1


# --- reading a log whole, with a stand-in that keeps the window as Google does ------------------------------------------


class _Google:
    """Keeps events inside the window, newest first, the end's own second left out, at most `cap` a read per account: the strict guess."""

    def __init__(self, rows, cap, fail_after=None):
        self.rows, self.cap, self.reads, self.fail_after = logs.newest_first(rows), cap, 0, fail_after

    async def __call__(self, users, since, until):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise GAMError.from_run(1, "ERROR: backendError", ["report"])
        lo, hi = logs.parse_time(since), logs.parse_time(until)
        kept = [r for r in self.rows if lo <= r.date < hi and (not users or r.who in users)]
        if len(users) <= 1:
            return kept[:self.cap]
        return [r for u in users for r in [x for x in kept if x.who == u][:self.cap]]


START = datetime(2026, 10, 1, tzinfo=UTC)


def _complete(google, days=3, **kw):
    return asyncio.run(pull.complete(kw.pop("users", ()), START, START + timedelta(days=days), google.cap, google, **kw))


def test_a_log_short_of_the_cap_is_one_read():
    google = _Google([_row(START + timedelta(hours=i), n=i) for i in range(5)], 10)
    rows, through = _complete(google)
    assert len(rows) == 5 and through is None and google.reads == 1


def test_a_log_at_the_cap_is_read_whole_a_day_at_a_time():
    events = [_row(START + timedelta(hours=i), n=i) for i in range(60)]    # 2.5 days, 24 a day, cap 10
    google = _Google(events, 10)
    rows, through = _complete(google)
    assert through is None and {r.identity for r in rows} == {e.identity for e in events}
    assert [r.date for r in rows] == sorted((r.date for r in rows), reverse=True)    # newest first, as one read gives them


def test_past_the_budget_it_stops_at_that_day_and_the_older_days_are_whole():
    events = [_row(START + timedelta(hours=i), n=i) for i in range(60)]
    rows, through = _complete(_Google(events, 10), budget=5)
    assert through == START + timedelta(days=1)
    day_one = {e.identity for e in events if e.date < through}
    assert day_one <= {r.identity for r in rows}


def test_a_read_that_fails_past_the_first_stops_there_and_keeps_what_came_back():
    events = [_row(START + timedelta(hours=i), n=i) for i in range(60)]
    google = _Google(events, 10, fail_after=6)
    rows, through = _complete(google)
    assert through is not None and through < START + timedelta(days=3) and rows


def test_a_first_read_that_fails_is_the_failure():
    with pytest.raises(GAMError):
        _complete(_Google([], 10, fail_after=0))


def test_a_day_too_busy_to_read_stops_rather_than_passing_a_part_as_all():
    # A whole read in one second has no way further back.
    second = START + timedelta(hours=1)
    events = [_row(second, n=i) for i in range(25)]
    rows, through = _complete(_Google(events, 10))
    assert through == START and len(rows) < 25


def test_accounts_at_the_cap_each_go_on_by_themselves():
    a = [_row(START + timedelta(minutes=i), who="a@e.com", n=i) for i in range(15)]
    b = [_row(START + timedelta(minutes=i), who="b@e.com", n=i) for i in range(3)]
    rows, stopped = asyncio.run(pull.paged(["a@e.com", "b@e.com"], (logs.report_time(START), logs.report_time(START + timedelta(days=1))), 10, 10, _Google(a + b, 10)))
    assert not stopped and len(rows) == 18


def test_an_address_at_the_cap_nobody_asked_for_is_capped():
    # An alias, an old name: no telling which to go on with.
    rows = [_row(START + timedelta(minutes=i), who="alias@e.com", n=i) for i in range(10)]

    async def read(users, since, until):
        return rows

    _, stopped = asyncio.run(pull.paged(["a@e.com", "b@e.com"], (logs.report_time(START), logs.report_time(START + timedelta(days=1))), 10, 10, read))
    assert stopped


def test_a_row_whose_address_was_not_asked_counts_toward_each_account():
    # 999 of a@'s and one with no address, at a cap of 1000: GAM stopped at the cap, though nobody's count reached it.
    rows = [_row(START + timedelta(minutes=i), who="a@e.com", n=i) for i in range(9)] + [_row(START, who="", n=99)]
    assert logs.capped(rows, ["a@e.com", "b@e.com"], cap=10)
    assert not logs.capped(rows[:8], ["a@e.com", "b@e.com"], cap=10)


def test_a_read_cut_at_the_cap_with_a_stray_row_is_read_further_back():
    a = [_row(START + timedelta(minutes=i), who="a@e.com", n=i) for i in range(15)]
    stray = _row(START + timedelta(minutes=20), who="old-name@e.com", n=99)
    google = _Google([*a, stray], 10)

    async def read(users, since, until):
        got = await _Google.__call__(google, users if len(users) == 1 else (), since, until)
        return got if len(users) == 1 else got[:10]

    rows, _ = asyncio.run(pull.paged(["a@e.com", "b@e.com"], (logs.report_time(START), logs.report_time(START + timedelta(days=1))), 10, 10, read))
    assert {r.identity for r in a} <= {r.identity for r in rows}


def test_paging_stops_at_the_window_s_start():
    # An end before the start is GAM's usage error.
    google = _Google([_row(START, n=i) for i in range(10)], 10)
    _, stopped = asyncio.run(pull.paged((), (logs.report_time(START), logs.report_time(START + timedelta(days=1))), 10, 10, google))
    assert not stopped and google.reads == 1


def test_an_estimate_says_up_front_how_far_it_will_get():
    whole = pull.Estimate.of(1000, 3600 * 10, 2.0, 72, 1000, 100, 200)
    assert whole.whole and whole.per_hour == 100
    part = pull.Estimate.of(1000, 60, 2.0, 720, 1000, 100, 200)
    assert part.too_busy or not part.whole


def test_the_estimate_is_told_when_a_first_read_is_full():
    told = []
    events = [_row(START + timedelta(hours=i), n=i) for i in range(60)]
    _complete(_Google(events, 10), estimated=told.append)
    assert told and told[0].per_hour


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


def _pull(client, state, *wanted, accounts=CAROL, ip="", hours=72) -> str:
    r = client.post("/incident/logs/pull", data={"accounts": accounts, "ip": ip, "hours": hours, "log": list(wanted or ("login", "token"))})
    job = state.jobs.get(state.incident_pull_job)
    if job is None:
        return r.text
    wait_for_job(client, job)
    return client.get(f"/incident/logs/status?job={job.id}").text


def test_the_page_offers_the_incident_logs_ticked(client):
    page = client.get("/incident/logs").text
    assert "Pull logs" in page and 'value="login" class="mt-1" checked' in page and 'value="drive" class="mt-1" />' in page
    assert 'href="/incident/logs"' in client.get("/incident").text


def test_a_pull_reads_and_changes_nothing(client, state, gam_calls):
    page = unescape(_pull(client, state))
    assert "Sign-ins (user log)" in page and "OAuth tokens (app access)" in page and "flagged by Google" in page and ATTACKER in page
    assert all(a[0] == "report" or a[:2] == ["info", "user"] or a[0] == "print" for a in gam_calls())


def test_a_pull_of_every_account_by_the_attacker_s_address(client, state, gam_calls):
    _pull(client, state, "login", "gmail", accounts="", ip=ATTACKER)
    reads = [a for a in gam_calls() if a[0] == "report"]
    assert all(a[2:4] == ["user", "all"] and a[a.index("ip") + 1] == ATTACKER for a in reads)
    log = state.incident_pull.log("login")
    assert log.rows and all(r.ip == ATTACKER for r in log.rows)
    assert state.incident_pull.log("gmail").rows


def test_a_pull_by_address_reads_the_devices_log_s_registrations(client, state, gam_calls):
    _pull(client, state, "mobile", ip=ATTACKER)
    read = next(a for a in gam_calls() if a[:2] == ["report", "mobile"])
    assert "ip" not in read and read[read.index("events") + 1] == "DEVICE_REGISTER_UNREGISTER_EVENT"


def test_a_log_the_edition_lacks_is_said_beside_the_rest(client, state, gam_state):
    (gam_state / "nolog").mkdir()
    (gam_state / "nolog" / "token").write_text("")
    page = unescape(_pull(client, state))
    assert "couldn't be read" in page and state.incident_pull.log("token").rows is None and state.incident_pull.log("login").rows
    assert client.get("/incident/logs/log.csv?application=token").status_code == 404     # never an empty CSV that passes as no events


def test_the_chrome_log_is_read_both_ways(client, state, gam_calls):
    _pull(client, state, "chrome")
    reads = [a for a in gam_calls() if a[:2] == ["report", "chrome"]]
    assert any("filter" in a for a in reads) and any(a[2:4] == ["user", CAROL] for a in reads)


def test_the_admin_log_is_read_both_ways(client, state, gam_calls):
    _pull(client, state, "admin")
    reads = [a for a in gam_calls() if a[:2] == ["report", "admin"]]
    assert any("userisactor" in a for a in reads) and any("userisactor" not in a for a in reads)


def test_gmail_is_held_to_thirty_days_on_a_longer_pull(client, state, gam_calls):
    page = unescape(_pull(client, state, "gmail", hours=4320))
    assert state.incident_pull.log("gmail").rows is not None and state.incident_pull.log("gmail").hours == 720
    assert "(the last 30 days: the most this log reads)" in page, "a shorter read never looks like the whole window"


def test_read_the_rest_on_gmail_ends_thirty_days_on_rather_than_skipping_what_s_between(client, state, gam_calls):
    _pull(client, state, "gmail", accounts=CAROL)
    held = state.incident_pull
    old = state.incident_pull.log("gmail")
    since = datetime.now(UTC) - timedelta(days=40)
    held.logs = [pull.Log.read("gmail", old.rows, 720, True, since)]
    client.post("/incident/logs/rest", data={"application": "gmail"})
    wait_for_job(client, state.jobs[state.incident_pull_job])
    read = [a for a in gam_calls() if a[:2] == ["report", "gmail"]][-1]
    assert read[read.index("start") + 1] == logs.report_time(since)
    assert read[read.index("end") + 1] == logs.report_time(since + timedelta(days=30))
    merged = state.incident_pull.log("gmail")
    assert merged.capped and merged.through == since.replace() + timedelta(days=30), "the ten days after it are still to read"


def test_a_log_stopped_before_anything_stood_is_never_an_empty_file(client, state):
    _pull(client, state, "login", "token")
    held = state.incident_pull
    held.logs = [pull.Log.read("login", [], 72, True, datetime(2026, 10, 1, tzinfo=UTC)), held.log("token")]
    assert client.get("/incident/logs/log.csv?application=login").status_code == 404
    with zipfile.ZipFile(io.BytesIO(client.get("/incident/logs/export.zip").content)) as archive:
        assert "login-log.csv" not in archive.namelist() and "login-log-partial.csv" not in archive.namelist()
        assert "Not exported, not read: the Sign-ins (user log) log: Read completely through" in archive.read("about.txt").decode()


def test_a_part_read_log_says_so_in_its_file_name(client, state):
    _pull(client, state, "login")
    held = state.incident_pull
    held.logs = [pull.Log.read("login", held.log("login").rows, 72, True, datetime(2026, 10, 1, tzinfo=UTC))]
    assert "login-log-partial.csv" in client.get("/incident/logs/log.csv?application=login").headers["content-disposition"]


def test_stop_ends_the_chrome_and_admin_reads_rather_than_passing_a_part_as_whole():
    async def never(*args, **kwargs):
        raise AssertionError("no read after Stop")

    conn = type("Conn", (), {"activity": never})()
    query = pull.Query(("chrome", "admin"), (CAROL,), 72)
    for application in ("chrome", "admin"):
        log = asyncio.run(pull.read_log(conn, application, query, datetime.now(UTC), stopped=lambda: True))
        assert log.rows is None and log.problem == pull.STOPPED


def test_a_parameter_named_json_is_a_parameter():
    rows = logs.rows_from("name,id.time,JSON\nx,2026-10-19T00:00:00Z,not json\n", "drive")
    assert len(rows) == 1 and rows[0].value("JSON") == "not json"


def test_the_about_note_says_what_a_pull_by_address_read_of_devices(client, state):
    _pull(client, state, "mobile", ip=ATTACKER)
    with zipfile.ZipFile(io.BytesIO(client.get("/incident/logs/export.zip").content)) as archive:
        about = archive.read("about.txt").decode()
    assert "not filtered by it" in about and "up to 20" in about


@pytest.mark.parametrize("data,says", [({"accounts": "a b@c,d"}, "isn't one email address"), ({"ip": "nope"}, "isn't one IP address"), ({"hours": "5"}, "how far back"),
                                       ({"accounts": ",".join(f"u{i}@e.com" for i in range(21))}, "At most 20")])
def test_what_can_t_be_read_is_refused_in_words(client, gam_calls, data, says):
    page = unescape(client.post("/incident/logs/pull", data={"accounts": CAROL, "log": ["login"], "hours": 72, **data}).text)
    assert says in page and not [a for a in gam_calls() if a[0] == "report"]


def test_no_log_ticked_is_refused(client, gam_calls):
    assert "Tick at least one log" in client.post("/incident/logs/pull", data={"accounts": CAROL, "hours": 72}).text


def test_the_directory_is_read_fresh_before_an_address_is_refused(client, state, monkeypatch):
    # An account the attacker made since the list was read is just who to look at.
    real = state.users
    asked = []

    async def users(force=False, stale_ok=False):
        asked.append(force)
        got = await real(force=force, stale_ok=stale_ok)
        return got if force else got[:1]

    monkeypatch.setattr(state, "users", users)
    client.post("/incident/logs/pull", data={"accounts": CAROL, "log": ["login"], "hours": 72})
    wait_for_job(client, state.jobs[state.incident_pull_job])
    assert True in asked and state.incident_pull.log("login").rows


def test_a_directory_that_can_t_be_read_is_said(client, state, monkeypatch):
    async def users(force=False, stale_ok=False):
        raise RuntimeError("no binary")

    monkeypatch.setattr(state, "users", users)
    page = unescape(client.post("/incident/logs/pull", data={"accounts": CAROL, "log": ["chrome"], "hours": 72}).text)
    assert "Couldn't read the directory" in page and not state.incident_pull_job


def test_an_account_not_in_the_directory_is_named_rather_than_failing_every_log(client, gam_calls):
    page = client.post("/incident/logs/pull", data={"accounts": "nobody-here@example.com", "log": ["login"], "hours": 72}).text
    assert "Not in the directory: nobody-here@example.com" in page and not [a for a in gam_calls() if a[0] == "report"]


def test_each_log_exports_and_export_all_says_what_was_pulled(client, state):
    _pull(client, state)
    r = client.get("/incident/logs/log.csv?application=login")
    assert r.headers["content-disposition"].startswith("attachment") and r.text.startswith("log,time,actor,event,ip_address")
    z = client.get("/incident/logs/export.zip")
    assert z.headers["content-disposition"].startswith("attachment")
    with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
        assert {"login-log.csv", "token-log.csv", "about.txt"} == set(archive.namelist())
        about = archive.read("about.txt").decode()
    assert f"Accounts: {CAROL}." in about and "From IP address: any." in about and "the Sign-ins (user log) log" in about


def test_what_the_attacker_wrote_is_inert_in_the_csv(client, state):
    _pull(client, state)
    log = state.incident_pull.log("login")
    log.rows[0] = logs.Row("x", "login", "=cmd()", None, "=cmd()", "e", "", (), False)
    text = client.get("/incident/logs/log.csv?application=login").text
    assert ",=cmd()" not in text and "'=cmd()" in text


def test_read_the_rest_goes_on_from_where_it_stopped(client, state, gam_calls):
    _pull(client, state, "login")
    held = state.incident_pull
    old = held.log("login")
    through = datetime(2026, 10, 18, tzinfo=UTC)
    held.logs = [pull.Log.read("login", old.rows[:1], 72, True, through)]
    page = unescape(client.get("/incident/logs").text)
    assert "Read the Rest" in page and "Read completely through 2026-10-18T00:00:00Z" in page
    client.post("/incident/logs/rest", data={"application": "login"})
    wait_for_job(client, state.jobs[state.incident_pull_job])
    read = [a for a in gam_calls() if a[:2] == ["report", "login"]][-1]
    assert read[read.index("start") + 1] == "2026-10-18T00:00:00Z"
    merged = state.incident_pull.log("login")
    assert len(merged.rows) == len(old.rows) and not merged.capped      # each event once


def test_read_the_rest_of_a_log_that_was_whole_is_refused(client, state):
    _pull(client, state, "login")
    assert "nothing left to read" in client.post("/incident/logs/rest", data={"application": "login"}).text


def test_read_the_logs_says_where_each_account_stands(client, state, gam_calls):
    _pull(client, state, "login", "token", "gmail", "admin", "mobile", "chrome", "useraccounts", accounts="", ip="")
    before = len(gam_calls())
    page = unescape(client.get("/incident/logs/reading").text)
    assert "Where each account stands" in page and "Still to do" in page and "What the logs show" in page and ATTACKER in page
    assert "/incident?person=" in page and "/phishing?message=" in page
    assert all(a[0] != "update" and "doit" not in a for a in gam_calls()[before:]), "reading changes nothing"
    first = state.incident_reading
    client.get("/incident/logs/reading")
    assert state.incident_reading is first, "worked out once a pull"


def test_read_the_logs_before_a_pull_says_so(client):
    assert "pull the logs first" in client.get("/incident/logs/reading").text


def test_a_finding_opens_phishing_with_its_message_id(client):
    assert "CAPh1sh+Notice=0001@mail.example.com</textarea>" in client.get("/phishing?message=CAPh1sh%2BNotice%3D0001%40mail.example.com").text


def test_the_pull_goes_with_the_connection(client, state):
    _pull(client, state)
    state.activate(state.connector)
    assert state.incident_pull is None and client.get("/incident/logs/export.zip").status_code == 404


def test_a_row_the_attacker_wrote_is_only_ever_text(client, state):
    _pull(client, state)
    log = state.incident_pull.log("login")
    log.rows[0] = logs.Row("x", "login", "t", None, "<script>x</script>", "login_success", "", (), False)
    page = client.get("/incident/logs").text
    assert "<script>x</script>" not in page and "&lt;script&gt;x" in page
