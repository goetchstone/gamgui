"""Reports' Inactive → suspend (roadmap 3.3, D3): the bucket comes from a cache, so the preview reads the directory again
and holds who it found; a new hire made inside the window is flagged; the admin GamGUI signs in as is left out by name."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape

from gamgui.core import reports
from gamgui.core.connectors.base import ChangeResult
from gamgui.core.gam.models import GAMUser

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
ALICE, CAROL = "alice@example.com", "carol@example.com"


def _user(email, last="2026-01-01T00:00:00Z", made="2024-01-01T00:00:00Z", uid="1", **kw):
    return GAMUser(primary_email=email, given_name=email.split("@")[0], last_login_time=last, creation_time=made,
                   raw={"id": uid}, **kw)


# --- who it would act on --------------------------------------------------------------------------------------------------

def test_the_admin_gamgui_signs_in_as_is_left_out_by_address_or_by_account_id():
    users = [_user("admin@x.com", uid="9"), _user("renamed@x.com", uid="7"), _user("a@x.com", uid="1")]
    targets, left_out = reports.inactive_to_suspend(users, "ADMIN@x.com", "7", now=NOW)
    assert [t.email for t in targets] == ["a@x.com"] and left_out == ["admin@x.com", "renamed@x.com"]


def test_a_new_hire_or_an_unknown_creation_is_flagged_never_just_inactive():
    users = [_user("old@x.com"), _user("hire@x.com", last=None, made="2026-11-01T00:00:00Z"), _user("unknown@x.com", made=None)]
    flags = {t.email: t.new for t in reports.inactive_to_suspend(users, "admin@x.com", "", now=NOW)[0]}
    assert flags == {"old@x.com": False, "hire@x.com": True, "unknown@x.com": True}


def test_only_active_inactive_accounts_are_offered():
    users = [_user("gone@x.com", suspended=True), _user("busy@x.com", last="2026-11-18T00:00:00Z"), _user("idle@x.com")]
    assert [t.email for t in reports.inactive_to_suspend(users, "admin@x.com", "", now=NOW)[0]] == ["idle@x.com"]


# --- the screen -------------------------------------------------------------------------------------------------------

def _preview(client):  # noqa: F811
    r = client.post("/reports/inactive/preview")
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _apply(client, token, **typed):  # noqa: F811
    r = client.post("/reports/inactive/apply", data={"preview": token, "confirmed": "1", "days": str(reports.INACTIVE_DAYS), **typed})
    st = client.app.state.gamgui
    job = next((j for j in st.jobs.values() if j.kind == "suspend_inactive"), None)
    if job is not None:
        wait_for_job(client, job)
    return r, job


def test_the_card_offers_it_and_the_preview_reads_again_and_writes_nothing(client, gam_calls):  # noqa: F811
    assert 'hx-post="/reports/inactive/preview"' in client.get("/reports").text
    reads = len([a for a in gam_calls() if a[:2] == ["print", "users"]])
    page, token = _preview(client)
    assert len([a for a in gam_calls() if a[:2] == ["print", "users"]]) == reads + 1     # read again, not the cache
    assert f"— {ALICE}" in page and f"— {CAROL}" in page and token and gam_writes(gam_calls()) == []


def test_apply_suspends_exactly_the_held_people_through_the_chokepoint(client, gam_calls):  # noqa: F811
    _, token = _preview(client)
    r, job = _apply(client, token)
    assert job is not None and (job.applied, job.failed_total) == (2, 0)
    assert sorted(gam_writes(gam_calls())) == [["update", "user", ALICE, "suspended", "on"], ["update", "user", CAROL, "suspended", "on"]]
    audit = [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert ("apply", ALICE, True) in audit and ("apply", CAROL, True) in audit
    again, _ = _apply(client, token)
    assert "expired or was already run" in again.text and len(gam_writes(gam_calls())) == 2     # single use


def test_the_connected_admin_is_left_out_and_said(client, gam_calls, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector

    async def alice_is_admin():
        return ALICE, "100000000000000000001", ""

    monkeypatch.setattr(conn, "connected_admin", alice_is_admin)
    page, token = _preview(client)
    assert f"Left out: {ALICE}, the admin GamGUI is connected as" in page and f"— {ALICE}" not in page
    _, job = _apply(client, token)
    assert gam_writes(gam_calls()) == [["update", "user", CAROL, "suspended", "on"]]


def test_a_renamed_admin_is_left_out_by_its_account_id(client, gam_calls, monkeypatch):  # noqa: F811
    # The sign-in token keeps the address the admin had when it was made; renamed since, only Google's account ID says it's
    # alice. The directory read must ask for `id` (the API returns only the fields named), or this match never happens.
    async def renamed():
        return "old.name@example.com", "100000000000000000001", ""

    monkeypatch.setattr(client.app.state.gamgui.connector, "connected_admin", renamed)
    page, _ = _preview(client)
    read = [a for a in gam_calls() if a[:2] == ["print", "users"]][-1]
    assert "id" in read[read.index("fields") + 1].split(",")
    assert f"Left out: {ALICE}, the admin GamGUI is connected as" in page and f"— {ALICE}" not in page


def test_an_admin_gamgui_cant_confirm_offers_nothing(client, gam_calls, monkeypatch):  # noqa: F811
    async def unconfirmed():
        return "", "", "its sign-in token doesn't say"

    monkeypatch.setattr(client.app.state.gamgui.connector, "connected_admin", unconfirmed)
    page, token = _preview(client)
    assert "can't confirm which account it signs in as" in page and not token and "doesn't say" not in page


def test_a_directory_that_couldnt_be_read_again_offers_nothing(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui

    async def refused(force=False, stale_ok=False):
        raise OSError("LEAK-MARKER")

    monkeypatch.setattr(st, "users", refused)
    page, token = _preview(client)
    assert "Couldn't read the directory again, so nothing is offered" in page and not token and "LEAK-MARKER" not in page


def test_someone_who_went_inactive_since_the_preview_isnt_suspended(client, gam_calls, monkeypatch):  # noqa: F811
    _, token = _preview(client)
    st = client.app.state.gamgui
    fresh = [_user("late@example.com")]

    async def grown(force=False, stale_ok=False):
        return fresh

    monkeypatch.setattr(st, "users", grown)
    _apply(client, token)
    assert sorted(w[2] for w in gam_writes(gam_calls())) == [ALICE, CAROL]     # the held list, never a rebuilt one


def test_ten_or_more_need_the_word_typed_and_more_than_25_the_count(client, gam_calls, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    many = [_user(f"idle{i:02}@example.com", uid=str(100 + i)) for i in range(30)]

    async def thirty(force=False, stale_ok=False):
        return many

    monkeypatch.setattr(st, "users", thirty)
    page, token = _preview(client)
    assert 'name="confirm"' in page and 'name="confirm_count"' in page
    refused, job = _apply(client, token)
    assert "Type confirm" in refused.text and job is None and gam_writes(gam_calls()) == []
    _, token = _preview(client)
    refused, job = _apply(client, token, confirm="confirm", confirm_count="29")
    assert "type 30 to confirm" in refused.text and job is None


def test_a_failed_suspend_is_listed_and_drops_the_cached_list(client, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector
    real = conn.apply

    async def one_fails(previews):
        if previews[0].target == CAROL:
            return [ChangeResult(preview=previews[0], ok=False, detail="GAM said no", remediation="Try again later.")]
        return await real(previews)

    monkeypatch.setattr(conn, "apply", one_fails)
    _, token = _preview(client)
    _, job = _apply(client, token)
    assert (job.applied, job.failed_items) == (1, [CAROL])
    page = unescape(client.get("/reports/inactive/status", params={"job": job.id}).text)
    assert "Suspended 1 of 2" in page and "Try again later." in page and "GAM said no" in page
    assert client.app.state.gamgui.user_cache.age_seconds is None    # dropped: the failed write's outcome isn't known
