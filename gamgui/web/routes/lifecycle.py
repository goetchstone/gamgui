"""Offboarding routine: preview the steps, then run them as a guarded, progress-tracked job.

The 'timer' is the last step (a reminder on the manager's calendar) — no app-side scheduler. The
final account deletion is a separate guarded action on the user's detail page, done by IT when the
manager confirms it's safe.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import lifecycle
from ...core.gam.errors import GAMError
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/lifecycle")


def _conn(request: Request):
    return request.app.state.gamgui.connector


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


def _days(value: str) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 30


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "lifecycle.html",
        {"connected": _conn(request) is not None, "subject": lifecycle.DEFAULT_SUBJECT,
         "message": lifecycle.DEFAULT_MESSAGE, "days": 30},
    )


@router.post("/offboard/preview", response_class=HTMLResponse)
async def offboard_preview(
    request: Request, user: str = Form(...), manager: str = Form(...),
    subject: str = Form(""), message: str = Form(""), days: str = Form("30"), notify: str = Form(""),
) -> HTMLResponse:
    if _conn(request) is None:
        return _err(request, "Not connected.")
    user, manager = user.strip(), manager.strip()
    if not user or not manager:
        return _err(request, "Enter both the departing user and the manager email.")
    days_i = _days(days)
    steps = lifecycle.build_offboard_steps(user, manager, subject, message, days_i, date.today(), notify=notify.strip())
    return TEMPLATES.TemplateResponse(
        request, "_offboard_preview.html",
        {"steps": steps, "user": user, "manager": manager, "days": days_i},
    )


async def _run_offboard(job, conn, steps) -> None:
    try:
        for step in steps:
            job.current = step.label
            try:
                res = await step.action(conn)
                ok = res is None or bool(getattr(res, "ok", True))
                detail = "" if res is None else getattr(res, "detail", "")
            except Exception as exc:  # noqa: BLE001 - report every step, never abort the routine
                ok, detail = False, str(exc)
            mark = "✓ " if ok else "✗ "
            job.log.append(mark + step.label + (f" — {detail}" if (not ok and detail) else ""))
            if ok:
                job.applied += 1
            else:
                job.failed.append(step.label)
            job.done += 1
    finally:
        job.current = ""
        job.finished = True


@router.post("/offboard/run", response_class=HTMLResponse)
async def offboard_run(
    request: Request, user: str = Form(...), manager: str = Form(...),
    subject: str = Form(""), message: str = Form(""), days: str = Form("30"), notify: str = Form(""),
) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    user, manager = user.strip(), manager.strip()
    if not user or not manager:
        return _err(request, "Enter both the departing user and the manager email.")
    steps = lifecycle.build_offboard_steps(user, manager, subject, message, _days(days), date.today(), notify=notify.strip())
    job = start_job(st.jobs, len(steps))
    job.task = asyncio.create_task(_run_offboard(job, conn, steps))
    st.invalidate_users()  # password/org/etc. changed
    return TEMPLATES.TemplateResponse(request, "_offboard_run.html", {"job": job, "user": user})


@router.get("/offboard/status", response_class=HTMLResponse)
async def offboard_status(request: Request, job: str = "") -> HTMLResponse:
    j = request.app.state.gamgui.jobs.get(job)
    if j is None:
        return _err(request, "That offboarding run is no longer available.")
    return TEMPLATES.TemplateResponse(request, "_offboard_run.html", {"job": j, "user": ""})
