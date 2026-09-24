from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.calendar_index import CalendarIndex, IndexedCalendar
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.models import GAMUser
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import TEST_HOSTS, assert_ok_partial, gam_writes, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


def _audited(client, n):
    """The last ``n`` audit records as (action, target, ok) — what the chokepoint says happened."""
    return [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()[-n:]]


def _job(client, html, path):
    """The background job a route just started, found by the status URL in its polling panel."""
    m = re.search(path + r"\?job=([A-Za-z0-9_\-]+)", html)
    assert m, html[:300]
    return client.app.state.gamgui.jobs[m.group(1)]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


@pytest.fixture
def unconnected_client(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def test_users_list(client):
    r = client.get("/users")
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "bob@example.com" in r.text
    assert "Suspended" in r.text  # bob is suspended in the fixture


def test_users_table_search_filters(client):
    r = client.get("/users/table", params={"q": "ali", "scope": "all"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "carol@example.com" not in r.text  # in-memory filter excludes non-matches


def test_user_detail_shows_info(client):
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Alice Anders" in r.text                   # name shown (header + info block)
    assert "a.anders@example.com" in r.text           # alias (served from the cached directory)
    assert "Gmail signature" in r.text
    assert "IT Director" in r.text                    # title / role surfaced
    assert "Vacation responder" in r.text
    assert "Super admin" in r.text                    # admin status is visible in the header


def test_user_detail_marks_admin_status(client):
    # Regression: clicking your own (super-admin) account must surface the admin status.
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert "Super admin" in r.text
    # A non-admin must NOT be labelled as one.
    r2 = client.get("/users/detail", params={"email": "carol@example.com"})
    assert "Super admin" not in r2.text


# Each lazy panel on a user's detail page reads THAT user: (the GAM read it must send, what only
# Carol's data shows, what only Alice's shows). The mock answers per user, so a panel reading the
# wrong or a fixed user fails here; it used to answer Alice's data for anyone (review F20).
CAROL = "carol@example.com"
PANEL_READS = {
    "/users/signature/current": (["user", CAROL, "show", "signature"], "Carol Clark", "Alice"),
    "/users/groups": (["print", "groups", "member", CAROL], None, None),
    "/users/delegates": (["user", CAROL, "print", "delegates"], "helpdesk@example.com", "assistant@example.com"),
    "/users/calendar": (["user", CAROL, "print", "calendaracls", "primary", "formatjson"],
                        "helpdesk@example.com", "assistant@example.com"),
    "/users/vacation": (["user", CAROL, "show", "vacation"], "Conference week", "Out of office"),
}


@pytest.mark.parametrize("route", sorted(PANEL_READS))
def test_a_detail_panel_reads_the_user_it_is_for(client, gam_calls, route):
    argv, carols, alices = PANEL_READS[route]
    r = client.get(route, params={"email": CAROL})
    assert_ok_partial(r)
    assert argv in gam_calls(), gam_calls()
    if carols:
        assert carols in r.text and alices not in r.text


def test_user_detail_lazy_loads_delegates(client):
    # The detail page renders before the delegates gam call; delegates arrive via a lazy endpoint.
    page = client.get("/users/detail", params={"email": "alice@example.com"})
    assert "/users/delegates?email=" in page.text     # lazy trigger present
    assert "assistant@example.com" not in page.text   # not fetched inline
    lazy = client.get("/users/delegates", params={"email": "alice@example.com"})
    assert lazy.status_code == 200
    assert "assistant@example.com" in lazy.text        # delegate from the fixture
    assert "Remove" in lazy.text


def test_vacation_get_renders_current_state(client):
    r = client.get("/users/vacation", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Out of office" in r.text                  # current subject from mock
    assert "Save auto-reply" in r.text


def test_vacation_set_and_off(client, gam_calls):
    r = client.post("/users/vacation/set", data={"email": "alice@example.com", "subject": "OOO", "message": "away"})
    assert_ok_partial(r)
    assert "Save auto-reply" in r.text                # the refreshed vacation form, not an error
    r2 = client.post("/users/vacation/off", data={"email": "alice@example.com"})
    assert_ok_partial(r2)
    assert gam_writes(gam_calls()) == [
        ["user", "alice@example.com", "vacation", "on", "subject", "OOO", "message", "away", "html",
         "contactsonly", "false", "domainonly", "false", "start", "Started", "end", "NotSpecified"],
        ["user", "alice@example.com", "vacation", "off"],
    ]
    assert _audited(client, 2) == [("set_vacation", "alice@example.com", True),
                                   ("clear_vacation", "alice@example.com", True)]


def test_vacation_form_unticks_and_round_trips_its_dates(client, gam_state):
    # GAM merges `vacation` into the stored settings, so unticking "Domain only" used to send nothing
    # and the box came back ticked; the dates were never shown, so a blank box kept a hidden date.
    email = "alice@example.com"
    r = client.post("/users/vacation/set", data={"email": email, "subject": "OOO", "message": "away",
                                                 "domainonly": "on", "start": "2026-07-01", "end": "2026-07-10"})
    assert_ok_partial(r)
    assert re.search(r'name="domainonly" value="on"\s+checked', r.text)
    assert 'value="2026-07-01"' in r.text and 'value="2026-07-10"' in r.text   # the stored dates, shown
    r = client.post("/users/vacation/set", data={"email": email, "subject": "OOO", "message": "away"})
    assert_ok_partial(r)
    assert not re.search(r'name="domainonly" value="on"\s+checked', r.text)
    assert 'value="2026-07-01"' not in r.text and 'value="2026-07-10"' not in r.text


def test_users_list_has_title_column(client):
    r = client.get("/users")
    assert "Title" in r.text and "IT Director" in r.text


def test_reports_page_renders(client):
    r = client.get("/reports")
    assert r.status_code == 200
    assert "No 2-step verification" in r.text
    assert "carol@example.com" in r.text  # carol: active, no 2SV


def test_reports_requires_connection(unconnected_client):
    r = unconnected_client.get("/reports")
    assert r.status_code == 200
    assert "Connect a domain first" in r.text


def test_groups_board_renders(client):
    r = client.get("/groups")
    assert r.status_code == 200
    assert "alice@example.com" in r.text       # draggable person card
    assert "sales@example.com" in r.text        # group option


def test_groups_board_members_view_and_mutate(client, gam_calls):
    r = client.get("/groups/members", params={"group": "sales@example.com"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text        # member from the group-members fixture
    add = client.post("/groups/members", data={"group": "sales@example.com", "email": "carol@example.com", "op": "add"})
    assert_ok_partial(add)
    rem = client.post("/groups/members", data={"group": "sales@example.com", "email": "alice@example.com", "op": "remove"})
    assert_ok_partial(rem)
    assert gam_writes(gam_calls()) == [
        ["update", "group", "sales@example.com", "add", "member", "carol@example.com"],
        ["update", "group", "sales@example.com", "remove", "alice@example.com"],
    ]
    assert _audited(client, 2) == [("add_group_member", "carol@example.com", True),
                                   ("remove_group_member", "alice@example.com", True)]


def test_groups_board_mutate_failure_shows_the_error(client):
    # A typo'd group 404s in GAM; the board must say so rather than re-render as if it worked.
    r = client.post("/groups/members", data={"group": "missing@example.com", "email": "carol@example.com", "op": "add"})
    assert "border-amber-300 bg-amber-50" in r.text and "not found" in r.text
    assert _audited(client, 1) == [("add_group_member", "carol@example.com", False)]


def test_usage_report_renders(client):
    r = client.get("/reports/usage")
    assert r.status_code == 200
    assert "GB" in r.text
    assert "bob@example.com" in r.text  # largest storage in the mock


def test_signatures_page_renders(client):
    r = client.get("/signatures")
    assert r.status_code == 200
    assert "Signature designer" in r.text
    assert "{role}" in r.text  # variable reference present


def test_signatures_preview(client):
    r = client.post("/signatures/preview", data={"template": "{name} | {email}", "scope_type": "company", "scope_value": ""})
    assert r.status_code == 200
    assert "Applies to" in r.text
    assert "Alice Anders" in r.text   # rendered for a real (active) sample user


def _start_apply(client, body):
    """Preview, then POST the preview's Apply, and assert it started a job-polling panel (the job is
    awaited with wait_for_job, never by polling the status endpoint under TestClient)."""
    _, token = _sig_preview(client, **body)
    r = _sig_apply(client, token, **body)
    assert r.status_code == 200
    assert re.search(r"/signatures/apply/status\?job=[A-Za-z0-9_\-]+", r.text), r.text[:200]
    return r


def test_signatures_apply(client, gam_calls):
    r = _start_apply(client, {"template": "{name}", "scope_type": "company", "scope_value": ""})
    assert "Applying signature" in r.text
    job = _job(client, r.text, "/signatures/apply/status")
    wait_for_job(client, job)
    # Every active user (bob is suspended) got their own rendered signature, and GAM accepted each.
    assert (job.applied, job.failed_total, job.error) == (2, 0, None)
    assert gam_writes(gam_calls()) == [
        ["user", "alice@example.com", "signature", "Alice Anders", "html"],
        ["user", "carol@example.com", "signature", "Carol Clark", "html"],
    ]
    assert _audited(client, 2) == [("set_signature", "alice@example.com", True),
                                   ("set_signature", "carol@example.com", True)]
    done = client.get("/signatures/apply/status", params={"job": job.id})
    assert_ok_partial(done)
    assert "Applied to 2 of 2 users." in done.text


def test_signatures_apply_empty_scope_is_friendly(client, gam_calls):
    # An empty single-user selection must not bulk-apply: its preview offers no Apply (and holds
    # nothing to apply), and an Apply posted anyway starts no job.
    body = {"template": "{name}", "scope_type": "user", "scope_value": ""}
    shown, token = _sig_preview(client, **body)
    assert "No active users match this scope." in shown and "/signatures/apply" not in shown and token == ""
    r = _sig_apply(client, token, **body)
    assert r.status_code == 200 and "apply/status" not in r.text
    assert gam_writes(gam_calls()) == []


def test_signatures_designer_defaults_to_one_user(client, gam_calls):
    # The designer once opened on "Whole company": Preview → Apply overwrote everyone. Now it opens on
    # a single test user, and a POST that names no scope reaches nobody.
    r = client.get("/signatures")
    first = re.search(r'<select name="scope_type"[^>]*>\s*<option value="(\w+)"', r.text)
    assert first and first.group(1) == "user", r.text[:300]
    r = client.post("/signatures/preview", data={"template": "{name}"})
    assert "Applies to <strong>0</strong>" in r.text
    r = client.post("/signatures/apply", data={"template": "{name}", "confirmed": "1"})
    assert "apply/status" not in r.text
    assert gam_writes(gam_calls()) == []


def _signed_in_as(client, email):
    """Store an oauth2.txt whose ID-token claims name ``email`` as the connected admin."""
    client.app.state.gamgui.vault.set(DOMAIN, "oauth2", json.dumps(
        {"refresh_token": "r", "decoded_id_token": {"email": email, "hd": "example.com"}}))


def test_signatures_test_user_is_the_connected_admin(client):
    # "Specific user (test)" preselected whoever sorts first in the directory — a colleague (alice
    # here) — so the page's default path, Preview then Apply, overwrote their live signature (review
    # F18). It opens on the operator's own account when that is an active user.
    _signed_in_as(client, "Carol@example.com")
    r = client.get("/signatures")
    assert 'var SIG_DEFAULT_USER = "carol@example.com";' in r.text


@pytest.mark.parametrize("admin", ["tok", "outsider@example.com", "bob@example.com"])
def test_signatures_test_user_is_an_explicit_choice_otherwise(client, admin):
    # Unknown admin (an oauth2.txt without the claim), one outside the directory, or a suspended one:
    # the "Which" list opens on a blank placeholder, and a Preview of it matches nobody.
    if admin != "tok":
        _signed_in_as(client, admin)
    r = client.get("/signatures")
    assert 'var SIG_DEFAULT_USER = "";' in r.text and "Choose a user" in r.text


def test_signatures_apply_over_the_threshold_needs_the_count_typed(client, gam_calls, monkeypatch):
    from gamgui.core import guard

    monkeypatch.setattr(guard, "COUNT_CONFIRM_ABOVE", 1)     # the fixture tenant has two active users
    body = {"template": "{name}", "scope_type": "company", "scope_value": ""}
    shown, _ = _sig_preview(client, **body)
    button = re.search(r'<button id="sig-apply-btn"[^>]*>', shown).group(0)
    assert 'name="confirm_count"' in shown and "Type <strong>2</strong>" in shown
    assert re.search(r"\sdisabled[\s>]", button) and "hx-confirm" not in button and "#sig-confirm-count" in button
    for typed in ({}, {"confirm_count": "3"}, {"confirm_count": "all"}):   # the click alone is not enough
        _, token = _sig_preview(client, **body)
        r = _sig_apply(client, token, **body, **typed)
        assert "type 2 to confirm" in r.text and "apply/status" not in r.text, r.text[:300]
    assert gam_writes(gam_calls()) == []
    _, token = _sig_preview(client, **body)
    r = _sig_apply(client, token, **body, confirm_count="2")
    wait_for_job(client, _job(client, r.text, "/signatures/apply/status"))
    assert [c[1] for c in gam_writes(gam_calls())] == ["alice@example.com", "carol@example.com"]


def test_signatures_apply_under_the_threshold_keeps_the_click_confirm(client):
    shown = client.post("/signatures/preview", data={"template": "{name}", "scope_type": "company"}).text
    button = re.search(r'<button id="sig-apply-btn"[^>]*>', shown).group(0)
    assert 'name="confirm_count"' not in shown and "hx-confirm=" in button
    assert not re.search(r"\sdisabled[\s>]", button)


def _sig_preview(client, **body):
    """Preview a signature; return the page and the single-use token its Apply button posts ("" if none)."""
    r = client.post("/signatures/preview", data=body)
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', r.text)
    return r.text, (m.group(1) if m else "")


def _sig_apply(client, token, **body):
    """Post Apply as the preview's button does: the live form (hx-include) + confirmed + the token."""
    return client.post("/signatures/apply", data={**body, "confirmed": "1", "preview": token})


ALICE_ONLY = {"template": "{name}", "scope_type": "user", "scope_value": "alice@example.com"}


@pytest.mark.parametrize("edit", [{"scope_type": "company", "scope_value": ""},   # widened after the preview
                                  {"scope_value": "carol@example.com"},          # someone nobody previewed
                                  {"template": "{name} EDITED"}])                # a body nobody previewed
def test_signatures_apply_runs_only_what_was_previewed(client, gam_calls, edit):
    # Apply once posted the live form: preview one user, switch the scope to Whole company, and the
    # stale "Apply to 1 user" overwrote everyone — at 25 people or fewer nothing else asked.
    shown, token = _sig_preview(client, **ALICE_ONLY)
    assert "Apply to 1 user" in shown
    r = _sig_apply(client, token, **{**ALICE_ONLY, **edit})
    assert "The form changed after the preview" in r.text and "apply/status" not in r.text
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.jobs == {}


def test_signatures_apply_is_single_use(client, gam_calls):
    _, token = _sig_preview(client, **ALICE_ONLY)
    wait_for_job(client, _job(client, _sig_apply(client, token, **ALICE_ONLY).text, "/signatures/apply/status"))
    assert len(gam_writes(gam_calls())) == 1
    r = _sig_apply(client, token, **ALICE_ONLY)                      # a replayed click
    assert "expired or was already run" in r.text and len(gam_writes(gam_calls())) == 1


def test_signatures_apply_writes_the_previewed_people_and_asks_their_count(client, gam_calls, monkeypatch):
    # The people written and the count to type are the preview's: someone who joined the scope after
    # the preview is neither written nor counted (apply once re-resolved the scope at click time).
    from gamgui.core import guard
    from gamgui.core.gam.models import GAMUser

    monkeypatch.setattr(guard, "COUNT_CONFIRM_ABOVE", 1)
    body = {"template": "{name}", "scope_type": "company", "scope_value": ""}
    shown, token = _sig_preview(client, **body)
    assert "Type <strong>2</strong>" in shown
    st = client.app.state.gamgui
    grown = client.portal.call(st.users) + [GAMUser(primary_email="dan@example.com", given_name="Dan")]

    async def users(force=False):
        return grown

    monkeypatch.setattr(st, "users", users)
    r = _sig_apply(client, token, **body, confirm_count="2")
    wait_for_job(client, _job(client, r.text, "/signatures/apply/status"))
    assert [c[1] for c in gam_writes(gam_calls())] == ["alice@example.com", "carol@example.com"]


def test_signatures_apply_status_unknown_job(client):
    r = client.get("/signatures/apply/status", params={"job": "nope"})
    assert r.status_code == 200
    assert "no longer available" in r.text


def test_job_record_tracks_tallies_and_feed():
    from gamgui.web.jobs import Job

    job = Job(total=3)
    job.record("a@x.com", True)
    job.record("b@x.com", False)
    job.record("c@x.com", True)
    assert (job.applied, job.failed_total, job.done, job.more) == (2, 1, 3, 0)
    assert job.failed_items == ["b@x.com"]
    # the live feed carries each outcome in order, newest last
    assert [(r.item, r.ok) for r in job.recent] == [
        ("a@x.com", True), ("b@x.com", False), ("c@x.com", True),
    ]
    assert not job.finished and not job.cancel_requested and not job.interrupted
    job.current = "c@x.com"
    job.finish()
    assert job.finished and job.finished_at > 0 and job.current == ""


@pytest.mark.parametrize("make", ["Job", "BatchJob", "OnboardJob"])
def test_every_job_record_stays_bounded_at_scale(make):
    # A domain-wide run must not let the polled status HTML grow with the user count: the retained
    # failed sample, the live feed and the text kept per failure are capped however many are processed.
    # One base (web/jobs.py) holds the bounds; each bulk job type is one.
    from gamgui.web import jobs
    from gamgui.web.jobs import DETAIL_CAP, FAILED_SAMPLE_CAP, RECENT_WINDOW
    from gamgui.web.routes.onboarding import OnboardJob

    job = {"Job": jobs.Job, "BatchJob": jobs.BatchJob, "OnboardJob": OnboardJob}[make](total=5000)
    for i in range(5000):   # half succeed, half fail — each failure with GAM echoing a huge body
        job.record(f"u{i}@x.com", ok=(i % 2 == 0), reason="r" * 10_000, detail="x" * 10_000)
    assert (job.done, job.applied, job.failed_total) == (5000, 2500, 2500)
    assert len(job.failed) == FAILED_SAMPLE_CAP     # sample capped, full count kept in failed_total
    assert job.more == 2500 - FAILED_SAMPLE_CAP     # the "+K more" a final panel prints
    assert all(len(r.detail) <= DETAIL_CAP and len(r.reason) <= DETAIL_CAP for r in job.failed + job.recent)
    assert len(job.recent) == RECENT_WINDOW         # feed is a fixed-size rolling window
    assert job.recent[-1].item == "u4999@x.com"     # newest last
    assert job.recent[0].item == f"u{5000 - RECENT_WINDOW}@x.com"


def test_signatures_apply_status_shows_live_feed(client):
    from gamgui.web.jobs import Job

    st = client.app.state.gamgui
    job = Job(id="feedtest", total=4)
    job.record("alice@example.com", True)
    job.record("bob@example.com", False)
    st.jobs[job.id] = job

    r = client.get("/signatures/apply/status", params={"job": job.id})
    assert r.status_code == 200
    # running tallies in the header
    assert "1 set" in r.text and "1 failed" in r.text
    # per-user feed: each processed user shown with an outcome marker, newest first
    assert "alice@example.com" in r.text and "bob@example.com" in r.text
    assert "✓" in r.text and "✗" in r.text
    assert r.text.index("bob@example.com") < r.text.index("alice@example.com")


def test_signatures_apply_final_summary_caps_failed_list(client):
    from gamgui.web.jobs import Job

    st = client.app.state.gamgui
    job = Job(id="captest", total=300)
    for i in range(300):
        job.record(f"u{i}@example.com", False)
    job.finished = True
    st.jobs[job.id] = job

    r = client.get("/signatures/apply/status", params={"job": job.id})
    assert r.status_code == 200
    assert "Failed (300)" in r.text  # full count, not the capped sample length
    assert "more" in r.text          # "+N more" overflow indicator for the truncated list


def test_signatures_apply_failure_keeps_a_reason_per_user(client):
    # The feed once listed failed emails only; each failure now says why in words, with GAM's own
    # error one click away — in the live feed and the final summary.
    from gamgui.core.gam.models import GAMUser
    from gamgui.web.jobs import Job
    from gamgui.web.routes.signatures import _run_apply

    st = client.app.state.gamgui
    job = Job(id="whytest", total=2)
    st.jobs[job.id] = job
    matched = [GAMUser("alice@example.com", "Alice"), GAMUser("gone-missing@example.com", "Gone")]
    client.portal.call(_run_apply, job, st.connector, matched, "{name}")
    not_found = "The requested user, group, or resource was not found."
    assert (job.applied, job.failed_total) == (1, 1)
    assert (job.failed[0].item, job.failed[0].reason) == ("gone-missing@example.com", not_found)
    assert "Does not exist" in job.failed[0].detail
    done = client.get("/signatures/apply/status", params={"job": job.id}).text
    assert f'gone-missing@example.com</span> — {not_found}' in done
    assert "<details" in done and "Does not exist" in done
    job.finished = False                                  # the same job, as the live feed shows it
    live = client.get("/signatures/apply/status", params={"job": job.id}).text
    assert "✗" in live and f"— {not_found}" in live and "Does not exist" not in live


def test_signatures_apply_stops_when_the_sign_in_has_expired(client, gam_calls, monkeypatch):
    # An expired admin sign-in fails every user the same way; the loop once kept going through the
    # whole company, burying the cause under one identical failure per mailbox.
    from gamgui.core.gam.errors import GAMError

    runner = client.app.state.gamgui.connector.runner
    real = runner.run_authenticated

    async def expired_writes(domain, argv, **kw):
        if kw.get("serialize"):
            raise GAMError.from_run(1, "ERROR: invalid_grant: Token has been expired or revoked", argv)
        return await real(domain, argv, **kw)

    monkeypatch.setattr(runner, "run_authenticated", expired_writes)
    r = _start_apply(client, {"template": "{name}", "scope_type": "company", "scope_value": ""})
    job = _job(client, r.text, "/signatures/apply/status")
    wait_for_job(client, job)
    assert (job.done, job.applied, job.failed_total) == (1, 0, 1)      # alice tried; carol never was
    assert _audited(client, 1) == [("set_signature", "alice@example.com", False)]
    done = client.get("/signatures/apply/status", params={"job": job.id}).text
    assert "Stopped: Your sign-in expired. Re-run setup to refresh authorization." in done
    assert "The remaining 1 was not attempted." in done and "Applied to 0 of 2 before stopping." in done
    assert "<details" in done and "invalid_grant" in done               # GAM's own error, one click away


def test_signature_set_failure_says_why_with_gams_error_expandable(client):
    r = client.post("/users/signature", data={"email": "gone-missing@example.com", "signature": "Hi"})
    headline, _, raw = r.text.partition("<details")
    assert "Couldn&#39;t set the signature. The requested user, group, or resource was not found." in headline
    assert "GAM failed" not in headline and "Does not exist" in raw


def test_signatures_preview_user_scope(client):
    r = client.post("/signatures/preview", data={"template": "{name} <{email}>", "scope_type": "user", "scope_value": "alice@example.com"})
    assert r.status_code == 200
    assert "Applies to" in r.text
    assert "Alice Anders" in r.text   # rendered for the single chosen user
    assert "bob@example.com" not in r.text  # nobody else in scope


def test_signatures_preview_group_scope(client):
    r = client.post("/signatures/preview", data={"template": "{name}", "scope_type": "group", "scope_value": "sales@example.com"})
    assert r.status_code == 200
    assert "Applies to" in r.text
    assert "Alice Anders" in r.text  # group member (suspended members excluded)


def test_signature_current_renders(client):
    r = client.get("/users/signature/current", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Best," in r.text  # current signature read from the mailbox
    assert "<iframe" in r.text and "srcdoc=" in r.text  # rendered preview, not just source
    assert "View HTML source" in r.text                  # raw HTML still available, collapsed
    # one-click copy of the raw HTML (copyEl copies the <pre> inside the .copy-wrap)
    assert "copy-wrap" in r.text and 'onclick="copyEl(this)"' in r.text and "Copy HTML" in r.text


def test_user_groups_view_add_remove(client, gam_calls):
    r = client.get("/users/groups", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "sales@example.com" in r.text            # current membership
    assert "it@example.com" in r.text               # available group in the add picker
    add = client.post("/users/groups/add", data={"email": "alice@example.com", "group": "it@example.com"})
    assert_ok_partial(add)
    rem = client.post("/users/groups/remove", data={"email": "alice@example.com", "group": "sales@example.com"})
    assert_ok_partial(rem)
    assert gam_writes(gam_calls()) == [
        ["update", "group", "it@example.com", "add", "member", "alice@example.com"],
        ["update", "group", "sales@example.com", "remove", "alice@example.com"],
    ]
    assert _audited(client, 2) == [("add_group_member", "alice@example.com", True),
                                   ("remove_group_member", "alice@example.com", True)]


def test_suspended_user_detail_shows_unsuspend(client):
    # Regression: the _suspend_zone include must receive `suspended` from the user.
    r = client.get("/users/detail", params={"email": "bob@example.com"})
    assert r.status_code == 200
    assert "Suspended" in r.text
    assert "Unsuspend" in r.text


def test_detail_has_role_store_editor(client):
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert 'name="department"' in r.text and 'name="title"' in r.text  # editable title/department form
    assert "Save title" in r.text


def test_set_organization_saves(client):
    r = client.post("/users/organization", data={"email": "alice@example.com", "title": "Design Lead", "department": "Marketing"})
    assert r.status_code == 200
    assert "Saved" in r.text
    assert "Marketing" in r.text  # the new value is echoed back into the form


def test_set_organization_refreshes_the_displayed_role(client):
    # The form only swaps itself, so without out-of-band updates the header + Title row keep the
    # pre-save value until a full reload — which looked like the title didn't save. The save must
    # carry hx-swap-oob refreshes for both spots that show the role.
    r = client.post("/users/organization",
                    data={"email": "alice@example.com", "title": "Account Manager", "department": "Marketing"})
    assert r.status_code == 200
    assert 'id="dtl-subtitle" hx-swap-oob="true"' in r.text
    assert 'id="dtl-title" hx-swap-oob="true"' in r.text
    # both OOB elements carry the just-saved value
    assert r.text.count("Account Manager") >= 2


def test_bulk_store_page_renders(client):
    r = client.get("/users/bulk")
    assert r.status_code == 200
    assert "Bulk: set department" in r.text
    assert 'name="store"' in r.text and 'name="emails"' in r.text


def test_bulk_store_preview_by_emails(client):
    r = client.post("/users/bulk/preview", data={"store": "Marketing", "group": "", "emails": "alice@example.com"})
    assert r.status_code == 200
    assert "Marketing" in r.text
    assert "alice@example.com" in r.text
    assert "Apply to 1" in r.text


def test_bulk_store_apply_requires_store_value(client):
    r = client.post("/users/bulk/apply", data={"store": "   ", "group": "", "emails": "alice@example.com"})
    assert "Enter a department first" in r.text


def _bulk_preview(client, **form):
    """Preview a bulk department change; return the page and its Apply button's token ("" if none)."""
    r = client.post("/users/bulk/preview", data=form)
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', r.text)
    return r.text, (m.group(1) if m else "")


def _bulk_apply(client, token, **form):
    """Post Apply as the preview's button does: the live form (hx-include) + confirmed + the token."""
    return client.post("/users/bulk/apply", data={**form, "confirmed": "1", "preview": token})


BULK_ALICE = {"store": "Sales", "group": "", "emails": "alice@example.com"}


@pytest.mark.parametrize("edit", [{"store": "Finance"},                                       # another value
                                  {"emails": "alice@example.com\ncarol@example.com"},         # more people
                                  {"emails": "", "group": "sales@example.com"}])              # a group instead
def test_bulk_store_apply_runs_only_what_was_previewed(client, gam_calls, edit):
    # Apply once posted the live form: preview Sales on alice, change the list and the department,
    # and the stale "Apply to 1 user" wrote Finance to both, under a dialog naming Sales and 1 user.
    shown, token = _bulk_preview(client, **BULK_ALICE)
    assert "Apply to 1 user" in shown
    r = _bulk_apply(client, token, **{**BULK_ALICE, **edit})
    assert "The form changed after the preview" in r.text
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.jobs == {}


def test_bulk_store_apply_is_single_use(client, gam_calls):
    _, token = _bulk_preview(client, **BULK_ALICE)
    wait_for_job(client, _job(client, _bulk_apply(client, token, **BULK_ALICE).text, "/users/bulk/status"))
    assert len(gam_writes(gam_calls())) == 1
    r = _bulk_apply(client, token, **BULK_ALICE)                     # a replayed click
    assert "expired or was already run" in r.text and len(gam_writes(gam_calls())) == 1


@pytest.mark.parametrize("directory", [[GAMUser("alice@example.com", suspended=True)], []],
                         ids=["suspended", "gone"])
def test_bulk_store_apply_refuses_someone_no_longer_active(client, gam_calls, monkeypatch, directory):
    # Alice was active at the preview; by Apply she is suspended or deleted, so nothing is written.
    _, token = _bulk_preview(client, **BULK_ALICE)

    async def users(force=False):
        return directory

    monkeypatch.setattr(client.app.state.gamgui, "users", users)
    r = _bulk_apply(client, token, **BULK_ALICE)
    assert "no longer an active user" in r.text
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.jobs == {}


def test_bulk_store_apply_runs_as_job(client, gam_calls):
    form = {"store": "Downtown", "group": "", "emails": "alice@example.com"}
    _, token = _bulk_preview(client, **form)
    cache = client.app.state.gamgui.user_cache
    assert cache._items is not None                              # the preview read the directory
    r = _bulk_apply(client, token, **form)
    assert_ok_partial(r)
    job = _job(client, r.text, "/users/bulk/status")
    wait_for_job(client, job)
    assert cache._items is None                                  # dropped, so the new departments show
    # Department set to the store, alice's existing title kept — and GAM accepted it.
    assert (job.applied, job.failed, job.error) == (1, [], None)
    assert gam_writes(gam_calls()) == [
        ["update", "user", "alice@example.com", "organization", "title", "IT Director",
         "department", "Downtown", "primary"],
    ]
    assert _audited(client, 1) == [("set_organization", "alice@example.com", True)]
    assert_ok_partial(client.get("/users/bulk/status", params={"job": job.id}))


async def test_set_departments_preserves_title_and_sets_department():
    # The bulk department loop, driven directly: the department is set, each person's existing title kept.
    from gamgui.core.bulk import set_departments
    from gamgui.core.gam.models import GAMUser
    from gamgui.web.jobs import BatchJob

    calls = []

    class _FakeResult:
        ok = True

    class _FakeConn:
        async def set_organization(self, email, title="", department=""):
            calls.append((email, title, department))
            return _FakeResult()

    targets = [
        GAMUser.from_json({"primaryEmail": "a@e.com", "organizations": [{"title": "Design Lead", "primary": True}]}),
        GAMUser.from_json({"primaryEmail": "b@e.com"}),  # no title
    ]
    job = BatchJob(id="t", total=len(targets))
    await set_departments(job, _FakeConn(), targets, "Marketing")

    assert job.finished and job.applied == 2 and job.failed == []
    assert calls == [("a@e.com", "Design Lead", "Marketing"), ("b@e.com", "", "Marketing")]


def test_calendar_access_view(client):
    r = client.get("/users/calendar", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "assistant@example.com" in r.text          # someone the calendar is shared with
    assert "Public (anyone)" in r.text                 # the default/public freebusy rule
    assert "See all event details" in r.text or "free/busy" in r.text.lower()
    assert "Remove" in r.text and 'name="target"' in r.text  # remove buttons + share form


def test_calendar_access_owner_not_removable(client):
    r = client.get("/users/calendar", params={"email": "alice@example.com"})
    # One remove button per shared party (assistant + the default rule) — never for the owner.
    assert r.text.count('hx-post="/users/calendar/remove"') == 2


def test_calendar_access_add_and_remove(client):
    add = client.post("/users/calendar/add", data={"email": "alice@example.com", "target": "carol@example.com", "role": "reader"})
    assert add.status_code == 200
    assert "assistant@example.com" in add.text  # re-rendered ACL list after the change
    rem = client.post("/users/calendar/remove", data={"email": "alice@example.com", "scope": "assistant@example.com"})
    assert rem.status_code == 200


def test_calendar_access_add_requires_target(client):
    r = client.post("/users/calendar/add", data={"email": "alice@example.com", "target": "  ", "role": "reader"})
    assert "Enter an email to share with." in r.text


@pytest.mark.parametrize("path,data", [
    ("/users/calendar/add", {"email": "alice@example.com", "target": "carol@example.com"}),
    ("/calendars/share", {"cal": "c_train123@group.calendar.google.com", "target": "carol@example.com"}),
])
def test_calendar_share_refuses_a_role_outside_the_grammar(client, gam_calls, path, data):
    # Both share paths: the builder's <CalendarACLRole> check surfaces as a friendly error, no gam write.
    # (/calendars/share used to coerce an unknown role to reader; /users/calendar/add passed it to GAM.)
    r = client.post(path, data={**data, "role": "admin"})
    assert r.status_code == 200
    assert "Couldn&#39;t share calendar: invalid calendar role &#39;admin&#39;" in r.text
    assert gam_writes(gam_calls()) == []


def test_calendars_page_renders(client):
    r = client.get("/calendars")
    assert r.status_code == 200
    assert "Calendars" in r.text and "Find a calendar by name" in r.text


def test_calendars_resources_search(client):
    r = client.get("/calendars/resources", params={"q": "aspen"})
    assert r.status_code == 200
    assert "Aspen Conference Room" in r.text and "View access" in r.text


def test_calendars_user_list(client):
    r = client.get("/calendars/user", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Team Events" in r.text


def _seed_index(client):
    """Populate the persistent index as a rebuild would (background build doesn't run under TestClient)."""
    client.app.state.gamgui.calendar_index.replace_all(DOMAIN, [
        IndexedCalendar("c_train123@group.calendar.google.com", "Training Calendar", "alice@example.com", "secondary", 2),
        IndexedCalendar("c_ops999@group.calendar.google.com", "Operations", "carol@example.com", "secondary", 1),
        IndexedCalendar("aspen@resource.calendar.google.com", "Aspen Conference Room", "", "room", 0),
    ])


def test_calendars_search_by_name_finds_secondary_and_owner(client):
    _seed_index(client)
    r = client.get("/calendars/search", params={"q": "training"})
    assert r.status_code == 200
    assert "Training Calendar" in r.text
    assert "owned by alice@example.com" in r.text   # owner identified from the index
    assert "Operations" not in r.text                # filtered out by the name query
    assert "View access" in r.text                   # click-through to details


def test_calendars_search_also_matches_rooms(client):
    _seed_index(client)
    r = client.get("/calendars/search", params={"q": "aspen"})
    assert "Aspen Conference Room" in r.text


def test_calendars_search_empty_index_prompts_build(client):
    # Nothing indexed yet -> guide the user to build it, don't silently return nothing.
    r = client.get("/calendars/search", params={"q": "training"})
    assert r.status_code == 200
    assert "Training Calendar" not in r.text
    assert "No calendar index yet" in r.text


def test_calendars_search_ignores_other_domains_index(client):
    # An index built for a DIFFERENT tenant must never be served (correctness + no cross-tenant leak).
    client.app.state.gamgui.calendar_index.replace_all("other-tenant.com", [
        IndexedCalendar("c_x@group.calendar.google.com", "Other Co Layoffs", "x@other-tenant.com", "secondary", 1)])
    r = client.get("/calendars/search", params={"q": "layoffs"})
    assert "Other Co Layoffs" not in r.text          # wrong domain -> not served
    assert "No calendar index yet" in r.text          # prompts a rebuild for the active domain


def test_calendars_page_shows_build_cta_when_empty(client):
    r = client.get("/calendars")
    assert "Build index" in r.text                   # the one-time build call-to-action


def test_calendars_index_rebuild_starts_background_job(client):
    r = client.post("/calendars/index/rebuild")
    assert r.status_code == 200
    assert "Scanning every user" in r.text           # progress partial
    assert "/calendars/index/status" in r.text       # self-poll wired up


def test_calendars_detail_shows_access_and_event_search(client):
    r = client.get("/calendars/detail", params={"cal": "aspen@resource.calendar.google.com"})
    assert r.status_code == 200
    assert "Who has access" in r.text
    assert "assistant@example.com" in r.text          # ACL rule from the fixture
    assert 'hx-get="/calendars/events"' in r.text      # event-search form present


def test_calendars_event_search_requires_filter(client):
    # No query/date -> no unbounded all-events scan.
    r = client.get("/calendars/events", params={"cal": "aspen@resource.calendar.google.com"})
    assert "Enter a title" in r.text


def test_calendars_event_search_flags_recurring(client):
    r = client.get("/calendars/events", params={"cal": "aspen@resource.calendar.google.com", "q": "stand"})
    assert "Weekly Standup" in r.text and "recurring" in r.text and "Delete" in r.text


def test_calendars_event_delete_preview_warns_on_recurring(client):
    r = client.post("/calendars/event/preview",
                    data={"cal": "aspen@resource.calendar.google.com", "event_id": "evt-weekly-standup"})
    assert r.status_code == 200
    assert "Delete this event?" in r.text
    assert "entire series" in r.text                    # recurring warning
    assert 'hx-post="/calendars/event/delete"' in r.text  # confirm button (guarded)


def test_calendars_event_delete_applies(client):
    r = client.post("/calendars/event/delete",
                    data={"cal": "aspen@resource.calendar.google.com", "event_id": "evt-weekly-standup",
                          "confirmed": "1"})
    assert r.status_code == 200
    assert "Event deleted." in r.text


SEC_CAL = "c_train123@group.calendar.google.com"      # secondary; owner alice (active) per fixtures
ORPHAN_CAL = "c_orphan@group.calendar.google.com"     # secondary; sole owner bob (suspended)


def test_calendars_detail_shows_delete_zone_for_secondary(client):
    r = client.get("/calendars/detail", params={"cal": SEC_CAL, "label": "Training Calendar"})
    assert r.status_code == 200
    assert "Danger zone" in r.text
    assert "Delete this calendar" in r.text                 # delete button present (active owner found)
    assert 'hx-post="/calendars/delete/preview"' in r.text


def test_calendars_detail_no_delete_zone_for_primary(client):
    # A primary calendar id IS a user's email — never deletable here.
    r = client.get("/calendars/detail", params={"cal": "alice@example.com"})
    assert r.status_code == 200
    assert "Danger zone" not in r.text
    assert "Delete this calendar" not in r.text


def test_calendars_detail_blocks_delete_when_owner_suspended(client):
    # Ex-employee case: the calendar's only owner is suspended -> no danger zone, explain why.
    r = client.get("/calendars/detail", params={"cal": ORPHAN_CAL})
    assert r.status_code == 200
    assert "Delete this calendar" not in r.text
    assert "suspended or no longer exist" in r.text


def test_calendars_delete_preview_shows_owner_and_confirm(client):
    r = client.post("/calendars/delete/preview", data={"cal": SEC_CAL, "label": "Training Calendar", "acl_count": 3})
    assert r.status_code == 200
    assert "Type" in r.text and "DELETE" in r.text
    assert "cannot be undone" in r.text
    assert "alice@example.com" in r.text                    # the active owner we'll act as


def test_calendars_delete_requires_exact_case_confirm(client):
    r = client.post("/calendars/delete", data={"cal": SEC_CAL, "confirm": "delete"})  # lowercase
    assert r.status_code == 200
    assert "Calendar deleted" not in r.text
    assert "capitals" in r.text                             # re-prompts, does not delete


def test_calendars_delete_applies_with_confirm(client):
    r = client.post("/calendars/delete", data={"cal": SEC_CAL, "confirm": "DELETE", "label": "Training Calendar"})
    assert r.status_code == 200
    assert "Calendar deleted" in r.text


def test_calendars_delete_refuses_primary(client):
    r = client.post("/calendars/delete", data={"cal": "alice@example.com", "confirm": "DELETE"})
    assert "Only secondary calendars" in r.text
    assert "Calendar deleted" not in r.text


def test_calendars_delete_refuses_resource_and_holiday(client):
    for cal in ("aspen@resource.calendar.google.com", "en.usa#holiday@group.v.calendar.google.com"):
        r = client.post("/calendars/delete", data={"cal": cal, "confirm": "DELETE"})
        assert "Only secondary calendars" in r.text, cal
        assert "Calendar deleted" not in r.text, cal


def test_calendars_delete_refuses_when_owner_suspended(client):
    r = client.post("/calendars/delete", data={"cal": ORPHAN_CAL, "confirm": "DELETE"})
    assert "Calendar deleted" not in r.text
    assert "suspended or no longer exist" in r.text


def test_calendars_share_adds_acl_and_subscribes(client):
    r = client.post("/calendars/share",
                    data={"cal": SEC_CAL, "target": "assistant@example.com", "role": "reader", "label": "Training Calendar"})
    assert r.status_code == 200
    assert "assistant@example.com" in r.text               # ACL list re-rendered
    assert "appear in their Google Calendar" in r.text      # subscribed -> emerald notice
    assert 'hx-post="/calendars/share"' in r.text           # share form still present


def _fanout_job_rendered(html: str) -> bool:
    """A subscribe fan-out job was created and rendered — either the live poller (job still running)
    or the final panel (the instant test stub finished it within the request). Both prove the job
    existed; which one shows is a timing detail, not behaviour worth pinning."""
    return "/calendars/share/status?job=" in html or "now appears in" in html


def _stub_subscribe(client, monkeypatch):
    """Make the fan-out job's per-member subscribe instant and I/O-free. These tests only assert the
    job STARTED; letting the real background task spawn a subprocess per member (via mock gam) leaves
    it racing TestClient teardown — a hang that showed up as a 60s timeout on one CI runner."""
    async def _instant(email, cal, selected=True):
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(client.app.state.gamgui.connector, "subscribe_calendar_for", _instant)


def test_calendars_share_group_fans_out_to_members(client, monkeypatch):
    # CHANGED BEHAVIOUR (was: "groups can't be auto-subscribed"). An ACL grants access but does not
    # put the calendar on anyone's list, which is why members kept reporting they couldn't see it —
    # so a group grant now subscribes each member as a background job.
    _stub_subscribe(client, monkeypatch)
    r = client.post("/calendars/share",
                    data={"cal": SEC_CAL, "target": "group:team@example.com", "role": "reader"})
    assert r.status_code == 200
    # The share-notice proves the group fan-out path was taken (a job was created for 2 members).
    assert "adding it to 2 members' calendars" in r.text or "adding it to 2 members&#39; calendars" in r.text
    # A fan-out job was created and rendered — still polling, or (with the instant stub) already done.
    assert _fanout_job_rendered(r.text), r.text[:400]


def test_calendars_share_bare_group_address_also_fans_out(client, monkeypatch):
    # No "group:" prefix: a bare address is ambiguous, and it has to resolve as a group from the
    # directory rather than from how it was typed.
    _stub_subscribe(client, monkeypatch)
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "sales@example.com"})
    assert r.status_code == 200
    assert "members" in r.text
    assert _fanout_job_rendered(r.text)


ALLHANDS = [f"member{i:02d}@example.com" for i in range(1, 13)]   # the mock's 12-member group


def test_calendars_share_to_a_large_group_asks_first_with_the_member_count(client, gam_calls):
    # Sharing with a group subscribes every member, one write each. Past the bulk threshold (10) the
    # guard wants a confirm; the route once started the job straight from the Share button.
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "group:allhands@example.com",
                                              "role": "reader", "label": "Training"})
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.jobs == {}
    text = unescape(r.text)
    assert "adds it to 12 members' calendars" in text and 'hx-post="/calendars/share/group"' in r.text
    assert 'value="group:allhands@example.com"' in r.text          # the form keeps what was typed
    token = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', text).group(1)
    form = {"cal": SEC_CAL, "target": "group:allhands@example.com", "role": "reader", "label": "Training"}
    refused = client.post("/calendars/share/group", data={**form, "preview": token})
    assert "needs confirmation" in refused.text and gam_writes(gam_calls()) == []
    r = client.post("/calendars/share", data=form)                  # a fresh confirm step
    token = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(r.text)).group(1)
    done = client.post("/calendars/share/group", data={**form, "preview": token, "confirmed": "1"})
    job = _job(client, done.text, "/calendars/share/status")
    wait_for_job(client, job)
    writes = gam_writes(gam_calls())
    assert writes[0] == ["calendars", SEC_CAL, "add", "calendaracls", "reader", "group:allhands@example.com"]
    assert [w[1] for w in writes[1:]] == ALLHANDS and (job.applied, job.total) == (12, 12)


def test_calendars_share_to_a_small_group_needs_no_confirm(client, monkeypatch):
    _stub_subscribe(client, monkeypatch)
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "group:team@example.com"})
    assert "/calendars/share/group" not in r.text and _fanout_job_rendered(r.text)


def test_calendars_share_known_user_is_never_treated_as_a_group(client):
    # The regression this guards: mistaking a person for a group skips the subscribe entirely, which
    # is the exact bug the feature exists to fix. A cached directory account wins over any lookup.
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "alice@example.com"})
    assert r.status_code == 200
    assert "appear in their Google Calendar" in r.text
    assert "/calendars/share/status?job=" not in r.text      # a single user needs no fan-out job


def test_calendars_share_empty_group_says_so(client):
    r = client.post("/calendars/share",
                    data={"cal": SEC_CAL, "target": "group:empty-group@example.com"})
    assert r.status_code == 200
    assert "no members" in r.text


def test_calendars_share_domain_scope_cannot_be_subscribed(client):
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "domain"})
    assert r.status_code == 200
    assert "can't be auto-subscribed" in r.text or "can&#39;t be auto-subscribed" in r.text


def test_calendars_share_status_reports_progress_then_result(client):
    from gamgui.web.jobs import start_job
    st = client.app.state.gamgui

    running = start_job(st.jobs, 3)
    running.record("alice@example.com", True)
    running.current = "bob@example.com"
    r = client.get("/calendars/share/status", params={"job": running.id})
    assert "1 added" in r.text and "bob@example.com" in r.text
    assert "/calendars/share/status?job=" in r.text          # still polling

    done = start_job(st.jobs, 3)
    for email, ok in (("alice@example.com", True), ("bob@example.com", True), ("carol@example.com", False)):
        done.record(email, ok, "The requested user, group, or resource was not found." if not ok else "")
    done.finished = True
    r = client.get("/calendars/share/status", params={"job": done.id})
    assert "appears in 2 of 3" in r.text
    assert "carol@example.com" in r.text                     # who to tell to add it manually
    assert "every 1s" not in r.text                          # stopped polling


def test_calendars_share_status_unknown_job_is_quiet(client):
    r = client.get("/calendars/share/status", params={"job": "nope"})
    assert r.status_code == 200
    assert "Adding to calendars" not in r.text


async def test_run_subscribe_bounds_its_feed_at_scale():
    # A big group must not make each 1s poll carry a line per member.
    import types
    from gamgui.web.jobs import RECENT_WINDOW, BatchJob
    from gamgui.web.routes.calendars import _run_subscribe

    emails = [f"u{i}@example.com" for i in range(500)]
    conn = types.SimpleNamespace(
        subscribe_calendar_for=lambda e, c: _ok(e))
    job = BatchJob(id="x", total=len(emails))
    await _run_subscribe(job, conn, "c@group.calendar.google.com", emails)
    assert job.done == 500 and job.applied == 490 and len(job.failed) == 10
    assert len(job.recent) == RECENT_WINDOW                  # bounded window, newest kept
    assert job.recent[-1].item == "u499@example.com"
    assert job.finished and job.current == ""


async def test_batch_job_failures_stay_bounded_and_render_the_overflow(client):
    # Invariant #9: a bulk store where every user fails keeps the full count but only a sample of
    # names, and the final panel says how many it isn't listing.
    from gamgui.core.bulk import set_departments
    from gamgui.core.gam.models import GAMUser
    from gamgui.web.jobs import FAILED_SAMPLE_CAP, BatchJob

    class _Refused:
        async def set_organization(self, email, title="", department=""):
            return SimpleNamespace(ok=False)

    n = FAILED_SAMPLE_CAP + 50
    targets = [GAMUser.from_json({"primaryEmail": f"u{i}@example.com"}) for i in range(n)]
    job = BatchJob(id="bulkcap", total=n)
    await set_departments(job, _Refused(), targets, "Sales")
    assert (job.done, job.applied, job.failed_total) == (n, 0, n)
    assert len(job.failed) == FAILED_SAMPLE_CAP and job.failed[-1].item == f"u{FAILED_SAMPLE_CAP - 1}@example.com"

    client.app.state.gamgui.jobs[job.id] = job
    html = client.get("/users/bulk/status", params={"job": job.id}).text
    assert f"Failed ({n})" in html and "+50 more" in html
    assert f"u{FAILED_SAMPLE_CAP}@example.com" not in html   # past the sample: counted, not listed


async def test_run_subscribe_caps_its_failed_sample(client):
    import types
    from gamgui.web.jobs import FAILED_SAMPLE_CAP, BatchJob
    from gamgui.web.routes.calendars import _run_subscribe

    async def _refused(email, cal):
        return types.SimpleNamespace(ok=False)

    n = FAILED_SAMPLE_CAP + 7
    job = BatchJob(id="subcap", total=n)
    await _run_subscribe(job, types.SimpleNamespace(subscribe_calendar_for=_refused), SEC_CAL,
                         [f"u{i}@example.com" for i in range(n)])
    assert (job.failed_total, len(job.failed)) == (n, FAILED_SAMPLE_CAP)

    client.app.state.gamgui.jobs[job.id] = job
    html = client.get("/calendars/share/status", params={"job": job.id}).text
    assert f"Couldn't add it for {n}" in html.replace("&#39;", "'") and "+7 more" in html


_GONE_WHY = "The requested user, group, or resource was not found."


def _gone_result(target: str):
    """A write that works for everyone but gone@example.com, which fails the way _run_write reports it."""
    from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel
    ok = not target.startswith("gone@")
    return ChangeResult(preview=ChangePreview(ConnectorID.GOOGLE_WORKSPACE, target, "write", RiskLevel.LOW), ok=ok,
                        detail="" if ok else "GAM failed (not_found, exit=56): User: gone@example.com, Does not exist",
                        remediation="" if ok else _GONE_WHY)


async def _department_feed():
    from gamgui.core.bulk import set_departments
    from gamgui.core.gam.models import GAMUser
    from gamgui.web.jobs import start_job

    async def set_organization(email, title="", department=""):
        return _gone_result(email)

    job = start_job({}, 2)
    await set_departments(job, SimpleNamespace(set_organization=set_organization),
                          [GAMUser("alice@example.com"), GAMUser("gone@example.com")], "Sales")
    return job


async def _fanout_feed():
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.calendars import _run_subscribe

    async def subscribe_calendar_for(email, cal):
        return _gone_result(email)

    job = start_job({}, 2)
    await _run_subscribe(job, SimpleNamespace(subscribe_calendar_for=subscribe_calendar_for), SEC_CAL,
                         ["alice@example.com", "gone@example.com"])
    return job


async def _sequence_feed():
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.builder import _run_sequence, _seq_previews

    async def apply(previews):
        return [_gone_result(p.target) for p in previews]

    seq = [{"target": t, "label": "Suspend user", "argv": ["update", "user", t, "suspended", "on"], "risk": 1}
           for t in ("alice@example.com", "gone@example.com")]
    job = start_job({}, 2, window=2)
    await _run_sequence(job, SimpleNamespace(apply=apply), _seq_previews(seq))
    return job


# Each BatchJob feed, the route that renders it, and whether its live panel lists targets as they land.
BULK_FEEDS = {"department": (_department_feed, "/users/bulk/status", False),
              "calendar fan-out": (_fanout_feed, "/calendars/share/status", True),
              "builder sequence": (_sequence_feed, "/builder/sequence/status", True)}


@pytest.mark.parametrize("feed", sorted(BULK_FEEDS))
async def test_each_bulk_feed_says_why_a_target_failed(client, feed):
    # Plan U9: these feeds listed failed addresses with no reason. Now each failure says why in words —
    # live, and in the final panel with GAM's error one click away.
    run, status, live_rows = BULK_FEEDS[feed]
    job = await run()
    assert (job.applied, job.failed_total) == (1, 1)
    assert (job.failed[0].reason, "Does not exist" in job.failed[0].detail) == (_GONE_WHY, True)
    client.app.state.gamgui.jobs[job.id] = job
    done = unescape(client.get(status, params={"job": job.id}).text)
    headline, _, expandable = done.partition("<details")
    assert "gone@example.com" in headline and _GONE_WHY in headline
    assert "GAM's error" in expandable and "Does not exist" in expandable
    if live_rows:
        job.finished = False                                   # the same job, as its live panel shows it
        live = unescape(client.get(status, params={"job": job.id}).text)
        assert "✗" in live and "gone@example.com" in live and _GONE_WHY in live and "Does not exist" not in live


def test_no_loop_appends_to_a_jobs_failed_list_directly():
    # Job.record() is what caps the sample; a bare `job.failed.append` would bypass it. The loops live in
    # web/routes and, since plan Q9, in core (bulk.py, lifecycle.py) — so the whole package is scanned.
    package = Path(__file__).parent.parent / "gamgui"
    offenders = [str(p.relative_to(package)) for p in package.rglob("*.py") if "job.failed.append(" in p.read_text()]
    assert offenders == []


def test_route_helpers_live_once_in_common():
    # Seven pasted `_friendly`s, five `_err`s and three `_conn`s had begun to drift (plan Q10); one copy
    # in _common.py keeps every screen saying the same thing the same way.
    routes = Path(__file__).parent.parent / "gamgui" / "web" / "routes"
    local = re.compile(r"^def (_friendly|_err|_conn|_st|_failed|_sig_store)\(", re.M)
    offenders = [f"{p.name}: {m}" for p in routes.glob("*.py") for m in local.findall(p.read_text())]
    assert offenders == []


async def _ok(email: str):
    # every 50th member fails, so the failed-list path is exercised too
    import types
    return types.SimpleNamespace(ok=not email.startswith(("u0@", "u50@", "u100@", "u150@", "u200@",
                                                          "u250@", "u300@", "u350@", "u400@", "u450@")))


def test_calendars_share_requires_target(client):
    r = client.post("/calendars/share", data={"cal": SEC_CAL, "target": "   "})
    assert "Enter a person or group to share with." in r.text


def test_calendars_share_partial_when_subscribe_fails(client):
    # The ACL add succeeds but the subscribe (add calendars) is refused for a SUBFAIL recipient.
    r = client.post("/calendars/share",
                    data={"cal": SEC_CAL, "target": "SUBFAIL-carol@example.com", "role": "reader"})
    assert r.status_code == 200
    assert "auto-add it to their calendar list" in r.text    # partial-success notice (apostrophe escaped)


def test_calendars_unshare_removes_access(client):
    r = client.post("/calendars/unshare",
                    data={"cal": SEC_CAL, "scope": "assistant@example.com", "label": "Training Calendar"})
    assert r.status_code == 200
    assert "Removed access for assistant@example.com" in r.text


def test_calendars_detail_has_share_form_and_row_remove(client):
    r = client.get("/calendars/detail", params={"cal": SEC_CAL, "label": "Training Calendar"})
    assert r.status_code == 200
    assert 'hx-post="/calendars/share"' in r.text and 'name="role"' in r.text
    # Owner row has NO Remove button; the two non-owner rules (reader + default) each get one.
    assert r.text.count('hx-post="/calendars/unshare"') == 2


# Offboarding checks both addresses against the directory: a plain active user leaves, and an active
# user (alice, a super admin — only a leaver's admin role draws a warning) takes over.
LEAVER, MGR = "carol@example.com", "alice@example.com"


def test_lifecycle_page_renders(client):
    r = client.get("/lifecycle")
    assert r.status_code == 200
    assert "Offboard a user" in r.text and 'name="manager"' in r.text
    assert r.text.count('name="done"') == 8            # the re-run's "already done" boxes, one per step


def test_lifecycle_page_intro_names_every_step_in_run_order(client):
    # The intro read "reset password → delegate → …" after revoke and forwarding became steps of their
    # own: the operator's first sentence about the routine left out the two that lock the leaver out.
    from gamgui.core import lifecycle

    text = unescape(client.get("/lifecycle").text)
    intro = text[text.index("Runs the sequence in order:"):]
    intro = intro[:intro.index("</p>")]
    positions = [intro.find(name) for name in lifecycle.STEP_NAMES.values()]
    assert -1 not in positions, f"the intro leaves out a step: {intro}"
    assert positions == sorted(positions), f"the intro names the steps out of order: {intro}"


def test_lifecycle_offboard_preview_lists_steps(client):
    r = client.post("/lifecycle/offboard/preview",
                    data={"user": LEAVER, "manager": MGR, "subject": "s", "message": "m", "days": "30"})
    assert r.status_code == 200
    assert "8 steps" in r.text  # Drive + Calendar are one combined transfer step (was two)
    assert "Reset password" in r.text and "Transfer Drive" in r.text and "Run offboarding" in r.text


def test_lifecycle_offboard_preview_shows_each_exact_command(client):
    # Before the first live run the operator reads the exact gam command of every step, quoted so a
    # value with spaces is visibly one argument.
    import html

    r = client.post("/lifecycle/offboard/preview",
                    data={"user": LEAVER, "manager": MGR, "subject": "Away now", "message": "m", "days": "30"})
    text = html.unescape(r.text)
    for line in [
        "gam update user carol@example.com password random changepassword off",
        "gam user carol@example.com deprovision signout",
        "gam user carol@example.com forward off",
        "gam user carol@example.com add delegate alice@example.com",
        "gam user carol@example.com vacation on subject 'Away now' message m html",
        "gam create datatransfer carol@example.com drive,calendar alice@example.com all",
        "gam all users delete calendaracls primary carol@example.com",
        "gam user alice@example.com add event primary summary 'Offboarding carol@example.com: confirm",
    ]:
        assert line in text, line
    assert text.count("<pre") == 8
    assert "stopped and reported failed after 60 min" in text      # the sweep's domain-wide bound


def test_lifecycle_offboard_preview_requires_both_emails(client):
    r = client.post("/lifecycle/offboard/preview", data={"user": LEAVER, "manager": "  "})
    assert "Enter both" in r.text


@pytest.mark.parametrize("user, manager, expected", [
    ("nobody@example.com", MGR, "nobody@example.com isn't in the directory"),
    (LEAVER, "alcie@example.com", "alcie@example.com isn't in the directory"),     # the typo'd manager
    ("a.anders@example.com", LEAVER, "a.anders@example.com is an alias of alice@example.com"),
    (LEAVER, "Carol@Example.com", "are the same account"),
])
def test_offboard_blocks_an_address_the_directory_does_not_confirm(client, gam_calls, user, manager, expected):
    # A typo'd manager meant a half-offboarded account: the password reset ran, then the delegate and
    # the transfer went to nobody. Refused at the preview, so there is nothing to Run (the run re-checks
    # the previewed addresses: test_offboard_run_rechecks_the_directory).
    import html

    r = client.post("/lifecycle/offboard/preview",
                    data={"user": user, "manager": manager, "subject": "s", "message": "m", "days": "30"})
    assert expected in html.unescape(r.text) and "Run offboarding" not in r.text
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.previews.count("offboard") == 0


def test_offboard_blocks_when_the_directory_cannot_be_read(client, gam_calls, monkeypatch):
    async def unreadable(force=False):
        raise RuntimeError("gam print users failed")

    monkeypatch.setattr(client.app.state.gamgui, "users", unreadable)
    r = client.post("/lifecycle/offboard/preview", data={"user": LEAVER, "manager": MGR})
    assert "Couldn&#39;t read the directory" in r.text and "Run offboarding" not in r.text
    assert gam_writes(gam_calls()) == []


def test_offboard_acts_on_the_directory_primary_address(client):
    r = client.post("/lifecycle/offboard/preview", data={"user": " CAROL@example.com ", "manager": "Alice@Example.com"})
    assert "gam update user carol@example.com password" in r.text
    assert "add delegate alice@example.com" in r.text


@pytest.mark.parametrize("user, manager, expected", [
    ("alice@example.com", LEAVER, "alice@example.com is a super admin"),
    (LEAVER, "bob@example.com", "The manager bob@example.com is suspended"),
    ("bob@example.com", LEAVER, "bob@example.com is already suspended"),
])
def test_offboard_preview_warns_but_does_not_block(client, user, manager, expected):
    r = client.post("/lifecycle/offboard/preview", data={"user": user, "manager": manager})
    assert expected in r.text and "Run offboarding" in r.text


def test_lifecycle_autoreply_substitutes_employee_and_manager(client):
    # The departing user's name (from the directory) + the manager fill the auto-reply.
    r = client.post("/lifecycle/offboard/preview", data={
        "user": "alice@example.com", "manager": "carol@example.com",
        "subject": "{employee} has left", "message": "Please contact {manager}.", "days": "30"})
    assert "Alice Anders has left" in r.text          # name resolved from the cached directory
    assert "Please contact Carol Clark (carol@example.com)." in r.text


def test_lifecycle_page_has_live_autoreply_preview(client):
    r = client.get("/lifecycle")
    assert 'id="autoreply-preview"' in r.text
    assert "/lifecycle/offboard/autoreply" in r.text          # wired to refresh as you type


def test_lifecycle_autoreply_live_fills_in_names(client):
    # As soon as user + manager are entered, the generated message is shown — no raw tokens.
    r = client.post("/lifecycle/offboard/autoreply",
                    data={"user": "alice@example.com", "manager": "mgr@example.com",
                          "subject": "", "message": ""})
    assert r.status_code == 200
    assert "Auto-reply senders will receive" in r.text
    assert "Alice Anders is no longer with the company" in r.text   # default subject, name resolved
    assert "mgr@example.com" in r.text                              # contact filled in
    assert "{employee}" not in r.text and "{manager}" not in r.text


def test_lifecycle_autoreply_resolves_manager_name(client):
    # The manager is shown as "Name (email)" — pulled from the directory — so senders can reach them.
    r = client.post("/lifecycle/offboard/autoreply", data={
        "user": "alice@example.com", "manager": "carol@example.com", "subject": "", "message": ""})
    assert "Carol Clark (carol@example.com)" in r.text
    assert "{manager}" not in r.text


def test_lifecycle_autoreply_manager_falls_back_to_email(client):
    # Manager not in the directory -> just the email, no crash.
    r = client.post("/lifecycle/offboard/autoreply", data={
        "user": "alice@example.com", "manager": "external@partner.com", "subject": "", "message": ""})
    assert "external@partner.com" in r.text


def test_lifecycle_autoreply_live_uses_placeholders_before_entry(client):
    # With nothing entered yet, the preview still reads sensibly (bracketed placeholders, no tokens).
    r = client.post("/lifecycle/offboard/autoreply", data={"user": "", "manager": ""})
    assert "[departing user]" in r.text and "[manager]" in r.text
    assert "{employee}" not in r.text and "{manager}" not in r.text


def test_lifecycle_preview_shows_autoreply_block(client):
    r = client.post("/lifecycle/offboard/preview", data={
        "user": "alice@example.com", "manager": "carol@example.com", "days": "30"})
    assert "Auto-reply senders will receive" in r.text          # prominent block in the step preview
    assert "Alice Anders is no longer with the company" in r.text


OFFBOARD_AUDIT = ["reset_password", "revoke_access", "forward_off", "add_delegate", "set_vacation", "transfer_data",
                  "remove_from_all_calendars", "add_calendar_event"]   # one audited write per step


def _offboard_writes(calls):
    """The offboarding writes, trimmed to their command heads (the long texts are checked elsewhere)."""
    return [c[:5] for c in gam_writes(calls)]


def offboard_writes(user="leaver@example.com", mgr="mgr@example.com"):
    return [
        ["update", "user", user, "password", "random"],
        ["user", user, "deprovision", "signout"],        # revoke access: tokens, app passwords, sessions
        ["user", user, "forward", "off"],
        ["user", user, "add", "delegate", mgr],
        ["user", user, "vacation", "on", "subject"],
        ["create", "datatransfer", user, "drive,calendar", mgr],
        ["all", "users", "delete", "calendaracls", "primary"],
        ["user", mgr, "add", "event", "primary"],
    ]


OFFBOARD_FORM = {"user": LEAVER, "manager": MGR, "subject": "s", "message": "m", "days": "30", "notify": ""}


def _offboard_preview(client, **changes):
    """Preview an offboarding; return the page and the single-use token its Run button posts."""
    r = client.post("/lifecycle/offboard/preview", data={**OFFBOARD_FORM, **changes})
    assert_ok_partial(r)
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', r.text)
    assert m, r.text[:300]
    return r, m.group(1)


def _offboard_run(client, token, **changes):
    """Post Run as the preview's button does: the live form (hx-include) + confirmed + the token."""
    return client.post("/lifecycle/offboard/run",
                       data={**OFFBOARD_FORM, **changes, "confirmed": "1", "preview": token})


def test_lifecycle_offboard_run_starts(client, gam_calls):
    # The route starts the routine and returns a polling panel. The job is awaited on the client's
    # own loop (not by polling the status endpoint — that hung CI for 6h before the fixtures were
    # context-managed), then the finished panel is rendered once.
    _, token = _offboard_preview(client)
    r = _offboard_run(client, token)
    assert_ok_partial(r)
    job = _job(client, r.text, "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert (job.applied, job.failed) == (8, [])
    assert _offboard_writes(gam_calls()) == offboard_writes(LEAVER, MGR)
    assert [(a, ok) for a, _, ok in _audited(client, 8)] == [(a, True) for a in OFFBOARD_AUDIT]
    done = client.get("/lifecycle/offboard/status", params={"job": job.id})
    assert_ok_partial(done)
    assert "Offboarding complete — 8 of 8 steps succeeded." in done.text


def test_offboard_a_refused_sign_out_is_a_failed_step_not_a_clean_run(client, gam_calls, monkeypatch):
    # Only the sign-out fails, the way GAM reports it. It was swallowed inside "Reset password": the
    # panel said "✓ Reset password" and "complete — 6 of 6 steps succeeded" while the leaver's sessions
    # stayed open. Now it is its own ✗ step, the panel isn't "complete", and it says what that means.
    import html

    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    runner = client.app.state.gamgui.connector.runner
    real = runner.run_authenticated

    async def sign_out_refused(domain, argv, **kw):
        if "signout" in argv:
            raise GAMError(GAMErrorKind.SCOPE_MISSING, exit_code=50,
                           stderr=f"User: {LEAVER}, Sign Out Failed: Not Authorized to access this resource/api")
        return await real(domain, argv, **kw)

    monkeypatch.setattr(runner, "run_authenticated", sign_out_refused)
    _, token = _offboard_preview(client)
    job = _job(client, _offboard_run(client, token).text, "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert (job.failed_items, job.skipped) == (["Revoke access & sign out"], [])
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert "✗ Revoke access & sign out — " in text and "Sign Out Failed" in text
    assert "Offboarding complete" not in text and "Offboarding incomplete" in text
    assert "may still be signed in" in text and "now has a calendar reminder" not in text


def test_offboard_stopped_panel_says_what_did_not_run(client):
    import html

    from gamgui.web.jobs import start_job

    job = start_job(client.app.state.gamgui.jobs, 3)
    job.record("Reset password", True)
    job.record("Set delegate", False)
    job.done, job.finished, job.skipped = 3, True, ["Set auto-responder"]
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert "Offboarding stopped — 1 of 3 steps succeeded; failed: Set delegate; not run: Set auto-responder." in text
    assert "Don't delete the account" in text and "now has a calendar reminder" not in text


def test_user_page_delete_refuses_an_alias(client, gam_calls):
    # The delete zone posts the page's primary address; a hand-made POST with an alias (typed back
    # exactly) would still delete the owning account in GAM — refused before any write.
    r = client.post("/users/delete/apply", data={"email": "a.anders@example.com", "confirmed": "1",
                                                 "confirm_email": "a.anders@example.com"})
    assert "is an alias of alice@example.com" in r.text and gam_writes(gam_calls()) == []


def test_offboard_interrupted_panel_is_not_complete(client):
    # A run cut off by quitting once read "complete" and promised the manager a reminder.
    import html

    from gamgui.web.jobs import start_job

    job = start_job(client.app.state.gamgui.jobs, 3)
    job.applied, job.done, job.finished = 2, 2, True
    job.skipped, job.interrupted = ["30-day reminder for mgr@example.com"], True
    job.log = ["✓ Reset password", "✓ Set delegate", "– 30-day reminder for mgr@example.com — not run: interrupted"]
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert "Offboarding interrupted — 2 of 3 steps succeeded" in text
    assert "now has a calendar reminder" not in text and "Don't delete the account" in text

    # A failed step whose GAM message happens to say "interrupted" is a failure, not a cut-off run.
    failed = start_job(client.app.state.gamgui.jobs, 1)
    failed.record("Reset password", False)
    failed.finished, failed.log = True, ["✗ Reset password — Connection interrupted by peer"]
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": failed.id}).text)
    assert "Offboarding incomplete" in text and "Offboarding interrupted" not in text


def test_offboard_panel_warns_when_revoke_never_ran(client):
    # A failed reset skips the revoke: the leaver's sessions and app passwords were never touched.
    import html

    from gamgui.web.jobs import start_job

    job = start_job(client.app.state.gamgui.jobs, 2)
    job.record("Reset password", False)
    job.done, job.finished, job.skipped = 2, True, ["Revoke access & sign out"]
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert "The leaver may still be signed in" in text


def test_offboard_refuses_the_connected_admin(client, gam_calls):
    _signed_in_as(client, LEAVER)
    r = client.post("/lifecycle/offboard/preview", data=OFFBOARD_FORM)
    assert f"GamGUI is connected as {LEAVER}" in r.text and "preview" not in re.findall(r'"preview"', r.text)
    assert gam_writes(gam_calls()) == []


def test_offboard_run_executes_exactly_the_previewed_commands(client, gam_calls):
    # Run once rebuilt the steps from the live form; now it runs the ones built for the preview.
    import html

    from gamgui.core.lifecycle import command_line

    shown, token = _offboard_preview(client, subject="Away now", notify="it@example.com")
    previewed = [html.unescape(t) for t in re.findall(r"<pre[^>]*>(.*?)</pre>", shown.text, re.S)]
    job = _job(client, _offboard_run(client, token, subject="Away now", notify="it@example.com").text,
               "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert [command_line(c) for c in gam_writes(gam_calls())] == previewed


@pytest.mark.parametrize("edit", [{"manager": "bob@example.com"}, {"subject": "Changed"}, {"days": "7"},
                                  {"done": ["password"]}])
def test_offboard_run_refuses_a_form_edited_after_the_preview(client, gam_calls, edit):
    _, token = _offboard_preview(client)
    r = _offboard_run(client, token, **edit)
    assert "The form changed after the preview" in r.text
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.jobs == {}


def test_offboard_run_needs_a_fresh_unused_preview(client, gam_calls, monkeypatch):
    from gamgui.web import previews

    assert "expired or was already run" in _offboard_run(client, "").text           # no preview at all
    _, token = _offboard_preview(client)
    wait_for_job(client, _job(client, _offboard_run(client, token).text, "/lifecycle/offboard/status"))
    writes = len(gam_writes(gam_calls()))
    assert "expired or was already run" in _offboard_run(client, token).text        # single use
    _, token = _offboard_preview(client)
    monkeypatch.setattr(previews, "PREVIEW_TTL", -1)
    assert "expired or was already run" in _offboard_run(client, token).text        # expired
    assert len(gam_writes(gam_calls())) == writes and len(client.app.state.gamgui.jobs) == 1


def test_offboard_run_rechecks_the_directory(client, gam_calls, monkeypatch):
    # The manager's account went away between Preview and Run: nothing runs.
    _, token = _offboard_preview(client)
    st = client.app.state.gamgui
    directory = [u for u in client.portal.call(st.users) if u.primary_email != MGR]

    async def users(force=False):
        return directory

    monkeypatch.setattr(st, "users", users)
    r = _offboard_run(client, token)
    assert f"{MGR} isn&#39;t in the directory" in r.text
    assert gam_writes(gam_calls()) == [] and st.jobs == {}


def test_offboard_preview_runs_the_default_text_for_an_emptied_field(client):
    # The auto-reply block shows the default subject for an empty field, so the command must send it.
    r, _ = _offboard_preview(client, subject="")
    assert "vacation on subject &#39;Carol Clark is no longer with the company&#39;" in r.text


def test_offboard_rerun_runs_only_the_steps_not_ticked_done(client, gam_calls):
    # A run stopped at the transfer: tick what succeeded, and only the transfer and the reminder run
    # (re-adding the delegate would fail; the reset and sweep would just repeat). Ticked steps count as
    # succeeded, so the reminder's dependency on the reset and the delegate is met.
    done = ["password", "revoke", "forward", "delegate", "vacation", "calacls"]
    r, token = _offboard_preview(client, done=done)
    assert "2 of 8 steps (6 ticked as already done)" in r.text and r.text.count("<pre") == 2
    assert "Run 2 offboarding steps for" in r.text
    job = _job(client, _offboard_run(client, token, done=done).text, "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert (job.total, job.applied, job.failed, job.skipped) == (2, 2, [], [])
    assert [w[:3] for w in gam_writes(gam_calls())] == [["create", "datatransfer", LEAVER], ["user", MGR, "add"]]


def test_offboard_preview_warns_when_the_manager_is_already_a_delegate(client, monkeypatch):
    # GAM fails re-adding an existing delegate (Gmail alreadyExists), which would stop the routine.
    async def delegates(email):
        return ["Alice@example.com"] if email == LEAVER else []

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_delegates", delegates)
    r, _ = _offboard_preview(client)
    assert f"{MGR} already has delegate access" in r.text
    r, _ = _offboard_preview(client, done=["delegate"])
    assert "already has delegate access" not in r.text


def test_offboard_with_the_manager_already_a_delegate_stops_at_the_delegate_and_says_why(client, gam_calls,
                                                                                          gam_state):
    # End to end through the mock, nothing patched: the manager was given access on the user page
    # first. GAM refuses the second add ("already exists", exit 50) — the preview must predict it from
    # its own `print delegates` read, the run must stop there and say why, and the re-run with
    # "Set delegate" ticked must finish the rest.
    import html

    added = client.post("/users/delegate/add", data={"email": LEAVER, "delegate": MGR})
    assert f"Added {MGR}." in added.text
    shown, token = _offboard_preview(client)
    assert f"{MGR} already has delegate access to {LEAVER}" in html.unescape(shown.text)
    job = _job(client, _offboard_run(client, token).text, "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert job.failed_items == ["Set delegate"] and job.applied == 4          # the calendar sweep still ran
    assert job.skipped == ["Set auto-responder", "Transfer Drive & Calendar ownership",
                           "30-day reminder for alice@example.com"]
    text = html.unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert f"✗ Set delegate — GAM failed (unknown, exit=50): User: {LEAVER}, Delegate: {MGR}, Add Failed: " \
           "Delegate already exists." in text
    assert "Offboarding stopped" in text and "Don't delete the account" in text
    assert [w[:4] for w in gam_writes(gam_calls())][-5:] == [
        ["update", "user", LEAVER, "password"], ["user", LEAVER, "deprovision", "signout"],
        ["user", LEAVER, "forward", "off"], ["user", LEAVER, "add", "delegate"],
        ["all", "users", "delete", "calendaracls"]]
    assert _audited(client, 2)[0] == ("add_delegate", LEAVER, False)

    done = ["password", "revoke", "forward", "delegate"]
    shown, token = _offboard_preview(client, done=done)
    assert "already has delegate access" not in shown.text
    job = _job(client, _offboard_run(client, token, done=done).text, "/lifecycle/offboard/status")
    wait_for_job(client, job)
    assert (job.applied, job.failed, job.skipped) == (4, [], [])


def test_offboard_preview_warns_when_the_leavers_delegates_cannot_be_read(client, monkeypatch):
    # The preview's `print delegates` read uses the same Gmail access as the delegate step. Its failure
    # (mail service off, a Gmail scope missing) was swallowed and the preview looked clean — then the
    # irreversible reset ran, the delegate failed and the routine stopped with nothing handed over.
    import html

    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    async def unreadable(email):
        raise GAMError(GAMErrorKind.UNKNOWN, exit_code=1,
                       stderr="ERROR: 400: failedPrecondition - Mail service not enabled")

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_delegates", unreadable)
    text = html.unescape(_offboard_preview(client)[0].text)
    assert f"Couldn't read {LEAVER}'s mail delegates" in text and "Mail service not enabled" in text
    assert "will likely fail" in text and "Run offboarding" in text     # a warning: the read may be transient
    assert "Couldn't read" not in html.unescape(_offboard_preview(client, done=["delegate"])[0].text)


def test_offboard_refuses_a_second_run_for_a_leaver_whose_offboarding_is_running(client, gam_calls, monkeypatch):
    # Two held previews for one leaver both ran, interleaved (two resets, transfers, hour-long sweeps
    # and reminders), and a reload lost the only progress view. While one runs, a preview or a Run for
    # the same leaver is refused and shows the running one's progress panel instead.
    import asyncio
    import html

    st = client.app.state.gamgui
    gate, real = asyncio.Event(), st.connector.reset_password

    async def held_reset(email):          # keeps the first run on its first step
        await gate.wait()
        return await real(email)

    monkeypatch.setattr(st.connector, "reset_password", held_reset)
    _, second = _offboard_preview(client)
    _, first = _offboard_preview(client)
    job = _job(client, _offboard_run(client, first).text, "/lifecycle/offboard/status")
    try:
        text = html.unescape(client.post("/lifecycle/offboard/preview", data=OFFBOARD_FORM).text)
        assert f"An offboarding of {LEAVER} is already running" in text and "Run offboarding" not in text
        assert f"/lifecycle/offboard/status?job={job.id}" in text          # its live progress panel
        text = html.unescape(_offboard_run(client, second).text)
        assert "is already running" in text and f"?job={job.id}" in text
        assert list(st.jobs) == [job.id]
        other = client.post("/lifecycle/offboard/preview", data={**OFFBOARD_FORM, "user": "alice@example.com",
                                                                 "manager": LEAVER})
        assert "already running" not in other.text and "Run offboarding" in other.text   # other leavers: fine
    finally:
        client.portal.call(gate.set)
        wait_for_job(client, job)
    assert (job.applied, job.failed) == (8, [])
    assert "Run offboarding" in _offboard_preview(client)[0].text        # finished: a new run may start


def test_offboard_with_every_step_ticked_done_has_nothing_to_run(client):
    from gamgui.core.lifecycle import REQUIRES

    r = client.post("/lifecycle/offboard/preview", data={**OFFBOARD_FORM, "done": list(REQUIRES)})
    assert "nothing to run" in r.text and "Run offboarding" not in r.text


def test_offboard_previews_held_are_bounded(client):
    for _ in range(20):
        _offboard_preview(client)
    assert client.app.state.gamgui.previews.count("offboard") <= 8


@pytest.mark.asyncio
async def test_offboard_executor_runs_every_step(connector, gam_calls):
    # Run the offboard step-runner directly and confirm every step SUCCEEDED — not just that the loop
    # reached the end (a step that fails still counts toward `done`).
    from datetime import date

    from gamgui.core import lifecycle
    from gamgui.web.jobs import start_job

    steps = lifecycle.build_offboard_steps("leaver@example.com", "mgr@example.com", "Away", "Bye", 30, date.today())
    job = start_job({}, len(steps))
    await lifecycle.run_offboard(job, connector, steps)
    assert job.finished and job.done == len(steps)
    assert (job.applied, job.failed) == (len(steps), [])
    assert all(line.startswith("✓ ") for line in job.log), job.log
    assert _offboard_writes(gam_calls()) == offboard_writes()
    audit = connector.audit.tail()
    assert [e["action"] for e in audit] == OFFBOARD_AUDIT and all(e["ok"] for e in audit)


def test_delete_zone_shows_button_then_typed_confirm(client):
    r = client.get("/users/delete/zone", params={"email": "alice@example.com"})
    assert "Delete account" in r.text
    c = client.post("/users/delete/confirm", data={"email": "alice@example.com"})
    assert "Permanently delete" in c.text and 'name="confirm_email"' in c.text


def test_delete_requires_exact_email_match(client, gam_calls):
    for typed in ({}, {"confirm_email": "wrong@example.com"}):
        r = client.post("/users/delete/apply", data={"email": "alice@example.com", "confirmed": "1", **typed})
        assert "Type the exact email" in r.text
    assert gam_writes(gam_calls()) == []


def test_delete_applies_with_matching_confirm(client, gam_calls):
    r = client.post("/users/delete/apply", data={"email": "alice@example.com", "confirmed": "1",
                                                 "confirm_email": " Alice@example.com "})
    assert r.status_code == 200
    assert "Account deleted" in r.text and "20 days" in r.text
    assert gam_writes(gam_calls()) == [["delete", "user", "alice@example.com"]]


def test_delete_confirm_warns_on_pending_transfer(client):
    # A user with an in-flight Drive/calendar transfer must be flagged before deletion (data loss).
    r = client.post("/users/delete/confirm", data={"email": "xferpending@example.com"})
    assert "Data transfer still in progress" in r.text
    assert "Drive and Docs" in r.text
    assert "permanently loses" in r.text


def test_delete_confirm_no_warning_when_transfers_done(client):
    r = client.post("/users/delete/confirm", data={"email": "alice@example.com"})
    assert "Data transfer still in progress" not in r.text
    assert "Permanently delete" in r.text   # the confirm form still renders


def test_remove_delegate_returns_list(client, gam_calls):
    r = client.post("/users/delegate/remove", data={"email": "alice@example.com", "delegate": " assistant@example.com "})
    assert_ok_partial(r)
    assert "Remove" in r.text                         # the refreshed delegate list
    assert gam_writes(gam_calls()) == [["user", "alice@example.com", "delete", "delegate", "assistant@example.com"]]
    assert _audited(client, 1) == [("remove_delegate", "alice@example.com", True)]


def test_remove_delegate_failure_is_reported(client):
    r = client.post("/users/delegate/remove", data={"email": "alice@example.com", "delegate": "missing@example.com"})
    assert "remove missing@example.com. The requested user, group, or resource was not found." in r.text
    assert "Does not exist" in r.text and "GAM's error" in r.text   # GAM's own line, one click away
    assert "assistant@example.com" in r.text and 'name="delegate"' in r.text   # the list + form stay put
    assert _audited(client, 1) == [("remove_delegate", "alice@example.com", False)]


def test_delegate_remove_asks_before_it_runs(client):
    r = client.get("/users/delegates", params={"email": "alice@example.com"})
    assert 'hx-confirm="Remove assistant@example.com\'s access to alice@example.com\'s mailbox?"' in r.text


def test_set_signature(client):
    r = client.post("/users/signature", data={"email": "alice@example.com", "signature": "Best,\nAlice", "html": "on"})
    assert r.status_code == 200
    assert "Signature updated." in r.text


def test_add_delegate_returns_list(client, gam_calls):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": " carol@example.com "})
    assert_ok_partial(r)
    assert "Added carol@example.com." in r.text and "assistant@example.com" in r.text  # refreshed list
    assert gam_writes(gam_calls()) == [["user", "alice@example.com", "add", "delegate", "carol@example.com"]]


@pytest.mark.parametrize("delegate, says", [
    ("oauthuser", "isn&#39;t an email address"),            # a GAM keyword / bare name -> name@<domain>
    ("a,b@example.com", "isn&#39;t an email address"),      # GAM splits a <UserList> on the comma
    ("alice@example.com", "delegated to its own owner"),
    ("a.anders@example.com", "an alias of alice@example.com"),
])
def test_add_delegate_refuses_a_bad_address_before_gam(client, gam_calls, delegate, says):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": delegate})
    assert says in r.text and 'name="delegate"' in r.text   # said in the panel; the form stays
    assert gam_writes(gam_calls()) == []


def test_add_delegate_outside_the_directory_needs_an_ok(client, gam_calls):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "new@example.com"})
    assert "new@example.com isn&#39;t in the directory" in r.text and "Add new@example.com anyway" in r.text
    assert gam_writes(gam_calls()) == []
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "new@example.com",
                                                 "confirmed": "1"})
    assert_ok_partial(r)
    assert gam_writes(gam_calls()) == [["user", "alice@example.com", "add", "delegate", "new@example.com"]]


def test_add_delegate_warns_on_a_suspended_account(client, gam_calls):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "bob@example.com"})
    assert "bob@example.com is suspended" in r.text and gam_writes(gam_calls()) == []


def test_signout_everywhere_succeeds(client):
    r = client.post("/users/signout", data={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Signed alice@example.com out of all sessions." in r.text


def test_user_detail_has_signout_button(client):
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Sign out everywhere" in r.text
    assert 'hx-post="/users/signout"' in r.text


def test_suspend_preview_is_guarded(client):
    r = client.post("/users/suspend/preview", data={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Confirm suspend" in r.text
    assert "alice@example.com" in r.text
    assert "DESTRUCTIVE" in r.text or "destructive" in r.text.lower()


def test_suspend_apply_toggles_zone(client):
    r = client.post("/users/suspend/apply", data={"email": "alice@example.com", "suspend": "on", "confirmed": "1"})
    assert r.status_code == 200
    assert "Unsuspend" in r.text  # now shows the suspended-state control


def test_users_requires_connection(unconnected_client):
    r = unconnected_client.get("/users")
    assert r.status_code == 200
    assert "Connect a domain first" in r.text


def test_user_detail_passes_email_into_suspend_zone(client):
    # Regression for the include-context fix: the suspend button must carry the email.
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert '"email": "alice@example.com"' in r.text


def test_users_page_shows_friendly_error_not_500(client, monkeypatch):
    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    async def boom(*a, **k):
        raise GAMError(GAMErrorKind.AUTH_EXPIRED, exit_code=1, stderr="invalid_grant")

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_users", boom)
    r = client.get("/users")
    assert r.status_code == 200
    assert "Re-run setup" in r.text  # GAMError.remediation, not a 500


def test_table_not_connected_shows_message(unconnected_client):
    r = unconnected_client.get("/users/table", params={"q": "x"})
    assert r.status_code == 200
    assert "Not connected" in r.text


def test_add_delegate_failure_is_reported(client, monkeypatch):
    from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel

    async def fail(email, delegate):
        preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=email, summary="x", risk=RiskLevel.LOW)
        return ChangeResult(preview=preview, ok=False, detail="403 permission denied",
                            remediation="Check the admin role.")

    monkeypatch.setattr(client.app.state.gamgui.connector, "add_delegate", fail)
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "carol@example.com"})
    assert "add the delegate. Check the admin role." in r.text and "403 permission denied" in r.text
    assert 'value="carol@example.com"' in r.text        # the typed address stays in the box


# Each user-detail write, how it is posted, the connector call behind it, and what its failure says.
_ALICE = "alice@example.com"
WRITE_FAILURES = [
    ("/users/groups/add", {"email": _ALICE, "group": "sales@example.com"}, "add_group_member",
     f"Couldn't add {_ALICE} to sales@example.com."),
    ("/users/groups/remove", {"email": _ALICE, "group": "sales@example.com"}, "remove_group_member",
     f"Couldn't remove {_ALICE} from sales@example.com."),
    ("/users/organization", {"email": _ALICE, "title": "Lead", "department": "IT"}, "set_organization",
     "Couldn't update the title and department."),
    ("/users/calendar/add", {"email": _ALICE, "target": "carol@example.com", "role": "reader"}, "add_calendar_acl",
     "Couldn't share the calendar with carol@example.com."),
    ("/users/calendar/remove", {"email": _ALICE, "scope": "user:carol@example.com"}, "remove_calendar_acl",
     "Couldn't remove user:carol@example.com's access."),
    ("/users/vacation/set", {"email": _ALICE, "subject": "Away", "message": "Back soon"}, "set_vacation",
     "Couldn't turn on the auto-reply."),
    ("/users/vacation/off", {"email": _ALICE}, "clear_vacation", "Couldn't turn off the auto-reply."),
    ("/users/signout", {"email": _ALICE}, "signout_user", f"Couldn't sign {_ALICE} out."),
    ("/users/suspend/apply", {"email": _ALICE, "suspend": "on", "confirmed": "1"}, "apply",
     f"Couldn't suspend {_ALICE}."),
    ("/users/suspend/apply", {"email": _ALICE, "suspend": "off", "confirmed": "1"}, "apply",
     f"Couldn't unsuspend {_ALICE}."),
    ("/users/delete/apply", {"email": _ALICE, "confirmed": "1", "confirm_email": _ALICE}, "delete_user",
     f"Couldn't delete {_ALICE}."),
]


# The Calendars screen's and the groups board's writes, which headlined GAM's raw line until U9's second pass.
WRITE_FAILURES += [
    ("/calendars/unshare", {"cal": SEC_CAL, "scope": "user:carol@example.com"}, "remove_calendar_acl_for",
     "Couldn't remove user:carol@example.com's access."),
    ("/calendars/delete", {"cal": SEC_CAL, "confirm": "DELETE"}, "delete_calendar", "Couldn't delete the calendar."),
    ("/calendars/event/delete", {"cal": SEC_CAL, "event_id": "evt-1", "confirmed": "1"}, "delete_event",
     "Couldn't delete the event."),
    ("/groups/members", {"group": "sales@example.com", "email": "carol@example.com", "op": "add"},
     "add_group_member", "Couldn't add carol@example.com to sales@example.com."),
    ("/groups/members", {"group": "sales@example.com", "email": _ALICE, "op": "remove"},
     "remove_group_member", f"Couldn't remove {_ALICE} from sales@example.com."),
]


@pytest.mark.parametrize("route, form, method, what", WRITE_FAILURES,
                         ids=[f"{r}-{f.get('suspend') or f.get('op', '')}" for r, f, _, _ in WRITE_FAILURES])
def test_a_failed_write_says_why_with_gams_error_expandable(client, monkeypatch, route, form, method, what):
    # Plan U9: these once headlined GAM's raw "GAM failed (permission_denied, exit=1): …" line. Now the
    # message says what didn't happen and what to do in words; GAM's error is one click away.
    from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel

    raw = "GAM failed (permission_denied, exit=1): ERROR: 403: Forbidden - insufficientPermissions"
    why = "The authorized account lacks permission for this action. Check the admin role and scopes."

    async def fail(*_a, **_k):
        preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=_ALICE, summary="x",
                                risk=RiskLevel.LOW)
        result = ChangeResult(preview=preview, ok=False, detail=raw, remediation=why)
        return [result] if method == "apply" else result

    monkeypatch.setattr(client.app.state.gamgui.connector, method, fail)
    r = client.post(route, data=form)
    headline, _, expandable = r.text.partition("<details")
    assert f"{what} {why}" in unescape(headline) and "GAM failed" not in headline
    assert "GAM's error" in expandable and "insufficientPermissions" in expandable


def test_add_delegate_empty_rejected(client):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "   "})
    assert "Enter a delegate email." in r.text
