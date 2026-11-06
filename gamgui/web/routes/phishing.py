"""Phishing: take an email out of every mailbox that has it, by its Message-ID.

Find is two domain-wide reads an email, as a background job. Trash is one guarded step per mailbox Find showed, each through
``_run_write``: it runs what its preview held, the number of steps typed, and Stop works between them. Nothing here runs a free
Gmail query: a Message-ID only (``core/gam/commands.MessageId``). The sender's Sent copy is left as evidence.
"""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import replace
from typing import Annotated, Any, Dict, List

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core import bulk, guard, phishing
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import MessageId
from ...core.gam.errors import GAMError
from ...core.incident import runs
from ...core.incident.store import Problem as IncidentProblem
from ...core.onboarding import looks_like_email
from ..csvutil import csv_safe
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, admin_address, error_partial, friendly, nobody_has_checks

router = APIRouter(prefix="/phishing")

_FLOW = "phishing_trash"
_SHOWN = 200     # mailboxes listed an email; the CSV has them all
_FIND = "_phishing_find.html"
_TRASH = "_phishing_trash.html"


def _csv(columns: List[str], rows: List[List[str]], disposition: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows([[csv_safe(c) for c in row] for row in rows])
    return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": disposition})


def _found_context(st) -> dict:
    found = st.phishing_found
    return {"found": found, "shown": _SHOWN}


@router.get("", response_class=HTMLResponse)
async def page(request: Request, message: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    return TEMPLATES.TemplateResponse(request, "phishing.html", {
        "connected": st.connector is not None, "most": phishing.MOST_AT_ONCE, "message": message, "job": st.jobs.get(st.phishing_job_id), **_found_context(st)})


async def _search_one(conn, message: MessageId) -> phishing.EmailSearch:
    """One email: its dry run, whose failure is the email's, beside it its Spam read, whose failure is only said, and then one copy
    read, so what the ID matched can be checked by eye before anything is trashed. That read blocks nothing either."""
    results: List[Any] = list(await asyncio.gather(conn.find_message_copies(message), conn.find_spam_copies(message), return_exceptions=True))
    copies, spam = results
    if isinstance(copies, BaseException):
        raise copies
    if isinstance(spam, BaseException):
        if isinstance(spam, asyncio.CancelledError):
            raise spam
        search = phishing.EmailSearch(message, copies, None, _why(spam))
    else:
        search = phishing.EmailSearch(message, copies, spam)
    first = search.first_with_copy
    if first is None:
        return search
    try:
        copy = await conn.mailbox_copy(first, message)
    except Exception as exc:  # noqa: BLE001 — said beside the result, which stands
        return replace(search, sample_problem=_why(exc))
    return replace(search, sample=(first, copy)) if copy else replace(search, sample_problem=f"{first}'s copy was gone when it was read.")


def _why(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, phishing.Incomplete) else friendly(exc if isinstance(exc, Exception) else Exception())


async def _find(job, st, conn, messages: List[MessageId]) -> None:
    found = phishing.Found()
    gate = asyncio.Semaphore(phishing.SEARCHED_AT_ONCE)

    async def one(message: MessageId):
        async with gate:
            if job.cancel_requested:
                return None
            job.current = message.id
            try:
                result = await _search_one(conn, message)
            except Exception as exc:  # noqa: BLE001 — an email that can't be searched is said beside the rest
                found.problems.append((message, _why(exc)))
                job.record(message.id, False, _why(exc))
                return None
            job.record(message.id, True)
            return result

    try:
        results = await asyncio.gather(*(one(m) for m in messages))
        found.searches = [r for r in results if r is not None]
        if job.cancel_requested:
            bulk.stop_requested(job)
        elif st.connector is conn:
            st.phishing_found = found
            _remember_gone(st, conn, found)
        else:
            job.error = "Not kept: the connection changed during the search. Find again."
    finally:
        job.finish()


def _remember_gone(st, conn, found: phishing.Found) -> None:
    """What Find showed of each email, kept so Respond can show it done, a read being never audited: gone only with every mailbox and its Spam searched. A copy or an unsure answer is kept too, since it
    takes back an earlier word that it was gone. Said on Respond if it can't be."""
    nobody, _ = nobody_has_checks(st)
    if nobody is not None and found.searches:
        try:
            nobody.record(conn.domain, [s.message.id for s in found.nobody_has], admin_address(st), not_gone=[s.message.id for s in (*found.with_copies, *found.unsure)])
            st.incident_nobody_problem = ""
        except IncidentProblem as exc:
            st.incident_nobody_problem = f"What Phishing's Find showed gone couldn't be kept: {exc}"


@router.post("/find", response_class=HTMLResponse)
async def find(request: Request, messages: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        parsed = phishing.parse_message_ids(messages)
    except ValueError as exc:
        return error_partial(request, str(exc))
    existing = st.jobs.get(st.phishing_job_id)
    if existing is not None and not existing.finished:
        return TEMPLATES.TemplateResponse(request, _FIND, {"job": existing, **_found_context(st)})
    st.phishing_found = None    # its counts are about to be old
    job = start_job(st.jobs, len(parsed), kind="phishing_find", title=f"Find {len(parsed)} email{'' if len(parsed) == 1 else 's'} in every mailbox")
    st.phishing_job_id = job.id
    job.task = asyncio.create_task(_find(job, st, conn, parsed))
    return TEMPLATES.TemplateResponse(request, _FIND, {"job": job, **_found_context(st)})


@router.get("/find/status", response_class=HTMLResponse)
async def find_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, _FIND, {"job": held if held is not None and held.kind == "phishing_find" else None, **_found_context(st)})


def _changes(steps: List[phishing.TrashStep]) -> list:
    several = len({s.message.id for s in steps}) > 1
    return guard.changes([f"{s.email} ({'Spam' if s.in_spam else 'mailbox'}) {s.message.id}" for s in steps], RiskLevel.DESTRUCTIVE,
                         phishing.step_summary(steps[0], several) if steps else "")


@router.post("/trash/preview", response_class=HTMLResponse)
async def trash_preview(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    found = st.phishing_found
    steps = phishing.trash_steps(found) if found else []
    if not steps:
        return error_partial(request, "No mailbox has a copy to trash.")
    decision = guard.evaluate(_changes(steps), typed_count_above=0)
    several = len({s.message.id for s in steps}) > 1
    # Bound to this Find: one run since replaces what's shown, so this preview is no longer what the page lists.
    st.previews.drop(_FLOW, found.run)
    token = st.previews.hold(_FLOW, found.run, steps, tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_phishing_confirm.html", {
        "steps": steps[:_SHOWN], "count": len(steps), "more": max(0, len(steps) - _SHOWN), "token": token, "decision": decision,
        "searches": found.with_copies,
        "summaries": [phishing.step_summary(s, several) for s in steps[:_SHOWN]]})


async def _trash(job, st, conn, steps: List[phishing.TrashStep]) -> None:
    """One step at a time: Stop works between them, and a failure every later step would share stops the rest."""
    counts: Dict[str, int] = {}
    for step in steps:
        counts[step.message.id] = counts.get(step.message.id, 0) + 1
    by = admin_address(st)
    try:
        for i, step in enumerate(steps):
            if bulk.stop_requested(job):
                break
            job.current = step.email
            about = runs.about(step.message.id, job.id, conn.domain, counts[step.message.id], step.searched_everywhere, by, "spam" if step.in_spam else "mailbox")
            kind, why, detail = None, "", ""
            try:
                if step.in_spam:
                    res = await conn.trash_messages(step.email, list(step.ids), about)
                else:
                    res = await conn.trash_message(step.email, step.message, step.copies, about)
                ok = res.ok
                if not ok:
                    kind, why, detail = res.kind, res.remediation, res.detail
            except Exception as exc:  # noqa: BLE001 — one mailbox must not stop the rest
                ok, kind, why, detail = False, getattr(exc, "kind", None), friendly(exc), str(exc)
            job.record(f"{step.email}{' (Spam)' if step.in_spam else ''}", ok, why, detail)
            if not ok and (reason := bulk.stop_reason(kind, why, len(steps) - i - 1)):
                job.error = reason
                break
    finally:
        job.finish()


@router.post("/trash/apply", response_class=HTMLResponse)
async def trash_apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    found = st.phishing_found
    steps, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), found.run if found else "", again="find again, then click Trash",
                                      what="search")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(_changes(steps), form, confirm_step=True, typed_count_above=0)
    if refusal:
        return error_partial(request, refusal)
    # Its counts are from before the run: Find Again shows what's left.
    st.phishing_kept_rows = phishing.all_copies_rows(st.phishing_found) if st.phishing_found else []
    st.phishing_found = None
    job = start_job(st.jobs, len(steps), window=12, kind="phishing", title=f"Trash copies from {len(steps)} mailbox step{'' if len(steps) == 1 else 's'}")
    job.task = asyncio.create_task(_trash(job, st, conn, steps))
    return TEMPLATES.TemplateResponse(request, _TRASH, {"job": job})


@router.get("/trash/status", response_class=HTMLResponse)
async def trash_status(request: Request, job: str = "") -> HTMLResponse:
    held = request.app.state.gamgui.jobs.get(job)
    if held is None or held.kind != "phishing":
        return error_partial(request, "That run is no longer available.")
    return TEMPLATES.TemplateResponse(request, _TRASH, {"job": held})


@router.get("/copies.csv")
async def copies_csv(request: Request) -> Response:
    st = request.app.state.gamgui
    rows = phishing.all_copies_rows(st.phishing_found) if st.phishing_found else st.phishing_kept_rows
    return _csv(phishing.ALL_COPIES_COLUMNS, rows, "attachment; filename=phishing-copies.csv")


@router.post("/recipients", response_class=HTMLResponse)
async def recipients(request: Request, sender: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "") -> HTMLResponse:
    """Who it was sent to, from the sender's own copy: a read. It never claims to be everyone."""
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    sender = sender.strip()
    if not looks_like_email(sender):
        return error_partial(request, "Enter the sender's address: only their own copy can show who it was sent to.")
    try:
        parsed = MessageId.parse(message)
    except ValueError as exc:
        return error_partial(request, str(exc))
    try:
        copy = await conn.mailbox_copy(sender, parsed)
        domains = await st.tenant_domains()
    except phishing.Incomplete as exc:
        return error_partial(request, str(exc))
    except GAMError as exc:
        return error_partial(request, exc.remediation)
    except Exception as exc:  # noqa: BLE001 — a read that failed changed nothing: said, whatever it was
        return error_partial(request, friendly(exc))
    if st.connector is not conn:     # not one tenant's recipients in another's CSV
        return error_partial(request, "The connection changed during the read. Read it again.")
    rows = phishing.recipients_rows(copy, domains) if copy else []
    st.phishing_recipients = rows
    finds = [s for s in (st.phishing_found.searches if st.phishing_found else []) if s.message == parsed]
    named = {a.lower() for a, _ in copy.recipients} if copy else set()
    unnamed = [m.email for s in finds for m in s.copies.with_copies if m.email.lower() not in named and m.email.lower() != sender.lower()]
    return TEMPLATES.TemplateResponse(request, "_phishing_recipients.html", {
        "copy": copy, "sender": sender, "message": parsed, "unnamed": unnamed,
        "outside": [r for r in rows if r[1] == "outside"], "inside": [r for r in rows if r[1] == "inside"],
        "check": [r for r in rows if r[1] == "check by hand"]})


@router.get("/recipients.csv")
async def recipients_csv(request: Request) -> Response:
    return _csv(phishing.RECIPIENTS_COLUMNS, request.app.state.gamgui.phishing_recipients, "attachment; filename=phishing-recipients.csv")

