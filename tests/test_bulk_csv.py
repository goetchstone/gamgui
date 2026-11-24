"""Change many people from a CSV (roadmap 4.6): an `email` column plus any of title, department, phone and org_unit.

A blank cell leaves a value alone. Google keeps organizations and phones as lists a write replaces whole, so every entry goes
back as read. The preview plans from the directory and holds the changes; the run reads each person again and changes nothing
for one who moved since. The preview's "before" file is the undo, and Reports' fill-in sheets come back as they went out."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core import bulk_csv
from gamgui.core.gam.commands import GAMCommands, Organization, Phone
from gamgui.core.gam.models import GAMUser

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

ALICE = "alice@example.com"   # mock: IT Director in IT, /Staff, no phone


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _user(email=ALICE, orgs=None, phones=None, ou="/Staff", aliases=()):
    raw = {"primaryEmail": email, "orgUnitPath": ou}
    if orgs is not None:
        raw["organizations"] = orgs
    if phones is not None:
        raw["phones"] = phones
    raw["aliases"] = list(aliases)
    return GAMUser.from_json(raw)


# --- reading the file -------------------------------------------------------------------------------------------------------

def test_a_byte_order_mark_is_dropped_and_anything_but_utf8_refused():
    assert bulk_csv.decode("﻿email,title\n".encode()) == "email,title\n"
    with pytest.raises(ValueError, match="CSV UTF-8"):
        bulk_csv.decode("email,title\na@example.com,Caf\xe9\n".encode("latin-1"))


def test_headers_are_read_loosely_and_unused_columns_are_said():
    parsed = bulk_csv.parse("Email,Name,Title,Org Unit,Work Phone,Location\nalice@example.com,Alice,Head,/Staff,555,HQ\n")
    assert parsed.rows[0].values == {"title": "Head", "org_unit": "/Staff", "phone": "555"}
    assert any("`location`" in n and "Admin console" in n for n in parsed.notes)
    assert any("`name`" in n for n in parsed.notes)


def test_excels_semicolons_are_read():
    assert bulk_csv.parse("email;department\nalice@example.com;Sales\n").rows[0].values == {"department": "Sales"}


def test_a_blank_cell_leaves_the_value_alone():
    parsed = bulk_csv.parse("email,title,department\nalice@example.com,,Sales\nbob@example.com,,\n")
    assert [r.values for r in parsed.rows] == [{"department": "Sales"}]
    assert any("set nothing" in n for n in parsed.notes)


def test_this_apps_formula_guard_is_undone_and_a_formula_refused():
    parsed = bulk_csv.parse("email,phone,title\nalice@example.com,'+1 555 0100,'-Lead\nbob@example.com,=A1+1,\n")
    assert parsed.rows[0].values == {"phone": "+1 555 0100", "title": "-Lead"}
    assert any("bob@example.com" in e and "formula" in e for e in parsed.errors)


@pytest.mark.parametrize("text, said", [
    ("name,title\nAlice,Head\n", "needs an `email` column"),
    ("email,name\nalice@example.com,Alice\n", "none of title"),
    ("email,title\nalice@example.com,Head\nALICE@example.com,Lead\n", "is on line 2 too"),
    ("email,title\nnot-an-address,Head\n", "isn't an email address"),
    ("email,title,Title\nalice@example.com,Head,Lead\n", "more than one `title` column"),
    ('email,title\nalice@example.com,"Head\nof IT"\n', "more than one line"),
    ("email,title\n" + "a@example.com,T\n" * (bulk_csv.MAX_ROWS + 1), "Split it"),
])
def test_what_cant_be_read_as_asked_is_refused_by_its_line(text, said):
    assert any(said in e for e in bulk_csv.parse(text).errors)


# --- the whole list goes back -------------------------------------------------------------------------------------------------

def test_every_organization_goes_back_as_read_with_only_the_change():
    orgs = [{"title": "Analyst", "department": "Ops", "costCenter": "CC-9", "location": "HQ", "primary": True, "type": "work"},
            {"name": "Board", "title": "Advisor", "customType": "volunteer"}]
    changes, errors, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Lead Analyst"})], [_user(orgs=orgs)], [])
    assert not errors
    assert changes[0].argv == [["update", "user", ALICE,
                                "organization", "type", "work", "title", "Lead Analyst", "department", "Ops", "costcenter", "CC-9",
                                "location", "HQ", "primary",
                                "organization", "customtype", "volunteer", "name", "Board", "title", "Advisor", "notprimary"]]


@pytest.mark.parametrize("orgs", [
    [{"title": "A", "jobDescription": "x", "primary": True}],          # a field GAM wouldn't send back
    [{"title": "A", "type": "partner"}],                               # a type GAM doesn't keep
    "not a list",
])
def test_an_organization_gam_couldnt_send_back_refuses_the_person(orgs):
    _, errors, _ = bulk_csv.plan([bulk_csv.Row(3, ALICE, {"department": "Sales"})], [_user(orgs=orgs)], [])
    assert errors and "isn't changed" in errors[0]


@pytest.mark.parametrize("orgs", [
    [Organization((("title", "A"),), primary=True), Organization((("title", "B"),), primary=True)],
    [Organization((("title", ""),))],
    [Organization((("title", "A"),), type="partner")],
])
def test_the_builder_refuses_what_gam_would_change_or_drop(orgs):
    with pytest.raises(ValueError):
        GAMCommands.update_profile(ALICE, orgs, None)


def test_a_phone_sends_every_other_phone_back_and_the_lists_go_in_one_write():
    phones = [{"type": "mobile", "value": "555 0001", "primary": True}, {"type": "home", "value": "555 0002"}]
    changes, _, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"phone": "555 0100", "department": "Sales"})],
                                  [_user(orgs=[{"title": "T", "primary": True}], phones=phones)], [])
    assert changes[0].argv == [["update", "user", ALICE, "organization", "title", "T", "department", "Sales", "primary",
                                "phone", "type", "mobile", "value", "555 0100", "primary",
                                "phone", "type", "home", "value", "555 0002", "notprimary"]]


def test_someone_with_no_organization_gets_one_primary():
    changes, _, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Head"})], [_user()], [])
    assert changes[0].organizations == (Organization((("title", "Head"),), primary=True),)


def test_an_unknown_person_an_alias_and_an_unknown_unit_are_refused_and_the_same_left_alone():
    rows = [bulk_csv.Row(2, "nobody@example.com", {"title": "X"}), bulk_csv.Row(3, "a.anders@example.com", {"title": "X"}),
            bulk_csv.Row(4, ALICE, {"org_unit": "/Nowhere"}), bulk_csv.Row(5, "bob@example.com", {"org_unit": "/Staff"})]
    directory = [_user(aliases=["a.anders@example.com"]), _user("bob@example.com")]
    changes, errors, unchanged = bulk_csv.plan(rows, directory, ["/", "/Staff"])
    assert not changes and unchanged == 1
    assert ["isn't an account here" in errors[0], "is an alias of alice@example.com" in errors[1], "/Nowhere" in errors[2]] == [True] * 3


def test_the_before_file_reads_back_as_the_undo():
    changes, _, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"phone": "555 0100", "title": "Head"})],
                                  [_user(orgs=[{"title": "=SUM Lead", "primary": True}], phones=[{"value": "+1 555 0001", "primary": True}])], [])
    undo = bulk_csv.parse(bulk_csv.before_csv(changes))     # each value guarded on the way out, read as text on the way in
    assert not undo.errors and undo.rows[0].values == {"title": "=SUM Lead", "phone": "+1 555 0001"}
    assert bulk_csv.blanks_before(bulk_csv.plan([bulk_csv.Row(2, ALICE, {"department": "Sales"})], [_user()], [])[0]) == 1


# --- the screen ---------------------------------------------------------------------------------------------------------------

def _upload(client, text):  # noqa: F811
    r = client.post("/users/csv/preview", files={"csv_file": ("people.csv", text.encode(), "text/csv")})
    found = re.search(r'name="preview" value="([^"]+)"', r.text)
    before = re.search(r'/users/csv/before\.csv\?key=([^"]+)"', r.text)
    return r, found.group(1) if found else "", unescape(before.group(1)) if before else ""


def _apply(client, token, **typed):  # noqa: F811
    r = client.post("/users/csv/apply", data={"preview": token, "confirmed": "1", **typed})
    job = next((j for j in client.app.state.gamgui.jobs.values() if j.kind == "profile_csv"), None)
    if job is not None:
        wait_for_job(client, job)
    return r, job


def test_the_users_page_links_it_and_the_page_takes_a_file(client):  # noqa: F811
    assert 'href="/users/csv"' in client.get("/users").text
    assert 'name="csv_file"' in client.get("/users/csv").text


def test_the_preview_shows_now_and_after_holds_it_and_writes_nothing(client, gam_calls):  # noqa: F811
    r, token, before = _upload(client, "email,title,phone\nalice@example.com,Head of IT,'+1 555 0100\n")
    page = _text(r.text)
    assert "Change 1 person?" in page and "Title IT Director Head of IT" in page and "Phone (empty) +1 555 0100" in page
    assert token and before and 'download class' in r.text and gam_writes(gam_calls()) == []


def test_change_runs_exactly_the_held_argv_once_audited(client, gam_calls):  # noqa: F811
    _, token, _ = _upload(client, "email,title,org_unit\nalice@example.com,Head of IT,/\n")
    r, job = _apply(client, token)
    assert job.applied == 1 and not job.failed_total
    assert gam_writes(gam_calls()) == [
        ["update", "user", ALICE, "organization", "title", "Head of IT", "department", "IT", "primary"],
        GAMCommands.move_user_to_org_unit("/", ALICE)]
    assert [e["action"] for e in client.app.state.gamgui.connector.audit.tail()].count("update_profile") == 1
    assert "expired or was already run" in _apply(client, token)[0].text and len(gam_writes(gam_calls())) == 2


def test_someone_who_changed_since_the_preview_is_left_as_they_are(client, monkeypatch, gam_calls):  # noqa: F811
    _, token, _ = _upload(client, "email,department\nalice@example.com,Sales\n")
    conn = client.app.state.gamgui.connector
    real = conn.profile_now

    async def moved(email):
        record = await real(email)
        record["organizations"] = [*record.get("organizations", []), {"title": "Board member"}]   # added elsewhere
        return record
    monkeypatch.setattr(conn, "profile_now", moved)
    _, job = _apply(client, token)
    assert job.failed_total == 1 and "changed since the preview" in job.failed[0].reason and gam_writes(gam_calls()) == []


def test_a_read_that_fails_at_the_run_changes_nothing_for_them(client, monkeypatch, gam_calls):  # noqa: F811
    _, token, _ = _upload(client, "email,department\nalice@example.com,Sales\n")

    async def unread(email):
        raise RuntimeError("no")
    monkeypatch.setattr(client.app.state.gamgui.connector, "profile_now", unread)
    _, job = _apply(client, token)
    assert job.failed_total == 1 and "Couldn't read" in job.failed[0].reason and gam_writes(gam_calls()) == []


def test_the_before_file_downloads_and_never_spends_the_runs_token(client, gam_calls):  # noqa: F811
    _, token, before = _upload(client, "email,department\nalice@example.com,Sales\n")
    for _ in range(2):
        r = client.get(f"/users/csv/before.csv?key={before}")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert r.text.splitlines() == ["email,title,department,phone,org_unit", "alice@example.com,,IT,,"]
    assert _apply(client, token)[1].applied == 1
    assert client.get("/users/csv/before.csv?key=nope").status_code == 404


def test_more_than_25_people_need_the_count_typed(client, monkeypatch, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    people = [_user(f"p{i}@example.com", orgs=[{"title": "T", "primary": True}]) for i in range(26)]

    async def directory(*a, **k):
        return people
    monkeypatch.setattr(st, "users", directory)
    r, token, _ = _upload(client, "email,department\n" + "".join(f"p{i}@example.com,Sales\n" for i in range(26)))
    assert 'name="confirm_count"' in r.text
    r, job = _apply(client, token)
    assert job is None and "type 26" in _text(r.text) and gam_writes(gam_calls()) == []


def test_a_reports_fill_in_sheet_comes_back_as_it_went_out(client):  # noqa: F811
    sheet = client.get("/reports/fill-in.csv?key=no_phone").text
    parsed = bulk_csv.parse(sheet)
    assert not parsed.errors and parsed.rows    # its title, department and org_unit read back by name


def test_an_unread_directory_offers_nothing(client, monkeypatch):  # noqa: F811
    async def unread(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(client.app.state.gamgui, "users", unread)
    r, token, _ = _upload(client, "email,title\nalice@example.com,Head\n")
    assert "Couldn't read the directory" in _text(r.text) and not token


def test_a_write_that_raises_fails_that_person_and_the_run_goes_on(client, monkeypatch, gam_calls):  # noqa: F811
    _, token, _ = _upload(client, "email,department\nalice@example.com,Sales\ncarol@example.com,Lab\n")
    conn = client.app.state.gamgui.connector
    real = conn.update_profile

    async def raises_for_alice(email, organizations, phones):
        if email == ALICE:
            raise RuntimeError("boom")
        return await real(email, organizations, phones)
    monkeypatch.setattr(conn, "update_profile", raises_for_alice)
    _, job = _apply(client, token)
    assert job.failed_total == 1 and job.applied == 1 and job.error is None


def test_the_run_panel_is_bounded(client):  # noqa: F811
    from gamgui.web.jobs import start_job
    job = start_job(client.app.state.gamgui.jobs, 300, kind="profile_csv", title="Change 300 people from a CSV")
    for i in range(300):
        job.record(f"p{i}@example.com", True)
    page = client.get(f"/users/csv/status?job={job.id}").text
    assert page.count("@example.com") == job.window


def test_phone_value_round_trips_as_a_phone_object():
    assert GAMCommands.update_profile(ALICE, None, [Phone("work", "", "+1 555", True)]) == [
        "update", "user", ALICE, "phone", "type", "work", "value", "+1 555", "primary"]


# --- what the adversarial pass proved, each held here ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    'email,name,title\nalice@example.com,"Alice,Director\nbob@example.com,Bob",Intern\n',    # Bob's title would land on Alice
    'email,name,title\nalice@example.com,"Alice,Head\n' + "".join(f"p{i}@example.com,P,T\n" for i in range(50)),   # never closed
])
def test_a_cell_over_more_than_one_line_anywhere_refuses_the_whole_file(text):
    parsed = bulk_csv.parse(text)
    assert not parsed.rows and "runs over more than one line" in parsed.errors[0]


def test_one_column_under_two_names_is_refused():
    assert "more than one `org_unit` column" in bulk_csv.parse("email,Org Unit,ou\nalice@example.com,/Staff,/Students\n").errors[0]


def test_old_mac_line_endings_with_semicolons_are_read():
    assert bulk_csv.parse("email;title\ralice@example.com;Head, IT\r").rows[0].values == {"title": "Head, IT"}


@pytest.mark.parametrize("was", ["'+1 desk", "'-Lead", "=SUM", "+1 555", "plain"])
def test_the_undo_file_puts_back_exactly_what_was_there(was):
    changes, _, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Head"})], [_user(orgs=[{"title": was, "primary": True}])], [])
    undo = bulk_csv.parse(bulk_csv.before_csv(changes))
    assert not undo.errors and undo.rows[0].values == {"title": was} and bulk_csv.blanks_before(changes) == 0


@pytest.mark.parametrize("was", [" padded", " ", "", "''+x"])
def test_a_value_the_undo_cant_put_back_is_counted(was):
    changes, _, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Head"})],
                                  [_user(orgs=[{"title": was, "department": "IT", "primary": True}])], [])
    assert bulk_csv.blanks_before(changes) == 1


def test_a_primary_that_isnt_true_or_false_refuses_the_person():
    _, errors, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Head"})],
                                 [_user(orgs=[{"title": "A", "primary": "false"}, {"title": "B", "primary": True}])], [])
    assert errors and "primary" in errors[0]
    _, errors, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"phone": "555"})], [_user(phones=[{"value": "1", "primary": "false"}])], [])
    assert errors and "primary" in errors[0]


def test_the_mocks_reread_keeps_the_record_once_phones_changed(client, gam_state):  # noqa: F811
    (gam_state / "phones").mkdir(exist_ok=True)
    (gam_state / "phones" / ALICE).write_text('[{"type": "work", "value": "+1 555 0100", "primary": true}]')
    record = client.portal.call(client.app.state.gamgui.connector.profile_now, ALICE)
    assert record["organizations"] and record["orgUnitPath"] == "/Staff" and record["phones"][0]["value"] == "+1 555 0100"


# --- what the code review found, each held here --------------------------------------------------------------------------------

def test_the_reread_compares_what_the_lists_hold_not_how_gam_printed_them():
    cached = {"primaryEmail": ALICE, "orgUnitPath": "/Staff", "organizations": [{"title": "T", "primary": True}]}
    reread = {"primaryEmail": ALICE, "orgUnitPath": "/Staff", "phones": [],
              "organizations": [{"primary": True, "title": "T", "customType": ""}]}
    assert bulk_csv.held(cached) == bulk_csv.held(reread)
    assert bulk_csv.held(cached) != bulk_csv.held({**reread, "organizations": [{"title": "T", "primary": True}, {"name": "Board"}]})


def test_an_empty_organization_refuses_the_person():
    _, errors, _ = bulk_csv.plan([bulk_csv.Row(2, ALICE, {"title": "Head"})],
                                 [_user(orgs=[{"title": "A", "primary": True}, {"primary": False}])], [])
    assert errors and "empty" in errors[0]


def test_the_preview_reads_the_directory_fresh(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    asked = []
    real = st.users

    async def users(force=False, stale_ok=False):
        asked.append(force)
        return await real(force=force, stale_ok=stale_ok)
    monkeypatch.setattr(st, "users", users)
    _upload(client, "email,title\nalice@example.com,Head\n")
    assert asked == [True]
