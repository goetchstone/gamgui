"""The incident report's screen and export. Reads and this Mac only: the review is the operator's words, kept with the incident, and the export is a file. Rules: ``core/incident/report.py``. Runbook:
docs/domains/incident.md."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import tempfile
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.background import BackgroundTask

from ...core.incident import logs, report_pdf, runs, store
from ...core.incident.report import Evidence, Report, cell_words, evidence_about, make, markdown_of, text_of
from ..jobs import CHANGE_WORDS
from ..server import TEMPLATES
from ._common import admin_address, error_partial, incident_store, open_incident_record
from .incident_kept import reopen_remembered
from .incident_logs import _current_reading, _partial, _running, _ticked

router = APIRouter(prefix="/incident/report")

_SHOWN = 300     # rows a list draws: its CSV has them all
_PANEL = "_incident_report.html"
# What each list's file is called in the export.
FILES = {"gotIt": "who-got-it.csv", "emails": "emails.csv", "accounts": "accounts.csv", "actions": "steps-taken.csv", "toDo": "still-to-do.csv", "pulled": "emails-pulled.csv"}


def _local(moment: datetime) -> str:
    """With its own zone: one line for the whole report would be an hour off across a clock change."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _change_words(action: str) -> str:
    return CHANGE_WORDS.get(action, action.replace("_", " ").capitalize())


async def _directory(st) -> Tuple[Optional[Dict[str, Tuple[bool, bool]]], str]:
    """Each address's (enrolled, suspended), and how old that is. None with the directory unread: then nothing is said of 2-Step Verification, never "on"."""
    try:
        users = await st.users(stale_ok=True)
    except Exception:  # noqa: BLE001 - nothing said of it
        return None, ""
    age = st.user_cache.age_seconds
    read_at = _local(datetime.now(timezone.utc) - timedelta(seconds=age)) if age is not None else ""
    return ({u.primary_email.lower(): (u.enrolled_2sv, u.suspended) for u in users} if users else None), read_at


def _coverage(st, reading, unread_saved: Sequence[str], saved_problem: str, audit_oldest: Optional[datetime], directory_age: str) -> List[str]:
    """What the numbers rest on and what they can't show, in the words the other screens use: a report gets handed on without them. Empty only when there's nothing to say."""
    held = st.incident_pull
    said: List[str] = []
    logs_read = [log for log in held.logs if log.rows is not None]
    who = ", ".join(held.query.users) if held.query.users else "every account"
    said.append(f"The logs read: {', '.join(log.name for log in logs_read) or 'none'}, for {who}, over {logs.window_words(held.query.hours)}" + (f", from {held.query.ip} only." if held.query.ip else "."))
    said += [f"{log.name} wasn't read: {log.problem or log.how_far}" for log in held.logs if log.rows is None]
    said += [f"{log.name} stopped short: {log.how_far}" for log in logs_read if log.capped]
    pulled = {log.application for log in held.logs}
    missing = [logs.NAMES[a] for a in logs.INCIDENT_DEFAULT if a not in pulled]
    if missing:
        said.append(f"Not pulled, so nothing from them is here: {', '.join(missing)}.")
    if "gmail" not in {log.application for log in logs_read}:
        said.append("Without the Gmail log, who the attacker emailed and whether they were warned can't be shown.")
    read = st.incident_groups
    if read is not None:
        said += [f"The group {g} counts on the warning's send, not by its members: {why}." for g, why in read.problems.items()]
        said += [f"{count} of {g}'s members have no address, so aren't listed." for g, count in read.nameless.items()]
    if held is not None and not held.from_evidence:
        said.append("These numbers come from a read that isn't saved with the incident, so it isn't in the evidence.")
    if unread_saved:
        said.append(f"Saved reads that couldn't be read, so aren't counted: {', '.join(unread_saved)}.")
    if saved_problem:
        said.append(f"The saved reads couldn't be listed: {saved_problem}")
    if audit_oldest is not None:
        said.append(f"This app's audit log was read back only to {_local(audit_oldest)}, so an earlier step of its own may be missing.")
    if directory_age:
        said.append(f"2-Step Verification is from the directory as read at {directory_age}.")
    return said


async def _report(st) -> Optional[Report]:
    """Made at most once for each change of what it's made of. None without an incident open or a finished read of its logs."""
    record = open_incident_record(st)
    reading = await _current_reading(st) if record else None
    reading_key = st.incident_reading[0] if reading else None     # here, with no wait between: the screen may read again during what follows
    conn = st.connector
    if record is None or reading is None or conn is None:
        return None
    kept, _ = incident_store(st)
    try:
        stat = await asyncio.to_thread(os.stat, conn.audit.path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = (0, 0)
    directory, age = await _directory(st)
    saved_problem = ""
    saved: Tuple[Tuple[str, str], ...] = ()
    try:
        saved = tuple(await asyncio.to_thread(kept.saved_about, record.id)) if kept else ()
    except (OSError, store.Problem) as exc:
        saved_problem = getattr(exc, "strerror", None) or str(exc)
    held = st.incident_pull
    if held is None:
        return None
    unread_saved = list(st.incident_saved_unreadable)
    read = st.incident_groups
    groups_said = (tuple(sorted(read.problems.items())), tuple(sorted(read.nameless.items()))) if read is not None else ()
    key = (reading_key, record, stamp, tuple(sorted(directory.items())) if directory is not None else None, age, saved, saved_problem, tuple(unread_saved), groups_said)
    if st.incident_report is None or st.incident_report[0] != key:
        signs = [s.attacked_at for s in reading.statuses if s.attacked_at]
        since = min([record.opened, *signs])
        changes, oldest = await asyncio.to_thread(runs.changes_since, conn.audit.path, since, _change_words)
        ticked = _ticked(st, reading, record)
        coverage = _coverage(st, reading, unread_saved, saved_problem, oldest, age)
        evidence = [Evidence(name, evidence_about(about) or "its about.txt couldn't be read") for name, about in saved]
        made = await asyncio.to_thread(make, ticked, record, changes, evidence, directory, datetime.now(timezone.utc), admin_address(st), _local, coverage, held.from_evidence)
        st.incident_report = (key, made)
    return st.incident_report[1]


def _context(st, report: Optional[Report], problem: str = "") -> dict:
    return {"connected": st.connector is not None, "report": report, "record": open_incident_record(st), "problem": problem, "when": _local, "here": "/incident/report", "open_incident": open_incident_record(st),
            "pulling": _running(st) is not None}


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    reopen_remembered(st)
    opening = st.jobs.get(st.incident_open_job)
    if opening is not None and not opening.finished and opening.task is not None:
        await asyncio.wait({opening.task})      # what it's reading is what this reports
    return TEMPLATES.TemplateResponse(request, "incident_report.html", _context(st, await _report(st)))


@router.get("/list", response_class=HTMLResponse)
async def count_list(request: Request, id: str = "") -> HTMLResponse:  # noqa: A002 - the name in the page's address
    """The list behind one number, bounded: its CSV has them all."""
    report = await _report(request.app.state.gamgui)
    count = report.count(id) if report else None
    if count is None:
        return error_partial(request, "That list isn't in the report now: open it again.")
    return TEMPLATES.TemplateResponse(request, "_incident_report_list.html", {"count": count, "rows": count.table.rows[:_SHOWN], "shown": _SHOWN, "words": lambda cell: cell_words(cell, _local)})


@router.get("/list.csv")
async def count_csv(request: Request, id: str = "") -> Response:  # noqa: A002 - the name in the page's address
    report = await _report(request.app.state.gamgui)
    count = report.count(id) if report else None
    if count is None:
        return Response("That list isn't in the report now.", status_code=404, media_type="text/plain")
    name = FILES.get(id, f"{id}.csv")
    return Response(count.table.csv_text(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


def _review_refused(request: Request, why: str) -> HTMLResponse:
    """Said above the form, leaving it as it is: what was typed is never lost to a refusal."""
    response = error_partial(request, why)
    response.headers["HX-Retarget"] = "#report-review-said"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.post("/review", response_class=HTMLResponse)
async def save_review(request: Request, kind: Annotated[str, Form()] = "", severity: Annotated[str, Form()] = "", meeting: Annotated[str, Form()] = "", attendees: Annotated[str, Form()] = "",
                      cause: Annotated[str, Form()] = "", went_well: Annotated[str, Form()] = "", improve: Annotated[str, Form()] = "", plan: Annotated[str, Form()] = "", summary: Annotated[str, Form()] = "",
                      incident: Annotated[str, Form()] = "") -> HTMLResponse:
    """The review, as the operator writes it, kept with the incident the form was for: a page left open on another incident never overwrites this one's."""
    st = request.app.state.gamgui
    kept, why = incident_store(st)
    record = open_incident_record(st)
    if kept is None or record is None:
        return _review_refused(request, why or "Open an incident first: the review is kept with it.")
    if incident != record.id:
        return _review_refused(request, "This form was for another incident, which isn't the one open now, so nothing was saved. Copy what you typed, then open that incident.")
    try:
        when = datetime.fromisoformat(meeting.strip()).astimezone(timezone.utc) if meeting.strip() else None
        if when is not None and not store.is_time(when):
            raise ValueError
    except (ValueError, OverflowError, OSError):
        return _review_refused(request, "That date isn't one an incident can keep.")
    try:
        kept.save(replace(record, review=store.Review(kind=kind.strip(), severity=severity.strip(), attendees=attendees.strip(), cause=cause.strip(), went_well=went_well.strip(), improve=improve.strip(),
                                                      plan=plan.strip(), summary=summary.strip(), meeting=when)))
    except store.Problem as exc:
        return _review_refused(request, str(exc))
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, await _report(st)))


def _write_export(report: Report, kept, record_id: str, path: str, partial: bool) -> None:
    """Into a private temp file, whole: the report in three formats, each list, and every saved read copied with its fingerprint. Evidence that can't be read stops it, so a report never says more than it holds.
    A list that can't be everyone says so in its name: a file gets passed around without its page."""
    hashes = []
    mark = "-partial" if partial else ""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, data, digest in kept.evidence_files(record_id):
            archive.writestr(f"evidence/{name}", data)
            hashes.append((f"evidence/{name}", digest))
        blocks = report.blocks(_local, "", hashes)
        archive.writestr("Incident report.md", markdown_of(blocks))
        archive.writestr("Incident report.txt", text_of(blocks))
        archive.writestr("Incident report.pdf", report_pdf.pdf_of(blocks))
        for count_id, name in FILES.items():
            count = report.count(count_id)
            if count is not None:
                archive.writestr(name.replace("who-got-it", f"who-got-it{mark}"), count.table.csv_text())
        archive.writestr(f"who-it-reached{mark}.csv", report.reached.csv_text())
        archive.writestr(f"timeline{'-partial' if report.timeline_cut else ''}.csv", report.timeline.csv_text())
        archive.writestr("evidence-sha256.txt", "".join(f"{digest}  {name}\n" for name, digest in hashes))


def _sweep_exports() -> None:
    """A made export that was never saved, whatever stranded it: it names people and addresses, so only the newest asked for is ever kept."""
    for stale in Path(tempfile.gettempdir()).glob("gamgui-report-*.zip"):
        with contextlib.suppress(OSError):
            stale.unlink()


@router.post("/export", response_class=HTMLResponse)
async def make_export(request: Request) -> HTMLResponse:
    """Made first, then saved: a download link's response is saved as a file in the app window whatever its status, so a failure has to be said here, on the screen."""
    st = request.app.state.gamgui
    report = await _report(st)
    kept, _ = incident_store(st)
    if report is None or kept is None:
        return error_partial(request, "Open an incident with its logs read first.")
    _sweep_exports()
    st.incident_export = None
    fd, path = tempfile.mkstemp(prefix="gamgui-report-", suffix=".zip")     # 0600: it names people and addresses
    os.close(fd)
    try:
        await asyncio.to_thread(_write_export, replace(report, made=datetime.now(timezone.utc)), kept, report.record.id, path, _partial(st))
    except (store.Problem, OSError) as exc:
        with contextlib.suppress(OSError):
            os.unlink(path)
        return error_partial(request, f"The report wasn't exported: {exc}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    token = secrets.token_urlsafe(16)
    st.incident_export = (token, path, report.record.id)
    return TEMPLATES.TemplateResponse(request, "_incident_report_export.html", {"token": token})


@router.get("/export.zip")
async def export(request: Request, file: str = "") -> Response:
    st = request.app.state.gamgui
    held = st.incident_export
    if held is None or not secrets.compare_digest(held[0], file):
        return Response("That export isn't ready: export it again.", status_code=404, media_type="text/plain")
    st.incident_export = None
    return FileResponse(held[1], media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=incident-report-{held[2]}.zip"}, background=BackgroundTask(_quietly_unlink, held[1]))


def _quietly_unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
