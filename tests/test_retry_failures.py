"""Retry failures only (plan U5).

A finished signature apply or bulk department run that failed for some people offers "Retry the N
that failed". It is never a write: it renders the run's own preview for exactly those people, whose
Apply is the same confirm step with its single-use token. A job keeps only a capped sample of its
failures (invariant #9), so past the cap there is no retry — a retry of the sample would pass for a
retry of them all.

Failures come from the strict mock's ``*missing*`` / ``*nonexistent*`` trigger ("Does not exist").
"""

from __future__ import annotations

import re

import pytest

from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser
from gamgui.web import jobs

from .helpers import gam_writes, wait_for_job
from .test_users_web import _bulk_apply, _bulk_preview, _job, _sig_apply, _sig_preview
from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture

DAN = GAMUser(primary_email="dan.missing@example.com", given_name="Dan", family_name="Missing")
ERIN = GAMUser(primary_email="erin.nonexistent@example.com", given_name="Erin", family_name="Gone")
COMPANY = {"template": "{name}", "scope_type": "company", "scope_value": ""}


def _directory_with(client, monkeypatch, *extra):  # noqa: F811
    """The fixture directory plus people GAM will say do not exist."""
    st = client.app.state.gamgui
    grown = client.portal.call(st.users) + list(extra)

    async def users(force=False, stale_ok=False):
        return grown

    monkeypatch.setattr(st, "users", users)


def _token(html: str) -> str:
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', html)
    return m.group(1) if m else ""


def _signature_run(client):  # noqa: F811
    _, token = _sig_preview(client, **COMPANY)
    job = _job(client, _sig_apply(client, token, **COMPANY).text, "/signatures/apply/status")
    wait_for_job(client, job)
    return job


def _department_run(client, *emails):  # noqa: F811
    form = {"store": "Sales", "group": "", "emails": "\n".join(emails)}
    _, token = _bulk_preview(client, **form)
    job = _job(client, _bulk_apply(client, token, **form).text, "/users/bulk/status")
    wait_for_job(client, job)
    return job


def test_a_signature_retry_is_the_preview_and_confirm_step_for_exactly_the_failed(client, gam_calls, monkeypatch):  # noqa: F811
    _directory_with(client, monkeypatch, DAN)
    job = _signature_run(client)
    assert (job.applied, job.failed_total, job.failed_items) == (2, 1, [DAN.primary_email])
    panel = client.get("/signatures/apply/status", params={"job": job.id}).text
    assert "Retry the 1 that failed" in panel and "/signatures/retry" in panel
    ran = len(gam_writes(gam_calls()))

    # The retry is a preview: it writes nothing, and its Apply carries the retry and a fresh token.
    shown = client.post("/signatures/retry", data={"job": job.id}).text
    assert f'id="retry-{job.id}"' in shown and "Applies to <strong>1</strong> user" in shown
    assert f'"retry": "{job.id}"' in shown and "#sig-form" not in shown
    assert len(gam_writes(gam_calls())) == ran

    # Its Apply without the token, or without the confirm step, runs nothing.
    r = client.post("/signatures/apply", data={"retry": job.id, "confirmed": "1"})
    assert "expired or was already run — click Retry again" in r.text
    r = client.post("/signatures/apply", data={"retry": job.id, "preview": _token(shown)})
    assert "apply/status" not in r.text and len(gam_writes(gam_calls())) == ran

    # The whole confirm step writes the run's template to the one who failed, and nobody else.
    shown = client.post("/signatures/retry", data={"job": job.id}).text
    r = client.post("/signatures/apply", data={"retry": job.id, "confirmed": "1", "preview": _token(shown)})
    again = _job(client, r.text, "/signatures/apply/status")
    wait_for_job(client, again)
    assert gam_writes(gam_calls())[ran:] == [GAMCommands.set_signature(DAN.primary_email, "Dan Missing")]
    assert again.title == "Signature retry — 1 user" and again.retry == "{name}"
    assert again.failed_items == [DAN.primary_email]     # GAM still refuses: it can be retried again
    # A replayed click is refused, the token spent.
    r = client.post("/signatures/apply", data={"retry": job.id, "confirmed": "1", "preview": _token(shown)})
    assert "expired or was already run" in r.text and len(gam_writes(gam_calls())) == ran + 1


def test_a_department_retry_is_the_preview_and_confirm_step_for_exactly_the_failed(client, gam_calls, monkeypatch):  # noqa: F811
    _directory_with(client, monkeypatch, DAN)
    job = _department_run(client, "alice@example.com", DAN.primary_email)
    assert (job.applied, job.failed_total, job.failed_items) == (1, 1, [DAN.primary_email])
    panel = client.get("/users/bulk/status", params={"job": job.id}).text
    assert "Retry the 1 that failed" in panel and "/users/bulk/retry" in panel
    ran = len(gam_writes(gam_calls()))

    shown = client.post("/users/bulk/retry", data={"job": job.id}).text
    assert f'id="retry-{job.id}"' in shown and "Department to <strong>Sales</strong> on <strong>1</strong>" in shown
    assert f'"retry": "{job.id}"' in shown and "#bulk-form" not in shown
    assert len(gam_writes(gam_calls())) == ran

    r = client.post("/users/bulk/apply", data={"retry": job.id, "preview": _token(shown)})   # no confirm step
    assert "bulk/status" not in r.text and len(gam_writes(gam_calls())) == ran

    shown = client.post("/users/bulk/retry", data={"job": job.id}).text
    r = client.post("/users/bulk/apply", data={"retry": job.id, "confirmed": "1", "preview": _token(shown)})
    again = _job(client, r.text, "/users/bulk/status")
    wait_for_job(client, again)
    assert gam_writes(gam_calls())[ran:] == [GAMCommands.update_organization(DAN.primary_email, department="Sales")]
    assert again.title == "Department “Sales” retry — 1 user" and again.retry == "Sales"


@pytest.mark.parametrize("flow", ["signatures", "department"])
def test_past_the_failure_cap_there_is_no_retry_and_the_panel_says_why(client, gam_calls, monkeypatch, flow):  # noqa: F811
    monkeypatch.setattr(jobs, "FAILED_SAMPLE_CAP", 1)
    _directory_with(client, monkeypatch, DAN, ERIN)
    if flow == "signatures":
        job, status, retry = _signature_run(client), "/signatures/apply/status", "/signatures/retry"
    else:
        job = _department_run(client, DAN.primary_email, ERIN.primary_email)
        status, retry = "/users/bulk/status", "/users/bulk/retry"
    assert (job.failed_total, len(job.failed), job.more) == (2, 1, 1)
    why = "Only 1 of the 2 failures were kept"
    panel = client.get(status, params={"job": job.id}).text
    assert why in panel and "Retry the" not in panel and retry not in panel
    # Posted anyway, the retry refuses and holds nothing to apply.
    ran = len(gam_writes(gam_calls()))
    r = client.post(retry, data={"job": job.id})
    assert why in r.text and _token(r.text) == "" and len(gam_writes(gam_calls())) == ran


def test_a_retry_refuses_a_run_that_is_not_its_own_finished_one(client, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    running = jobs.register_job(st.jobs, jobs.Job(total=2, kind="signatures", retry="{name}"))
    running.record(DAN.primary_email, False, "Does not exist")
    done = jobs.register_job(st.jobs, jobs.Job(total=1, kind="signatures", retry="{name}"))
    done.record(DAN.primary_email, False, "Does not exist")
    done.finish()
    clean = jobs.register_job(st.jobs, jobs.Job(total=1, kind="department", retry="Sales"))
    clean.record("alice@example.com", True)
    clean.finish()
    for path, job, says in [("/signatures/retry", "nope", "no longer available"),
                            ("/signatures/retry", running.id, "no longer available"),   # still running
                            ("/users/bulk/retry", done.id, "no longer available"),      # another screen's run
                            ("/users/bulk/retry", clean.id, "Nothing failed")]:
        r = client.post(path, data={"job": job})
        assert says in r.text and _token(r.text) == "", (path, r.text[:200])
    assert gam_writes(gam_calls()) == []


def test_a_stopped_department_run_lists_its_failures_and_offers_their_retry(client):  # noqa: F811
    # The stopped panel once said only "Stopped … Updated N of M" — the failures before the stop vanished.
    st = client.app.state.gamgui
    job = jobs.start_job(st.jobs, 3, kind="department")
    job.retry = "Sales"
    job.record(DAN.primary_email, False, "That user doesn't exist.")
    job.error = "Stopped by you — 2 not attempted."
    job.finish()
    panel = client.get("/users/bulk/status", params={"job": job.id}).text
    assert "Stopped by you" in panel and DAN.primary_email in panel and "Retry the 1 that failed" in panel
