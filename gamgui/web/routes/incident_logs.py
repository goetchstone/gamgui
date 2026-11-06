"""Incident logs: pull every log an incident needs, by account and by the attacker's address, and export what came back.

Reads only, as a background job: a pull for every account can take many reads a log, and the parsing runs off the event loop. Rules:
``core/incident/logs.py`` and ``pull.py``. Runbook: docs/domains/incident.md.
"""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated, List

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core.gam.commands import is_one_address
from ...core.incident import logs, pull, reader, reads
from ...core.incident.logs import report_time
from ..csvutil import csv_safe
from ..jobs import start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, error_partial

router = APIRouter(prefix="/incident/logs")

_SHOWN = 100     # rows drawn a log: the CSV has them all
_PANEL = "_incident_logs.html"


def _running(st):
    job = st.jobs.get(st.incident_pull_job)
    return job if job is not None and not job.finished else None


def _context(st, job=None) -> dict:
    return {"job": job, "pull": st.incident_pull, "progress": st.incident_progress, "names": logs.NAMES, "shown": _SHOWN, "report_time": report_time,
            "window_words": logs.window_words}


@router.get("", response_class=HTMLResponse)
async def page(request: Request, accounts: str = "", ip: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    return TEMPLATES.TemplateResponse(request, "incident_logs.html", {
        "connected": st.connector is not None, "applications": logs.APPLICATIONS, "defaults": logs.INCIDENT_DEFAULT, "hours": logs.HOURS,
        "default_hours": logs.DEFAULT_HOURS, "window_words": logs.window_words, "accounts": accounts, "ip": ip, **_context(st, _running(st))})


async def _pull(job, st, conn, query: pull.Query, into: pull.Pull) -> None:
    def stopped() -> bool:
        return job.cancel_requested

    def finished() -> None:
        job.done += 1

    try:
        await pull.pull_logs(conn, query, into, st.incident_progress, stopped=stopped, finished=finished)
    finally:
        job.finish()


def _start(st, conn, query: pull.Query, into: pull.Pull, title: str):
    st.incident_pull = into
    st.incident_progress = {a: pull.Progress() for a in query.applications}
    job = start_job(st.jobs, len(query.applications), kind="incident_logs", title=title)
    st.incident_pull_job = job.id
    job.task = asyncio.create_task(_pull(job, st, conn, query, into))
    return job


@router.post("/pull", response_class=HTMLResponse)
async def start_pull(request: Request, accounts: Annotated[str, Form()] = "", ip: Annotated[str, Form()] = "",
                     hours: Annotated[int, Form()] = logs.DEFAULT_HOURS) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    wanted = [str(v) for v in form.getlist("log") if str(v) in logs.NAMES]
    if not wanted:
        return error_partial(request, "Tick at least one log to pull.")
    if hours not in logs.HOURS:
        return error_partial(request, "Pick how far back to read.")
    ip = ip.strip()
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return error_partial(request, f"“{ip}” isn't one IP address.")
    users = logs.accounts_typed(accounts)
    if len(users) > pull.MOST_ACCOUNTS:
        return error_partial(request, f"At most {pull.MOST_ACCOUNTS} accounts at once: pull the rest after these, or every account by the attacker's address.")
    if bad := next((u for u in users if not is_one_address(u)), None):
        return error_partial(request, f"“{bad}” isn't one email address.")
    if existing := _running(st):
        return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, existing))
    # One address that isn't a user fails the whole log in GAM: said here, by name, rather than as every log's failure.
    # Read fresh before refusing: an account the attacker made since the list was read is just who to look at. Chrome's log finds nothing for a typo, without a word, so unchecked isn't safe.
    if users:
        try:
            known = {u.primary_email.lower() for u in await st.users(stale_ok=True)}
            if any(u.lower() not in known for u in users):
                known = {u.primary_email.lower() for u in await st.users(force=True)}
        except Exception as exc:  # noqa: BLE001 - said, whatever it was
            return error_partial(request, f"Couldn't read the directory to check those addresses: {reads.in_words(exc)}")
        if unknown := [u for u in users if u.lower() not in known]:
            return error_partial(request, f"Not in the directory: {', '.join(unknown)}. Check the address, or use the primary one, not an alias.")
    query = pull.Query(tuple(wanted), tuple(users), hours, ip)
    title = f"Pull {len(wanted)} log{'' if len(wanted) == 1 else 's'}"
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, _start(st, conn, query, pull.Pull(query), title)))


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, held if held is not None and held.kind == "incident_logs" else None))


def _merged(old: pull.Log, log: pull.Log, until: datetime, now: datetime) -> pull.Log:
    seen = {r.identity for r in old.rows or []}
    rows = logs.newest_first([*(old.rows or []), *(r for r in log.rows or [] if r.identity not in seen)])
    through = log.through if log.capped else until if until < now else None     # a window ended short of now leaves the rest still to read
    return pull.Log.read(old.application, rows, old.hours, log.capped or until < now, through)


async def _read_rest(job, st, conn, held: pull.Pull, application: str, since: datetime) -> None:
    """One log from where it stopped to now: added to what's shown, each event once, read through the later of the two. Gmail reads at most 30 days at once: its window ends 30
    days past where it stopped, never starts later than that, or the stretch between would be skipped and the log claimed whole."""
    now = datetime.now(timezone.utc)
    until = min(now, since + timedelta(hours=logs.GMAIL_MAX_HOURS)) if application == "gmail" else now
    query = pull.Query((application,), held.query.users, held.query.hours, held.query.ip, since, until)
    st.incident_progress = {application: pull.Progress()}

    def stopped() -> bool:
        return job.cancel_requested

    try:
        log = await pull.read_log(conn, application, query, now, progress=st.incident_progress[application], stopped=stopped)
        old = held.log(application)
        if old is None or st.incident_pull is not held:
            return
        if log.rows is None:
            job.error = f"Couldn't read the rest of the {old.name} log: {log.problem}"
            return
        merged = await asyncio.to_thread(_merged, old, log, until, now)
        held.logs = [merged if x.application == application else x for x in held.logs]
        await asyncio.to_thread(held.work_out_addresses)
    finally:
        job.done += 1
        job.finish()


@router.post("/rest", response_class=HTMLResponse)
async def read_the_rest(request: Request, application: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    held = st.incident_pull
    log = held.log(application) if held else None
    if log is None or log.through is None:
        return error_partial(request, "That log has nothing left to read: pull it again.")
    if existing := _running(st):
        return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, existing))
    job = start_job(st.jobs, 1, kind="incident_logs", title=f"Read the rest of the {log.name} log")
    st.incident_pull_job = job.id
    job.task = asyncio.create_task(_read_rest(job, st, conn, held, application, log.through))
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, job))


def _when(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def _context_for(st, held: pull.Pull) -> reader.Context:
    """What the pull says that its events don't. The directory, when it can be read: until it is, a group can't be told from a person."""
    people = {}
    try:
        for user in await st.users(stale_ok=True):
            people[user.primary_email.lower()] = user.primary_email.lower()
            people.update({alias.lower(): user.primary_email.lower() for alias in user.aliases})
    except Exception:  # noqa: BLE001 - said on the to-do list: read the directory to finish
        people = {}
    capped = {}
    for log in held.logs:
        dates = [r.date for r in log.rows or [] if r.date]
        if log.capped and not log.through and dates:    # read newest first to its cap: its oldest event is as far back as it got
            capped[log.application] = min(dates)
        elif log.capped and log.through:
            capped[log.application] = log.through
    return reader.Context(pulled_by=held.query.ip, read={log.application for log in held.logs if log.rows is not None}, capped=capped,
                          domains={d.lower() for d in await st.tenant_domains()}, people=people or None)


@router.get("/reading", response_class=HTMLResponse)
async def reading(request: Request) -> HTMLResponse:
    """The reader's findings, worked out once a pull and off the event loop: it's linear in the events, and a whole pull holds hundreds of thousands."""
    st = request.app.state.gamgui
    held = st.incident_pull
    if held is None or not held.ended:
        return TEMPLATES.TemplateResponse(request, "_incident_reading.html", {"reading": None})
    context = await _context_for(st, held)
    key = (held.run, id(held.logs))
    if st.incident_reading is None or st.incident_reading[0] != key:
        def read_pull() -> reader.Reading:
            return reader.read([reader.event_from_row(r) for log in held.logs for r in log.rows or []], context)

        st.incident_reading = (key, await asyncio.to_thread(read_pull))
    return TEMPLATES.TemplateResponse(request, "_incident_reading.html", {"reading": st.incident_reading[1], "when": _when})


def _csv_text(rows: List[List[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows([[csv_safe(c) for c in row] for row in rows])
    return buffer.getvalue()


@router.get("/log.csv")
async def log_csv(request: Request, application: str = "") -> Response:
    held = request.app.state.gamgui.incident_pull
    log = held.log(application) if held else None
    if log is None or log.nothing_kept:     # never an empty CSV that would pass as no events
        return Response(f"That log wasn't read. {log.how_far if log else ''}".strip(), status_code=404, media_type="text/plain")
    text = await asyncio.to_thread(lambda: _csv_text(logs.table(log.rows or [])))
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={log.file_name}"})


def _zip(held: pull.Pull, stamp: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for log in held.logs:
            if not log.nothing_kept:
                archive.writestr(log.file_name, _csv_text(logs.table(log.rows or [])))
        archive.writestr("about.txt", pull.about(held, stamp))
    return buffer.getvalue()


@router.get("/export.zip")
async def export_all(request: Request) -> Response:
    held = request.app.state.gamgui.incident_pull
    if held is None or not held.ended:
        return Response("There's no finished pull to export.", status_code=404, media_type="text/plain")
    now = datetime.now().astimezone()
    data = await asyncio.to_thread(_zip, held, now.strftime("%Y-%m-%d %H:%M %Z"))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=incident-logs-{now.strftime('%Y-%m-%d-%H%M')}.zip"})
