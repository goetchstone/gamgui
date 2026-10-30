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
        'alice@e.com,c_train@group.calendar.google.com,'
        '"{""accessRole"":""owner"",""id"":""c_train@group.calendar.google.com"",""summary"":""Training Calendar""}"\n'
    )
    recs = parse_records(text)
    assert len(recs) == 1
    assert recs[0]["primaryEmail"] == "alice@e.com"          # sibling preserved
    assert recs[0]["id"] == "c_train@group.calendar.google.com"  # from JSON
    assert recs[0]["summary"] == "Training Calendar"
    assert recs[0]["accessRole"] == "owner"


def test_plain_csv():
    text = "primaryEmail,suspended\nx@e.com,False\ny@e.com,True\n"
    recs = parse_records(text)
    assert recs[0]["primaryEmail"] == "x@e.com"
    assert recs[1]["suspended"] == "True"  # plain CSV values stay strings


# GAM 7.49.02's own bytes: scripts/gam_rc_table.py's samples, written by the build's Python (3.15) in GAM's
# setDialect, which escapes with a backslash. Read without that escape, Ann's row was dropped and every
# backslash came back doubled (failure log 2026-10-30). tests/test_gam_csv_dialect.py holds a newer build.
_GAM_FORMATJSON = (
    'primaryEmail,JSON\n'
    'ann@example.com,"{""name"": {""fullName"": ""Ann \\\\""Q\\\\"" Lee""}, ""orgUnitPath"": ""/"", ""primaryEmail"": ""ann@example.com""}"\n'
    'bo@example.com,"{""notes"": ""C:\\\\\\\\dir\\\\\\\\x"", ""primaryEmail"": ""bo@example.com"", ""title"": ""line1\\\\nline2""}"\n'
    'cy@example.com,"{""department"": ""Zürich \\\\\\\\ R&D"", ""name"": {""fullName"": ""Cy, Plain""}, ""primaryEmail"": ""cy@example.com""}"\n'
)
_GAM_PLAIN = (
    'User,delegateAddress,note\n'
    'ann@example.com,d@example.com,back\\\\slash\n'
    'bo@example.com,e@example.com,"say ""hi"", twice\\\\"\n'
)


def test_gams_escaped_formatjson_csv_reads_back_whole():
    recs = parse_records(_GAM_FORMATJSON)
    assert [r["primaryEmail"] for r in recs] == ["ann@example.com", "bo@example.com", "cy@example.com"]
    assert recs[0]["name"]["fullName"] == 'Ann "Q" Lee'
    assert recs[1]["notes"] == "C:\\dir\\x"
    assert recs[1]["title"] == "line1\nline2"
    assert recs[2]["department"] == "Zürich \\ R&D"


def test_gams_escaped_plain_csv_reads_back_whole():
    assert parse_records(_GAM_PLAIN) == [
        {"User": "ann@example.com", "delegateAddress": "d@example.com", "note": "back\\slash"},
        {"User": "bo@example.com", "delegateAddress": "e@example.com", "note": 'say "hi", twice\\'},
    ]
