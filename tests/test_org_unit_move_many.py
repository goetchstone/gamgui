"""Move many people to an org unit (roadmap 4.2, the bulk department's mechanism, 2.1): a unit the tree holds, a group read
again or pasted addresses matched to the directory, the people held by the preview, moved one at a time through the
chokepoint as a stoppable job, the typed count above the guard's limit, and a retry of the ones that failed."""

from __future__ import annotations

import re
from html import unescape

from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser, GroupMember

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"   # /Staff, /Staff (suspended), /Students
GROUP = "sales@example.com"                                                        # alice and bob


def _preview(client, org_unit="/Sales", group="", emails=""):  # noqa: F811
    r = client.post("/users/bulk-ou/preview", data={"org_unit": org_unit, "group": group, "emails": emails})
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _apply(client, token, org_unit="/Sales", group="", emails="", **extra):  # noqa: F811
    r = client.post("/users/bulk-ou/apply", data={"org_unit": org_unit, "group": group, "emails": emails, "preview": token,
                                                  "confirmed": "1", **extra})
    jobs = [j for j in client.app.state.gamgui.jobs.values() if j.kind == "org_unit_move"]
    for job in jobs:
        wait_for_job(client, job)
    return r, (jobs[-1] if jobs else None)


def _moved(gam_calls):
    return sorted((a[2], a[-1]) for a in gam_writes(gam_calls()) if a[:2] == ["update", "org"])


def test_the_users_page_links_it_and_the_page_offers_the_tree_and_the_groups(client):  # noqa: F811
    assert 'href="/users/bulk-ou"' in client.get("/users").text
    page = client.get("/users/bulk-ou").text
    assert '<option value="/Sales/West">' in page and f'<option value="{GROUP}">' in page


def test_a_tree_that_cant_be_read_offers_no_unit(client, gam_state):  # noqa: F811
    (gam_state / "org_units_refused").write_text("")
    page = unescape(client.get("/users/bulk-ou").text)
    assert "Couldn't read the org units or the groups" in page and 'name="org_unit"' not in page


def test_the_preview_reads_the_group_again_shows_from_and_to_and_writes_nothing(client, gam_calls):  # noqa: F811
    page, token = _preview(client, group=GROUP)
    assert "Move 2 people to /Sales?" in page and "policies follow the org unit" in page and token
    assert f"{ALICE}</span> <span class=\"text-brand-grayink\">— /Staff → /Sales" in page and "· suspended" in page
    assert ["print", "group-members", "group", GROUP, "formatjson"] in gam_calls()
    assert GAMCommands.print_org_units() in gam_calls() and gam_writes(gam_calls()) == []


def test_pasted_addresses_without_an_account_are_named_and_left_out(client):  # noqa: F811
    page, token = _preview(client, emails=f"{CAROL}\nnobody@example.com, {ALICE}")
    assert "Move 2 people to /Sales?" in page and "No account in the directory, left out (1): nobody@example.com" in page


def test_someone_already_there_is_marked_and_still_moved(client):  # noqa: F811
    page, _ = _preview(client, org_unit="/Students", emails=CAROL)
    assert "/Students → /Students (already there)" in page


def test_an_unknown_unit_nothing_picked_or_an_empty_group_previews_nothing(client, monkeypatch):  # noqa: F811
    for org_unit, emails, said in (("/Nope", ALICE, "isn't an org unit here"), ("", ALICE, "Pick the org unit"),
                                   ("/Sales", "", "Pick a group or paste")):
        page, token = _preview(client, org_unit=org_unit, emails=emails)
        assert said in page and not token

    async def empty(group):
        return []

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", empty)
    page, token = _preview(client, group=GROUP)
    assert f"{GROUP} has no members, read just now" in page and not token


def test_a_group_and_a_pasted_list_together_are_refused_not_one_dropped(client):  # noqa: F811
    page, token = _preview(client, group=GROUP, emails=CAROL)
    assert "not both" in page and not token


def test_a_nested_group_and_the_whole_organization_are_named_never_no_account(client, monkeypatch):  # noqa: F811
    members = [GroupMember(email=ALICE), GroupMember(email="team@example.com", member_type="GROUP"),
               GroupMember.from_json({"id": "C01abc", "role": "MEMBER", "type": "CUSTOMER", "status": "ACTIVE"})]

    async def mixed(group):
        return members

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", mixed)
    page, token = _preview(client, group=GROUP)
    assert "Move 1 person to /Sales?" in page and token
    assert "Nested groups, not expanded, so their members aren't moved: team@example.com" in page
    assert "a member that stands for everyone in the organization" in page and "No account in the directory" not in page

    async def only_everyone(group):
        return members[2:]

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", only_everyone)
    page, token = _preview(client, group=GROUP)
    assert "has no members" not in page and "stands for everyone in the organization" in page and not token


def test_a_group_that_couldnt_be_read_offers_nothing_never_nobody(client, monkeypatch):  # noqa: F811
    async def refused(group):
        raise OSError("LEAK-MARKER")

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", refused)
    page, token = _preview(client, group=GROUP)
    assert "Couldn't read the org units, the group or the directory, so nothing is offered" in page and not token
    assert "LEAK-MARKER" not in page


def test_move_takes_exactly_the_held_people_through_the_chokepoint(client, gam_calls):  # noqa: F811
    _, token = _preview(client, emails=f"{ALICE}\n{CAROL}")
    r, job = _apply(client, token, emails=f"{ALICE}\n{CAROL}")
    assert (job.applied, job.failed_total) == (2, 0) and _moved(gam_calls) == [("/Sales", ALICE), ("/Sales", CAROL)]
    audit = [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert ("move_org_unit", ALICE, True) in audit and ("move_org_unit", CAROL, True) in audit
    panel = unescape(client.get("/users/bulk-ou/status", params={"job": job.id}).text)
    assert "Moved 2 of 2 to /Sales." in panel
    cached = {u.primary_email: u.org_unit_path for u in client.app.state.gamgui.user_cache.cached}
    assert cached[ALICE] == cached[CAROL] == "/Sales"
    again, _ = _apply(client, token, emails=f"{ALICE}\n{CAROL}")
    assert "expired or was already run" in again.text and len(_moved(gam_calls)) == 2     # single use


def test_a_unit_or_people_changed_since_the_preview_moves_nobody(client, gam_calls):  # noqa: F811
    _, token = _preview(client, emails=ALICE)
    r, job = _apply(client, token, org_unit="/Students", emails=ALICE)
    assert "changed after the preview" in r.text and job is None
    _, token = _preview(client, emails=ALICE)
    r, job = _apply(client, token, emails=f"{ALICE}\n{CAROL}")
    assert "changed after the preview" in r.text and job is None and _moved(gam_calls) == []


def test_more_than_25_need_the_count_typed(client, gam_calls, monkeypatch):  # noqa: F811
    many = [GAMUser(primary_email=f"m{i:02}@example.com", org_unit_path="/Staff") for i in range(30)]

    async def users(**kw):
        return many

    monkeypatch.setattr(client.app.state.gamgui, "users", users)
    pasted = "\n".join(u.primary_email for u in many)
    page, token = _preview(client, emails=pasted)
    assert "Move 30 people to /Sales?" in page and 'name="confirm_count"' in page
    r, job = _apply(client, token, emails=pasted, confirm_count="29")
    assert "type 30 to confirm" in r.text and job is None and gam_writes(gam_calls()) == []


def test_the_ones_that_failed_can_be_retried_alone(client, gam_calls, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.move_user_to_org_unit

    async def carol_fails(email, org_unit):
        if email == CAROL:
            preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=email, summary="x", risk=RiskLevel.LOW)
            return ChangeResult(preview=preview, ok=False, detail="ERROR: 503", remediation="Try again in a minute.")
        return await real(email, org_unit)

    monkeypatch.setattr(conn, "move_user_to_org_unit", carol_fails)
    _, token = _preview(client, emails=f"{ALICE}\n{CAROL}")
    _, job = _apply(client, token, emails=f"{ALICE}\n{CAROL}")
    panel = unescape(client.get("/users/bulk-ou/status", params={"job": job.id}).text)
    assert "Moved 1 of 2" in panel and "Try again in a minute." in panel and "Retry the 1 that failed" in panel
    monkeypatch.setattr(conn, "move_user_to_org_unit", real)
    retry = unescape(client.post("/users/bulk-ou/retry", data={"job": job.id}).text)
    assert "Move 1 person to /Sales?" in retry
    token = re.search(r'name="preview" value="([^"]+)"', retry).group(1)
    _apply(client, token, retry=job.id)
    assert _moved(gam_calls) == [("/Sales", ALICE), ("/Sales", CAROL)]


def test_a_retry_whose_unit_is_gone_offers_nothing(client, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector

    async def fails(email, org_unit):
        preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=email, summary="x", risk=RiskLevel.LOW)
        return ChangeResult(preview=preview, ok=False, detail="ERROR: 503", remediation="Try again in a minute.")

    monkeypatch.setattr(conn, "move_user_to_org_unit", fails)
    _, token = _preview(client, emails=ALICE)
    _, job = _apply(client, token, emails=ALICE)

    async def smaller():
        return ["/", "/Staff"]

    monkeypatch.setattr(conn, "org_units", smaller)
    retry = unescape(client.post("/users/bulk-ou/retry", data={"job": job.id}).text)
    assert "“/Sales” isn't an org unit here any more" in retry and 'name="preview"' not in retry
