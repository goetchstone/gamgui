"""Change many people from a CSV (roadmap 4.6): upload, preview each change, run as a job.

The preview reads the directory (fresh, never a stale list) and the org units, plans each row with ``core/bulk_csv.plan`` and
holds the changes under a single-use token; the confirm step posts only that token. The run reads each person again before
their write (``bulk_csv.run``). The preview's "before" downloads as the undo file, held apart so a download never spends the
run's token.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, List

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from ...core import bulk_csv, guard
from ...core.connectors.base import RiskLevel
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, error_partial, unread_why

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users/csv")

_FLOW = "profile_csv"
_BEFORE_FLOW = "profile_csv_before"   # the undo file: read by its download, never run
_KEY = ("profile_csv",)               # the confirm step posts only its token
_KIND = "profile_csv"
_PAGE = "bulk_csv.html"
_TRY_AGAIN = "Try again in a moment."


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": request.app.state.gamgui.connector is not None,
                                                       "columns": bulk_csv.HEADER, "max_rows": bulk_csv.MAX_ROWS})


@router.get("/template.csv")
async def template() -> Response:
    return Response(",".join(bulk_csv.HEADER) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="people.csv"'})


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, csv_file: Annotated[UploadFile, File()]) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to (web/previews.py)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        data = await csv_file.read(bulk_csv.MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - the upload itself: said in fixed words, logged
        logger.warning("reading an uploaded CSV failed", exc_info=exc)
        return error_partial(request, "Couldn't read the file. Choose it again and try once more.")
    if len(data) > bulk_csv.MAX_BYTES:
        return error_partial(request, "That file is over 1 MB. Split it into several, or check you picked the right file.")
    try:
        text = bulk_csv.decode(data)
    except ValueError as exc:   # bulk_csv.decode's own words
        return error_partial(request, str(exc))
    parsed = bulk_csv.parse(text)
    changes: List[bulk_csv.Change] = []
    refused, unchanged = list(parsed.errors), 0
    if parsed.rows:
        try:   # the directory as it is now (never a stale list), and the units only when a row names one
            directory = await st.users()
            units = await conn.org_units() if any("org_unit" in r.values for r in parsed.rows) else []
        except Exception as exc:  # noqa: BLE001 - unread: nothing is offered, whatever it was
            return error_partial(request, "Couldn't read the directory or the org units, so nothing is offered. "
                                 + unread_why(exc, _TRY_AGAIN))
        changes, more, unchanged = bulk_csv.plan(parsed.rows, directory, units)
        refused += more
    token = before = ""
    if changes:
        token = st.previews.hold(_FLOW, _KEY, changes, tenant=tenant)
        before = st.previews.hold(_BEFORE_FLOW, _KEY, bulk_csv.before_csv(changes), tenant=tenant)
    decision = guard.evaluate(_changes(changes), typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    return TEMPLATES.TemplateResponse(request, "_bulk_csv_preview.html", {
        "changes": changes, "refused": refused, "notes": parsed.notes, "unchanged": unchanged, "token": token,
        "before": before, "blanks": bulk_csv.blanks_before(changes), "typed_count": decision.requires_typed_count,
        "labels": bulk_csv.LABELS, "columns": bulk_csv.COLUMNS})


def _changes(changes: List[bulk_csv.Change]):
    return guard.changes([c.email for c in changes], RiskLevel.LOW, "Change from a CSV")


@router.get("/before.csv")
async def before_csv(request: Request, key: str = "") -> Response:
    """The preview's undo file: each changed person's values as they were. Gone with the preview (15 minutes)."""
    text = request.app.state.gamgui.previews.peek(_BEFORE_FLOW, key)
    if text is None:
        return Response("That preview's before file is gone: preview the CSV again.\n", status_code=404, media_type="text/plain")
    return Response(text, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="before.csv"'})


async def _run(job, st, conn, changes: List[bulk_csv.Change]) -> None:
    try:
        await bulk_csv.run(job, conn, changes)
    finally:
        st.invalidate_users()   # titles, phones and units changed: the list mustn't keep the old ones


@router.post("/apply", response_class=HTMLResponse)
async def apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    changes, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), _KEY, again="upload the CSV and preview it again")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(_changes(changes), form, confirm_step=True, typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return error_partial(request, f"{refusal} Nothing ran: preview again.")
    n = len(changes)
    job = start_job(st.jobs, n, kind=_KIND, title=f"Change {n} {'person' if n == 1 else 'people'} from a CSV")
    job.task = asyncio.create_task(_run(job, st, conn, changes))
    return TEMPLATES.TemplateResponse(request, "_bulk_csv_run.html", {"job": job})


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != _KIND:
        return error_partial(request, "That run is no longer here.")
    return TEMPLATES.TemplateResponse(request, "_bulk_csv_run.html", {"job": found})
