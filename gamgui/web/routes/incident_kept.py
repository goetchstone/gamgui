"""A kept incident: made by hand or from an export, its notes and details, and every pull of the logs made while it's open, on this Mac. It reopens without reading Google again.

Reads and this Mac only: nothing here changes the tenant. Rules: ``core/incident/store.py``. Runbook: docs/domains/incident.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from ...core.incident import pull, store
from ..jobs import start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, admin_address, error_partial, incident_store, open_incident_record

router = APIRouter(prefix="/incident/kept")

_PANEL = "_incident_kept.html"


def _when(text: str) -> Optional[datetime]:
    """A time from a form's `datetime-local` or `date`, as this Mac's; None when left empty, and said when it isn't one this can keep (a mistyped year), never swapped for now."""
    if not (text or "").strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip()).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise store.Problem("That date isn't one an incident can keep.") from exc
    if not store.is_time(moment):
        raise store.Problem("That date isn't one an incident can keep.")
    return moment


def _context(request: Request, problem: str = "", job=None) -> dict:
    st = request.app.state.gamgui
    domain = st.connector.domain if st.connector else ""
    kept, why = incident_store(st)
    open_id = st.incident_open
    return {"connected": st.connector is not None, "records": kept.for_domain(domain) if kept else [], "unreadable": kept.unreadable if kept else [], "store_problem": why, "problem": problem,
            "open": kept.get(open_id) if kept and open_id else None, "saved": st.incident_saved, "saved_unreadable": st.incident_saved_unreadable, "job": job or _running(st),
            "today": datetime.now().astimezone().strftime("%Y-%m-%d"), "default_by": admin_address(st), "here": "/incident/kept", "open_incident": open_incident_record(st), "folder": str(kept.root / open_id) if kept and open_id else ""}


def _running(st):
    job = st.jobs.get(st.incident_open_job)
    return job if job is not None and not job.finished else None


async def read_back(st, conn, kept: store.IncidentStore, record_id: str) -> bool:
    """Every saved read of the incident, off the event loop, put together once as its evidence. False when it's no longer the one open here."""
    exports, unreadable = await asyncio.to_thread(kept.exports, record_id)
    together, broken = await asyncio.to_thread(pull.evidence, exports)
    if st.connector is not conn or st.incident_open != record_id:
        return False
    st.incident_evidence = together
    st.incident_saved = [e.name for e in exports]
    st.incident_saved_unreadable = [*unreadable, *broken]
    st.incident_swept = {a for a in (pull.swept_address(e.about) for e in exports if e.name not in broken) if a}
    st.incident_reading = None
    return True


async def _read_saved(job, st, conn, kept: store.IncidentStore, record_id: str) -> None:
    """Opens the incident's evidence as what the logs and the reader show."""
    try:
        if not await read_back(st, conn, kept, record_id):
            return
        together = st.incident_evidence
        # Showing the evidence waits for a pull still reading, which it would otherwise replace. An incident with none leaves a pull on screen that wasn't saved, but not another's evidence.
        shown = st.incident_pull
        if not _pull_running(st) and (together is not None or (shown is not None and shown.from_evidence)):
            st.incident_pull = together
    except Exception as exc:  # noqa: BLE001 - said on the screen, whatever it was
        job.error = f"Couldn't read the incident's saved logs: {exc}"
    finally:
        job.done += 1
        job.finish()


def _pull_running(st) -> bool:
    job = st.jobs.get(st.incident_pull_job)
    return job is not None and not job.finished


def open_incident(st, conn, kept: store.IncidentStore, record_id: str):
    """Opens the incident on the screens, its saved reads read in the background."""
    st.incident_open = record_id
    st.incident_evidence = None
    st.incident_saved = []
    st.incident_saved_unreadable = []
    st.incident_swept = set()
    st.incident_reading = None
    kept.remember_open(record_id)
    job = start_job(st.jobs, 1, kind="incident_open", title="Read the incident's saved logs")
    st.incident_open_job = job.id
    job.task = asyncio.create_task(_read_saved(job, st, conn, kept, record_id))
    return job


def reopen_remembered(st) -> None:
    """The incident last open, opened again after a restart or a rebuild, only while connected to its domain."""
    if st.incident_open or st.connector is None or _pull_running(st):
        return
    kept, _ = incident_store(st)
    remembered = kept.remembered_open() if kept else ""
    record = kept.get(remembered) if kept and remembered else None
    if record and record.domain == st.connector.domain.lower():
        open_incident(st, st.connector, kept, record.id)


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    reopen_remembered(st)
    return TEMPLATES.TemplateResponse(request, "incident_kept.html", _context(request))


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request, job=held if held is not None and held.kind == "incident_open" else None))


def _refused(request: Request, why: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request, why))


async def _export_from(st, conn, upload: UploadFile) -> store.Export:
    """An uploaded Export all, read and checked: it reads back whole, and isn't all another tenant's, by the domains of the connection it's for."""
    if (upload.size or 0) > store.EXPORT_CAP:
        raise store.Problem(f"The export is larger than {store.EXPORT_CAP >> 30} GB.")
    data = await upload.read()
    name = (upload.filename or "").rsplit("/", 1)[-1].removesuffix(".zip")
    export = await asyncio.to_thread(store.read_zip, name or "Incident logs", data)
    held = await asyncio.to_thread(pull.pull_of_export, export)
    if held is None:
        raise store.Problem("That export can't be read back: a log in it isn't one of this app's, or it holds too many events.")
    domains = {d.lower() for d in await st.tenant_domains()}
    if st.connector is not conn:
        raise store.Problem("The connection changed: nothing was added.")
    if await asyncio.to_thread(pull.from_another_tenant, held, domains):
        raise store.Problem("That export's events are another domain's: one tenant's logs never go into another's incident.")
    return export


@router.post("/new", response_class=HTMLResponse)
async def new(request: Request, title: Annotated[str, Form()] = "", reported: Annotated[str, Form()] = "", opened_by: Annotated[str, Form()] = "", opened: Annotated[str, Form()] = "",
              export: Optional[UploadFile] = None) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    kept, why = incident_store(st)
    if kept is None:
        return _refused(request, why)
    if _pull_running(st):
        return _refused(request, "A pull of the logs is going on: wait for it to finish, then make the incident, so it takes that pull.")
    try:
        picked = await _export_from(st, conn, export) if export and export.filename else None
        record = await asyncio.to_thread(kept.create, conn.domain, title, _when(opened) or datetime.now(timezone.utc), opened_by, reported)
    except store.Problem as exc:
        return _refused(request, str(exc))
    said = ""
    try:
        # A new incident takes the pull just made: else making one after a pull lost it.
        shown = st.incident_pull if st.connector is conn and not picked else None
        made = picked or (await asyncio.to_thread(pull.as_export, shown) if shown is not None and shown.ended and not shown.from_evidence else None)
        if made:
            await asyncio.to_thread(kept.keep, made, record.id)
    except store.Problem as exc:        # the incident is made: it's open below, with the place to add it again
        said = f"The incident is kept, but its logs weren't saved to it: {exc}"
    if st.connector is not conn or _pull_running(st):   # either changed while it was saved: it opens when asked, not over them
        return _refused(request, f"“{record.title}” is kept. {said} Open it from the list once the pull has finished, or while connected to its domain.".replace("  ", " "))
    job = open_incident(st, conn, kept, record.id)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request, said, job))


@router.post("/open", response_class=HTMLResponse)
async def open_one(request: Request, incident: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    kept, why = incident_store(st)
    if kept is None:
        return _refused(request, why)
    record = kept.get(incident)
    if record is None or record.domain != conn.domain.lower():     # it opens only while connected to its domain
        return _refused(request, "That incident isn't one of this domain's.")
    if _pull_running(st):
        return _refused(request, "A pull of the logs is going on: wait for it to finish, so it's saved where you meant.")
    job = open_incident(st, conn, kept, record.id)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request, job=job))


@router.post("/close", response_class=HTMLResponse)
async def close(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    kept, _ = incident_store(st)
    if _pull_running(st):
        return _refused(request, "A pull of the logs is going on: wait for it to finish, so it's saved where you meant.")
    st.incident_open = ""
    st.incident_evidence = None
    st.incident_saved = []
    st.incident_saved_unreadable = []
    st.incident_swept = set()
    if st.incident_pull is not None and st.incident_pull.from_evidence:     # never a pull that wasn't saved
        st.incident_pull = None
    st.incident_reading = None
    if kept:
        kept.remember_open("")
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request))


def _open_record(request: Request):
    st = request.app.state.gamgui
    kept, why = incident_store(st)
    record = kept.get(st.incident_open) if kept and st.incident_open else None
    return kept, record, why or ("Open an incident first." if record is None else "")


@router.post("/details", response_class=HTMLResponse)
async def details(request: Request, title: Annotated[str, Form()] = "", reported: Annotated[str, Form()] = "", opened_by: Annotated[str, Form()] = "", opened: Annotated[str, Form()] = "") -> HTMLResponse:
    kept, record, why = _open_record(request)
    if not kept or not record:
        return _refused(request, why)
    try:
        kept.save(replace(record, title=title, reported=reported, opened_by=opened_by, opened=_when(opened) or record.opened))
    except store.Problem as exc:
        return _refused(request, str(exc))
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request))


@router.post("/note", response_class=HTMLResponse)
async def note(request: Request, text: Annotated[str, Form()] = "", at: Annotated[str, Form()] = "") -> HTMLResponse:
    """A note with its own time, which can be earlier than now: "told the insurer at 9"."""
    kept, record, why = _open_record(request)
    if not kept or not record:
        return _refused(request, why)
    try:
        kept.add_note(record.id, text, _when(at) or datetime.now(timezone.utc))
    except store.Problem as exc:
        return _refused(request, str(exc))
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request))


@router.post("/import", response_class=HTMLResponse)
async def import_export(request: Request, export: Optional[UploadFile] = None) -> HTMLResponse:
    """An export copied in, byte for byte: the evidence is exactly what was exported, which is what to hand anyone."""
    st = request.app.state.gamgui
    kept, record, why = _open_record(request)
    if not kept or not record:
        return _refused(request, why)
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    if not export or not export.filename:
        return _refused(request, "Pick an export of the logs: the zip file Export all saves.")
    if _pull_running(st):
        return _refused(request, "A pull of the logs is going on: wait for it to finish.")
    conn = st.connector
    try:
        picked = await _export_from(st, conn, export)
        if st.incident_open != record.id:
            raise store.Problem("The incident open here changed: nothing was added.")
        await asyncio.to_thread(kept.keep, picked, record.id)
    except store.Problem as exc:
        return _refused(request, str(exc))
    # Added whatever came next, and shown only if it's still the one open: a close, another incident or a pull started meanwhile isn't undone by reopening this.
    if st.incident_open != record.id or st.connector is not conn:
        return TEMPLATES.TemplateResponse(request, _PANEL, _context(request))
    if _pull_running(st):
        return _refused(request, "It's added: it shows once the pull going on has finished.")
    job = open_incident(st, conn, kept, record.id)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(request, job=job))
