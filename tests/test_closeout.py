"""Close Out (roadmap 4.5, plan D2): after the leaver's transfer has finished, what they still have, each step off until picked:
groups, admin roles, org unit, suspend or archive, licenses, aliases to the manager. Nothing is offered while a transfer from
them is unfinished or can't be checked (invariant 10). The preview reads everything again and holds the steps; Run reads the
transfer and the aliases again, then runs exactly what was held. The delete stays the Users delete zone."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core import closeout
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import LicenseSeat, RoleAssignment
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

ALICE = "alice@example.com"   # mock: in sales@ and staff@, Super Admin (9170001), /Staff, alias a.anders@, Business Standard
SEAT = "Google-Apps|1010020028"


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _token(html: str) -> str:
    found = re.search(r'name="preview" value="([^"]+)"', html)
    return found.group(1) if found else ""


def _preview(client, **picks):  # noqa: F811
    data = {"user": ALICE, **picks}
    r = client.post("/lifecycle/closeout/preview", data=data)
    return r, _token(r.text)


def _apply(client, token, **typed):  # noqa: F811
    return client.post("/lifecycle/closeout/apply", data={"preview": token, "confirmed": "1", "confirm_email": ALICE, **typed})


def _run(client, token, **typed):  # noqa: F811
    r = _apply(client, token, **typed)
    job = next(j for j in client.app.state.gamgui.jobs.values() if j.kind == "closeout")
    wait_for_job(client, job)
    return r, job


# --- the argv ---------------------------------------------------------------------------------------------------------------

def test_the_new_writes_are_one_element_each():
    assert GAMCommands.delete_admin_role("9170001") == ["delete", "admin", "9170001"]
    assert GAMCommands.set_archived(ALICE) == ["update", "user", ALICE, "archived", "on"]
    assert GAMCommands.move_alias("a.anders@example.com", "bob@example.com") == [
        "update", "alias", "a.anders@example.com", "user", "bob@example.com"]


@pytest.mark.parametrize("build", [
    lambda: GAMCommands.delete_admin_role("9170001 9170002"),      # two assignments
    lambda: GAMCommands.delete_admin_role(""),
    lambda: GAMCommands.move_alias("a@example.com,b@example.com", "bob@example.com"),   # GAM would move both
    lambda: GAMCommands.move_alias("a@example.com", "bob@example.com carol@example.com"),
])
def test_what_gam_would_read_as_more_than_one_is_refused(build):
    with pytest.raises(ValueError):
        build()


# --- the picks and the steps ------------------------------------------------------------------------------------------------

def _found(**over):
    base = dict(user=ALICE, org_unit="/Staff", archived=False, aliases=("a.anders@example.com",),
                groups=["sales@example.com", "staff@example.com"], roles=[RoleAssignment("9170001", "Super Admin", "the whole domain")],
                seats=[LicenseSeat(ALICE, "Google-Apps", "1010020028", "Google Workspace", "Business Standard")],
                units=["/", "/Leavers", "/Staff"], pending=[])
    return closeout.Found(**{**base, **over})


def test_only_what_the_read_found_is_picked_and_anything_else_is_named():
    picks, stale = closeout.pick(_found(), groups=["Sales@example.com", "gone@example.com"], roles=["9170001"],
                                 org_unit="/Leavers", state="suspend", seats=[SEAT], aliases=["a.anders@example.com"])
    assert picks.groups == ("sales@example.com",) and [r.id for r in picks.roles] == ["9170001"]
    assert picks.org_unit == "/Leavers" and picks.state == "suspend" and len(picks.seats) == 1
    assert stale == ["gone@example.com"]


@pytest.mark.parametrize("over, picked", [
    ({"suspended": True}, {"state": "suspend"}),        # never offered: already suspended
    ({"archived": None}, {"state": "archive"}),         # unread is never "not archived"
    ({"units": None}, {"org_unit": "/Leavers"}),        # the units unread: nowhere to move to
    ({"groups": None}, {"groups": ["sales@example.com"]}),
])
def test_a_pick_of_something_not_offered_is_stale(over, picked):
    _, stale = closeout.pick(_found(**over), **picked)
    assert stale


def test_the_steps_run_in_the_plans_order_each_from_its_builder():
    picks, _ = closeout.pick(_found(), groups=["staff@example.com"], roles=["9170001"], org_unit="/Leavers", state="suspend",
                             seats=[SEAT], aliases=["a.anders@example.com"])
    steps = closeout.steps(ALICE, picks, "bob@example.com")
    assert [s.key for s in steps] == ["group", "role", "org_unit", "suspend", "license", "alias"]
    assert [s.commands[0] for s in steps] == [
        GAMCommands.remove_group_member("staff@example.com", ALICE), GAMCommands.delete_admin_role("9170001"),
        GAMCommands.move_user_to_org_unit("/Leavers", ALICE), GAMCommands.set_suspended(ALICE, True),
        GAMCommands.remove_license(ALICE, "Google-Apps", "1010020028"), GAMCommands.move_alias("a.anders@example.com", "bob@example.com")]
    assert all(not s.requires for s in steps)   # a refused one never stops the rest


@pytest.mark.parametrize("picked, manager", [
    ({"state": "archive", "seats": [SEAT]}, ""),          # archiving already swaps the license
    ({"aliases": ["a.anders@example.com"]}, ""),           # nowhere to move them
    ({"aliases": ["a.anders@example.com"]}, ALICE),        # onto the account they're on
])
def test_a_plan_that_cant_run_as_asked_is_refused(picked, manager):
    picks, _ = closeout.pick(_found(), **picked)
    with pytest.raises(ValueError):
        closeout.steps(ALICE, picks, manager)


@pytest.mark.parametrize("pending, refused", [
    (None, "Couldn't check whether a data transfer"),
    ([{"application": "Drive and Docs", "status": "inProgress"}], "hasn't finished (Drive and Docs (inProgress))"),
    ([{"application": "Calendar", "status": "failed"}], ""),     # stopped: said, not refused
    ([], ""),
])
def test_an_unfinished_or_unread_transfer_offers_nothing(pending, refused):
    why = closeout.transfer_refusal(ALICE, pending)
    assert (refused in why) if refused else why == ""


# --- the screen -------------------------------------------------------------------------------------------------------------

def test_the_offboard_page_has_the_section(client):  # noqa: F811
    page = client.get("/lifecycle").text
    assert 'hx-get="/lifecycle/closeout/check"' in page and "Close out" in page


def test_the_check_lists_what_is_left_every_pick_off_and_writes_nothing(client, gam_calls):  # noqa: F811
    r = client.get(f"/lifecycle/closeout/check?user={ALICE}")
    page = _text(r.text)
    for words in ("Take them out of sales@example.com", "Remove Super Admin, for the whole domain", "Move them to /",
                  "Suspend it", "Archive it", "Remove Google Workspace Business Standard", "Move a.anders@example.com to the manager"):
        assert words in page
    assert " checked" not in r.text.replace('value="" checked', "")   # only "leave it as it is" starts on
    assert f'hx-get="/users/delete/zone?email={ALICE.replace("@", "%40")}"' in r.text
    assert gam_writes(gam_calls()) == []


def test_a_pending_transfer_offers_nothing_but_the_delete_zone_and_its_own_warning(client):  # noqa: F811
    page = _text(client.get("/lifecycle/closeout/check?user=xferpending@example.com").text)
    assert "hasn't finished" in page and "Preview changes" not in page


def test_an_unread_transfer_is_never_none(client, monkeypatch):  # noqa: F811
    async def unknown(email):
        return None
    monkeypatch.setattr(client.app.state.gamgui.connector, "incomplete_transfers_for", unknown)
    page = _text(client.get(f"/lifecycle/closeout/check?user={ALICE}").text)
    assert "Couldn't check whether a data transfer" in page and "Preview changes" not in page
    r, token = _preview(client, group="sales@example.com")
    assert "Couldn't check" in _text(r.text) and not token


def test_a_read_that_fails_offers_nothing_for_it_and_says_so(client, monkeypatch):  # noqa: F811
    async def unread(email):
        raise Incomplete("GAM answered without them.")
    monkeypatch.setattr(client.app.state.gamgui.connector, "groups_of", unread)
    page = _text(client.get(f"/lifecycle/closeout/check?user={ALICE}").text)
    assert "Couldn't read alice@example.com's groups, so none are offered." in page
    assert "Take them out of" not in page and "In no group" not in page and "Remove Super Admin" in page


def test_an_alias_is_refused_for_its_primary(client):  # noqa: F811
    assert "is an alias of alice@example.com" in _text(client.get("/lifecycle/closeout/check?user=a.anders@example.com").text)


ALICE_ID = "100000000000000000001"


@pytest.mark.parametrize("claim, account_id", [
    (ALICE, ALICE_ID),                     # the admin GamGUI signs in as
    ("a.anders@example.com", ALICE_ID),    # the same admin, renamed: the token still names the old address
    ("", ""),                              # an admin GamGUI can't confirm: never "not this one"
])
def test_the_admin_gamgui_signs_in_as_is_never_closed_out(client, monkeypatch, gam_calls, claim, account_id):  # noqa: F811
    _, token = _preview(client, role="9170001", state="archive")     # held while the identity read as someone else
    vault = client.app.state.gamgui.vault
    monkeypatch.setattr(vault, "oauth_admin_email", lambda domain: claim)
    monkeypatch.setattr(vault, "oauth_admin_id", lambda domain: account_id)
    assert "Preview changes" not in client.get(f"/lifecycle/closeout/check?user={ALICE}").text
    assert not _preview(client, role="9170001", state="archive")[1]
    assert "nothing" in _text(_apply(client, token).text).lower()
    assert gam_writes(gam_calls()) == []


def test_archiving_counts_as_cutting_an_account_off():
    from gamgui.core.gam.commands import cut_off_account
    assert cut_off_account(GAMCommands.set_archived(ALICE)) == ALICE
    assert cut_off_account(["update", "user", ALICE, "archived", "off"]) is None


@pytest.mark.parametrize("out, pending", [
    ("Getting all Data Transfers for alice@example.com\n", None),        # no `id` title: no answer, never "none"
    ("id,overallTransferStatusCode,application\nT1,,Drive and Docs\n", [{"application": "Drive and Docs", "status": "status unknown"}]),
    ("id\n", []),
])
def test_a_transfer_read_without_its_table_or_a_status_is_never_finished(client, monkeypatch, out, pending):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.runner.run_authenticated

    async def transfers(domain, argv, *a, **k):
        return out if argv[:2] == ["print", "datatransfers"] else await real(domain, argv, *a, **k)
    monkeypatch.setattr(conn.runner, "run_authenticated", transfers)
    assert client.portal.call(conn.incomplete_transfers_for, ALICE) == pending


def test_an_offboarding_cant_start_while_a_close_out_of_them_runs(client):  # noqa: F811
    from gamgui.web.jobs import start_job
    job = start_job(client.app.state.gamgui.jobs, 3, kind="closeout", title=f"Closing out {ALICE}")
    job.about = ALICE
    r = client.post("/lifecycle/offboard/preview", data={"user": ALICE, "manager": "bob@example.com"})
    assert "A Close Out of alice@example.com is running" in _text(r.text)


def test_an_alias_gone_from_them_by_the_step_is_never_moved(client, gam_calls):  # noqa: F811
    conn = client.app.state.gamgui.connector
    result = client.portal.call(conn.move_alias, "carol@example.com", "a.anders@example.com", "bob@example.com")   # alice's
    assert not result.ok and "isn't carol@example.com's alias any more" in result.remediation
    assert gam_writes(gam_calls()) == []


def test_the_preview_holds_the_picked_steps_and_writes_nothing(client, gam_calls):  # noqa: F811
    r, token = _preview(client, group="staff@example.com", role="9170001", state="suspend")
    page = _text(r.text)
    assert token and "Closing out alice@example.com: 3 steps" in page and gam_writes(gam_calls()) == []
    assert "gam update group staff@example.com remove alice@example.com" in page and "gam delete admin 9170001" in page


def test_a_pick_the_read_no_longer_has_is_refused(client):  # noqa: F811
    r, token = _preview(client, group="it@example.com")
    assert "changed since the check (it@example.com" in _text(r.text) and not token


def test_nothing_picked_holds_nothing(client):  # noqa: F811
    r, token = _preview(client)
    assert "Nothing is picked" in _text(r.text) and not token


def test_run_writes_exactly_the_held_steps_audited_once(client, gam_calls):  # noqa: F811
    _, token = _preview(client, group="staff@example.com", org_unit="/", state="suspend", seat=SEAT,
                        alias="a.anders@example.com", manager="bob@example.com")
    r, job = _run(client, token)
    assert gam_writes(gam_calls()) == [
        GAMCommands.remove_group_member("staff@example.com", ALICE), GAMCommands.move_user_to_org_unit("/", ALICE),
        GAMCommands.set_suspended(ALICE, True), GAMCommands.remove_license(ALICE, "Google-Apps", "1010020028"),
        GAMCommands.move_alias("a.anders@example.com", "bob@example.com")]
    assert job.applied == 5 and not job.failed_total
    actions = [e["action"] for e in client.app.state.gamgui.connector.audit.tail()]
    assert actions.count("closeout") == 1 and "move_alias" in actions and "suspend_user" in actions
    assert "expired or was already run" in _apply(client, token).text and len(gam_writes(gam_calls())) == 5


def test_run_needs_the_leaver_typed_and_ten_steps_need_confirm_typed(client, gam_calls, monkeypatch):  # noqa: F811
    _, token = _preview(client, group="staff@example.com")
    r = client.post("/lifecycle/closeout/apply", data={"preview": token, "confirmed": "1"})
    assert "Type the exact email address" in _text(r.text) and gam_writes(gam_calls()) == []
    many = [f"g{i}@example.com" for i in range(10)]

    async def groups(email):
        return many
    monkeypatch.setattr(client.app.state.gamgui.connector, "groups_of", groups)
    r = client.post("/lifecycle/closeout/preview", data={"user": ALICE, "group": many})
    assert 'name="confirm"' in r.text
    assert "Type confirm" in _text(_apply(client, _token(r.text)).text) and gam_writes(gam_calls()) == []


def test_a_refused_step_is_reported_and_the_rest_still_run(client, gam_state, gam_calls):  # noqa: F811
    _, token = _preview(client, role="9170001", state="suspend")
    (gam_state / "admins_deleted").write_text("9170001\n")     # removed elsewhere since the preview
    _, job = _run(client, token)
    assert job.failed_total == 1 and job.applied == 1
    assert GAMCommands.set_suspended(ALICE, True) in gam_writes(gam_calls())


def test_a_transfer_started_since_the_preview_stops_the_run(client, monkeypatch, gam_calls):  # noqa: F811
    _, token = _preview(client, state="suspend")

    async def running(email):
        return [{"application": "Drive and Docs", "status": "inProgress"}]
    monkeypatch.setattr(client.app.state.gamgui.connector, "incomplete_transfers_for", running)
    assert "Nothing ran." in _text(_apply(client, token).text) and gam_writes(gam_calls()) == []


def test_an_alias_no_longer_theirs_is_never_moved(client, monkeypatch, gam_calls):  # noqa: F811
    _, token = _preview(client, alias="a.anders@example.com", manager="bob@example.com")
    conn = client.app.state.gamgui.connector
    real = conn.get_user

    async def moved(email, fields=None):
        user = await real(email, fields)
        user.aliases = []
        return user
    monkeypatch.setattr(conn, "get_user", moved)
    # `update alias` deletes it from whoever holds it now: someone else's would go.
    assert "isn't alice@example.com's alias any more, so nothing ran" in _text(_apply(client, token).text)
    assert gam_writes(gam_calls()) == []


def test_a_manager_who_isnt_an_account_is_refused(client):  # noqa: F811
    r, token = _preview(client, alias="a.anders@example.com", manager="nobody@example.com")
    assert "isn't an account here" in _text(r.text) and not token


def test_an_offboarding_still_running_refuses_the_check(client):  # noqa: F811
    from gamgui.web.jobs import start_job
    st = client.app.state.gamgui
    job = start_job(st.jobs, 8, kind="offboard")
    st.offboard_jobs[ALICE] = job.id
    assert "offboarding is still running" in _text(client.get(f"/lifecycle/closeout/check?user={ALICE}").text)


def test_the_run_panel_feeds_only_its_bounded_window(client):  # noqa: F811
    from gamgui.web.jobs import start_job
    job = start_job(client.app.state.gamgui.jobs, 400, kind="closeout", title=f"Closing out {ALICE}")
    job.about = ALICE
    for i in range(400):        # a leaver in 400 groups
        job.record(f"Take them out of g{i}@example.com", True)
    page = client.get(f"/lifecycle/closeout/status?job={job.id}").text
    assert page.count("Take them out of g") == job.window < 400 and "g399@example.com" in page
    job.finish()
    assert "Closed out alice@example.com: all 400 steps succeeded." in _text(client.get(f"/lifecycle/closeout/status?job={job.id}").text)


def test_a_run_finishes_and_says_so(client):  # noqa: F811
    _, token = _preview(client, state="suspend")
    _, job = _run(client, token)
    assert "Closed out alice@example.com: all 1 step succeeded." in _text(client.get(f"/lifecycle/closeout/status?job={job.id}").text)


def test_a_failed_alias_move_says_the_alias_may_be_on_nobody(client, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.get_user

    async def holds_it(email, fields=None):     # alice holds the alias GAM will fail to add (the mock's *addfail*)
        user = await real(email, fields)
        user.aliases = ["x.addfail@example.com"]
        return user
    monkeypatch.setattr(conn, "get_user", holds_it)
    result = client.portal.call(conn.move_alias, ALICE, "x.addfail@example.com", "bob@example.com")
    assert not result.ok and result.remediation.startswith("x.addfail@example.com may now be on nobody")


def test_a_section_unread_at_preview_is_said_as_unread_never_changed(client, monkeypatch):  # noqa: F811
    async def unread(email):
        raise Incomplete("GAM answered without them.")
    monkeypatch.setattr(client.app.state.gamgui.connector, "groups_of", unread)
    r, token = _preview(client, group="sales@example.com")
    page = _text(r.text)
    assert "Couldn't read alice@example.com's groups" in page and "changed since" not in page and not token
