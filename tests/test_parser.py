from __future__ import annotations

from gamgui.core.gam.parser import parse_one, parse_records


def test_empty_returns_empty():
    assert parse_records("") == []
    assert parse_records("   \n  ") == []
    assert parse_one("") == {}


def test_single_json_object(fixtures_dir):
    text = (fixtures_dir / "info_user.json").read_text()
    rec = parse_one(text)
    assert rec["primaryEmail"] == "alice@example.com"


def test_ndjson_multiple_objects(fixtures_dir):
    text = (fixtures_dir / "print_users.json").read_text()
    recs = parse_records(text)
    assert len(recs) == 3
    assert {r["primaryEmail"] for r in recs} == {
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    }


def test_json_array(fixtures_dir):
    text = (fixtures_dir / "group_members.json").read_text()
    recs = parse_records(text)
    assert len(recs) == 2
    assert recs[1]["role"] == "MANAGER"


def test_csv_with_json_column():
    text = 'JSON\n"{""primaryEmail"": ""x@e.com"", ""suspended"": false}"\n'
    recs = parse_records(text)
    assert recs == [{"primaryEmail": "x@e.com", "suspended": False}]


def test_csv_with_key_and_json_columns():
    # GAM's real `print users formatjson` shape: a plain key column alongside the JSON blob.
    text = 'primaryEmail,JSON\n"x@e.com","{""primaryEmail"":""x@e.com"",""suspended"":true}"\n'
    recs = parse_records(text)
    assert recs == [{"primaryEmail": "x@e.com", "suspended": True}]


def test_csv_sibling_column_not_in_json_is_preserved():
    # `gam all users print calendars formatjson` emits `primaryEmail,calendarId,JSON` where the
    # owning user lives ONLY in the sibling column, not inside the JSON blob. Dropping it broke
    # calendar-owner detection, so the sibling must survive (JSON still wins on shared keys).
    text = (
        "primaryEmail,calendarId,JSON\n"
        'achenard@e.com,c_house@group.calendar.google.com,'
        '"{""accessRole"":""owner"",""id"":""c_house@group.calendar.google.com"",""summary"":""House Call Calendar""}"\n'
    )
    recs = parse_records(text)
    assert len(recs) == 1
    assert recs[0]["primaryEmail"] == "achenard@e.com"          # sibling preserved
    assert recs[0]["id"] == "c_house@group.calendar.google.com"  # from JSON
    assert recs[0]["summary"] == "House Call Calendar"
    assert recs[0]["accessRole"] == "owner"


def test_plain_csv():
    text = "primaryEmail,suspended\nx@e.com,False\ny@e.com,True\n"
    recs = parse_records(text)
    assert recs[0]["primaryEmail"] == "x@e.com"
    assert recs[1]["suspended"] == "True"  # plain CSV values stay strings
