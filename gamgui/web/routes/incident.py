"""Incident: check a compromised account, and secure it from one preview.

Check Account is reads only, as a background job since listing thousands of drafts can take minutes. Secure runs what its preview held,
bound to that check, with the person's address typed. Before the first write the account is read fresh: the same one, not the admin
GamGUI signs in as, and each scheduled email held still scheduled. Rules: ``core/incident``. Runbook: docs/domains/incident.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, FrozenSet, List, Tuple

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard, lifecycle
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import GAMCommands
from ...core.incident import reads, secure
from ...core.incident.check import DRAFT_CAP, SCHEDULED_CAP, Account, Check, Draft, drafts_matching
from ...core.onboarding import looks_like_email
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from .incident_kept import reopen_remembered
from ._common import open_incident_record, NOT_CONNECTED, error_partial

router = APIRouter(prefix="/incident")

_FLOW = "incident_secure"
_SHOWN = 50     # drafts and scheduled emails drawn: the step still names how many, and acts on them all
_CHECK = "_incident_check.html"
_RUN = "_incident_run.html"


@dataclass(frozen=True)
class _Held:
    user: str
    account: Account
    steps: List[lifecycle.OffboardStep]
    scheduled: Tuple[str, ...] = ()    # read again at the run: one Gmail sent since may now be its Sent copy, under the same ID


def _on_incident(request: Request) -> bool:
    """The Incident screen asked, not the Jobs page, which has no place for a plan."""
    return "/incident" in request.headers.get("HX-Current-URL", "")


def _context(st, job=None, on_screen: bool = True) -> dict:
    return {"job": job, "check": st.incident_check, "shown": _SHOWN, "pick": _pick_for(st), "switchable": secure.SWITCHABLE, "on_by_default": secure.ON_BY_DEFAULT,
            "notes": secure.NOTES, "names": lifecycle.STEP_NAMES, "on_screen": on_screen, "draft_cap": DRAFT_CAP, "scheduled_cap": SCHEDULED_CAP}


def _running(st, job_id: str):
    job = st.jobs.get(job_id)
    return job if job is not None and not job.finished else None


def _pick_for(st) -> str:
    """The one fix Do It Here asked for, for the check on screen only: a check of anyone else leaves it unpicked."""
    held, pick = st.incident_check, st.incident_pick
    return pick[1] if held is not None and pick and pick[0] in (held.asked.lower(), held.account.primary_email.lower()) else ""


@router.get("", response_class=HTMLResponse)
async def page(request: Request, person: str = "", fix: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    reopen_remembered(st)
    person = person.strip()
    held = st.incident_check
    # From a person's page: their check, read now unless it's the one on screen.
    start = bool(person) and not (held is not None and person.lower() in (held.asked.lower(), held.account.primary_email.lower()))
    # Do It Here: this one fix picked and the lock left off, the lock being a to-do item of its own. Anything else asked for drops it.
    pick = fix if person and fix in secure.SWITCHABLE and not _running(st, st.incident_run_job) else ""
    st.incident_pick = (person.lower(), pick) if pick and not start else None
    return TEMPLATES.TemplateResponse(request, "incident.html", {
        "connected": st.connector is not None, "person": person, "start": start, "check_pick": pick if start else "", "here": "/incident", "open_incident": open_incident_record(st),
        "has_pull": (st.incident_pull is not None and st.incident_pull.ended) or _running(st, st.incident_open_job) is not None,
        **_context(st, _running(st, st.incident_check_job))})


async def _read(job, st, conn, person: str) -> None:
    def note() -> None:
        job.done += 1

    try:
        check: Check = await reads.read_check(conn, person, note)
        if st.connector is not conn:
            job.error = "Not kept: the connection changed during the check. Check again."
            return
        check.done = st.incident_done.get(check.account.primary_email.lower(), frozenset())    # only that person's run
        st.incident_check = check
    except Exception as exc:  # noqa: BLE001 - the account couldn't be read: said, whatever it was
        job.error = f"Couldn't read {person}: {reads.in_words(exc)}"
    finally:
        job.finish()


@router.post("/check", response_class=HTMLResponse)
async def check_account(request: Request, person: Annotated[str, Form()] = "", after_run: Annotated[str, Form()] = "", pick: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    # A check by hand, another person's, or a run going on drops it: the screen's own form posts no pick.
    st.incident_pick = (person.strip().lower(), pick) if pick in secure.SWITCHABLE and person.strip() and not _running(st, st.incident_run_job) else None
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    person = person.strip()
    if not looks_like_email(person):
        return error_partial(request, "Enter the address of the account that was taken over.")
    if _running(st, st.incident_run_job):
        return error_partial(request, "A run is going on: wait for it to finish, then check again.")
    if existing := _running(st, st.incident_check_job):
        return TEMPLATES.TemplateResponse(request, _CHECK, _context(st, existing))
    st.incident_check = None    # a plan made before is dropped: its picks were from the last check
    job = start_job(st.jobs, reads.READ_COUNT, kind="incident_check", title=f"Check {person}")
    st.incident_check_job = job.id
    job.task = asyncio.create_task(_read(job, st, conn, person))
    # A new check drops the plan on screen; the check a run's end asks for keeps its result there.
    return TEMPLATES.TemplateResponse(request, _CHECK, {**_context(st, job), "clear_plan": not after_run})


@router.get("/check/status", response_class=HTMLResponse)
async def check_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, _CHECK, _context(st, held if held is not None and held.kind == "incident_check" else None,
                                                                _on_incident(request)))


def _changes(steps: List[lifecycle.OffboardStep]) -> list:
    # Ten steps with a destructive one is a destructive bulk change: the steps count as targets, so it needs the word typed too.
    return guard.changes([s.label for s in steps], RiskLevel.DESTRUCTIVE, "Secure an account")


def _drafts_picked(check: Check, choice: str, text: str) -> List[Draft]:
    listed = check.drafts.value or []
    return listed if choice == "all" else drafts_matching(listed, text) if choice == "subject" else []


@router.post("/secure/preview", response_class=HTMLResponse)
async def secure_preview(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    check = st.incident_check
    if check is None or str(form.get("run") or "") != check.run:
        return error_partial(request, "Check the account first: the steps act on what it read.")
    if check.cut_off:
        return error_partial(request, check.cut_off)
    if _running(st, st.incident_run_job):
        return error_partial(request, "A run is going on: wait for it to finish, then preview again.")
    user = check.account.primary_email
    # What the check listed, whatever was posted: a pick names something it read, or nothing.
    picked_filters = {str(v) for v in form.getlist("filter")}
    picked_delegates = {str(v) for v in form.getlist("delegate")}
    picked_send_as = {str(v) for v in form.getlist("send_as")}
    choice, text = str(form.get("drafts") or "none"), str(form.get("drafts_subject") or "")
    drafts = _drafts_picked(check, choice, text)
    if choice == "subject" and not drafts:
        return error_partial(request, f"No draft's subject contains “{text.strip()}”: change the text, or keep the drafts.")
    scheduled = check.scheduled_sends if form.get("delete_scheduled") else []
    try:
        steps = secure.steps(
            user, frozenset(str(v) for v in form.getlist("switch")) & frozenset(secure.SWITCHABLE),
            filters=[f for f in check.filters.value or [] if f.id in picked_filters],
            delegates=[d for d in check.delegates.value or [] if d in picked_delegates],
            send_as=[s.address for s in check.other_send_as if s.address in picked_send_as],
            auto_reply_off=bool(form.get("auto_reply")) and bool(check.auto_reply.value and check.auto_reply.value.enabled),
            scheduled=scheduled, drafts=drafts, recovery=(check.account.recovery_email, check.account.recovery_phone))
    except ValueError as exc:
        return error_partial(request, f"Couldn't plan this: {exc}. Remove it in the Admin console or Gmail's settings instead.")
    if not steps:
        return error_partial(request, "No step is switched on and nothing is ticked to undo: there is nothing to run.")
    decision = guard.evaluate(_changes(steps), typed_address=user)
    st.previews.drop(_FLOW, check.run)
    token = st.previews.hold(_FLOW, check.run, _Held(user, check.account, steps, tuple(s.id for s in scheduled)), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_incident_confirm.html", {
        "user": user, "steps": steps, "token": token, "typed": decision.requires_typed_confirmation,
        "stops": secure.what_stops_what(any(s.key == "password" for s in steps))})


async def _still_the_same(st, conn, held: _Held) -> str:
    """Why this run mustn't start, or "". Read now, right before the first write."""
    try:
        now = await conn.account_to_check(held.user)
    except Exception as exc:  # noqa: BLE001 - unread: nothing runs, whatever it was
        return f"Couldn't read {held.user} again ({reads.in_words(exc)}), so nothing ran."
    if not now.same_as(held.account):
        return f"{held.user} changed after the check, so nothing ran. Check the account again."
    if refusal := await conn.cut_off_refusal([GAMCommands.deprovision_user(held.user)]):
        return refusal
    if held.scheduled:
        try:
            still = {s.id for s in await conn.scheduled_sends_of(held.user) if s.is_scheduled}
        except Exception as exc:  # noqa: BLE001 - unread: nothing runs, whatever it was
            return f"Couldn't read {held.user}'s scheduled emails again ({reads.in_words(exc)}), so nothing ran."
        if not set(held.scheduled) <= still:
            return (f"A scheduled email of {held.user}'s was sent or cancelled after the check, so nothing ran: one sent may now be its Sent "
                    f"copy under the same ID. Check the account again: a new preview holds what's scheduled now.")
    return ""


async def _secure(job, st, conn, held: _Held) -> None:
    """The run. What finished is kept for that person alone: another's plan mustn't skip their sign-out."""
    try:
        await lifecycle.run_offboard(job, conn, held.steps)
    finally:
        missed = set(job.failed_items) | set(job.skipped)
        finished: FrozenSet[str] = frozenset(s.key for s in held.steps if s.key in secure.SWITCHABLE and s.label not in missed)
        if st.connector is conn:
            who = held.user.lower()
            st.incident_done[who] = st.incident_done.get(who, frozenset()) | finished    # an earlier run's reset stays done
            st.invalidate_users()


@router.post("/secure/apply", response_class=HTMLResponse)
async def secure_apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    check = st.incident_check
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), check.run if check else "",
                                     again="check the account again, then preview", what="check")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(_changes(held.steps), form, confirm_step=True, typed_address=held.user)
    if refusal:
        return error_partial(request, f"{refusal} Nothing ran: preview again.")
    if refusal := await _still_the_same(st, conn, held):
        st.incident_check = None      # previewing this check again would hold the same picks
        return error_partial(request, refusal)
    # No await from here to the registration, so two Runs can't both pass, and nothing changed under the reads above.
    if st.connector is not conn:
        return error_partial(request, "The connection changed, so nothing ran.")
    if _running(st, st.incident_run_job) or _running(st, st.incident_check_job):
        return error_partial(request, "A check or a run is going on: wait for it to finish, then preview again. Nothing ran.")
    try:
        conn.audit.record("incident_secure", target=held.user, ok=True, extra={"planned": [s.key for s in held.steps]})
    except OSError as exc:
        return error_partial(request, f"The audit log couldn't record this ({exc.strerror or type(exc).__name__}), so nothing ran.")
    st.incident_check = None          # its lists are from before the run: it's read again when the run ends
    job = start_job(st.jobs, len(held.steps), kind="incident_secure", title=f"Securing {held.user}")
    job.about = held.user
    st.incident_run_job = job.id
    job.task = asyncio.create_task(_secure(job, st, conn, held))
    return TEMPLATES.TemplateResponse(request, _RUN, _run_context(request, st, job))


def _run_context(request: Request, st, job) -> dict:
    revoke = lifecycle.STEP_NAMES["revoke"]
    return {"job": job, "user": job.about, "revoke_label": revoke, "recheck": _on_incident(request)}


@router.get("/secure/status", response_class=HTMLResponse)
async def secure_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    if held is None or held.kind != "incident_secure":
        return error_partial(request, "That run is no longer available.")
    return TEMPLATES.TemplateResponse(request, _RUN, _run_context(request, st, held))
