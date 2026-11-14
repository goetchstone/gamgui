"""Who it reached and where it started (back-port plan E6): the screens over the reader's Reach and WayIn, the groups read for their members, and whose Gmail log a pull holds whole. Ported from SwiftGamGui's
GroupStoreTests, IncidentEvidenceTests and IncidentStoreTests. The reader's own rules are held by test_incident_reader.py."""

from __future__ import annotations

import asyncio
import csv
import io
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.incident import groups, logs, pull, reader
from gamgui.web.server import TEMPLATES

from .helpers import wait_for_job
from .test_incident_respond import CAROL, DOMAIN, ON_INCIDENT, _open_new, _pull, _read, _read_post, _reading, client, state  # noqa: F401 - the fixtures

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
EVERYONE = f"{CAROL},alice@example.com,bob@example.com"
PHISH = "phish-1@mail.example.com"
WARNING = "warning-1@mail.example.com"
VENDOR = "inv-4471@mail.vendor.example"


# --- the groups ------------------------------------------------------------------------------------------------------------------


def _member(email, kind="USER"):
    return SimpleNamespace(email=email, member_type=kind)


class _Conn:
    """Groups as GAM reads them, holding each other. An address that isn't one is GAM's not-found."""

    def __init__(self, held):
        self.held = held
        self.asked = []

    async def list_group_members(self, group):
        self.asked.append(group)
        if group not in self.held:
            raise GAMError(GAMErrorKind.NOT_FOUND, 56, "Group: nosuch, Does not exist")
        return self.held[group]


NESTED = {"team@example.com": [_member("sales@example.com", "GROUP"), _member("staff@example.com", "GROUP")],
          "sales@example.com": [_member("alice@example.com"), _member("it@example.com", "GROUP")],
          "staff@example.com": [_member("bob@example.com"), _member("alice@example.com"), _member("id:customer", "CUSTOMER")],
          "it@example.com": [_member("Carol@Example.com"), _member("team@example.com", "GROUP")]}


def test_the_groups_an_email_went_to_are_read_with_the_groups_in_them():
    conn, read = _Conn(NESTED), groups.GroupsRead()
    asyncio.run(groups.read_groups(conn, [" TEAM@example.com", "nosuch@example.com"], read))
    assert conn.asked == ["team@example.com", "nosuch@example.com", "sales@example.com", "staff@example.com", "it@example.com"]
    assert read.members == {"team@example.com": ["sales@example.com", "staff@example.com"], "sales@example.com": ["alice@example.com", "it@example.com"],
                            "staff@example.com": ["bob@example.com", "alice@example.com"], "it@example.com": ["carol@example.com", "team@example.com"]}
    assert read.everyone == {"staff@example.com"} and read.problems == {"nosuch@example.com": "GAM finds no group at that address"}
    asyncio.run(groups.read_groups(conn, ["team@example.com", "it@example.com"], read))
    assert len(conn.asked) == 5, "asked again, nothing is read again"


def test_past_the_limit_a_group_is_said_not_read():
    someone = [_member("p@example.com")]
    conn, read = _Conn({"a@example.com": someone, "b@example.com": someone, "c@example.com": someone}), groups.GroupsRead()
    asyncio.run(groups.read_groups(conn, ["a@example.com", "b@example.com", "c@example.com"], read, limit=2))
    assert conn.asked == ["a@example.com", "b@example.com"] and read.problems == {"c@example.com": "not read: this screen reads at most 2 groups"}
    read.forget_failures()
    asyncio.run(groups.read_groups(conn, groups.to_ask((), read), read, limit=2))
    assert "c@example.com" in read.members and not read.problems, "the limit is a call's, never the connection's"


def test_a_failure_is_tried_again_when_the_screen_opens_again():
    conn, read = _Conn({}), groups.GroupsRead()
    asyncio.run(groups.read_groups(conn, ["a@example.com"], read))
    assert read.problems and "a@example.com" in read.asked
    read.forget_failures()
    assert read.problems and not read.asked, "said until it answers"
    conn.held["a@example.com"] = [_member("b@example.com")]
    asyncio.run(groups.read_groups(conn, groups.to_ask((), read), read))
    assert read.members == {"a@example.com": ["b@example.com"]} and not read.problems and not read.retry


def test_a_nested_group_that_failed_is_tried_again_though_no_email_names_it():
    conn, read = _Conn({"top@example.com": [_member("nested@example.com", "GROUP")]}), groups.GroupsRead()
    asyncio.run(groups.read_groups(conn, ["top@example.com"], read))
    assert read.problems == {"nested@example.com": "GAM finds no group at that address"}
    read.forget_failures()
    conn.held["nested@example.com"] = [_member("carol@example.com")]
    asked = groups.to_ask((), read)
    assert asked == ["nested@example.com"]
    asyncio.run(groups.read_groups(conn, asked, read))
    assert read.members["nested@example.com"] == ["carol@example.com"] and not read.problems


def test_a_group_listing_no_one_counts_on_the_send_and_a_member_with_no_address_is_said():
    conn, read = _Conn({"empty@example.com": [], "odd@example.com": [_member(""), _member("not-an-address")], "part@example.com": [_member("a@example.com"), _member("")]}), groups.GroupsRead()
    asyncio.run(groups.read_groups(conn, ["empty@example.com", "odd@example.com", "part@example.com"], read))
    assert "empty@example.com" not in read.members and "odd@example.com" not in read.members and "lists no one" in read.problems["empty@example.com"]
    assert read.members["part@example.com"] == ["a@example.com"] and read.nameless == {"part@example.com": 1}


def test_which_groups_are_read():
    def recipient(address, inside=True, mailbox=False, outcome="notAMailbox", via=None):
        return reader.Recipient(address, inside, mailbox, outcome, via=via)

    reach = SimpleNamespace(recipients=(recipient("g@example.com"), recipient("g@example.com"), recipient("person@example.com", mailbox=True, outcome="received"), recipient("out@other.example", inside=False),
                                        recipient("bounced@example.com", outcome="bounced"), recipient("through@example.com", via="g@example.com")))
    assert groups.groups_to_read([reach]) == ["g@example.com"]


# --- whose Gmail log a pull holds whole -------------------------------------------------------------------------------------------


def _row(hours, who=CAROL):
    return logs.Row(f"{who}{hours}", "gmail", "t", NOW + timedelta(hours=hours), who, "e", "", (), False)


def test_a_read_with_no_address_filter_holds_its_accounts_between_its_events():
    whole = pull.Log.read("gmail", [_row(0), _row(5)], 72, scope=(CAROL.upper(),)).whole
    assert [(w.accounts, w.from_time, w.to) for w in whole] == [(frozenset({CAROL}), NOW, NOW + timedelta(hours=5))]
    assert pull.Log.read("gmail", [_row(0), _row(5)], 72, scope=()).whole[0].accounts is None, "every account"


def test_a_read_stopped_at_its_cap_holds_only_up_to_where_it_read_through():
    through = NOW + timedelta(hours=3)
    assert pull.Log.read("gmail", [_row(0), _row(5)], 72, True, through, scope=()).whole[0].to == through, "the day it stopped in may be held only in part"
    assert pull.Log.read("gmail", [_row(0), _row(5)], 72, True, None, scope=()).whole == (), "newest first to a cap can't say"


@pytest.mark.parametrize("rows,scope", [([_row(0), _row(5)], None), ([], ()), ([_row(0)], ())])
def test_what_can_t_say_holds_nothing_whole(rows, scope):
    assert pull.Log.read("gmail", rows, 72, scope=scope).whole == ()


HEADER = ",".join(logs.TABLE_HEADER)


def _saved(about, application="gmail"):
    rows = [f"{application},2026-10-19T0{h}:00:00Z,{CAROL},e,,,," for h in (1, 5)]
    return pull.pull_of_export(SimpleNamespace(about=f"Incident logs exported x.\n{about}", logs=((application, "\n".join([HEADER, *rows]) + "\n"),)))


def test_a_saved_read_is_judged_by_what_its_about_says():
    assert _saved(f"Accounts: {CAROL}.\nFrom IP address: any.\n").log("gmail").whole[0].accounts == frozenset({CAROL})
    assert _saved("Accounts: every account.\nFrom IP address: any.\n").log("gmail").whole[0].accounts is None
    assert _saved(f"Accounts: {CAROL}.\nFrom IP address: 198.51.100.23.\n").log("gmail").whole == (), "by an address, only that address's"
    assert _saved(f"Accounts: {CAROL}.\n").log("gmail").whole == (), "it doesn't say from where"
    assert _saved("From IP address: any.\n").log("gmail").whole == (), "it doesn't say whose"
    assert _saved(f"Accounts: {CAROL}.\nFrom IP address: any.\n", "admin").log("admin").whole == (), "newest first, each account to its own cap"
    assert _saved("Accounts: every account.\nFrom IP address: any.\n", "admin").log("admin").whole != ()


def test_whole_is_kept_through_the_merge():
    a = pull.Pull(pull.Query(("gmail",), (CAROL,), 72), [pull.Log.read("gmail", [_row(0), _row(1)], 72, scope=(CAROL,))], ended=True)
    b = pull.Pull(pull.Query(("gmail",), ("b@example.com",), 72), [pull.Log.read("gmail", [_row(2, "b@example.com"), _row(3, "b@example.com")], 72, scope=("b@example.com",))], ended=True)
    assert {w.accounts for w in pull.merged([a, b]).log("gmail").whole} == {frozenset({CAROL}), frozenset({"b@example.com"})}


def test_a_pull_passes_it_to_the_reader_and_by_an_address_holds_nothing(client, state):  # noqa: F811
    _pull(client, state, "gmail", accounts=EVERYONE)
    _reading(client, state)
    assert state.incident_reading[0][2].gmail_whole
    client.post("/incident/logs/pull", data={"ip": "198.51.100.23", "hours": 72, "log": ["gmail"]})
    wait_for_job(client, state.jobs[state.incident_pull_job])
    _reading(client, state)
    assert not state.incident_reading[0][2].gmail_whole


def test_a_quiet_mailbox_read_whole_isn_t_asked_for_again(client, state):  # noqa: F811
    _pull(client, state, "login", "gmail")
    assert any(r.unread for r in _reading(client, state).reaches), "their Gmail log isn't in the pull"
    _pull(client, state, "login", "gmail", accounts=EVERYONE)
    assert not any(r.unread for r in _reading(client, state).reaches)


# --- the words -------------------------------------------------------------------------------------------------------------------


def _reach(*recipients, warnings=(), unreadable=0, unnamed=0):
    return reader.Reach(PHISH, "Phish", (CAROL,), recipients, (), warnings, unreadable, unnamed, ())


R = reader.Recipient


def test_the_summary_says_each_kind_and_leaves_out_what_none_are():
    text = reader.reach_summary(_reach(R("a@example.com", True, True, "received"), R("b@other.example", False, False, "sent"), R("c@example.com", True, True, "sent"),
                                       R("d@other.example", False, False, "bounced"), R("e@example.com", True, True, "blocked"), R("f@example.com", True, False, "notAMailbox")))
    assert text.startswith("It went to 6 addresses: 1 received it here, 1 outside with no bounce in the logs read, 1 here with no receipt in the logs read, 1 bounced, 1 quarantined or rejected, 1 no one's mailbox")
    assert text.endswith("No warning picked yet.")
    assert reader.reach_summary(_reach(R("a@example.com", True, True, "received"))).startswith("It went to 1 address: 1 received it here.")
    assert reader.reach_summary(_reach()).startswith("It went to 0 addresses.")


def test_the_summary_says_whether_the_warnings_count():
    counted = reader.Candidate(WARNING, "alice@example.com", "Warning", NOW, 1)
    wrong = reader.Candidate(WARNING, "", "", None, 0, problem="it's the attacker's")
    left = R("a@example.com", True, True, "received", warned=False)
    warned = R("a@example.com", True, True, "received", warned=True)
    assert "No warning picked counts yet, so who was warned can't be told." in reader.reach_summary(_reach(left, warnings=(wrong,)))
    assert "doesn't show the warnings picked reaching 1 of the people to warn" in reader.reach_summary(_reach(left, warnings=(counted,)))
    assert "reaching everyone to warn." in reader.reach_summary(_reach(warned, warnings=(counted,)))
    assert "reaching everyone listed, but the list isn't whole" in reader.reach_summary(_reach(warned, warnings=(counted,), unnamed=1))


def test_the_summary_says_what_it_can_t_read_and_a_group_by_its_members():
    text = reader.reach_summary(_reach(R("g@example.com", True, False, "notAMailbox", expanded=True), R("a@example.com", True, True, "received", via="g@example.com"), unreadable=2, unnamed=3))
    assert "A group here counts by its members, 1 of them listed only through a group." in text
    assert "2 more the log names don't read as an address" in text and "3 sends of it name no recipient" in text


def test_who_else_got_the_email_that_started_it():
    def said(*received):
        email = reader.WayInEmail(VENDOR, "billing@vendor.example", "Invoice", NOW, received)
        return reader.received_words(email, reader.WayIn(CAROL, NOW + timedelta(hours=1), (email,), 0, email, None), lambda when: "then")

    assert said().endswith("No one here is shown receiving it.") and said().startswith("First received then, before the attacker's first sign, on carol@example.com at then.")
    assert said(CAROL).endswith("Only carol@example.com is shown receiving it.")
    assert said(CAROL, "a@example.com").endswith("carol@example.com received it, and so did a@example.com: they may have fallen for it too.")
    assert said("a@example.com").endswith("Received here by a@example.com: they may have fallen for it too.")


# --- through the screens, on the mock ---------------------------------------------------------------------------------------------


def test_respond_says_who_it_reached_and_investigate_doesn_t(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    page = _read(client)
    assert "Who “Shared file: Q4 invoices” reached" in page and "It went to 5 addresses" in page and "1 bounced" in page and "Every address it went to" in page
    assert "pat@partner.example" in page and "to warn" in page and "guest@example.org" in page
    assert "Who “Shared file" not in _read(client, "findings")


def test_the_csv_has_every_address_and_what_was_found(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    r = client.get(f"/incident/logs/reach.csv?message={PHISH}")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert tuple(rows[0]) == ("email", "Message-ID", *reader.REACH_COLUMNS) and {row[2] for row in rows[1:]} >= {"alice@example.com", "guest@example.org", "pat@partner.example"}
    assert all(row[1] == PHISH for row in rows[1:]), "a file gets passed around without its page"
    assert client.get("/incident/logs/reach.csv?message=nothing@mail.example.com").status_code == 404
    assert f'href="/incident/logs/reach.csv?message={PHISH.replace("@", "%40")}"' in client.get("/incident/logs/reading?part=respond").text


def test_a_csv_cell_the_attacker_wrote_is_inert(client, state):  # noqa: F811
    from gamgui.web.routes import incident_logs

    _pull(client, state, accounts=EVERYONE)

    async def attacker(st):
        return SimpleNamespace(reaches=(reader.Reach(PHISH, "s", (CAROL,), (R("=cmd()@example.com", False, False, "sent"),), (), (), 0, 0, ()),))

    original = incident_logs._current_reading
    incident_logs._current_reading = attacker
    try:
        assert "'=cmd()@example.com" in client.get(f"/incident/logs/reach.csv?message={PHISH}").text
    finally:
        incident_logs._current_reading = original


def test_a_long_list_shows_so_many_and_says_the_csv_has_them_all():
    reach = _reach(*(R(f"p{i}@example.com", True, True, "received") for i in range(301)))
    reading = reader.Reading((), (), (), (), 1, (reach,), None)
    page = TEMPLATES.env.get_template("_incident_reading.html").render(reading=reading, part="respond", when=str, store_problems=[], marks=[], shows_query=str, most_emails=100, group_problems={},
                                                                        reach_summary=reader.reach_summary, outcome_words=reader.outcome_words)
    assert "Showing 300 of 301" in page and "p299@example.com" in page and "p300@example.com" not in page


def test_what_the_attacker_wrote_is_only_ever_text():
    reach = reader.Reach("x@y", '<script>s</script>', (CAROL,), (R('"><script>a</script>', False, False, "sent"),), (reader.Candidate("c@y", "<b>who</b>", "<i>subject</i>", NOW, 1),), (), 0, 0, ())
    way_in = reader.WayIn(CAROL, NOW, (reader.WayInEmail("w@y", "<u>sender</u>", "<s>subject</s>", NOW, (CAROL,)),), 0, None, None)
    reading = reader.Reading((), (), (), (), 1, (reach,), way_in)
    page = TEMPLATES.env.get_template("_incident_reading.html").render(reading=reading, part="", when=str, store_problems=[], marks=[], shows_query=str, most_emails=100, group_problems={}, ticking=True,
                                                                        reach_summary=reader.reach_summary, outcome_words=reader.outcome_words, received_words=reader.received_words)
    assert not any(tag in page for tag in ("<script>", "<b>who", "<i>subject", "<u>sender", "<s>subject"))


# Pick the warning


def test_picking_the_warning_needs_an_incident_open(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    assert "to pick which email was the warning" in _read(client) and "It's the warning" not in _read(client)
    assert "Open an incident first" in _read_post(client, "/incident/logs/warning", {"email": PHISH, "warning": WARNING})


def test_a_warning_picked_is_kept_and_counts_by_the_log(client, state):  # noqa: F811
    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    assert "It's the warning" in _read(client) and "No warning picked yet." in _read(client)
    page = _read_post(client, "/incident/logs/warning", {"email": PHISH, "warning": WARNING, "part": "respond"})
    assert "Warning picked: alice@example.com" in page and "Not the warning" in page
    assert [(w.email, w.warning) for w in state.incident_store.get(record.id).warnings] == [(PHISH, WARNING)]
    reach = _reading(client, state).reaches[0]
    assert [c.message_id for c in reach.warnings] == [WARNING] and any(r.warned is True for r in reach.recipients)
    page = _read_post(client, "/incident/logs/unwarning", {"email": PHISH, "warning": WARNING, "part": "respond"})
    assert "No warning picked yet." in page and state.incident_store.get(record.id).warnings == ()


@pytest.mark.parametrize("email,warning", [(PHISH, "another@mail.example.com"), ("another@mail.example.com", WARNING), (PHISH, PHISH)])
def test_only_an_email_the_logs_offer_can_be_picked(client, state, email, warning):  # noqa: F811
    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    assert "isn't one sent after it" in _read_post(client, "/incident/logs/warning", {"email": email, "warning": warning})
    assert state.incident_store.get(record.id).warnings == ()


def test_a_warning_goes_with_its_incident(client, state):  # noqa: F811
    _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    client.post("/incident/logs/warning", data={"email": PHISH, "warning": WARNING})
    second = _open_new(client, state, "Another")
    assert state.incident_store.get(second.id).warnings == ()
    assert state.incident_pull is None or not state.incident_pull.logs or not _reading(client, state).reaches or not _reading(client, state).reaches[0].warnings


# Groups, on the screen


def test_an_address_gam_finds_no_group_at_is_said_under_the_email(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    first = client.get("/incident/logs/reading?part=respond").text
    assert "Reading the members of the groups it went to" in first and 'hx-trigger="load delay:1000ms"' in first and "/incident/logs/groups/status?job=" in first
    assert 'id="incident-reading"' not in first and first.count("load delay") == 1, "a small poll, never the whole panel every second"
    wait_for_job(client, state.jobs[state.incident_groups_job])
    page = _read(client)
    assert "Members not read, so each counts on the warning's send: dana@example.com (GAM finds no group at that address)." in page and "Reading the members" not in page and "load delay" not in page


def test_a_group_counts_by_its_members_once_they_re_read(client, state, monkeypatch):  # noqa: F811
    async def members(group):
        if group == "dana@example.com":
            return [_member("erin@example.com"), _member("alice@example.com")]
        raise GAMError(GAMErrorKind.NOT_FOUND, 56, "no")

    monkeypatch.setattr(state.connector, "list_group_members", members)
    _pull(client, state, accounts=EVERYONE)
    _read(client)
    wait_for_job(client, state.jobs[state.incident_groups_job])
    reach = _reading(client, state).reaches[0]
    assert state.incident_reading[0][2].groups == {"dana@example.com": ["erin@example.com", "alice@example.com"]}
    dana = next(r for r in reach.recipients if r.address == "dana@example.com")
    assert dana.expanded and next(r for r in reach.recipients if r.address == "erin@example.com").via == "dana@example.com"
    assert "A group here counts by its members" in _read(client)


def test_a_group_is_read_once_and_a_failure_again_when_respond_opens_again(client, state, gam_calls):  # noqa: F811
    def group_reads():
        return [a for a in gam_calls() if a[:2] == ["print", "group-members"]]

    _pull(client, state, accounts=EVERYONE)
    _read(client)
    wait_for_job(client, state.jobs[state.incident_groups_job])
    _read(client)
    assert len(group_reads()) == 1
    client.get("/incident", headers=ON_INCIDENT)
    _read(client)
    wait_for_job(client, state.jobs[state.incident_groups_job])
    assert len(group_reads()) == 2


def test_the_groups_read_go_with_the_connection(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    _read(client)
    wait_for_job(client, state.jobs[state.incident_groups_job])
    assert state.incident_groups.asked
    state.activate(state.connector)
    assert state.incident_groups is None


# Where it started


def test_where_it_started_lists_what_came_from_outside_before_it(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    page = _read(client, "findings")
    assert "Where it started" in page and "From billing@vendor.example" in page and "also received by 1 more here" in page and "Keep an incident</a> to keep which it was" in page
    assert "That's the phish" not in page and "Where it started" not in _read(client)


def test_that_s_the_phish_is_kept_with_who_else_got_it(client, state):  # noqa: F811
    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    assert "That's the phish" in _read(client, "findings")
    page = _read_post(client, "/incident/logs/phish", {"message": VENDOR, "part": "findings"})
    assert "The email that started it:" in page and "from billing@vendor.example" in page and "carol@example.com received it, and so did alice@example.com: they may have fallen for it too." in page
    assert state.incident_store.get(record.id).phish == VENDOR
    assert f"/phishing?message={VENDOR.replace('@', '%40')}" in client.get("/incident/logs/reading?part=findings").text
    page = _read_post(client, "/incident/logs/phish", {"message": "", "part": "findings"})
    assert "The email that started it:" not in page and state.incident_store.get(record.id).phish is None


def test_only_an_email_the_logs_list_can_be_the_phish(client, state):  # noqa: F811
    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    assert "isn't one the logs list" in _read_post(client, "/incident/logs/phish", {"message": PHISH})
    assert state.incident_store.get(record.id).phish is None
    assert "Open an incident first" not in _read_post(client, "/incident/logs/phish", {"message": ""})


def test_the_phish_needs_an_incident_open(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    assert "Open an incident first" in _read_post(client, "/incident/logs/phish", {"message": VENDOR})


def test_a_pick_the_logs_read_don_t_show_says_so(client, state):  # noqa: F811
    from dataclasses import replace

    record = _open_new(client, state)
    state.incident_store.save(replace(record, phish="gone@mail.example.com"))
    _pull(client, state, accounts=EVERYONE)
    assert "The email picked, gone@mail.example.com, isn't shown reaching anyone here" in _read(client, "findings")


def test_read_the_rest_keeps_whose_events_a_log_holds_past_its_cap():
    from gamgui.web.routes.incident_logs import _merged

    old = pull.Log.read("gmail", [_row(0)], 72, True, NOW, {"b@example.com"})
    rest = _merged(old, pull.Log.read("gmail", [_row(1)], 72, True, NOW + timedelta(hours=1)), NOW + timedelta(hours=1), NOW + timedelta(days=9))
    assert rest.capped and rest.covered == {"b@example.com"}


def test_the_poll_is_small_and_refreshes_the_reading_once(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    _read(client)
    job = state.jobs[state.incident_groups_job]
    wait_for_job(client, job)
    done = client.get(f"/incident/logs/groups/status?job={job.id}&part=respond", headers=ON_INCIDENT).text
    assert 'hx-get="/incident/logs/reading?part=respond"' in done and 'hx-trigger="load"' in done and "load delay" not in done
    elsewhere = client.get(f"/incident/logs/groups/status?job={job.id}").text
    assert "#incident-reading" not in elsewhere and 'href="/incident"' in elsewhere, "the Jobs page has no reading to refresh"
    assert job.status_url.startswith("/incident/logs/groups/status?job=")


def test_a_group_read_ending_during_the_reading_still_shows_its_members(client, state, monkeypatch):  # noqa: F811
    async def members(group):
        return [_member("erin@example.com")]

    monkeypatch.setattr(state.connector, "list_group_members", members)
    _pull(client, state, accounts=EVERYONE)
    real, once = reader.read, []

    def ends_meanwhile(events, context):
        reading = real(events, context)
        if not once:
            once.append(1)
            state.incident_groups = groups.GroupsRead(members={"dana@example.com": ["erin@example.com"]}, asked={"dana@example.com"})
        return reading

    monkeypatch.setattr(reader, "read", ends_meanwhile)
    assert "erin@example.com" in _read(client)


def _user(primary, aliases=(), domain_aliases=None):
    from gamgui.core.gam.models import GAMUser
    record = {"primaryEmail": primary, "aliases": list(aliases)}
    if domain_aliases is not None:
        record["nonEditableAliases"] = domain_aliases
    return GAMUser.from_json(record)


def test_a_domain_alias_is_read_as_its_person():
    from gamgui.web.routes.incident_logs import _people_of

    people = _people_of([_user("Alice@example.com", ["a@example.com"], ["Alice@alias.example.net"]), _user("b@example.com", ["alice@example.com", "b2@example.com"], "not a list")])
    assert people["alice@alias.example.net"] == "alice@example.com" and people["a@example.com"] == "alice@example.com" and people["b2@example.com"] == "b@example.com"
    assert people["alice@example.com"] == "alice@example.com", "a person's own address is never taken by another's alias"


def test_the_csv_says_when_it_can_t_be_everyone(client, state, monkeypatch):  # noqa: F811
    def name():
        return client.get(f"/incident/logs/reach.csv?message={PHISH}").headers["Content-Disposition"]

    _pull(client, state, accounts=EVERYONE)
    _read(client)
    wait_for_job(client, state.jobs[state.incident_groups_job])
    assert name().endswith("-partial.csv"), "dana@ wasn't read as a group"
    state.incident_groups.problems.clear()
    assert re.search(r"who-it-reached-[0-9a-f]{8}\.csv$", name()), "a name that tells two emails apart"


def test_another_email_s_csv_has_another_name():
    assert reader.fingerprint("a@mail.example.com")[:8] != reader.fingerprint("b@mail.example.com")[:8]


def test_a_pick_never_overwrites_what_was_saved_while_it_waited(client, state, monkeypatch):  # noqa: F811
    from gamgui.web.routes import incident_logs

    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    real = incident_logs._current_reading

    async def note_meanwhile(st):
        reading = await real(st)
        st.incident_store.add_note(record.id, "Told the insurer", NOW)
        return reading

    monkeypatch.setattr(incident_logs, "_settled_reading", note_meanwhile)
    client.post("/incident/logs/warning", data={"email": PHISH, "warning": WARNING})
    held = state.incident_store.get(record.id)
    assert "Told the insurer" in [n.text for n in held.notes] and held.warnings


def test_a_narrow_pull_says_why_no_warning_can_be_picked(client, state):  # noqa: F811
    _open_new(client, state)
    _pull(client, state, "login", "gmail")
    assert "No email sent after it shows in the logs read" in _read(client) and "It's the warning" not in _read(client)


def test_a_narrow_read_s_read_through_never_stands_for_a_wider_one():
    through_wide, through_narrow = NOW, NOW + timedelta(days=2)
    wide = pull.Pull(pull.Query(("gmail",), (), 72), [pull.Log.read("gmail", [_row(0)], 72, True, through_wide)], ended=True)
    bob = pull.Pull(pull.Query(("gmail",), ("b@example.com",), 72), [pull.Log.read("gmail", [_row(1, "b@example.com")], 72, True, through_narrow)], ended=True)
    assert pull.merged([wide, bob]).log("gmail").through == through_wide
    rest = pull.Pull(pull.Query(("gmail",), (), 72), [pull.Log.read("gmail", [_row(2)], 72, True, through_narrow)], ended=True)
    assert pull.merged([wide, rest]).log("gmail").through == through_narrow, "the same reach, further on: it goes on from there"
