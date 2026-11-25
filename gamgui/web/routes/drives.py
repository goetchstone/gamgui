"""Shared drives (/drives, roadmap 4.7): find a drive, see its members, and add, re-role or remove one, each previewed.

The finder lists the cached drives (``AppState.drives``, its age shown). A drive's members are read fresh each time. A change
is planned against that read (``core/shared_drives.plan``) and held under a single-use token; its confirm step posts only
the token. Apply reads the members again and runs the change only if it still fits (``shared_drives.moved``): never a
drive left with no manager, never a member whose role moved since the preview.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core import shared_drives as sd
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import is_drive_item_id, is_one_address
from ...core.phishing import Incomplete
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, as_of, error_partial, friendly

PICK_LIMIT = 15
_FLOW = "drive_member"
_KEY = ("drive_member",)     # the confirm step posts only its token
_PANEL = "_drive_panel.html"

router = APIRouter(prefix="/drives")


def _why(exc: Exception) -> str:
    return str(exc) if isinstance(exc, Incomplete) else friendly(exc)


async def _choices(st, q: str = "", refresh: bool = False) -> dict:
    try:
        drives = await st.drives(force=refresh, stale_ok=True)
    except Exception as exc:  # noqa: BLE001 — unread: said in words, never "no drives"
        return {"drives": [], "more": False, "find_q": q, "find_error": "Couldn't list the shared drives. " + _why(exc)}
    ql = q.strip().lower()
    hits = [d for d in drives if not ql or ql in d.name.lower() or ql in d.id.lower()]
    return {"drives": hits[:PICK_LIMIT], "more": len(hits) > PICK_LIMIT, "find_q": q, "find_error": "",
            **as_of(st.drive_cache)}


async def _drive(st, drive_id: str) -> sd.SharedDrive:
    """The drive as the finder lists it, for its name; an ID the list doesn't hold keeps the ID as its name."""
    try:
        drives = await st.drives(stale_ok=True)
    except Exception:  # noqa: BLE001 — only the heading's name comes from here
        drives = []
    return next((d for d in drives if d.id == drive_id), sd.SharedDrive(drive_id, ""))


async def _panel_ctx(conn, drive: sd.SharedDrive, **extra) -> dict:
    ctx = {"drive": drive, "members": [], "unread": "", "roles": sd.ROLE_CHOICES, "kinds": sd.KIND_LABELS,
           "label": sd.label, "message": "", "error": "", "details": "", **extra}
    try:
        ctx["members"] = await conn.drive_members(drive.id)
    except Exception as exc:  # noqa: BLE001 — unknown, never "no members" (invariant 10)
        ctx["unread"] = _why(exc) or "GAM didn't answer."   # the panel names the drive before it
    return ctx


def _panel(request: Request, ctx: dict, retarget: bool = False) -> HTMLResponse:
    """The drive's panel. From the confirm step (``retarget``), it replaces the whole panel: that step's own target is the
    confirm box, so a refusal there leaves the members on screen."""
    return TEMPLATES.TemplateResponse(request, _PANEL, ctx, headers={"HX-Retarget": "#drive-panel"} if retarget else None)


@router.get("", response_class=HTMLResponse)
async def board(request: Request, id: str = "") -> HTMLResponse:   # noqa: A002 — a drive to open, as the orphan check links it
    st = app_state(request)
    connected = st.connector is not None
    return TEMPLATES.TemplateResponse(request, "drives.html", {"connected": connected, "opened": id if is_drive_item_id(id) else "",
                                                               **(await _choices(st) if connected else {})})


@router.get("/orphans", response_class=HTMLResponse)
async def orphans(request: Request, email: str = "") -> HTMLResponse:
    """The orphan check (roadmap 4.7): the shared drives ``email`` is the only active manager of, for the delete and Close
    Out. A warning, never a refusal; a read that failed says so, never "none" (invariant 10). Writes nothing."""
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    email = email.strip()
    if not is_one_address(email):
        return error_partial(request, "Pick a person first.")
    ctx = {"email": email, "found": None, "why": ""}
    try:   # the drive list and every drive's permissions read now; the directory (suspended included) fresh within its TTL
        drives, rows, users = await asyncio.gather(conn.shared_drives(), conn.drive_permissions(), st.users())
    except Exception as exc:  # noqa: BLE001 — unknown, never "manages none alone"
        ctx["why"] = _why(exc)
        return TEMPLATES.TemplateResponse(request, "_drive_orphans.html", ctx)
    # A drive's members name the primary address, so an alias typed is read as the account it belongs to.
    key = email.lower()
    account = next((u for u in users if u.primary_email.lower() == key
                    or key in (a.lower() for a in [*u.aliases, *u.non_editable_aliases])), None)
    if account is None:
        ctx["why"] = "The directory doesn't list that address, so its account can't be matched to a drive's members."
    else:
        ctx["email"] = account.primary_email
        ctx["found"] = sd.managed_alone(account.primary_email, drives, rows, users)
    return TEMPLATES.TemplateResponse(request, "_drive_orphans.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", refresh: int = 0) -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    return TEMPLATES.TemplateResponse(request, "_drive_results.html", await _choices(st, q, bool(refresh)))


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, id: str = "") -> HTMLResponse:   # noqa: A002 — the drive's ID, as the finder links it
    st = app_state(request)
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    if not is_drive_item_id(id):
        return error_partial(request, "Pick a shared drive first.")
    drive, ctx = await asyncio.gather(_drive(st, id), _panel_ctx(st.connector, sd.SharedDrive(id, "")))
    return _panel(request, {**ctx, "drive": drive})


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, drive: Annotated[str, Form()] = "", action: Annotated[str, Form()] = "",
                  email: Annotated[str, Form()] = "", kind: Annotated[str, Form()] = "user",
                  role: Annotated[str, Form()] = "", member: Annotated[str, Form()] = "") -> HTMLResponse:
    """Read the members again and hold the one change asked for, or say why not. Writes nothing."""
    st = app_state(request)
    tenant = st.tenant_key()   # before the read: what the preview is bound to (web/previews.py)
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    if not is_drive_item_id(drive):
        return error_partial(request, "Pick a shared drive first.")
    read = asyncio.gather(_drive(st, drive), st.connector.drive_members(drive))
    try:
        picked, now = await read
    except Exception as exc:  # noqa: BLE001 — unread: nothing is offered (only the members read can raise)
        picked = sd.SharedDrive(drive, "")
        return error_partial(request, f"Couldn't read {picked.name or picked.id}'s members, so nothing is changed. {_why(exc)}")
    change, why = sd.plan(now, picked, action, email=email, kind=kind, role=role, member_id=member)
    if change is None:
        return error_partial(request, why)
    token = st.previews.hold(_FLOW, _KEY, change, tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_drive_confirm.html", {"change": change, "token": token})


@router.post("/apply", response_class=HTMLResponse)
async def apply(request: Request) -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    change, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), _KEY, again="preview the change again")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(guard.changes([change.who], RiskLevel.LOW, "Change a shared drive's members"), form,
                            confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    drive = sd.SharedDrive(change.drive_id, change.drive_name)
    async with st.drive_member_lock:   # one change at a time: two clicks can't each see the other manager and both go
        try:   # read again at the click: the preview may be minutes old
            now = await conn.drive_members(change.drive_id)
        except Exception as exc:  # noqa: BLE001 — unread: never run on a guess
            return error_partial(request, f"Couldn't read {change.drive_name or change.drive_id}'s members again, so "
                                          f"nothing is changed. {_why(exc)}")
        if since := sd.moved(change, now):
            return _panel(request, await _panel_ctx(conn, drive, error=f"{since} Nothing is changed: preview it again."),
                          retarget=True)
        result = await conn.change_drive_member(change)
    if not result.ok:
        return _panel(request, await _panel_ctx(conn, drive, error=f"Couldn't {change.words}. {result.remediation}".strip(),
                                                details=result.detail), retarget=True)
    return _panel(request, await _panel_ctx(conn, drive, message=change.done), retarget=True)
