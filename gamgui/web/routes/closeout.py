"""Close Out (roadmap 4.5, plan D2): a section of the Offboard screen for after the transfer has finished.

Check reads what the leaver still has; Preview reads it all again, holds the picked steps under a single-use token and shows
them; Run reads the transfer and the leaver again, then runs exactly the held steps as a job (`lifecycle.run_offboard`). The
delete is the Users delete zone, shown under the check: one delete path, with its own refusals.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import closeout, guard, lifecycle
from ...core.connectors.base import RiskLevel
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.gam.models import GAMUser
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, error_partial, unread_why
from .lifecycle import _running as _offboarding

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lifecycle/closeout")

_FLOW = "closeout"
_KEY = ("closeout",)   # the confirm step posts only its token: nothing in its form names what runs
_KIND = "closeout"
_TRY_AGAIN = "Try again in a moment."


@dataclass
class _Held:
    user: str
    manager: str
    steps: List[lifecycle.OffboardStep]
    aliases: Tuple[str, ...]


def _closing(st, user: str):
    """The Close Out still running for ``user``, or None."""
    return next((j for j in st.jobs.values() if j.kind == _KIND and not j.finished and j.about.lower() == user.lower()), None)


async def _account(st, conn, typed: str) -> Tuple[Optional[GAMUser], str]:
    """``(the account's record, "")`` for ``typed``, or ``(None, why not)``. One read: what Close Out acts on."""
    typed = typed.strip()
    if not typed:
        return None, "Type the leaver's address first."
    try:
        record = await conn.get_user(typed, fields=closeout.RECORD_FIELDS)
    except GAMError as exc:
        if exc.kind == GAMErrorKind.NOT_FOUND:
            return None, f"{typed} isn't an account here."
        return None, f"Couldn't read {typed}'s account. {unread_why(exc, _TRY_AGAIN)}"
    except Exception as exc:  # noqa: BLE001 - unread: nothing is offered, whatever it was
        return None, f"Couldn't read {typed}'s account. {unread_why(exc, _TRY_AGAIN)}"
    primary = record.primary_email
    if primary.lower() != typed.lower():
        return None, f"{typed} is an alias of {primary}: close out {primary}."
    admin = st.vault.oauth_admin_email(conn.domain) if st.vault is not None else ""
    if admin and admin.lower() == primary.lower():
        return None, f"{primary} is the admin GamGUI is connected as: it can't close itself out."
    return record, ""


def _busy(st, user: str) -> str:
    if job := _offboarding(st, user):
        return f"{user}'s offboarding is still running ({job.done} of {job.total} steps): close out once it has finished."
    if _closing(st, user):
        return f"A Close Out of {user} is running: wait for it to finish, then check again."
    return ""


async def _read(st, conn, typed: str) -> Tuple[closeout.Found, str]:
    record, why = await _account(st, conn, typed)
    if record is None:
        return closeout.Found(user=typed.strip()), why
    if why := _busy(st, record.primary_email):
        return closeout.Found(user=record.primary_email), why
    return await closeout.read(conn, record, lambda exc: unread_why(exc, _TRY_AGAIN)), ""


@router.get("/check", response_class=HTMLResponse)
async def check(request: Request, user: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    found, why = await _read(st, conn, user)
    if why:
        return error_partial(request, why)
    return TEMPLATES.TemplateResponse(request, "_closeout_check.html", {"found": found, "key": closeout.seat_key,
                                                                       "archive_note": closeout.ARCHIVE_SWAPS, "archive_source": closeout.ARCHIVE_SOURCE})


async def _manager(conn, manager: str, user: str) -> str:
    """Why the aliases can't go to ``manager``, or ""."""
    try:
        primary = await conn.primary_address(manager)
    except Exception as exc:  # noqa: BLE001 - unread: nothing is held
        return f"Couldn't read {manager}'s account. {unread_why(exc, _TRY_AGAIN)}"
    if primary is None:
        return f"{manager} isn't an account here, so the aliases have nowhere to go."
    if primary.lower() != manager.lower():
        return f"{manager} is an alias of {primary}: move the aliases to {primary}."
    if primary.lower() == user.lower():
        return "The aliases are moved to someone else: name the manager."
    return ""


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to (web/previews.py)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    found, why = await _read(st, conn, str(form.get("user") or ""))
    if why or (why := found.refusal):
        return error_partial(request, why)
    if unread := [found.unread[n] for n, f in closeout.PICK_FIELDS.items() if n in found.unread and any(_texts(form, f))]:
        return error_partial(request, " ".join(unread) + " Nothing is held: check again.")
    picks, stale = closeout.pick(found, groups=_texts(form, "group"), roles=_texts(form, "role"),
                                 org_unit=str(form.get("org_unit") or ""), state=str(form.get("state") or ""),
                                 seats=_texts(form, "seat"), aliases=_texts(form, "alias"))
    if stale:
        return error_partial(request, f"{found.user} changed since the check ({', '.join(stale)} isn't as it was): check again.")
    manager = str(form.get("manager") or "").strip()
    if picks.aliases and (why := await _manager(conn, manager, found.user)):
        return error_partial(request, why)
    try:
        steps = closeout.steps(found.user, picks, manager)
    except ValueError as exc:
        return error_partial(request, f"Couldn't plan this: {exc}.")
    if not steps:
        return error_partial(request, "Nothing is picked: there is nothing to run.")
    decision = guard.evaluate(_changes(steps), typed_address=found.user)
    token = st.previews.hold(_FLOW, _KEY, _Held(found.user, manager, steps, picks.aliases), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_closeout_confirm.html", {
        "user": found.user, "steps": steps, "token": token, "typed": decision.requires_typed_confirmation,
        "failed_transfers": found.failed_transfers})


def _texts(form, name: str) -> List[str]:
    return [str(v) for v in form.getlist(name)]


def _changes(steps: List[lifecycle.OffboardStep]):
    return guard.changes([s.label for s in steps], RiskLevel.DESTRUCTIVE, "Close out")


async def _still_the_same(conn, held: _Held) -> str:
    """Why the held steps mustn't start, or "". Read now, right before the first write."""
    pending = await conn.incomplete_transfers_for(held.user)
    if refusal := closeout.transfer_refusal(held.user, pending):
        return f"{refusal} Nothing ran."
    if held.aliases:
        try:
            record = await conn.get_user(held.user, fields=closeout.RECORD_FIELDS)
        except Exception as exc:  # noqa: BLE001 - unread: nothing runs
            return f"Couldn't read {held.user} again ({unread_why(exc, _TRY_AGAIN)}), so nothing ran."
        mine = {a.lower() for a in record.aliases}
        if gone := [a for a in held.aliases if a.lower() not in mine]:
            # `update alias` deletes the alias from whoever holds it now: never from someone else.
            return f"{', '.join(gone)} isn't {held.user}'s alias any more, so nothing ran. Check again."
        if why := await _manager(conn, held.manager, held.user):
            return f"{why} Nothing ran."
    return ""


async def _run(job, st, conn, steps: List[lifecycle.OffboardStep]) -> None:
    try:
        await lifecycle.run_offboard(job, conn, steps)
    finally:
        st.invalidate_users()   # suspended, the org unit and the aliases changed: the list mustn't keep the old ones


@router.post("/apply", response_class=HTMLResponse)
async def apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), _KEY, again="click Preview again")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(_changes(held.steps), form, confirm_step=True, typed_address=held.user)
    if refusal:
        return error_partial(request, f"{refusal} Nothing ran: preview again.")
    if refusal := _busy(st, held.user) or await _still_the_same(conn, held):
        return error_partial(request, refusal)
    # No await from here to the registration, so two Runs can't both pass, and a switch during the reads can't start this.
    if st.connector is not conn:
        return error_partial(request, "The connection changed, so nothing ran.")
    if _closing(st, held.user) or _offboarding(st, held.user):
        return error_partial(request, f"A run for {held.user} started meanwhile, so nothing ran. Check again once it finishes.")
    try:
        conn.audit.record("closeout", target=held.user, ok=True, extra={"planned": [s.label for s in held.steps]},
                          domain=conn.domain)
    except OSError as exc:
        logger.warning("the audit log refused a closeout record", exc_info=exc)
        return error_partial(request, "The audit log couldn't record this, so nothing ran. Check the disk has room, then try again.")
    job = start_job(st.jobs, len(held.steps), kind=_KIND, title=f"Closing out {held.user}")
    job.about = held.user
    job.task = asyncio.create_task(_run(job, st, conn, held.steps))
    return TEMPLATES.TemplateResponse(request, "_closeout_run.html", {"job": job})


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != _KIND:
        return error_partial(request, "That run is no longer here.")
    return TEMPLATES.TemplateResponse(request, "_closeout_run.html", {"job": found})

