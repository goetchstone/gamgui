"""Audit runs (plan F3): Reports' ready-made reads, run when asked, with Stop between steps.

A run is a fixed list of reads (``core/audit_runs.py`` ``RUNS``); nothing here writes. Each step is one GAM read through
``GAMConnector.audit_read``, which leaves a ``read`` record of origin ``audit_run`` (F1). The run is a background job
(kind ``audit_run``, its run's key in ``job.bucket``), stoppable between steps: a step after the Stop is "not run", and
every table that needs it says unknown. Each step's table is kept for the run, in memory, for the connector it was read
on (``AppState.audit_runs``, dropped on a switch); a run that ends after a switch isn't kept.

Bounded (invariant 9): the poll draws the steps alone, never a table; the finished panel draws ``ROWS_SHOWN`` rows of each
table, and the table's CSV is the whole of it. ``tests/test_write_routes_guarded.py`` lists ``/start`` as local: reads only.
"""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core import audit_runs as runs_mod
from ...core import clock
from ...core.bulk import stop_requested
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.gam.models import GAMUser
from ...core.phishing import Incomplete
from ...core.reports import INACTIVE_DAYS
from ..csvutil import csv_safe
from ..jobs import Job, start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, error_partial, friendly, gam_detail

router = APIRouter(prefix="/reports/runs")

_PAGE = "audit_runs.html"
_PANEL = "_audit_run.html"
KIND = "audit_run"
# Fixed words, never an exception's own text (tests/test_route_error_text.py).
_NO_TABLE = "GAM ended without writing the table, so this step has no answer."
_TIMED_OUT = "GAM didn't finish in the hour a read is allowed, so this step has no answer."
_NOT_KEPT = "Not kept: the connection changed during the run. Run it again."
_STOPPED_BEFORE = "The run was stopped before this step."


def _days(value) -> Optional[int]:
    """A window the picker offers, else None: nothing else is read as a number of days."""
    try:
        days = int(str(value or "").strip())
    except ValueError:
        return None
    return days if days in runs_mod.INACTIVE_WINDOWS else None


def _job_of(st, run) -> Optional[Job]:
    return st.jobs.get(st.audit_run_jobs.get(run.key, ""))


def _panel_context(st, run, job=None, announce: bool = False) -> dict:
    result = st.audit_runs.get(run.key)
    ctx = {"run": run, "connected": st.connector is not None, "job": job, "announce": announce, "result": result,
           "windows": runs_mod.INACTIVE_WINDOWS, "days": result.days if result and result.days else INACTIVE_DAYS,
           "shown": runs_mod.ROWS_SHOWN, "inactive_run": runs_mod.INACTIVE_USERS.key}
    if result is not None:
        ctx["age"] = clock.now() - result.when
    return ctx


def _panel(request: Request, run, job=None, announce: bool = False) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _PANEL, _panel_context(app_state(request), run, job, announce))


@router.get("", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    """Every run, each with its last result on this connector or its button. Loading the page reads nothing."""
    st = app_state(request)
    panels = [_panel_context(st, run, _job_of(st, run)) for run in runs_mod.RUNS]
    return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": st.connector is not None, "panels": panels})


@router.get("/status", response_class=HTMLResponse)
async def run_status(request: Request, run: str = "", job: str = "") -> HTMLResponse:
    """A run's panel: the job asked for (a poll, or the jobs tray's page), else the run's latest. A finished job that isn't
    the run's latest says nothing about what's kept: it was another tenant's, or overtaken."""
    st = app_state(request)
    asked = st.jobs.get(job) if job else None
    if asked is not None and asked.kind != KIND:
        asked = None
    found = runs_mod.run_named(asked.bucket if asked is not None else run)
    if found is None:
        return error_partial(request, "That run is no longer here.")
    if asked is not None and asked.finished and asked.id != st.audit_run_jobs.get(found.key):
        asked = None
    return _panel(request, found, asked or _job_of(st, found), announce=asked is not None)


@router.post("/start", response_class=HTMLResponse)
async def start_run(request: Request, run: Annotated[str, Form()] = "", days: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    found = runs_mod.run_named(run)
    if found is None:
        return error_partial(request, "That isn't one of the audit runs.")
    window = INACTIVE_DAYS
    if found is runs_mod.INACTIVE_USERS:
        picked = _days(days)
        if picked is None:
            return error_partial(request, "Pick one of the windows the run offers.")
        window = picked
    if st.connector is None:
        return _panel(request, found)
    existing = _job_of(st, found)
    if existing is not None and not existing.finished:   # one of each run at a time: a second press joins the first
        return _panel(request, found, existing, announce=True)
    job = start_job(st.jobs, len(found.steps), kind=KIND, title=f"Audit run: {found.title}")
    job.bucket = found.key
    st.audit_run_jobs[found.key] = job.id
    job.task = asyncio.create_task(_run(job, st, st.connector, found, window))
    return _panel(request, found, job, announce=True)


def _why(exc: Exception) -> str:
    if isinstance(exc, Incomplete):
        return _NO_TABLE
    if isinstance(exc, GAMError) and exc.kind is GAMErrorKind.TIMEOUT:
        return _TIMED_OUT
    return friendly(exc)


async def _run(job, st, conn, run, days: int) -> None:
    """The job's task: each step in turn, Stop checked before each. A step that fails is unknown and the run goes on: the
    tables that don't need it still stand. Kept only while ``conn`` is still the connected tenant's."""
    reads, tables = [], {}
    try:
        for i, step in enumerate(run.steps):
            if stop_requested(job):
                reads += [runs_mod.StepRead(s, runs_mod.NOT_RUN, _STOPPED_BEFORE) for s in run.steps[i:]]
                break
            job.current = step.title
            try:
                rows = await conn.audit_read(runs_mod.step_command(run, step), runs_mod.step_argv(step), step.key_column)
            except Exception as exc:  # noqa: BLE001 — a failed step is unknown, said in words; the run goes on
                why = _why(exc)
                reads.append(runs_mod.StepRead(step, runs_mod.UNKNOWN, why, detail=gam_detail(exc)))
                job.record(step.title, False, why, gam_detail(exc))
                continue
            empty = runs_mod.implausibly_empty(step, len(rows))
            reads.append(runs_mod.StepRead(step, runs_mod.UNKNOWN if empty else runs_mod.READ, empty, rows=len(rows)))
            if not empty:
                tables[step.key] = [GAMUser.from_json(r) for r in rows] if step is runs_mod.DIRECTORY else rows
            job.record(step.title, not empty, empty)
        result = runs_mod.RunResult(run, reads, runs_mod.findings_for(run, reads, tables, days=days), tables, clock.now(),
                                    days if run is runs_mod.INACTIVE_USERS else 0)
        if st.connector is conn:
            st.audit_runs[run.key] = result
        else:
            job.error = _NOT_KEPT
    except Exception as exc:  # noqa: BLE001 — surface any failure in the panel
        job.error = friendly(exc)
    finally:
        job.finish()


@router.get("/table.csv")
async def table_csv(request: Request, run: str = "", finding: str = "") -> Response:
    """One of the last run's tables, whole, formula-safe. A table that's unknown is said in plain text, never a sheet
    holding only its titles, which would read as "none" (invariant 10)."""
    st = app_state(request)
    found = runs_mod.run_named(run)
    result = st.audit_runs.get(found.key) if found is not None else None
    table = result.finding(finding) if result is not None else None
    if found is None or table is None:
        return Response("That table isn't here: run it again.\n", status_code=404, media_type="text/plain")
    if st.connector is None:
        return Response(NOT_CONNECTED + "\n", status_code=400, media_type="text/plain")
    if not table.known:
        return Response(table.why + "\n", status_code=409, media_type="text/plain")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow([csv_safe(v) for v in row])
    # Both keys are the run's own (run_named, RunResult.finding), so the name holds only filename-safe characters.
    name = f"{found.key}-{table.key}".replace("_", "-")
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
