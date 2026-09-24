"""Offboarding routine: preview the steps, then run them as a guarded, progress-tracked job.

The 'timer' is the last step (a reminder on the manager's calendar) — no app-side scheduler. The
final account deletion is a separate guarded action on the user's detail page, done by IT when the
manager confirms it's safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Annotated, FrozenSet, List

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
# token (web/previews.py), and Run refuses a form that no longer matches the one previewed (Run once
# posted the live form, so a field edited after Preview ran against values nobody had checked).
_FLOW = "offboard"


@dataclass
class _Preview:
    user: str                  # the directory's primary addresses the steps act on
    manager: str
    steps: List[lifecycle.OffboardStep]   # the steps to run — the previewed ones not ticked done
    done: FrozenSet[str]                  # keys ticked "already done": not run, and count as succeeded


def _done(form) -> FrozenSet[str]:
    """The steps the operator ticked as already done (a re-run after a failure)."""
    return frozenset(form.getlist("done")).intersection(lifecycle.REQUIRES)


def _form_key(user: str, manager: str, subject: str, message: str, days: str, notify: str,
              done: FrozenSet[str]) -> tuple:
    return (user.strip().lower(), manager.strip().lower(), subject, message, _days(days), notify.strip().lower(),
            tuple(sorted(done)))


def _running(st, user: str):
    """The offboarding still running for ``user``, or None. One per leaver at a time: a second one
    would reset the password again and repeat the transfer, the hour-long sweep and the reminder."""
    job = st.jobs.get(st.offboard_jobs.get(user.lower(), ""))
    return job if job is not None and not job.finished else None


def _already_running(request: Request, job, user: str) -> HTMLResponse:
    """The refusal, with the running job's own progress panel — the way back to it after a reload."""
    return TEMPLATES.TemplateResponse(request, "_offboard_running.html",
                                      {"job": job, "user": user, "revoke_label": lifecycle.STEP_NAMES["revoke"]})


async def _check(st, user: str, manager: str) -> lifecycle.AddressCheck:
    """Both addresses against the cached directory — before the preview and again before the run.
    Fails closed: a directory that can't be read blocks the routine rather than skipping the check."""
    try:
        directory = await st.users()
    except Exception as exc:  # noqa: BLE001 - any read failure blocks; the message says why
        return lifecycle.AddressCheck(errors=[f"Couldn't read the directory to check the addresses — {_friendly(exc)}"])
    admin = st.vault.oauth_admin_email(st.connector.domain) if st.vault is not None and st.connector else ""
    return lifecycle.check_addresses(directory, user, manager, connected_admin=admin)


async def _delegate_warning(conn, user: str, manager: str) -> str:
    """The preview's warning about the delegate step, or "". Re-adding an existing delegate fails in
    GAM; and a failed read of ``user``'s delegates means the same Gmail access is broken for the
    delegate step — which runs after the irreversible reset, and stops the routine when it fails. A
    warning, not a block: the read may have failed for a transient reason (it was swallowed once, and
    the preview looked clean)."""
    try:
        delegates = {d.lower() for d in await conn.list_delegates(user)}
    except Exception as exc:  # noqa: BLE001 - any failure is reported, whatever it was
        reason = exc.message if isinstance(exc, GAMError) else _friendly(exc)
        return (f"Couldn't read {user}'s mail delegates ({reason}). The delegate step uses the same Gmail "
                f"access, so it will likely fail too — after the password has been reset, and a failed "
                f"delegate stops the routine. Fix the cause and preview again.")
    if manager.lower() in delegates:
        return (f"{manager} already has delegate access to {user}'s mailbox. GAM fails a second add, and a "
                f"failed delegate stops the routine — tick “Set delegate” as already done.")
    return ""


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
         "message": lifecycle.DEFAULT_MESSAGE, "days": 30, "step_names": lifecycle.STEP_NAMES},
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
    done = _done(await request.form())
    form = _form_key(user, manager, subject, message, days, notify, done)
    user, manager = user.strip(), manager.strip()
    if not user or not manager:
        return _err(request, "Enter both the departing user and the manager email.")
    if done == frozenset(lifecycle.REQUIRES):
        return _err(request, "Every step is ticked as already done — there is nothing to run.")
    check = await _check(st, user, manager)
    if check.errors:
        return _err(request, " ".join(check.errors))
    user, manager = check.user.primary_email, check.manager.primary_email
    if running := _running(st, user):
        return _already_running(request, running, user)
    if "delegate" not in done and (warning := await _delegate_warning(st.connector, user, manager)):
        check.warnings.append(warning)
    # An emptied field runs the default text — the auto-reply block below shows the default too.
    subject, message = subject or lifecycle.DEFAULT_SUBJECT, message or lifecycle.DEFAULT_MESSAGE
    days_i = _days(days)
    steps = lifecycle.build_offboard_steps(
        user, manager, subject, message, days_i, date.today(),
        notify=notify.strip(), employee_name=await _employee_name(st, user),
        manager_contact=await _manager_contact(st, manager))
    to_run = [s for s in steps if s.key not in done]
    token = st.previews.hold(_FLOW, form, _Preview(user, manager, to_run, done))
    ar_subject, ar_message = await _compose_autoreply(st, user, manager, subject, message)
    return TEMPLATES.TemplateResponse(
        request, "_offboard_preview.html",
        {"steps": steps, "done": done, "run_count": len(to_run), "user": user, "manager": manager,
         "days": days_i, "warnings": check.warnings, "token": token,
         "ar_subject": ar_subject, "ar_message": ar_message},
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


async def _run_offboard(job, conn, steps, done: FrozenSet[str] = frozenset()) -> None:
    """Run the steps in order. A step whose ``requires`` did not all succeed is not run (logged "–"),
    so a failed reset or delegate stops the routine instead of half-offboarding the account. ``done``
    are the steps ticked as already done by an earlier run: they satisfy ``requires``."""
    succeeded = set(done)
    labels = {s.key: s.label for s in steps}
    handled = 0   # steps fully accounted for (run or deliberately skipped)
    try:
        for step in steps:
            unmet = [k for k in step.requires if k not in succeeded]
            if unmet:
                job.log.append(f"– {step.label} — not run: “{labels.get(unmet[0], unmet[0])}” didn't succeed")
                job.skipped.append(step.label)
                job.done += 1
                handled += 1
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
            handled += 1
    finally:
        # Cut off (the app quit mid-run): every step not accounted for is "not run", so the panel can't
        # call a half-done routine complete — or tell the manager about a reminder that was never added.
        for i, step in enumerate(steps[handled:]):
            cut = i == 0 and job.current == step.label
            job.log.append(f"– {step.label} — " + ("interrupted before it finished" if cut else "not run: interrupted"))
            job.skipped.append(step.label)
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
    # Destructive for the leaver (resets the password, revokes access, strips calendar access
    # domain-wide): confirmed=1.
    form = await request.form()
    refusal = guard.enforce(guard.changes([user.strip()], RiskLevel.DESTRUCTIVE, "Offboard"), form)
    if refusal:
        return _err(request, refusal)
    # Single use: a second Run, or a Run after the form was edited, needs a new preview.
    held, refusal = st.previews.take(_FLOW, preview, _form_key(user, manager, subject, message, days, notify,
                                                               _done(form)), again="click Preview steps again")
    if refusal:
        return _err(request, refusal)
    check = await _check(st, held.user, held.manager)   # the directory may have changed since
    if check.errors:
        return _err(request, " ".join(check.errors))
    user, steps = held.user, held.steps
    # No await from here to the registration, so two Runs can't both pass the check.
    if running := _running(st, user):
        return _already_running(request, running, user)
    job = start_job(st.jobs, len(steps))
    st.offboard_jobs = {u: j for u, j in st.offboard_jobs.items() if _running(st, u)}   # drop finished ones
    st.offboard_jobs[user.lower()] = job.id
    job.task = asyncio.create_task(_run_offboard(job, conn, steps, done=held.done))
    st.invalidate_users()  # password/org/etc. changed
    return _panel(request, job, user)


@router.get("/offboard/status", response_class=HTMLResponse)
async def offboard_status(request: Request, job: str = "") -> HTMLResponse:
    j = request.app.state.gamgui.jobs.get(job)
    if j is None:
        return _err(request, "That offboarding run is no longer available.")
    return _panel(request, j, "")


def _panel(request: Request, job, user: str) -> HTMLResponse:
    """The run's progress panel (polls itself), then its outcome."""
    return TEMPLATES.TemplateResponse(request, "_offboard_run.html",
                                      {"job": job, "user": user, "revoke_label": lifecycle.STEP_NAMES["revoke"]})
