"""The jobs tray (plan U5): every background job listed from any page, its panel on a page of its own,
and Stop.

A job still lives only in memory on ``AppState.jobs``; the tray reads that registry, so a run survives
navigating away — not a quit. Stop sets ``cancel_requested``, which each loop checks before its next
target (``core/bulk.py`` ``stop_requested``): the write in flight always finishes and is audited, and
nothing here ever cancels a task mid-``gam``. No GAM call is made from this module.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..jobs import tray
from ..server import TEMPLATES
from ._common import app_state, error_partial

router = APIRouter(prefix="/jobs")


@router.get("/status", response_class=HTMLResponse)
async def tray_status(request: Request) -> HTMLResponse:
    """The tray's list, polled from every page (a /status path, so app.js treats it as a poll), with
    the header's "N running" count out of band."""
    return TEMPLATES.TemplateResponse(request, "_jobs_tray.html", {"tray": tray(app_state(request).jobs), "oob": True})


@router.post("/stop", response_class=HTMLResponse)
async def stop(request: Request, job: Annotated[str, Form()] = "", where: Annotated[str, Form()] = "panel",
               confirmed: Annotated[str, Form()] = "") -> HTMLResponse:
    j = app_state(request).jobs.get(job)
    if j is None:
        return error_partial(request, "That job is no longer available.")
    if not j.finished:
        if not j.can_stop:
            return error_partial(request, "This job can't be stopped: it is one call, not a loop.")
        # An offboarding stopped part-way leaves the leaver half offboarded: its Stop asks first.
        if j.confirm_stop and confirmed != "1":
            return error_partial(request, "Stop this from its Stop button, which asks you first.")
        j.cancel_requested = True
    return TEMPLATES.TemplateResponse(request, "_job_stop.html",
                                      {"job": j, "where": where if where in ("panel", "tray") else "panel"})


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    """A job's own panel, from the tray: the screen that started it renders it (its status route)."""
    return TEMPLATES.TemplateResponse(request, "job.html", {"job": app_state(request).jobs.get(job_id)})
