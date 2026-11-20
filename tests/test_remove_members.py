"""Remove several members from a group (roadmap 3.5): tick them, preview what the group holds now, remove exactly those as
a stoppable job through the chokepoint, and retry the ones that failed (the bulk department's mechanism, 2.1)."""

from __future__ import annotations

import re
from html import unescape

from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel
from gamgui.core.gam.models import GroupMember

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

GROUP, ALICE, BOB = "sales@example.com", "alice@example.com", "bob@example.com"


def _preview(client, picks):  # noqa: F811
    r = client.post("/groups/members/remove-many/preview", data={"group": GROUP, "pick": picks})
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _remove(client, token, picks, **extra):  # noqa: F811
    r = client.post("/groups/members/remove-many",
                    data={"group": GROUP, "picks": ",".join(sorted(picks)), "preview": token, "confirmed": "1", **extra})
    jobs = [j for j in client.app.state.gamgui.jobs.values() if j.kind == "group_remove"]
    for job in jobs:
        wait_for_job(client, job)
    return r, (jobs[-1] if jobs else None)


def _removed(gam_calls):
    return sorted(a[-1] for a in gam_writes(gam_calls()) if "remove" in a)


def test_each_member_row_can_be_ticked(client):  # noqa: F811
    page = client.get("/groups/members/list", params={"group": GROUP}).text
    assert 'id="remove-many-form"' in page
    assert f'name="pick" value="{ALICE}" form="remove-many-form"' in page and f'value="{BOB}" form="remove-many-form"' in page


def test_the_preview_reads_the_group_again_and_writes_nothing(client, gam_calls):  # noqa: F811
    page, token = _preview(client, [ALICE, BOB, "gone@example.com"])
    assert "Remove 2 members from sales@example.com?" in page and "(manager)" in page and token
    assert "Not in the group any more, left out: gone@example.com" in page
    assert ["print", "group-members", "group", GROUP, "formatjson"] in gam_calls() and gam_writes(gam_calls()) == []


def test_remove_takes_exactly_the_held_members_through_the_chokepoint(client, gam_calls):  # noqa: F811
    _, token = _preview(client, [ALICE, BOB])
    _, job = _remove(client, token, [ALICE, BOB])
    assert (job.applied, job.failed_total) == (2, 0) and _removed(gam_calls) == [ALICE, BOB]
    audit = [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert ("remove_group_member", ALICE, True) in audit and ("remove_group_member", BOB, True) in audit
    again, _ = _remove(client, token, [ALICE, BOB])
    assert "expired or was already run" in again.text and len(_removed(gam_calls)) == 2     # single use


def test_a_selection_changed_since_the_preview_removes_nobody(client, gam_calls):  # noqa: F811
    _, token = _preview(client, [ALICE, BOB])
    r, job = _remove(client, token, [ALICE])
    assert "selection changed after the preview" in r.text and job is None and _removed(gam_calls) == []


def test_a_group_that_couldnt_be_read_again_offers_nothing(client, monkeypatch):  # noqa: F811
    async def refused(group):
        raise OSError("LEAK-MARKER")

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", refused)
    page, token = _preview(client, [ALICE])
    assert "Couldn't read sales@example.com's members again, so nothing is offered" in page and not token
    assert "LEAK-MARKER" not in page


def test_an_owner_is_named_and_more_than_25_need_the_count_typed(client, gam_calls, monkeypatch):  # noqa: F811
    many = [GroupMember(email=f"m{i:02}@example.com", role="OWNER" if i == 0 else "MEMBER") for i in range(30)]

    async def thirty(group):
        return many

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", thirty)
    picks = [m.email for m in many]
    page, token = _preview(client, picks)
    assert "1 is an owner" in page and "m00@example.com" in page and 'name="confirm_count"' in page
    r, job = _remove(client, token, picks, confirm_count="29")
    assert "type 30 to confirm" in r.text and job is None and gam_writes(gam_calls()) == []


def test_the_ones_that_failed_can_be_retried_alone(client, gam_calls, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.remove_group_member

    async def bob_fails(group, member):
        if member == BOB:
            preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=member, summary="x", risk=RiskLevel.LOW)
            return ChangeResult(preview=preview, ok=False, detail="ERROR: 503", remediation="Try again in a minute.")
        return await real(group, member)

    monkeypatch.setattr(conn, "remove_group_member", bob_fails)
    _, token = _preview(client, [ALICE, BOB])
    _, job = _remove(client, token, [ALICE, BOB])
    panel = unescape(client.get("/groups/members/remove-many/status", params={"job": job.id}).text)
    assert "Removed 1 of 2" in panel and "Try again in a minute." in panel and "Retry the 1 that failed" in panel
    monkeypatch.setattr(conn, "remove_group_member", real)
    retry = client.post("/groups/members/remove-many/retry", data={"job": job.id}).text
    assert "Remove 1 member from sales@example.com?" in unescape(retry)
    token = re.search(r'name="preview" value="([^"]+)"', retry).group(1)
    client.post("/groups/members/remove-many", data={"group": GROUP, "retry": job.id, "preview": token, "confirmed": "1"})
    for j in [j for j in client.app.state.gamgui.jobs.values() if j.kind == "group_remove"]:
        wait_for_job(client, j)
    assert _removed(gam_calls) == [ALICE, BOB]
