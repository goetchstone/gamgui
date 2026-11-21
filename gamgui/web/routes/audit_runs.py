"""Audit runs (plan F3): Reports' ready-made reads, run when asked, with Stop between steps.

A run is a fixed list of reads (``core/audit_runs.py`` ``RUNS``); nothing here writes. Each step is one GAM read through
``GAMConnector.audit_read``, which leaves a ``read`` record of origin ``audit_run`` (F1). The run is a background job
(kind ``audit_run``, its run's key in ``job.bucket``), stoppable between steps: a step after the Stop is "not run", and
every table that needs it says unknown. Each step's table is kept for the run, in memory, for the connector it was read
on (``AppState.audit_runs``, dropped on a switch); a run that ends after a switch isn't kept.

Bounded (invariant 9): the poll draws the steps alone, never a table; the finished panel draws ``ROWS_SHOWN`` rows of each
table, and the table's CSV is the whole of it. ``tests/test_write_routes_guarded.py`` lists ``/start`` as local: reads only.

Schedules (plan F7a, ``core/schedules.py``): a run can be given a cadence on the active domain (``/schedule``), and then says
when it is due, here and on Home. Nothing runs on its own: the Run button is the run. A scheduled run's tables are compared
with its last kept run's and kept on this Mac (``after_run``); ``/kept/delete`` deletes them. Both are local: no GAM.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core import audit_runs as runs_mod
from ...core import clock
from ...core import schedules as sched
from ...core.bulk import stop_requested
from ...core.catalog.catalog import load_catalog
from ...core.connectors.gam_connector import READ_ORIGIN_AUDIT_RUN, READ_ORIGIN_NAMED_REPORT, NoRowUnsaid
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.gam.models import GAMUser
from ...core.phishing import Incomplete
from ...core.reports import INACTIVE_DAYS
from ..csvutil import csv_safe
from ..jobs import Job, start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, error_partial, friendly, gam_detail, schedule_stores

router = APIRouter(prefix="/reports/runs")
logger = logging.getLogger(__name__)

_PAGE = "audit_runs.html"
_PANEL = "_audit_run.html"
KIND = "audit_run"
# Fixed words, never an exception's own text (tests/test_route_error_text.py).
_NO_TABLE = "GAM ended without writing the table, so this step has no answer."
_NO_ROW_UNSAID = ("GAM wrote no row and didn't say there were none, which it does when there are. A service-account "
                  "token Google refuses ends this way, so this step has no answer: check the domain's access on Setup.")
_TIMED_OUT = "GAM didn't finish in the hour a read is allowed, so this step has no answer."
_NOT_KEPT = "Not kept: the connection changed during the run. Run it again."
_STOPPED_BEFORE = "The run was stopped before this step."
_NEEDS_EARLIER = "It reads for the people an earlier step found, and that step has no answer, so nothing was read."
_SOME_UNREAD = "GAM couldn't read {}, and read the rest: what it couldn't read is unknown, not none."


def _days(value) -> Optional[int]:
    """A window the picker offers, else None: nothing else is read as a number of days."""
    try:
        days = int(str(value or "").strip())
    except ValueError:
        return None
    return days if days in runs_mod.INACTIVE_WINDOWS else None


def _named(st) -> tuple:
    """The named reports (F2) that run here (plan F7a), from the catalog the Builder uses: loaded on first use."""
    if st.catalog is None:
        st.catalog = load_catalog()
    return runs_mod.named_runs(st.catalog)


def _find(st, key: str):
    """An audit run, or a named report run here, by its key; None for anything else."""
    return runs_mod.run_named(key) or next((r for r in _named(st) if r.key == key), None)


def _job_of(st, run) -> Optional[Job]:
    return st.jobs.get(st.audit_run_jobs.get(run.key, ""))


# A panel's schedule part before it's read (a running job's panel, or no connection): nothing drawn.
_NO_SCHEDULE = {"scheduling": False, "domain": "", "schedule": None, "due": False, "due_line": "", "schedule_problem": "", "kept": [],
                "kept_problem": "", "cadences": sched.CADENCES, "cadence_words": sched.CADENCE_WORDS,
                "keep_default": sched.KEEP_DEFAULT, "keep_cap": sched.KEEP_CAP, "when_words": sched.when_words}


def _schedule_context(st, run) -> dict:
    """The run's schedule on the active domain, whether it's due, and its kept runs (headers only). Read from disk, so only
    for a panel that isn't polling: a running job's panel draws its steps alone."""
    store, kept = schedule_stores(st)
    domain = st.connector.domain
    ctx: dict = {**_NO_SCHEDULE, "scheduling": True, "domain": domain.lower()}
    try:
        ctx["schedule"] = store.schedules(domain).get(run.key)
    except sched.StoreProblem as exc:
        ctx["schedule_problem"] = str(exc)
    if ctx["schedule"] is not None:
        due = sched.due_words(run.title, ctx["schedule"], time.time())
        ctx["due"], ctx["due_line"] = due.due, due.line
    try:
        ctx["kept"] = kept.runs(domain, run.key)
    except sched.StoreProblem as exc:
        ctx["kept_problem"] = str(exc)
    return ctx


def _panel_context(st, run, job=None, announce: bool = False, notice: str = "", notice_problem: bool = False) -> dict:
    result = st.audit_runs.get(run.key)
    ctx = {"run": run, "connected": st.connector is not None, "job": job, "announce": announce, "result": result,
           "windows": runs_mod.INACTIVE_WINDOWS, "days": result.days if result and result.days else INACTIVE_DAYS,
           "shown": runs_mod.ROWS_SHOWN, "inactive_run": runs_mod.INACTIVE_USERS.key,
           "notice": notice, "notice_problem": notice_problem, **_NO_SCHEDULE}
    if result is not None:
        ctx["age"] = clock.now() - result.when
    if st.connector is not None and not (job is not None and not job.finished):
        ctx.update(_schedule_context(st, run))
        newest = next((k for k in ctx["kept"] if not k.problem), None)
        if result is None and run is runs_mod.INACTIVE_USERS and newest is not None and newest.days in runs_mod.INACTIVE_WINDOWS:
            ctx["days"] = newest.days     # the window its last kept run read: one click runs the same again
    return ctx


def _panel(request: Request, run, job=None, announce: bool = False, notice: str = "",
           notice_problem: bool = False) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _PANEL, _panel_context(app_state(request), run, job, announce, notice,
                                                                      notice_problem))


@router.get("", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    """Every run, each with its last result on this connector or its button. Loading the page reads nothing."""
    st = app_state(request)
    panels = [_panel_context(st, run, _job_of(st, run)) for run in runs_mod.RUNS]
    named = [_panel_context(st, run, _job_of(st, run)) for run in _named(st)]
    set_aside = schedule_stores(st)[0].set_aside if st.connector is not None else None
    return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": st.connector is not None, "panels": panels,
                                                       "named": named, "set_aside": set_aside})


@router.get("/status", response_class=HTMLResponse)
async def run_status(request: Request, run: str = "", job: str = "") -> HTMLResponse:
    """A run's panel: the job asked for (a poll, or the jobs tray's page), else the run's latest. A finished job that isn't
    the run's latest says nothing about what's kept: it was another tenant's, or overtaken."""
    st = app_state(request)
    asked = st.jobs.get(job) if job else None
    if asked is not None and asked.kind != KIND:
        asked = None
    found = _find(st, asked.bucket if asked is not None else run)
    if found is None:
        return error_partial(request, "That run is no longer here.")
    if asked is not None and asked.finished and asked.id != st.audit_run_jobs.get(found.key):
        asked = None
    return _panel(request, found, asked or _job_of(st, found), announce=asked is not None)


@router.post("/start", response_class=HTMLResponse)
async def start_run(request: Request, run: Annotated[str, Form()] = "", days: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    found = _find(st, run)
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
    job = start_job(st.jobs, len(found.steps), kind=KIND,
                    title=f"{'Named report' if found.named else 'Audit run'}: {found.title}")
    job.bucket = found.key
    st.audit_run_jobs[found.key] = job.id
    job.task = asyncio.create_task(_run(job, st, st.connector, found, window))
    return _panel(request, found, job, announce=True)


def _why(exc: Exception) -> str:
    if isinstance(exc, NoRowUnsaid):
        return _NO_ROW_UNSAID
    if isinstance(exc, Incomplete):
        return _NO_TABLE
    if isinstance(exc, GAMError) and exc.kind is GAMErrorKind.TIMEOUT:
        return _TIMED_OUT
    return friendly(exc)


async def _run(job, st, conn, run, days: int) -> None:
    """The job's task: each step in turn, Stop checked before each. A step that fails is unknown and the run goes on: the
    tables that don't need it still stand. Kept only while ``conn`` is still the connected tenant's."""
    reads: list = []
    tables: dict = {}
    try:
        for i, step in enumerate(run.steps):
            if stop_requested(job):
                reads += [runs_mod.StepRead(s, runs_mod.NOT_RUN, _STOPPED_BEFORE) for s in run.steps[i:]]
                break
            job.current = step.title
            argv = runs_mod.step_argv(step, tables)
            if argv is None:   # it reads for what an earlier step found, and that step has no table: nothing to run
                why = _NEEDS_EARLIER
                reads.append(runs_mod.StepRead(step, runs_mod.UNKNOWN, why))
                job.record(step.title, False, why)
                continue
            try:
                rows, unread = await conn.audit_read(runs_mod.step_command(run, step), argv, step.key_column,
                                                     ok_rcs=step.ok_rcs, partial_rcs=step.partial_rcs,
                                                     empty_rcs=step.empty_rcs,
                                                     origin=READ_ORIGIN_NAMED_REPORT if run.named else READ_ORIGIN_AUDIT_RUN)
            except Exception as exc:  # noqa: BLE001 — a failed step is unknown, said in words; the run goes on
                why = _why(exc)
                reads.append(runs_mod.StepRead(step, runs_mod.UNKNOWN, why, detail=gam_detail(exc)))
                job.record(step.title, False, why, gam_detail(exc))
                continue
            empty = runs_mod.implausibly_empty(step, len(rows))
            state = runs_mod.UNKNOWN if empty else (runs_mod.PARTIAL if unread else runs_mod.READ)
            why = empty or (_SOME_UNREAD.format(", ".join(unread)) if unread else "")
            reads.append(runs_mod.StepRead(step, state, why, rows=len(rows), unread=unread))
            if not empty:
                tables[step.key] = [GAMUser.from_json(r) for r in rows] if step is runs_mod.DIRECTORY else rows
            job.record(step.title, not empty, why)
        # "Leaving": an offboarding started in GamGUI since it opened, the one place the app knows of someone on the way out.
        leaving = frozenset(str(a).lower() for a in getattr(st, "offboard_jobs", {}) or {})
        result = runs_mod.RunResult(run, reads, runs_mod.findings_for(run, reads, tables, days=days, leaving=leaving), tables,
                                    clock.now(), days if run is runs_mod.INACTIVE_USERS else 0)
        if st.connector is conn:
            st.audit_runs[run.key] = result
            # A scheduled run (F7a): compared with its last kept run, then kept, on the domain it read. Off the event loop:
            # a domain's tables can be tens of thousands of rows.
            store, kept = schedule_stores(st)
            after = await asyncio.to_thread(sched.after_run, store, kept, conn.domain, run.key, result.findings,
                                            result.days, result.answered, time.time())
            result.changes, result.kept_note, result.kept_problem = after.changes, after.note, after.problem
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
    found = _find(st, run)
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


def _kept_changed(st, run) -> None:
    """What the run on screen said of its keep ("Kept on this Mac, as one of the last 5…") no longer holds once its schedule
    or its kept runs change: dropped, so the panel says only what the notice says now."""
    result = st.audit_runs.get(run.key)
    if result is not None:
        result.kept_note, result.kept_problem = "", False


def _keep_count(value) -> Optional[int]:
    """How many runs to keep, as typed: blank is the default; anything but a whole number from 1 to the cap is None."""
    text = str(value or "").strip()
    if not text:
        return sched.KEEP_DEFAULT
    try:
        keep = int(text)
    except ValueError:
        return None
    return keep if 1 <= keep <= sched.KEEP_CAP else None


def _runs_word(n: int) -> str:
    return f"{n} kept run{'' if n == 1 else 's'}".capitalize()


@router.post("/schedule", response_class=HTMLResponse)
async def save_schedule(request: Request, run: Annotated[str, Form()] = "", cadence: Annotated[str, Form()] = "",
                        keep: Annotated[str, Form()] = "") -> HTMLResponse:
    """Give a run a cadence on the active domain, change it, or (no cadence) stop scheduling it, which deletes what it
    kept. Local: nothing reaches Google, and nothing runs."""
    st = app_state(request)
    found = _find(st, run)
    if found is None:
        return error_partial(request, "That isn't one of the audit runs.")
    if st.connector is None:
        return _panel(request, found)
    cadence = cadence.strip().lower()
    count = _keep_count(keep)
    if cadence and cadence not in sched.CADENCES:
        return _panel(request, found, notice="Pick daily, weekly, monthly or not scheduled.", notice_problem=True)
    if cadence and count is None:
        return _panel(request, found, notice=f"Keep a whole number of runs, from 1 to {sched.KEEP_CAP}.",
                      notice_problem=True)
    store, kept = schedule_stores(st)
    domain = st.connector.domain
    try:
        saved, gone = await asyncio.to_thread(sched.set_schedule, store, kept, domain, found.key, cadence,
                                              count or sched.KEEP_DEFAULT, time.time())
    except sched.StoreProblem as problem:
        return _panel(request, found, notice=str(problem), notice_problem=True)
    except OSError as exc:
        logger.warning("saving a schedule failed", exc_info=exc)
        return _panel(request, found, notice="The schedule couldn't be saved. Check the disk has room.", notice_problem=True)
    _kept_changed(st, found)
    if saved is None:
        notice = "Not scheduled."
    else:
        notice = (f"Saved: {sched.CADENCE_WORDS[saved.cadence].lower()}, keeping the last {saved.keep} "
                  f"run{'' if saved.keep == 1 else 's'}.")
    if gone is None:   # what it keeps now (nothing when unscheduled, else its newest ``keep``) couldn't all be made so
        return _panel(request, found, notice=notice + " Its kept runs couldn't all be deleted: Delete them below, or try "
                                                      "again.", notice_problem=True)
    if gone:
        notice += f" {_runs_word(gone)} {'were' if gone != 1 else 'was'} deleted from this Mac."
    return _panel(request, found, notice=notice)


@router.post("/kept/delete", response_class=HTMLResponse)
async def delete_kept(request: Request, run: Annotated[str, Form()] = "") -> HTMLResponse:
    """Delete every run kept on this Mac for this run on the active domain. Its schedule stays. Local: no GAM."""
    st = app_state(request)
    found = _find(st, run)
    if found is None:
        return error_partial(request, "That isn't one of the audit runs.")
    if st.connector is None:
        return _panel(request, found)
    try:
        gone = await asyncio.to_thread(sched.delete_kept, schedule_stores(st)[1], st.connector.domain, found.key)
        _kept_changed(st, found)
    except sched.StoreProblem as problem:
        return _panel(request, found, notice=str(problem), notice_problem=True)
    except OSError as exc:
        logger.warning("deleting kept runs failed", exc_info=exc)
        return _panel(request, found, notice="The kept runs couldn't all be deleted. Try again.", notice_problem=True)
    return _panel(request, found, notice=f"Deleted {_runs_word(gone)} from this Mac." if gone else "Nothing was kept.")
