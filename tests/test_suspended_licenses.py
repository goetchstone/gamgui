"""Suspended → remove their licenses (roadmap 4.3, D3): the Suspended card and the License usage run come from earlier reads,
so the preview reads the seats and the directory again and holds each seat that read found on a suspended account; Apply
removes exactly those as a stoppable job, each person's data transfers read just before their first seat goes, and a
transfer unfinished, failed or unread keeps their seats (invariant 10)."""

from __future__ import annotations

import re
from html import unescape
from types import SimpleNamespace

from gamgui.core import bulk, licenses
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser, LicenseSeat
from gamgui.core.phishing import Incomplete
from gamgui.web.jobs import start_job

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

BOB = "bob@example.com"
VAULT = ("Google-Vault", "Google-Vault-Former-Employee")
STANDARD = ("Google-Apps", "1010020028")


def _seat(user, product, sku, name=""):
    return LicenseSeat(user, product, sku, product, name or sku)


def _user(email, suspended=True):
    return GAMUser(primary_email=email, given_name=email.split("@")[0], suspended=suspended)


# --- who it would act on ---------------------------------------------------------------------------------------------------

def test_only_seats_the_directory_read_says_are_on_suspended_accounts_one_per_license():
    seats = [_seat("gone@x.com", "P", "b", "Beta"), _seat("gone@x.com", "P", "a", "Alpha"), _seat("gone@x.com", "P", "a", "Alpha"),
             _seat("here@x.com", "P", "a"), _seat("stray@x.com", "P", "a"), _seat("also@x.com", "Q", "q")]
    users = [_user("Gone@X.com"), _user("here@x.com", suspended=False), _user("also@x.com")]
    held = licenses.on_suspended(seats, users)
    # The address as the directory gives it, by address then license; a stray seat's holder is unknown, never "suspended".
    assert [(e, s.sku) for e, s in held] == [("also@x.com", "q"), ("Gone@X.com", "Alpha"), ("Gone@X.com", "Beta")]


def test_a_run_keeps_the_seats_of_anyone_whose_transfer_is_unfinished_failed_or_unread():
    assert licenses.many_refusal(BOB, []) == ""
    assert "hasn't finished" in licenses.many_refusal(BOB, [{"application": "Drive and Docs", "status": "inProgress"}])
    assert "failed (Drive and Docs)" in licenses.many_refusal(BOB, [{"application": "Drive and Docs", "status": "failed"}])
    assert "Couldn't check whether a data transfer" in licenses.many_refusal(BOB, None)


def test_a_seat_gam_cant_be_given_isnt_nameable():
    assert licenses.nameable(_seat(BOB, *STANDARD)) and not licenses.nameable(_seat(BOB, "a,b", "S"))


# --- the loop --------------------------------------------------------------------------------------------------------------

class _Conn:
    def __init__(self, transfers):
        self.transfers, self.reads, self.removed = transfers, [], []

    async def incomplete_transfers_for(self, email):
        self.reads.append(email)
        return self.transfers[email]

    async def remove_license(self, email, seat):
        self.removed.append((email, seat.sku_id))
        return SimpleNamespace(ok=True)


async def test_each_person_s_transfers_are_read_once_and_a_refusal_keeps_every_seat_of_theirs():
    held = [("a@x.com", _seat("a@x.com", "P", "1")), ("a@x.com", _seat("a@x.com", "Q", "2")),
            ("b@x.com", _seat("b@x.com", "P", "1")), ("b@x.com", _seat("b@x.com", "Q", "2")),
            ("c@x.com", _seat("c@x.com", "P", "1"))]
    conn = _Conn({"a@x.com": [], "b@x.com": [{"application": "Drive and Docs", "status": "new"}], "c@x.com": None})
    job = start_job({}, len(held), kind="suspended_licenses")
    await bulk.remove_licenses(job, conn, held)
    assert conn.reads == ["a@x.com", "b@x.com", "c@x.com"]                 # once a person, just before their first seat
    assert conn.removed == [("a@x.com", "1"), ("a@x.com", "2")]
    assert (job.applied, job.failed_total, job.finished) == (2, 3, True)
    assert job.failed_items == ["b@x.com — 1", "b@x.com — 2", "c@x.com — 1"]
    assert "hasn't finished" in job.failed[0].reason and "Couldn't check" in job.failed[2].reason


async def test_stop_is_honoured_between_seats():
    conn = _Conn({"a@x.com": []})
    job = start_job({}, 2, kind="suspended_licenses")
    job.cancel_requested = True
    await bulk.remove_licenses(job, conn, [("a@x.com", _seat("a@x.com", "P", "1")), ("a@x.com", _seat("a@x.com", "Q", "2"))])
    assert conn.removed == [] and "2 not attempted" in job.error


# --- the screen ------------------------------------------------------------------------------------------------------------

def _preview(client):  # noqa: F811
    r = client.post("/reports/suspended-licenses/preview")
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return " ".join(unescape(re.sub(r"<[^>]+>", "", r.text)).split()), (token.group(1) if token else ""), r.text


def _apply(client, token, **typed):  # noqa: F811
    r = client.post("/reports/suspended-licenses/apply", data={"preview": token, "confirmed": "1", **typed})
    st = client.app.state.gamgui
    job = next((j for j in st.jobs.values() if j.kind == "suspended_licenses"), None)
    if job is not None:
        wait_for_job(client, job)
    return r, job


def test_the_card_offers_it_and_the_preview_reads_both_again_and_writes_nothing(client, gam_calls):  # noqa: F811
    assert 'hx-post="/reports/suspended-licenses/preview"' in client.get("/reports").text
    before = len(gam_calls())
    page, token, _ = _preview(client)
    assert sorted(a[:2] for a in gam_calls()[before:]) == [["print", "licenses"], ["print", "users"]]   # not the cache
    assert "Remove 2 licenses from 1 suspended account" in page and token and gam_writes(gam_calls()) == []
    assert f"{BOB} — Google Vault - Former Employee (Google Vault)" in page
    assert f"{BOB} — Google Workspace Business Standard (Google Workspace)" in page
    assert licenses.MIGHT_LOSE_DATA in page and f"Type {BOB} to confirm" in page     # one person's: as their page asks
    assert "left@example.com" not in page                                              # not in the directory: unknown


def test_apply_removes_exactly_the_held_seats_through_the_chokepoint(client, gam_state, gam_calls):  # noqa: F811
    _, token, _ = _preview(client)
    r, job = _apply(client, token, confirm_email=BOB)
    assert job is not None and (job.applied, job.failed_total) == (2, 0)
    assert gam_writes(gam_calls()) == [GAMCommands.remove_license(BOB, *VAULT), GAMCommands.remove_license(BOB, *STANDARD)]
    audit = [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert audit.count(("remove_license", BOB, True)) == 2
    again, _ = _apply(client, token, confirm_email=BOB)
    assert "expired or was already run" in again.text and len(gam_writes(gam_calls())) == 2     # single use
    page, token, _ = _preview(client)
    assert "no suspended account holds any of the seats read" in page and not token             # the seats read again


def test_apply_runs_what_the_preview_held_not_what_a_later_read_would_find(client, monkeypatch, gam_state, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    _, token, _ = _preview(client)
    real_seats, real_users = st.connector.license_seats, st.users

    async def seats():   # carol is suspended after the preview, holding a seat: no preview showed her
        return [*await real_seats(), _seat("carol@example.com", *STANDARD)]

    async def users(force=False, **kw):
        return [GAMUser(primary_email=u.primary_email, given_name=u.given_name, suspended=True)
                if u.primary_email == "carol@example.com" else u for u in await real_users(force=force, **kw)]
    monkeypatch.setattr(st.connector, "license_seats", seats)
    monkeypatch.setattr(st, "users", users)
    _, job = _apply(client, token, confirm_email=BOB)
    assert job.applied == 2
    assert gam_writes(gam_calls()) == [GAMCommands.remove_license(BOB, *VAULT), GAMCommands.remove_license(BOB, *STANDARD)]
    page = _preview(client)[0]   # bob's are gone; a new preview offers her
    assert "Remove 1 license from 1 suspended account" in page and "carol@example.com — " in page


def test_one_person_s_address_must_be_typed(client, gam_calls):  # noqa: F811
    _, token, _ = _preview(client)
    r, job = _apply(client, token)
    assert "Type the exact email address" in unescape(r.text) and job is None and gam_writes(gam_calls()) == []


def test_an_unfinished_transfer_keeps_their_seats_and_says_why(client, monkeypatch, gam_calls):  # noqa: F811
    async def running(email):
        return [{"application": "Drive and Docs", "status": "inProgress"}]
    monkeypatch.setattr(client.app.state.gamgui.connector, "incomplete_transfers_for", running)
    _, token, _ = _preview(client)
    r, job = _apply(client, token, confirm_email=BOB)
    assert (job.applied, job.failed_total) == (0, 2) and gam_writes(gam_calls()) == []
    panel = " ".join(unescape(client.get(f"/reports/suspended-licenses/status?job={job.id}").text).split())
    assert "Removed 0 of 2 licenses." in panel and "Not removed (2)" in panel and "hasn't finished" in panel
    assert "Preview again" in panel


def test_seats_or_a_directory_that_cant_be_read_offer_nothing(client, monkeypatch, gam_calls):  # noqa: F811
    async def unread():
        raise Incomplete("GAM answered without the license seats, so who holds which license can't be told.")
    monkeypatch.setattr(client.app.state.gamgui.connector, "license_seats", unread)
    page, token, _ = _preview(client)
    assert "Couldn't read the license seats or the directory again, so nothing is offered." in page and not token
    assert "Nothing to remove" not in page and gam_writes(gam_calls()) == []


def test_many_people_s_seats_ask_for_the_word_and_above_the_limit_the_count(client, monkeypatch, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    people = [f"u{i:02}@example.com" for i in range(30)]

    async def seats():
        return [_seat(p, *STANDARD) for p in people]

    async def users(force=False):
        return [_user(p) for p in people]
    monkeypatch.setattr(st.connector, "license_seats", seats)
    monkeypatch.setattr(st, "users", users)
    page, token, html = _preview(client)
    assert "Remove 30 licenses from 30 suspended accounts" in page and "to confirm:" not in page   # no one address
    assert 'name="confirm"' in html and 'name="confirm_count"' in html and 'name="confirm_email"' not in html
    r, job = _apply(client, token, confirm="confirm")
    assert "type 30 to confirm" in unescape(r.text) and job is None and gam_writes(gam_calls()) == []


def test_the_license_run_offers_it_beside_its_suspended_seats(client):  # noqa: F811
    client.post("/reports/runs/start", data={"run": "license_usage"})
    st = client.app.state.gamgui
    wait_for_job(client, st.jobs[st.audit_run_jobs["license_usage"]])
    page = unescape(client.get("/reports/runs").text)
    assert "Seats on suspended accounts (2)" in page and "Remove these licenses…" in page
    assert 'hx-post="/reports/suspended-licenses/preview"' in page
