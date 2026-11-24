"""A group's settings (roadmap 4.4): seven of Google's, read with `info group` and changed with `update group`, only the ones
that change in the argv. The preview reads them again and holds the change; Change reads them a third time and refuses one
changed since. A setting the read didn't give is unknown, never a default (invariant 10), and Google's own refusal reaches
the screen in its words."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core import group_settings
from gamgui.core.gam.commands import GROUP_SETTINGS, GAMCommands
from gamgui.core.gam.errors import GAMError
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes
from .test_users_web import client  # noqa: F401  (the fixture)

SALES = "sales@example.com"   # mock: anyone can post, people can ask to join, the organization sees the members
FIELDS = "email,whocanpostmessage,whocanjoin,whocanviewmembership,allowexternalmembers,messagemoderationlevel,allowwebposting,isarchived"


def _record(**over):
    base = {"email": SALES, "whoCanPostMessage": "ANYONE_CAN_POST", "whoCanJoin": "CAN_REQUEST_TO_JOIN",
            "whoCanViewMembership": "ALL_IN_DOMAIN_CAN_VIEW", "allowExternalMembers": "false",
            "messageModerationLevel": "MODERATE_NONE", "allowWebPosting": "true", "isArchived": "true"}
    return {k: v for k, v in {**base, **over}.items() if v is not None}


# --- the argv ---------------------------------------------------------------------------------------------------------------

def test_the_read_names_one_group_and_the_seven_settings():
    assert GAMCommands.info_group_settings(SALES) == ["info", "group", SALES, "quick", "fields", FIELDS, "formatjson"]
    with pytest.raises(ValueError):
        GAMCommands.info_group_settings("sales@example.com,it@example.com")   # GAM would read both


def test_the_write_names_only_the_settings_that_change_each_value_one_element():
    assert GAMCommands.update_group_settings(SALES, {"whocanjoin": "invited_can_join", "isarchived": "false"}) == [
        "update", "group", SALES, "whocanjoin", "invited_can_join", "isarchived", "false"]


@pytest.mark.parametrize("group, changes", [
    (SALES, {}),                                               # nothing to send
    (SALES, {"whocanjoin": "everyone"}),                       # not a value the grammar takes
    (SALES, {"accesstype": "public"}),                         # not a setting offered here
    (SALES, {"whocanjoin": "invited_can_join whocanpostmessage anyone_can_post"}),
    ("abuse@example.com", {"whocanjoin": "invited_can_join"}),       # GAM writes nothing and says Updated
    ("Postmaster@example.com", {"isarchived": "true"}),
    ("sales@example.com it@example.com", {"isarchived": "true"}),     # a list: several groups
])
def test_what_gam_would_not_do_as_shown_is_refused(group, changes):
    with pytest.raises(ValueError):
        GAMCommands.update_group_settings(group, changes)


# --- reading and comparing --------------------------------------------------------------------------------------------------

def test_each_setting_reads_as_offered_and_a_value_not_offered_is_googles_own():
    found = group_settings.read(_record(whoCanPostMessage="ALL_MEMBERS_CAN_POST", allowExternalMembers=True,
                                        whoCanViewMembership="SOMETHING_NEW"))
    by = {s.key: s for s in found}
    assert [s.key for s in found] == list(GROUP_SETTINGS)
    assert by["whocanpostmessage"].value == "all_members_can_post" and by["whocanpostmessage"].shown == "Members"
    assert by["allowexternalmembers"].value == "true"                     # a JSON true reads as Google's "true"
    assert by["whocanviewmembership"].value is None
    assert by["whocanviewmembership"].shown == "Google's value: SOMETHING_NEW"


@pytest.mark.parametrize("missing", list(f for f, _ in GROUP_SETTINGS.values()))
def test_a_setting_the_read_left_out_is_unread_never_a_default(missing):
    assert group_settings.read(_record(**{missing: None})) is None
    assert group_settings.read(_record(**{missing: ""})) is None


def test_only_what_differs_is_a_change_and_a_value_not_offered_raises():
    now = group_settings.read(_record())
    held = group_settings.changes(now, {"whocanjoin": "invited_can_join", "isarchived": "true", "whocanpostmessage": ""})
    assert [(c.key, c.old, c.new) for c in held] == [("whocanjoin", "CAN_REQUEST_TO_JOIN", "invited_can_join")]
    assert (held[0].label, held[0].before, held[0].after) == ("Who can join", "People can ask to join", "Only people invited")
    with pytest.raises(ValueError):
        group_settings.changes(now, {"whocanjoin": "INVITED_CAN_JOIN"})


def test_a_held_change_whose_setting_moved_since_is_named():
    held = group_settings.changes(group_settings.read(_record()), {"whocanjoin": "invited_can_join"})
    assert group_settings.moved(held, group_settings.read(_record())) == []
    assert group_settings.moved(held, group_settings.read(_record(whoCanJoin="ANYONE_CAN_JOIN"))) == ["Who can join"]


def test_googles_refusal_is_its_own_words():
    detail = "Getting Group sales@x (1/1)\n  Group: sales@x, Update Failed: Invalid value for whoCanJoin\n"
    assert group_settings.google_said(detail) == "Invalid value for whoCanJoin"
    assert group_settings.google_said("ERROR: something else") == ""


# --- the mock fails the way GAM does ----------------------------------------------------------------------------------------

async def test_the_read_and_its_failures(connector):
    assert group_settings.read(await connector.group_settings(SALES)) == group_settings.read(_record())
    assert group_settings.read(await connector.group_settings("abuse@example.com")) is None   # GAM skipped its settings
    with pytest.raises(GAMError):
        await connector.group_settings("nonexistent-group@example.com")


@pytest.mark.parametrize("out", ["", "A notice GAM printed\n", '{"email": "a@x"}\n{"email": "b@x"}\n'])
async def test_output_with_no_one_record_is_incomplete(connector, monkeypatch, out):
    async def answered(domain, argv, timeout=None, serialize=False):
        return out
    monkeypatch.setattr(connector.runner, "run_authenticated", answered)
    with pytest.raises(Incomplete):
        await connector.group_settings(SALES)


async def test_the_write_and_googles_refusal(connector, gam_state):
    ok = await connector.update_group_settings(SALES, {"whocanjoin": "invited_can_join"})
    assert ok.ok and group_settings.read(await connector.group_settings(SALES))[1].value == "invited_can_join"
    refused = await connector.update_group_settings("locked@example.com", {"isarchived": "false"})
    assert not refused.ok and group_settings.google_said(refused.detail) == "Invalid Value"


# --- the screen -------------------------------------------------------------------------------------------------------------

def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _form(group=SALES, **changed):
    now = {s.key: s.value for s in group_settings.read(_record())}
    return {"group": group, **now, **changed}


def _preview(client, **changed):  # noqa: F811
    r = client.post("/groups/settings/preview", data=_form(**changed))
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return r, (token.group(1) if token else "")


def _apply(client, token):  # noqa: F811
    return client.post("/groups/settings/apply", data={"preview": token, "confirmed": "1"})


def test_the_panel_offers_it_and_the_card_starts_at_what_google_has(client, gam_calls):  # noqa: F811
    assert f'hx-get="/groups/settings?group={SALES.replace("@", "%40")}"' in client.get(f"/groups?group={SALES}").text
    card = client.get(f"/groups/settings?group={SALES}").text
    assert '<option value="anyone_can_post" selected>' in card and '<option value="can_request_to_join" selected>' in card
    assert 'hx-post="/groups/settings/preview"' in card and gam_writes(gam_calls()) == []


def test_the_preview_reads_again_holds_only_the_changes_and_writes_nothing(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    r, token = _preview(client, whocanjoin="invited_can_join", allowexternalmembers="true")
    page = _text(r.text)
    assert gam_calls()[before:] == [GAMCommands.info_group_settings(SALES)] and gam_writes(gam_calls()) == []
    assert "Change 2 settings of sales@example.com?" in page and token
    assert "Who can join People can ask to join Only people invited" in page
    assert "Members from outside the organization Not allowed Allowed" in page and "Who can post" not in page


def test_nothing_changed_holds_nothing(client):  # noqa: F811
    r, token = _preview(client)
    assert "Nothing to change" in _text(r.text) and not token


def test_a_value_not_offered_is_refused(client):  # noqa: F811
    r, token = _preview(client, whocanjoin="everyone")
    assert "Pick each setting from its list." in _text(r.text) and not token


def test_change_writes_exactly_the_held_settings_once_audited(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, whocanjoin="invited_can_join", isarchived="false")
    r = _apply(client, token)
    assert "Changed 2 settings of sales@example.com." in _text(r.text)
    assert gam_writes(gam_calls()) == [GAMCommands.update_group_settings(SALES, {"whocanjoin": "invited_can_join",
                                                                                  "isarchived": "false"})]
    audit = [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert audit.count(("update_group_settings", SALES, True)) == 1
    assert "expired or was already run" in _apply(client, token).text and len(gam_writes(gam_calls())) == 1
    assert '<option value="invited_can_join" selected>' in client.get(f"/groups/settings?group={SALES}").text


def test_a_change_made_since_the_preview_is_never_undone(client, gam_state, gam_calls):  # noqa: F811
    _, older = _preview(client, whocanjoin="invited_can_join")
    _, newer = _preview(client, whocanjoin="anyone_can_join")
    _apply(client, newer)
    r = _apply(client, older)     # would set it back over the newer change
    assert "Who can join changed since the preview, so nothing is changed." in _text(r.text)
    assert gam_writes(gam_calls()) == [GAMCommands.update_group_settings(SALES, {"whocanjoin": "anyone_can_join"})]


def test_googles_refusal_reaches_the_screen_in_its_words(client, gam_calls):  # noqa: F811
    r = client.post("/groups/settings/preview", data=_form("locked@example.com", isarchived="false"))
    token = re.search(r'name="preview" value="([^"]+)"', r.text).group(1)
    page = _text(_apply(client, token).text)
    assert "Google refused the change to locked@example.com's settings: “Invalid Value”. Nothing was changed." in page


def test_settings_that_cant_be_read_offer_nothing(client, monkeypatch, gam_calls):  # noqa: F811
    async def unread(group):
        raise Incomplete("GAM answered without the group's settings, so they can't be told.")
    monkeypatch.setattr(client.app.state.gamgui.connector, "group_settings", unread)
    card = _text(client.get(f"/groups/settings?group={SALES}").text)
    assert "Couldn't read sales@example.com's settings." in card and "Preview changes" not in card
    r, token = _preview(client, whocanjoin="invited_can_join")
    assert "so nothing is changed" in _text(r.text) and not token and gam_writes(gam_calls()) == []


def test_an_abuse_group_is_said_to_be_out_of_reach(client, gam_calls):  # noqa: F811
    card = _text(client.get("/groups/settings?group=abuse@example.com").text)
    assert "GAM doesn't read or change the settings of an abuse@ or postmaster@ group" in card
    assert all(c[:2] != ["info", "group"] for c in gam_calls())   # GAM would read nothing, so nothing is asked


def test_a_read_that_fails_at_the_click_changes_nothing(client, monkeypatch, gam_calls):  # noqa: F811
    _, token = _preview(client, whocanjoin="invited_can_join")

    async def unread(group):
        raise Incomplete("GAM answered without the group's settings, so they can't be told.")
    monkeypatch.setattr(client.app.state.gamgui.connector, "group_settings", unread)
    assert "so nothing is changed" in _text(_apply(client, token).text) and gam_writes(gam_calls()) == []


def test_an_alias_is_held_and_written_as_the_group_gam_read(client, monkeypatch, gam_calls):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.group_settings

    async def via_alias(group):
        return await real(SALES if group == "sales-team@example.com" else group)
    monkeypatch.setattr(conn, "group_settings", via_alias)
    r = client.post("/groups/settings/preview", data=_form("sales-team@example.com", whocanjoin="invited_can_join"))
    assert "Change 1 setting of sales@example.com?" in _text(r.text)
    token = re.search(r'name="preview" value="([^"]+)"', r.text).group(1)
    _apply(client, token)
    assert gam_writes(gam_calls()) == [GAMCommands.update_group_settings(SALES, {"whocanjoin": "invited_can_join"})]


def test_a_group_gone_since_the_preview_isnt_called_a_refusal(client, monkeypatch):  # noqa: F811
    _, token = _preview(client, whocanjoin="invited_can_join")
    conn = client.app.state.gamgui.connector
    real = conn.update_group_settings

    async def gone(group, changes):
        return await real("nonexistent-group@example.com", changes)
    monkeypatch.setattr(conn, "update_group_settings", gone)
    page = _text(_apply(client, token).text)
    assert "Couldn't change sales@example.com's settings." in page and "Google refused" not in page
