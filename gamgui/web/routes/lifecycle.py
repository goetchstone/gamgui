"""Offboarding routine: preview the steps, then run them as a guarded, progress-tracked job.

The 'timer' is the last step (a reminder on the manager's calendar) — no app-side scheduler. The
final account deletion is a separate guarded action on the user's detail page, done by IT when the
manager confirms it's safe.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, List

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard, lifecycle
from ...core.connectors.base import RiskLevel
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


# Run executes exactly what the preview showed: the steps built for it are held under a single-use
# token, and Run refuses a form that no longer matches the one previewed (Run once posted the live
# form, so a field edited after Preview ran against values nobody had checked).
PREVIEW_TTL = 15 * 60
_PREVIEWS_KEPT = 8


@dataclass
class _Preview:
    form: tuple                # _form_key of the form the preview was built from
    user: str                  # the directory's primary addresses the steps act on
    manager: str
    steps: List[lifecycle.OffboardStep]
    at: float = field(default_factory=time.monotonic)


def _form_key(user: str, manager: str, subject: str, message: str, days: str, notify: str) -> tuple:
    return (user.strip().lower(), manager.strip().lower(), subject, message, _days(days), notify.strip().lower())


def _hold(previews: dict, preview: _Preview) -> str:
    """Keep ``preview`` under a fresh token; drop expired ones and cap the rest (oldest first)."""
    now = time.monotonic()
    for token in [t for t, p in previews.items() if now - p.at > PREVIEW_TTL]:
        del previews[token]
    while len(previews) >= _PREVIEWS_KEPT:
        del previews[next(iter(previews))]
    token = secrets.token_urlsafe(16)
    previews[token] = preview
    return token


async def _check(st, user: str, manager: str) -> lifecycle.AddressCheck:
    """Both addresses against the cached directory — before the preview and again before the run.
    Fails closed: a directory that can't be read blocks the routine rather than skipping the check."""
    try:
        directory = await st.users()
    except Exception as exc:  # noqa: BLE001 - any read failure blocks; the message says why
        return lifecycle.AddressCheck(errors=[f"Couldn't read the directory to check the addresses — {_friendly(exc)}"])
    return lifecycle.check_addresses(directory, user, manager)


async def _resolve_name(st, email: str) -> str:
    """A user's directory display name, falling back to the email if not found."""
    try:
        for u in await st.users():
            if u.primary_email.lower() == email.lower():
                return u.full_name
    except Exception:
        pass
    return email


# Kept for build_offboard_steps' employee_name (subject uses the name alone, not name+email).
async def _employee_name(st, email: str) -> str:
    return await _resolve_name(st, email)


async def _manager_contact(st, email: str) -> str:
    """The manager as a sender-facing contact: 'Jane Smith (jane@x.com)', or just the email."""
    email = email.strip()
    if not email:
        return "[manager]"
    name = await _resolve_name(st, email)
    return f"{name} ({email})" if name and name.lower() != email.lower() else email


async def _compose_autoreply(st, user: str, manager: str, subject: str, message: str):
    """The filled auto-reply (subject, body) exactly as senders will see it.

    Resolves both names from the directory — the departing user (subject uses the name) and the
    manager (shown as 'Name (email)' so senders can actually reach them). Falls back to readable
    placeholders for fields not entered yet so the live preview always reads sensibly.
    """
    user, manager = user.strip(), manager.strip()
    employee = (await _resolve_name(st, user)) if user else ""
    employee = employee or user or "[departing user]"
    contact = await _manager_contact(st, manager)
    subject = lifecycle.fill_autoreply(subject or lifecycle.DEFAULT_SUBJECT, employee, contact)
    message = lifecycle.fill_autoreply(message or lifecycle.DEFAULT_MESSAGE, employee, contact)
    return subject, message


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "lifecycle.html",
        {"connected": _conn(request) is not None, "subject": lifecycle.DEFAULT_SUBJECT,
         "message": lifecycle.DEFAULT_MESSAGE, "days": 30},
    )


@router.post("/offboard/preview", response_class=HTMLResponse)
async def offboard_preview(
    request: Request,
    user: Annotated[str, Form()], manager: Annotated[str, Form()],
    subject: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "",
    days: Annotated[str, Form()] = "30", notify: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _err(request, "Not connected.")
    form = _form_key(user, manager, subject, message, days, notify)
    user, manager = user.strip(), manager.strip()
    if not user or not manager:
        return _err(request, "Enter both the departing user and the manager email.")
    check = await _check(st, user, manager)
    if check.errors:
        return _err(request, " ".join(check.errors))
    user, manager = check.user.primary_email, check.manager.primary_email
    # An emptied field runs the default text — the auto-reply block below shows the default too.
    subject, message = subject or lifecycle.DEFAULT_SUBJECT, message or lifecycle.DEFAULT_MESSAGE
    days_i = _days(days)
    steps = lifecycle.build_offboard_steps(
        user, manager, subject, message, days_i, date.today(),
        notify=notify.strip(), employee_name=await _employee_name(st, user),
        manager_contact=await _manager_contact(st, manager))
    token = _hold(st.offboard_previews, _Preview(form, user, manager, steps))
    ar_subject, ar_message = await _compose_autoreply(st, user, manager, subject, message)
    return TEMPLATES.TemplateResponse(
        request, "_offboard_preview.html",
        {"steps": steps, "user": user, "manager": manager, "days": days_i, "warnings": check.warnings,
         "token": token, "ar_subject": ar_subject, "ar_message": ar_message},
    )


@router.post("/offboard/autoreply", response_class=HTMLResponse)
async def offboard_autoreply(
    request: Request, user: Annotated[str, Form()] = "", manager: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Live preview of the generated auto-reply as the user/manager/text are entered."""
    st = request.app.state.gamgui
    if st.connector is None:
        return HTMLResponse("")
    ar_subject, ar_message = await _compose_autoreply(st, user, manager, subject, message)
    return TEMPLATES.TemplateResponse(
        request, "_offboard_autoreply.html", {"subject": ar_subject, "message": ar_message})


async def _run_offboard(job, conn, steps) -> None:
    """Run the steps in order. A step whose ``requires`` did not all succeed is not run (logged "–"),
    so a failed reset or delegate stops the routine instead of half-offboarding the account."""
    succeeded = set()
    labels = {s.key: s.label for s in steps}
    try:
        for step in steps:
            unmet = [k for k in step.requires if k not in succeeded]
            if unmet:
                job.log.append(f"– {step.label} — not run: “{labels.get(unmet[0], unmet[0])}” didn't succeed")
                job.skipped.append(step.label)
                job.done += 1
                continue
            job.current = step.label
            try:
                res = await step.action(conn)
                ok = res is None or bool(getattr(res, "ok", True))
                detail = "" if res is None else getattr(res, "detail", "")
            except Exception as exc:  # noqa: BLE001 - a raising step is a failed step, reported like one
                ok, detail = False, str(exc)
            mark = "✓ " if ok else "✗ "
            job.log.append(mark + step.label + (f" — {detail}" if (not ok and detail) else ""))
            if ok:
                job.applied += 1
                succeeded.add(step.key)
            else:
                job.fail(step.label)
            job.done += 1
    finally:
        job.current = ""
        job.finished = True


@router.post("/offboard/run", response_class=HTMLResponse)
async def offboard_run(
    request: Request,
    user: Annotated[str, Form()], manager: Annotated[str, Form()],
    subject: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "",
    days: Annotated[str, Form()] = "30", notify: Annotated[str, Form()] = "",
    preview: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    # Destructive for the leaver (locks sign-in, strips calendar access domain-wide): confirmed=1.
    refusal = guard.enforce(guard.changes([user.strip()], RiskLevel.DESTRUCTIVE, "Offboard"), await request.form())
    if refusal:
        return _err(request, refusal)
    held = st.offboard_previews.pop(preview, None)   # single use: a second Run needs a new preview
    if held is None or time.monotonic() - held.at > PREVIEW_TTL:
        return _err(request, "That preview has expired or was already run — click Preview steps again.")
    if held.form != _form_key(user, manager, subject, message, days, notify):
        return _err(request, "The form changed after the preview — click Preview steps again, so what runs "
                             "is what you checked.")
    check = await _check(st, held.user, held.manager)   # the directory may have changed since
    if check.errors:
        return _err(request, " ".join(check.errors))
    user, steps = held.user, held.steps
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
