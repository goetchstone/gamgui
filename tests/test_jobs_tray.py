"""The jobs tray and Stop (plan U5).

A bulk run was visible only in the panel that started it: navigate away and its progress and final
failure list were gone, and nothing could halt a mis-scoped apply. The header's tray lists every job
(bounded) with a link to its own panel, and Stop sets ``cancel_requested``, which each loop reads
before its next target — the write in flight finishes and is recorded, never cut off mid-``gam``.

Each loop is driven directly through the strict mock (never by polling a ``create_task``'d job under
TestClient — docs/domains/web-screens-jobs.md), with Stop pressed while a write is in flight.
"""

from __future__ import annotations

from datetime import date
from html import unescape

import pytest

from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser
from gamgui.core.onboarding import RunbookStore
from gamgui.core.signatures import SignatureStore
from gamgui.web.jobs import TRAY_ROWS, BatchJob, Job, start_job, tray

from .helpers import gam_writes
from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture

USERS = [GAMUser(primary_email=f"{n}@example.com", given_name=n.title(), family_name="Doe")
         for n in ("alice", "bob", "carol")]
EMAILS = [u.primary_email for u in USERS]
CAL = "c_train123@group.calendar.google.com"


class _StopDuring:
    """The real connector, with Stop pressed while the ``nth`` call to ``method`` is in flight — what
    the tray's POST does to a running loop."""

    def __init__(self, conn, job, method: str, nth: int = 1) -> None:
        self._conn, self._job, self._method, self._left = conn, job, method, nth

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if name != self._method:
            return attr

        async def pressed(*a, **k):
            self._left -= 1
            if self._left == 0:
                self._job.cancel_requested = True
            return await attr(*a, **k)
        return pressed


def _stopped_after_one(job) -> None:
    assert job.finished and not job.interrupted
    assert (job.done, job.applied) == (1, 1), "the write in flight when Stop was pressed must finish and count"
    assert job.error == "Stopped by you — 2 not attempted."


@pytest.mark.asyncio
async def test_stop_ends_the_signature_apply_after_the_write_in_flight(connector, gam_calls):
    from gamgui.web.routes.signatures import _run_apply

    job = Job(total=3, kind="signatures")
    await _run_apply(job, _StopDuring(connector, job, "set_signature"), USERS, "<b>{name}</b>")
    _stopped_after_one(job)
    assert gam_writes(gam_calls()) == [GAMCommands.set_signature("alice@example.com", "<b>Alice Doe</b>")]


@pytest.mark.asyncio
async def test_stop_ends_the_bulk_department(connector, gam_calls):
    from gamgui.core.bulk import set_departments

    job = start_job({}, 3, kind="department")
    await set_departments(job, _StopDuring(connector, job, "set_organization"), USERS, "Sales")
    _stopped_after_one(job)
    assert gam_writes(gam_calls()) == [GAMCommands.update_organization("alice@example.com", department="Sales")]


@pytest.mark.asyncio
async def test_stop_ends_the_calendar_fan_out(connector, gam_calls):
    from gamgui.web.routes.calendars import _run_subscribe

    job = start_job({}, 3, kind="subscribe")
    await _run_subscribe(job, _StopDuring(connector, job, "subscribe_calendar_for"), CAL, EMAILS)
    _stopped_after_one(job)
    assert gam_writes(gam_calls()) == [GAMCommands.subscribe_calendar("alice@example.com", CAL)]


@pytest.mark.asyncio
async def test_stop_ends_the_builder_sequence_between_steps(connector, gam_calls):
    from gamgui.web.routes.builder import _run_sequence, _seq_previews

    seq = [{"target": e, "label": "Suspend user", "argv": GAMCommands.set_suspended(e, True), "risk": 1} for e in EMAILS]
    job = start_job({}, 3, window=3, kind="sequence")
    await _run_sequence(job, _StopDuring(connector, job, "apply"), _seq_previews(seq))
    _stopped_after_one(job)
    assert gam_writes(gam_calls()) == [GAMCommands.set_suspended("alice@example.com", True)]


@pytest.mark.asyncio
async def test_stop_ends_bulk_onboarding_between_hires_never_mid_hire(connector, gam_calls, tmp_path):
    # Stop lands during the first hire's first write; that hire's every step still runs (a half-set-up
    # account is worse than one more whole one), and no later hire is started.
    from gamgui.web.routes.onboarding import OnboardJob, _run_bulk_onboard

    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], groups=["sales@example.com"])
    hires = [{"role": "Sales", "name": f"New {i}", "email": f"new{i}@example.com", "first": "New", "last": str(i),
              "manager": "", "assignee": "it@example.com", "create_account": True, "send_welcome": False,
              "notify": ""} for i in range(3)]
    job = OnboardJob(total=3, kind="onboard")
    await _run_bulk_onboard(job, _StopDuring(connector, job, "create_user"), SignatureStore(tmp_path / "sig.json"),
                            store, [(h, store.role("Sales")) for h in hires])
    _stopped_after_one(job)
    writes = [" ".join(w) for w in gam_writes(gam_calls())]
    assert writes[0].startswith("create user new0@example.com")
    assert any(w.startswith("update group sales@example.com add member") and "new0@example.com" in w for w in writes)
    assert any("create tasklist" in w and "New 0" in w for w in writes), "the first hire's last step ran after Stop"
    assert not [w for w in writes if "new1" in w or "new2" in w or "New 1" in w or "New 2" in w]
    assert job.account_created == 1


@pytest.mark.asyncio
async def test_stop_ends_offboarding_between_steps_and_says_what_did_not_run(connector, gam_calls):
    from gamgui.core.lifecycle import build_offboard_steps, run_offboard

    steps = build_offboard_steps("leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps), kind="offboard")
    await run_offboard(job, _StopDuring(connector, job, "revoke_access"), steps)   # pressed during step 2
    assert job.finished and not job.interrupted
    assert gam_writes(gam_calls()) == steps[0].commands + steps[1].commands   # step 2 finished, then nothing
    rest = [s.label for s in steps[2:]]
    assert (job.applied, job.skipped) == (2, rest)
    assert job.log[2:] == [f"– {label} — not run: stopped" for label in rest]
    assert job.error == f"Stopped by you — {len(rest)} not attempted."
    assert [e["ok"] for e in connector.audit.tail()] == [True, True]


def test_an_offboarding_stopped_part_way_says_so_and_never_complete(client):  # noqa: F811
    job = start_job(client.app.state.gamgui.jobs, 3, kind="offboard", title="Offboarding leaver@example.com")
    job.record("Reset password", True)
    job.log += ["✓ Reset password", "– Set delegate — not run: stopped", "– Transfer — not run: stopped"]
    job.skipped += ["Set delegate", "Transfer"]
    job.error = "Stopped by you — 2 not attempted."
    job.finish()
    html = unescape(client.get("/lifecycle/offboard/status", params={"job": job.id}).text)
    assert "Offboarding stopped — 1 of 3 steps succeeded; not run: Set delegate, Transfer." in html
    assert "Stopped by you — 2 not attempted." in html and "complete" not in html


# --- the Stop route ----------------------------------------------------------------------------------

def _running(client, kind: str, title: str = "A job") -> BatchJob:  # noqa: F811
    job = start_job(client.app.state.gamgui.jobs, 5, kind=kind, title=title)
    job.record("alice@example.com", True)
    return job


def test_stop_sets_the_flag_and_answers_stopping_in_place(client, gam_calls):  # noqa: F811
    job = _running(client, "signatures")
    r = client.post("/jobs/stop", data={"job": job.id, "where": "tray"})
    assert job.cancel_requested and not job.finished
    assert f'id="stop-tray-{job.id}"' in r.text and "Stopping after the current one" in r.text
    assert "data-focus" in r.text          # focus lands on it, and keeps its id across the panel's polls
    assert gam_calls() == []


def test_an_offboarding_stop_needs_its_confirmation(client):  # noqa: F811
    job = _running(client, "offboard", "Offboarding carol@example.com")
    r = client.post("/jobs/stop", data={"job": job.id})
    assert not job.cancel_requested and "asks you first" in r.text
    tray_html = client.get("/jobs/status").text
    assert 'hx-confirm="Stop “Offboarding carol@example.com” after the current step?' in tray_html
    assert '"confirmed": "1"' in tray_html
    client.post("/jobs/stop", data={"job": job.id, "confirmed": "1"})
    assert job.cancel_requested


def test_stop_refuses_a_job_that_is_not_a_loop_or_not_there(client):  # noqa: F811
    index = start_job(client.app.state.gamgui.jobs, 0, kind="index", title="Calendar index rebuild")
    r = client.post("/jobs/stop", data={"job": index.id})
    assert not index.cancel_requested and "can't be stopped" in unescape(r.text)
    assert "Stop</button>" not in client.get("/jobs/status").text
    assert "no longer available" in client.post("/jobs/stop", data={"job": "gone"}).text


def test_stop_after_the_end_changes_nothing(client):  # noqa: F811
    job = _running(client, "department")
    job.finish()
    r = client.post("/jobs/stop", data={"job": job.id})
    assert not job.cancel_requested and "already finished" in r.text


def test_each_running_panel_offers_stop_and_the_stopped_one_says_stopping(client):  # noqa: F811
    panels = {"signatures": "/signatures/apply/status", "department": "/users/bulk/status",
              "subscribe": "/calendars/share/status", "sequence": "/builder/sequence/status",
              "offboard": "/lifecycle/offboard/status"}
    for kind, status in panels.items():
        job = _running(client, kind)
        html = client.get(status, params={"job": job.id}).text
        assert f'id="stop-panel-{job.id}"' in html and "Stop</button>" in html, kind
        job.cancel_requested = True
        html = client.get(status, params={"job": job.id}).text
        assert f'id="stop-panel-{job.id}"' in html and "Stopping after the current one" in html, kind
        job.finish()
        html = client.get(status, params={"job": job.id}).text
        assert f'id="stop-panel-{job.id}"' in html and "Stop</button>" not in html, kind   # focus lands on the result


# --- the tray ----------------------------------------------------------------------------------------

def test_the_tray_lists_running_jobs_first_and_stays_bounded():
    jobs: dict = {}
    for i in range(TRAY_ROWS + 3):
        j = start_job(jobs, 1, kind="department", title=f"Job {i}", keep=50)
        if i != 1:
            j.finish()
    rows, running, more = tray(jobs)
    assert [j.title for j in rows] == ["Job 1"] + [f"Job {i}" for i in range(TRAY_ROWS + 2, 3, -1)]
    assert (len(rows), running, more) == (TRAY_ROWS, 1, 3)


def test_every_page_carries_the_tray_and_its_poll_updates_the_count(client):  # noqa: F811
    page = client.get("/users").text
    assert 'id="jobs-toggle"' in page and 'aria-expanded="false"' in page and 'aria-controls="jobs-panel"' in page
    assert "Nothing has run in the background" in page
    job = _running(client, "signatures", "Signature for the whole company — 5 users")
    page = client.get("/audit").text
    assert '<span id="jobs-count" class="rounded-full' in page and "1 running" in page
    poll = client.get("/jobs/status").text
    assert 'id="jobs-count" hx-swap-oob="true"' in poll and "1 running" in poll
    assert f'href="/jobs/{job.id}"' in poll and "Running · 1 of 5" in poll
    assert "data-announce" not in poll        # a start is the operator's own click: its panel says "started"
    job.finish()
    poll = unescape(client.get("/jobs/status").text)
    assert "Done · 1 of 5 succeeded" in poll and 'class="hidden"' in poll      # no badge at 0 running
    # A finish is spoken from here (app.js: unless the job's own panel is on the page to say it).
    assert (f'<span hidden data-announce="tray-{job.id}">Finished in the background: Signature for the whole '
            "company — 5 users — Done · 1 of 5 succeeded.</span>") in poll


def test_a_jobs_page_loads_its_own_panel(client):  # noqa: F811
    job = _running(client, "offboard", "Offboarding carol@example.com")
    page = client.get(f"/jobs/{job.id}").text
    assert "Offboarding carol@example.com" in page
    assert f'hx-get="/lifecycle/offboard/status?job={job.id}" hx-trigger="load"' in page
    index = start_job(client.app.state.gamgui.jobs, 0, kind="index", title="Calendar index rebuild")
    assert 'id="cal-index"' in client.get(f"/jobs/{index.id}").text   # the panel it re-renders
    assert "no longer here" in client.get("/jobs/unknown").text


def test_every_job_a_screen_starts_has_a_kind_and_a_title():
    # The tray names each job and links to its panel; a start_job/register_job without them would be a
    # row with no title and a page with nothing to load.
    import re
    from pathlib import Path

    routes = Path(__file__).parent.parent / "gamgui" / "web" / "routes"
    starts = [(p.name, m) for p in routes.glob("*.py")
              for m in re.findall(r"(?:start_job|register_job)\((?:[^()]|\([^()]*\))*\)", p.read_text(), re.S)]
    assert len(starts) == 7, starts
    assert [s for s in starts if "kind=" not in s[1] or "title=" not in s[1]] == []
