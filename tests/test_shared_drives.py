"""Shared drives (roadmap 4.7): a drive's members read with `print drivefileacls`, and one member added, re-roled or removed
at a time, each previewed. The preview reads the members again and holds the change; the click reads them a third time and
refuses one that no longer fits. A drive is never left without a manager here, and a members read that failed or came back
without the drive is unknown, never "no members" (invariant 10)."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core import shared_drives as sd
from gamgui.core.gam.commands import DRIVE_ROLES, GAMCommands

from .helpers import gam_writes
from .test_users_web import client  # noqa: F401  (the fixture)

FINANCE = "0AFINANCEuk9PVA"      # mock: alice manages it, pat@partner.org contributes
LEGAL = "0ALEGALuk9PVA"          # alice its only manager; the drive takes only the domain's users
OLD = "0AOLDuk9PVA"              # bob its only manager; a group (it-admins) is a content manager
ALICE_ID, PAT_ID, BOB_ID, GROUP_ID = "100000000000000000001", "07001", "100000000000000000002", "07003"


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _m(pid, role, email="", kind="user", deleted=False):
    return sd.Member(pid, kind, role, email or f"{pid}@example.com", deleted=deleted)


DRIVE = sd.SharedDrive(FINANCE, "Finance")


# --- the argv ---------------------------------------------------------------------------------------------------------------

def test_each_change_is_one_drive_one_member_and_each_value_one_element():
    assert GAMCommands.print_drive_members(FINANCE) == ["print", "drivefileacls", "shareddriveid", FINANCE, "formatjson"]
    assert GAMCommands.add_drive_member(FINANCE, "group", "it@example.com", "fileorganizer") == [
        "create", "drivefileacl", "shareddriveid", FINANCE, "group", "it@example.com", "role", "fileorganizer"]
    assert GAMCommands.set_drive_member_role(FINANCE, PAT_ID, "reader") == [
        "update", "drivefileacl", "shareddriveid", FINANCE, "id:07001", "role", "reader"]
    assert GAMCommands.remove_drive_member(FINANCE, PAT_ID) == ["delete", "drivefileacl", "shareddriveid", FINANCE, "id:07001"]


@pytest.mark.parametrize("call", [
    lambda: GAMCommands.print_drive_members("0A x"),                                   # a second word
    lambda: GAMCommands.print_drive_members("shareddrive:Finance"),                    # GAM reads a name
    lambda: GAMCommands.add_drive_member(FINANCE, "anyone", "a@example.com", "reader"),   # not a member
    lambda: GAMCommands.add_drive_member(FINANCE, "domain", "example.com", "reader"),
    lambda: GAMCommands.add_drive_member(FINANCE, "user", "alice", "reader"),          # GAM adds its domain
    lambda: GAMCommands.add_drive_member(FINANCE, "user", "a@example.com,b@example.com", "reader"),
    lambda: GAMCommands.add_drive_member(FINANCE, "user", "a@example.com", "owner"),   # GAM transfers ownership
    lambda: GAMCommands.add_drive_member(FINANCE, "user", "a@example.com", "manager"), # GAM takes it; this app sends one word each
    lambda: GAMCommands.set_drive_member_role(FINANCE, "alice@example.com", "reader"), # by ID only (Drive v2's lookup)
    lambda: GAMCommands.set_drive_member_role(FINANCE, "07001 role owner", "reader"),
    lambda: GAMCommands.remove_drive_member(FINANCE, ""),
    lambda: GAMCommands.remove_drive_member("", PAT_ID),
])
def test_what_gam_would_not_do_as_shown_is_refused(call):
    with pytest.raises(ValueError):
        call()


def test_the_roles_offered_are_drives_five_in_gams_words():
    assert [w for w, _ in sd.ROLE_CHOICES] == list(DRIVE_ROLES)
    assert [label for _, label in sd.ROLE_CHOICES] == ["Manager", "Content manager", "Contributor", "Commenter", "Viewer"]


# --- reading the members ----------------------------------------------------------------------------------------------------

def _row(perms, drive=FINANCE):
    return {"Owner": "admin@example.com", "id": drive, "permissions": perms}


def test_one_row_for_the_drive_is_its_members_managers_first():
    got = sd.members_from([_row([
        {"id": PAT_ID, "type": "user", "role": "writer", "emailAddress": "pat@partner.org"},
        {"id": ALICE_ID, "type": "user", "role": "organizer", "emailAddress": "alice@example.com", "displayName": "Alice"},
    ])], FINANCE)
    assert [(m.id, m.role, m.who) for m in got] == [(ALICE_ID, "organizer", "alice@example.com"), (PAT_ID, "writer", "pat@partner.org")]


def test_a_drive_read_with_no_member_is_an_answer():
    assert sd.members_from([_row([])], FINANCE) == []


@pytest.mark.parametrize("records", [
    [],                                                     # GAM passed the drive over without a word
    [_row([], drive=LEGAL)],                                # another drive's row
    [_row([]), _row([])],                                   # more than the one drive
    [{"Owner": "admin@example.com", "id": FINANCE}],        # the members left out
    [_row([{"type": "user", "role": "organizer", "emailAddress": "alice@example.com"}])],   # no ID: no change could name it
    [_row([{"id": "07001 role owner", "role": "reader"}])],
])
def test_a_read_without_its_answer_is_unknown_never_no_members(records):
    with pytest.raises(sd.Unread):
        sd.members_from(records, FINANCE)


# --- planning a change ------------------------------------------------------------------------------------------------------

MEMBERS = [_m(ALICE_ID, "organizer", "alice@example.com"), _m(PAT_ID, "writer", "pat@partner.org")]


def test_an_add_is_held_as_typed_with_its_role_and_kind():
    change, why = sd.plan(MEMBERS, DRIVE, sd.ADD, email=" carol@example.com ", kind="user", role="commenter")
    assert why == "" and change.argv == GAMCommands.add_drive_member(FINANCE, "user", "carol@example.com", "commenter")
    assert change.question == "Add carol@example.com to Finance as Commenter?"
    assert change.done == "Added carol@example.com to Finance as Commenter."


@pytest.mark.parametrize("email, kind, role, said", [
    ("PAT@partner.org", "user", "reader", "pat@partner.org is already on Finance as Contributor: change their role instead."),
    ("carol", "user", "reader", "Enter one full email address"),
    ("carol@example.com", "anyone", "reader", "person or a group"),
    ("carol@example.com", "user", "owner", "Pick a role."),
])
def test_an_add_that_cant_be_made_as_asked_says_why(email, kind, role, said):
    change, why = sd.plan(MEMBERS, DRIVE, sd.ADD, email=email, kind=kind, role=role)
    assert change is None and said in why


def test_a_role_change_and_a_removal_name_the_member_by_its_permission():
    change, _ = sd.plan(MEMBERS, DRIVE, sd.ROLE, member_id=PAT_ID, role="reader")
    assert change.argv == GAMCommands.set_drive_member_role(FINANCE, PAT_ID, "reader")
    assert change.question == "Change pat@partner.org on Finance from Contributor to Viewer?"
    change, _ = sd.plan(MEMBERS, DRIVE, sd.REMOVE, member_id=PAT_ID)
    assert change.argv == GAMCommands.remove_drive_member(FINANCE, PAT_ID)
    assert change.question == "Remove pat@partner.org (Contributor) from Finance?"


def test_the_last_manager_is_never_removed_or_demoted():
    for action, role in ((sd.REMOVE, ""), (sd.ROLE, "fileorganizer")):
        change, why = sd.plan(MEMBERS, DRIVE, action, member_id=ALICE_ID, role=role)
        assert change is None and "alice@example.com is Finance's only manager" in why
    two = MEMBERS + [_m("08", "organizer", "carol@example.com")]
    assert sd.plan(two, DRIVE, sd.REMOVE, member_id=ALICE_ID)[0] is not None


def test_a_manager_whose_account_is_gone_doesnt_count():
    members = MEMBERS + [_m("08", "organizer", "gone@example.com", deleted=True)]
    assert sd.plan(members, DRIVE, sd.REMOVE, member_id=ALICE_ID)[0] is None
    assert sd.plan(members, DRIVE, sd.REMOVE, member_id="08")[0] is not None   # removing the gone one is fine


def test_a_group_manager_counts_as_a_manager():
    members = MEMBERS + [_m("08", "organizer", "it@example.com", kind="group")]
    assert sd.plan(members, DRIVE, sd.REMOVE, member_id=ALICE_ID)[0] is not None


@pytest.mark.parametrize("action, member, role, said", [
    (sd.ROLE, PAT_ID, "writer", "already Contributor"),
    (sd.ROLE, "999", "reader", "isn't on Finance any more"),
    (sd.REMOVE, "999", "", "isn't on Finance any more"),
    ("transfer", PAT_ID, "", "Pick what to change."),
])
def test_a_change_to_a_member_that_doesnt_fit_says_why(action, member, role, said):
    change, why = sd.plan(MEMBERS, DRIVE, action, member_id=member, role=role)
    assert change is None and said in why


def test_a_change_runs_only_against_the_members_it_was_made_from():
    add, _ = sd.plan(MEMBERS, DRIVE, sd.ADD, email="carol@example.com", kind="user", role="reader")
    assert sd.moved(add, MEMBERS) == ""
    assert "was added to Finance since the preview" in sd.moved(add, MEMBERS + [_m("08", "reader", "Carol@example.com")])
    demote, _ = sd.plan(MEMBERS, DRIVE, sd.ROLE, member_id=PAT_ID, role="reader")
    assert sd.moved(demote, MEMBERS) == ""
    assert "isn't on Finance any more" in sd.moved(demote, MEMBERS[:1])
    assert "role on Finance changed since the preview, to Manager" in sd.moved(demote, [MEMBERS[0], _m(PAT_ID, "organizer")])
    two = MEMBERS + [_m("08", "organizer", "carol@example.com")]
    remove, _ = sd.plan(two, DRIVE, sd.REMOVE, member_id=ALICE_ID)
    assert "only manager" in sd.moved(remove, MEMBERS)     # the other manager went meanwhile
    promote, _ = sd.plan(two, DRIVE, sd.ROLE, member_id="08", role="writer")
    assert "only manager" in sd.moved(promote, [MEMBERS[1], two[2]])


# --- the screen ---------------------------------------------------------------------------------------------------------------

def _preview(client, **form):  # noqa: F811
    r = client.post("/drives/preview", data={"drive": FINANCE, **form})
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return r, (token.group(1) if token else "")


def _apply(client, token):  # noqa: F811
    return client.post("/drives/apply", data={"preview": token, "confirmed": "1"})


def test_the_finder_lists_the_drives_and_groups_links_to_it(client, gam_calls):  # noqa: F811
    assert 'href="/drives"' in client.get("/groups").text
    page = client.get("/drives").text
    assert "Finance" in page and "Legal hold" in page and f"/drives/members?id={FINANCE}" in page
    assert "Finance" in client.get("/drives/search?q=fin").text and "Legal hold" not in client.get("/drives/search?q=fin").text
    assert gam_writes(gam_calls()) == []


def test_a_list_that_couldnt_be_read_says_so_never_no_drives(client, gam_state):  # noqa: F811
    (gam_state / "drive_token_silent").touch()
    page = _text(client.get("/drives/search?refresh=1").text)
    assert "Couldn't list the shared drives" in page and "has no shared drive" not in page
    (gam_state / "drive_token_silent").unlink()
    (gam_state / "nodrives").touch()
    assert "The domain has no shared drive." in _text(client.get("/drives/search?refresh=1").text)   # GAM's own none (60)


def test_the_panel_shows_each_member_and_offers_each_change(client, gam_calls):  # noqa: F811
    panel = client.get(f"/drives/members?id={FINANCE}").text
    text = _text(panel)
    assert "alice@example.com" in text and "pat@partner.org" in text and "Manager" in text and "Contributor" in text
    assert f'name="member" value="{PAT_ID}"' in panel and 'hx-post="/drives/preview"' in panel
    assert gam_writes(gam_calls()) == []


def test_members_that_couldnt_be_read_are_unknown_never_none(client, gam_state):  # noqa: F811
    (gam_state / "acl_dropped").mkdir()
    (gam_state / "acl_dropped" / FINANCE).touch()
    text = _text(client.get(f"/drives/members?id={FINANCE}").text)
    assert "Couldn't read Finance's members" in text and "has no members" not in text
    (gam_state / "acl_dropped" / FINANCE).unlink()
    (gam_state / "drive_token_silent").touch()     # the titles alone, exit 0: no row for the drive
    text = _text(client.get(f"/drives/members?id={FINANCE}").text)
    assert "GAM answered without the drive's members" in text and "has no members" not in text


def test_the_preview_holds_the_change_and_writes_nothing(client, gam_calls):  # noqa: F811
    r, token = _preview(client, action="add", email="carol@example.com", kind="user", role="reader")
    text = _text(r.text)
    assert token and "Add carol@example.com to Finance as Viewer?" in text
    assert "gam create drivefileacl shareddriveid 0AFINANCEuk9PVA user carol@example.com role reader" in text
    assert gam_writes(gam_calls()) == []


def test_an_add_runs_and_the_list_shows_it(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, action="add", email="carol@example.com", kind="user", role="reader")
    text = _text(_apply(client, token).text)
    assert "Added carol@example.com to Finance as Viewer." in text and "carol@example.com" in text
    assert gam_writes(gam_calls()) == [GAMCommands.add_drive_member(FINANCE, "user", "carol@example.com", "reader")]
    assert "already on Finance as Viewer" in _text(_preview(client, action="add", email="carol@example.com", kind="user",
                                                            role="reader")[0].text)


def test_a_role_change_and_a_removal_run_as_previewed(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, action="role", member=PAT_ID, role="commenter")
    assert "pat@partner.org is now Commenter on Finance." in _text(_apply(client, token).text)
    _, token = _preview(client, action="remove", member=PAT_ID)
    text = _text(_apply(client, token).text)
    assert "Removed pat@partner.org from Finance." in text and "pat@partner.org (" not in text
    assert gam_writes(gam_calls()) == [GAMCommands.set_drive_member_role(FINANCE, PAT_ID, "commenter"),
                                       GAMCommands.remove_drive_member(FINANCE, PAT_ID)]


def test_the_last_manager_is_refused_at_the_preview(client, gam_calls):  # noqa: F811
    r, token = _preview(client, drive=OLD, action="remove", member=BOB_ID)
    assert not token and "bob@example.com is Old projects's only manager" in _text(r.text)
    r, token = _preview(client, drive=OLD, action="remove", member=GROUP_ID)
    assert token     # a content manager may go: bob still manages it
    assert gam_writes(gam_calls()) == []


def test_a_change_made_since_the_preview_is_never_run(client, gam_state, gam_calls):  # noqa: F811
    _, demote = _preview(client, action="role", member=PAT_ID, role="reader")
    _, remove = _preview(client, action="remove", member=PAT_ID)
    _apply(client, remove)
    text = _text(_apply(client, demote).text)
    assert "pat@partner.org isn't on Finance any more. Nothing is changed: preview it again." in text
    assert gam_writes(gam_calls()) == [GAMCommands.remove_drive_member(FINANCE, PAT_ID)]


def test_the_confirm_step_runs_once_and_only_with_its_token(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, action="role", member=PAT_ID, role="reader")
    assert "preview the change again" in _text(client.post("/drives/apply", data={"preview": "forged", "confirmed": "1"}).text)
    client.post("/drives/apply", data={"preview": token})            # not from its confirm step
    assert gam_writes(gam_calls()) == []
    _, token = _preview(client, action="role", member=PAT_ID, role="reader")
    _apply(client, token)
    assert "already run" in _text(_apply(client, token).text)
    assert gam_writes(gam_calls()) == [GAMCommands.set_drive_member_role(FINANCE, PAT_ID, "reader")]


def test_a_members_read_that_fails_at_the_click_runs_nothing(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, action="remove", member=PAT_ID)
    (gam_state / "acl_dropped").mkdir()
    (gam_state / "acl_dropped" / FINANCE).touch()
    assert "Couldn't read Finance's members again, so nothing is changed." in _text(_apply(client, token).text)
    assert gam_writes(gam_calls()) == []


def test_googles_refusal_reaches_the_screen_and_nothing_reads_as_done(client, gam_state, gam_calls):  # noqa: F811
    r, token = _preview(client, drive=LEGAL, action="add", email="pat@partner.org", kind="user", role="reader")
    text = _text(_apply(client, token).text)
    assert "Couldn't add pat@partner.org to Legal hold as Viewer." in text and "Added" not in text
    assert "only allows members from its own domain" in text   # GAM's error, one click away


def test_a_write_gam_skipped_without_a_word_isnt_done(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, action="remove", member=PAT_ID)
    original = client.app.state.gamgui.connector.drive_members

    async def read_then_go_silent(drive_id):
        found = await original(drive_id)
        (gam_state / "drive_token_silent").touch()   # Google refuses the token between the read and the write
        return found
    client.app.state.gamgui.connector.drive_members = read_then_go_silent
    text = _text(_apply(client, token).text)
    assert "GAM exited without saying it did this" in text and "Removed" not in text
    audit = (client.app.state.gamgui.connector.audit.path).read_text()
    assert '"action": "remove_drive_member"' in audit and '"ok": false' in audit
