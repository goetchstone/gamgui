"""Files (/files, plan F4): a read-only list of what this app made — the Google Sheets the Builder exported, read back from
the audit log, and the incidents' evidence folders. It runs no gam and writes nothing of its own (core/files.py)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import files
from ...core.catalog import load_catalog
from ..server import TEMPLATES
from ._common import app_state, incident_store
from .audit import _audit_path

router = APIRouter(prefix="/files")


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = app_state(request)
    sheets = await asyncio.to_thread(files.sheets_made, _audit_path(request))
    store, problem = incident_store(st)
    evidence = await asyncio.to_thread(files.evidence_folders, store) if store is not None else []
    if st.catalog is None:   # parsed off the event loop: a first visit shouldn't stall every other request
        st.catalog = await asyncio.to_thread(load_catalog)
    names = {s.command: (getattr(st.catalog.by_id(s.command), "name", "") or s.command) for s in sheets.made}
    return TEMPLATES.TemplateResponse(request, "files.html", {
        "sheets": sheets, "names": names, "shown": files.SHEETS_SHOWN, "scanned": files.RECORDS_SCANNED,
        "evidence": evidence, "incidents_problem": problem,
    })
