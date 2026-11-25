"""The closed list of writes (roadmap 5.1): rename a user, delete a group, add an admin role, create and rename a shared drive.

Each is a curated Builder command, so it runs only from its preview, through the guard, to `_run_write`. These hold each
builder to one value, one object (GAM reads a list, a bare name or an alias as something else); the mock's refusals to
GAM's (GAM 7.52.05's source, read for this: updateUsers, doDeleteGroups, doCreateAdmin, createSharedDrive,
updateSharedDrive); and a run that exited 0 without GAM's line saying it did it to "may not have happened". Offline only:
none of this has run against a tenant (README, live verification).
"""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core.catalog.catalog import load_catalog
from gamgui.core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from gamgui.core.connectors.gam_connector import builder_done
from gamgui.core.gam.commands import GAMCommands as C

from .helpers import ADMIN, gam_writes
from .test_builder import _builder_preview, _builder_run
from .test_users_web import client  # noqa: F401  (the fixture)

FINANCE = "0AFINANCEuk9PVA"
IDS = ("build.rename_user", "build.delete_group", "build.create_admin", "build.create_shared_drive", "build.rename_shared_drive")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


# --- the argv ---------------------------------------------------------------------------------------------------------

def test_each_write_is_the_one_shape_gam_was_read_for():
    assert C.rename_user("bob@example.com", "robert@example.com") == [
        "update", "user", "bob@example.com", "primaryemail", "robert@example.com", "noactionifalias"]
    assert C.delete_group("sales@example.com") == ["delete", "group", "sales@example.com", "noactionifalias"]
    assert C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE") == [
        "create", "admin", "bob@example.com", "_HELP_DESK_ADMIN_ROLE", "customer"]
    assert C.create_admin("it@example.com", "Help Desk Lite", "/Sales") == [
        "create", "admin", "it@example.com", "Help Desk Lite", "org_unit", "/Sales"]
    assert C.create_shared_drive("alice@example.com", "Finance 2027") == [
        "user", "alice@example.com", "create", "shareddrive", "Finance 2027"]
    assert C.rename_shared_drive(FINANCE, "Finance (closed)") == [
        "update", "shareddrive", "shareddriveid", FINANCE, "name", "Finance (closed)"]


@pytest.mark.parametrize("call", [
    # GAM appends its own domain to a bare name, reads `Name <a@b>` as a@b and `uid:`/`id:` as an ID, and splits a list.
    lambda: C.rename_user("bob", "robert@example.com"),
    lambda: C.rename_user("bob@example.com", "robert"),
    lambda: C.rename_user("bob@example.com", "uid:123@example.com"),
    lambda: C.rename_user("Bob <bob@example.com>", "robert@example.com"),
    lambda: C.rename_user("bob@example.com", "vfe@example.com"),        # GAM makes up a vfe.<user>.<NNNNN> address
    lambda: C.rename_user("bob@example.com", "Bob@Example.com"),        # the address they have now
    lambda: C.delete_group("sales@example.com,staff@example.com"),       # two groups
    lambda: C.delete_group("sales@example.com staff@example.com"),
    lambda: C.delete_group("csv"),                                       # a list's source, read from the next word
    lambda: C.delete_group("sales"),
    lambda: C.delete_group(""),                                          # GAM: no group at all, exit 0
    lambda: C.create_admin("bob", "_HELP_DESK_ADMIN_ROLE"),
    lambda: C.create_admin("bob@example.com", "id:9170005"),            # an unchecked role ID
    lambda: C.create_admin("bob@example.com", "UID: 9170005"),
    lambda: C.create_admin("bob@example.com", " _HELP_DESK_ADMIN_ROLE"),  # GAM doesn't strip it, and won't find it
    lambda: C.create_admin("bob@example.com", ""),
    lambda: C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE", "/"),     # blank is the whole organization
    lambda: C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE", "Root"),
    lambda: C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE", "  "),
    lambda: C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE", "//"),      # GAM drops one trailing /: the top
    lambda: C.create_shared_drive("all users", "Finance"),
    lambda: C.create_shared_drive("alice@example.com", " "),            # the next word would be read as the name
    lambda: C.rename_shared_drive("shareddrive:Finance", "New"),        # a name GAM matches without case
    lambda: C.rename_shared_drive("0A FIN", "New"),
    lambda: C.rename_shared_drive(FINANCE, ""),
])
def test_a_value_gam_would_read_as_something_else_is_refused(call):
    with pytest.raises(ValueError):
        call()


def test_the_catalog_offers_the_five_with_their_risk():
    catalog = load_catalog()
    risks = {cid: catalog.by_id(cid).risk for cid in IDS}
    assert risks == {"build.rename_user": RiskLevel.DESTRUCTIVE, "build.delete_group": RiskLevel.DESTRUCTIVE,
                     "build.create_admin": RiskLevel.DESTRUCTIVE, "build.create_shared_drive": RiskLevel.LOW,
                     "build.rename_shared_drive": RiskLevel.LOW}
    assert all(catalog.by_id(cid).buildable for cid in IDS)


# --- did GAM say it did it --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, said", [
    (C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE"),
     "Admin Role Assignment: 9170100, Created: User bob@example.com, Admin Role _HELP_DESK_ADMIN_ROLE, Scope CUSTOMER"),
    (C.create_shared_drive("alice@example.com", "X"), "User: alice@example.com, Shared Drive Name: X, Shared Drive ID: 0AN, Created"),
    (C.rename_shared_drive(FINANCE, "X"), f"User: admin@example.com, Shared Drive Name: X, Shared Drive ID: {FINANCE}, Updated: name"),
    (C.delete_group("Sales@example.com"), "Group: sales@example.com, Deleted"),            # GAM prints it lowercased
    (C.rename_user("Bob@example.com", "robert@example.com"), "User: bob@example.com, Updated"),
])
def test_a_run_counts_as_done_only_when_gam_says_so(argv, said):
    check = builder_done(argv)
    assert check is not None and check(said) == ""
    assert "may not have happened" in check("")


def test_other_commands_have_no_such_check():
    assert builder_done(C.set_suspended("a@example.com", True)) is None
    assert builder_done(C.create_group("g@example.com")) is None


# --- the mock fails as GAM does ---------------------------------------------------------------------------------------

def _change(argv, cid):
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=argv[2] if argv[0] != "user" else argv[1], summary=cid, argv=argv,
                         risk=RiskLevel.DESTRUCTIVE, meta={"cid": cid})


@pytest.mark.parametrize("argv, cid, ok, said", [
    (C.rename_user("bob@example.com", "robert@example.com"), "build.rename_user", True, ""),
    (C.rename_user("a.anders@example.com", "anna@example.com"), "build.rename_user", False, "Not a primary email address"),
    # The cut-off check reads the account before a rename (GamGUI knows its admin by address): none, and nothing runs.
    (C.rename_user("nobody@example.com", "anna@example.com"), "build.rename_user", False, "GAM has no account"),
    (C.rename_user("bob@example.com", "carol@example.com"), "build.rename_user", False, "already exists"),
    (C.rename_user("bob@example.com", "sales@example.com"), "build.rename_user", False, "already exists"),   # a group's
    (C.rename_user("bob@example.com", "bob@partner.org"), "build.rename_user", False, "Domain not found"),
    (C.delete_group("sales@example.com"), "build.delete_group", True, ""),
    (C.delete_group("everyone@example.com"), "build.delete_group", False, "Not a primary email address"),   # an alias
    (C.delete_group("nogroup@example.com"), "build.delete_group", False, "Does not exist"),
    (C.create_admin("bob@example.com", "_help_desk_admin_role"), "build.create_admin", True, ""),           # any case
    (C.create_admin("alice@example.com", "_SEED_ADMIN_ROLE"), "build.create_admin", False, "Duplicate"),
    (C.create_admin("bob@example.com", "Help Desk"), "build.create_admin", False, "Invalid choice"),
    (C.create_admin("nobody@example.com", "_HELP_DESK_ADMIN_ROLE"), "build.create_admin", False, "Does not exist"),
    (C.create_admin("bob@example.com", "_SEED_ADMIN_ROLE", "/Sales"), "build.create_admin", False, "Create Failed"),
    (C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE", "/Nowhere"), "build.create_admin", False, "Does not exist"),
    (C.create_shared_drive("alice@example.com", "Finance"), "build.create_shared_drive", True, ""),   # one name, two drives
    (C.create_shared_drive("nobody@example.com", "X"), "build.create_shared_drive", False, "invalid_grant"),
    (C.rename_shared_drive(FINANCE, "Finance (closed)"), "build.rename_shared_drive", True, ""),
    (C.rename_shared_drive("0AGONEuk9PVA", "X"), "build.rename_shared_drive", False, "not found"),
])
async def test_each_write_runs_or_fails_as_gam_does(connector, gam_state, argv, cid, ok, said):
    [result] = await connector.apply([_change(argv, cid)])
    assert result.ok is ok, result.detail
    assert said in (result.detail or "")


async def test_a_rename_leaves_the_old_address_an_alias_and_takes_the_new_one(connector, gam_state):
    [first] = await connector.apply([_change(C.rename_user("bob@example.com", "robert@example.com"), "build.rename_user")])
    assert first.ok
    [again] = await connector.apply([_change(C.rename_user("bob@example.com", "bobby@example.com"), "build.rename_user")])
    assert not again.ok and "Not a primary email address" in again.detail
    [taken] = await connector.apply([_change(C.rename_user("carol@example.com", "robert@example.com"), "build.rename_user")])
    assert not taken.ok and "already exists" in taken.detail


async def test_a_group_deleted_is_gone_and_a_second_grant_is_a_duplicate(connector, gam_state):
    [gone] = await connector.apply([_change(C.delete_group("sales@example.com"), "build.delete_group")])
    [again] = await connector.apply([_change(C.delete_group("sales@example.com"), "build.delete_group")])
    assert gone.ok and not again.ok and "Does not exist" in again.detail
    grant = C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE")
    [given] = await connector.apply([_change(grant, "build.create_admin")])
    [twice] = await connector.apply([_change(grant, "build.create_admin")])
    assert given.ok and not twice.ok and "Duplicate" in twice.detail


async def test_a_grant_gam_passed_over_without_a_word_is_never_done(connector, gam_state):
    # Google's internalError: GAM passes over it and exits 0, and the role may or may not have been given.
    (gam_state / "admin_internal_error").touch()
    [result] = await connector.apply([_change(C.create_admin("bob@example.com", "_HELP_DESK_ADMIN_ROLE"), "build.create_admin")])
    assert not result.ok and "may not have happened" in result.detail


async def test_a_drive_create_the_policy_refuses_says_so(connector, gam_state):
    (gam_state / "no_drive_create").touch()
    [result] = await connector.apply([_change(C.create_shared_drive("alice@example.com", "X"), "build.create_shared_drive")])
    assert not result.ok and "permissions to create shared drives" in result.detail


# --- the Builder ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("form, shown", [
    ({"cid": "build.rename_user", "email": "bob@example.com", "new_email": "robert@example.com"},
     "gam update user bob@example.com primaryemail robert@example.com noactionifalias"),
    ({"cid": "build.delete_group", "group": "sales@example.com"}, "gam delete group sales@example.com noactionifalias"),
    ({"cid": "build.create_admin", "email": "bob@example.com", "role": "_HELP_DESK_ADMIN_ROLE", "org_unit": ""},
     "gam create admin bob@example.com _HELP_DESK_ADMIN_ROLE customer"),
])
def test_a_destructive_one_asks_for_confirm_and_writes_nothing_at_the_preview(client, gam_calls, form, shown):  # noqa: F811
    page, token = _builder_preview(client, **form)
    assert shown in _text(page) and token and "Confirm &amp; run" in page
    assert gam_writes(gam_calls()) == []


@pytest.mark.parametrize("form, shown", [
    ({"cid": "build.create_shared_drive", "email": "alice@example.com", "name": "Finance 2027"},
     "Shared Drive ID: 0ANEW1Uk9PVA"),
    ({"cid": "build.rename_shared_drive", "drive_id": FINANCE, "name": "Finance (closed)"}, "Shared Drive Name: Finance (closed)"),
    ({"cid": "build.delete_group", "group": "sales@example.com"}, "Group: sales@example.com, Deleted"),
])
def test_each_runs_from_its_preview_and_shows_what_gam_said(client, gam_state, gam_calls, form, shown):  # noqa: F811
    _, token = _builder_preview(client, **form)
    page = _builder_run(client, token, **form).text
    assert shown in _text(page)
    assert len(gam_writes(gam_calls())) == 1


def test_renaming_the_admin_gamgui_signs_in_as_is_refused_at_the_preview(client, gam_calls):  # noqa: F811
    page, token = _builder_preview(client, cid="build.rename_user", email=ADMIN, new_email="it-admin@example.com")
    assert "connected as" in _text(page) and not token
    assert gam_writes(gam_calls()) == []


def test_a_bad_value_is_said_at_the_preview_and_nothing_is_held(client, gam_calls):  # noqa: F811
    page, token = _builder_preview(client, cid="build.delete_group", group="sales@example.com,staff@example.com")
    assert "isn't one group address" in _text(page) and not token
    page, token = _builder_preview(client, cid="build.create_admin", email="bob@example.com", role="id:9170005", org_unit="")
    assert "print adminroles" in _text(page) and not token
    assert gam_writes(gam_calls()) == []


def test_a_title_or_name_spelled_like_a_rename_word_isnt_a_rename():
    from gamgui.core.gam.commands import cut_off_account   # noqa: PLC0415
    assert cut_off_account(C.update_organization(ADMIN, "Username", "Email")) is None
    assert cut_off_account(C.rename_user(ADMIN, "it-admin@example.com")) == ADMIN


def test_an_alias_refused_says_so_in_words(client, gam_calls):  # noqa: F811
    form = {"cid": "build.delete_group", "group": "everyone@example.com"}
    _, token = _builder_preview(client, **form)
    text = _text(_builder_run(client, token, **form).text)
    assert "That address is an alias, so nothing was changed" in text and "done" not in text


def test_a_sequence_shows_gams_line_for_each_new_drive(client, gam_state):  # noqa: F811
    from .helpers import wait_for_job   # noqa: PLC0415
    from .test_builder import _seq_preview   # noqa: PLC0415
    client.post("/builder/sequence/add", data={"cid": "build.create_shared_drive", "email": "alice@example.com", "name": "A"})
    client.post("/builder/sequence/add", data={"cid": "build.create_shared_drive", "email": "bob@example.com", "name": "B"})
    _, token = _seq_preview(client)
    run = client.post("/builder/sequence/run", data={"confirmed": "1", "preview": token})
    job = client.app.state.gamgui.jobs[re.search(r"status\?job=([A-Za-z0-9_\-]+)", run.text).group(1)]
    wait_for_job(client, job)
    assert job.applied == 2
    status = _text(client.get(f"/builder/sequence/status?job={job.id}").text)
    assert "Shared Drive ID: 0ANEW1Uk9PVA" in status and "Shared Drive ID: 0ANEW2Uk9PVA" in status
