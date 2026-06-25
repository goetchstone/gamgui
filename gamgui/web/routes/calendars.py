"""Calendars: find a calendar (a room/resource or a user's), see who has access, search its events,
and delete a stray event.

Reads are bounded (one calendar; event search requires a query or date window + a result cap).
Deletion is destructive on real calendars, so it's preview -> confirm -> audited, targeting one
specific event id (a recurring master removes the whole series).
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.gam.errors import GAMError
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/calendars")

EVENT_CAP = 200
_NOT_CONNECTED = "Not connected."
_CALENDAR_LIST_TEMPLATE = "_calendar_list.html"
_CALENDAR_INDEX_JOB_TEMPLATE = "_calendar_index_job.html"
# Strict allowlist: only true secondary calendars can be deleted. Primary calendars are a user's
# email; holiday/system use @group.v.calendar.google.com or a `#…@` id; rooms use
# @resource.calendar.google.com; imports use @import.calendar.google.com — none end in this suffix.
SECONDARY_SUFFIX = "@group.calendar.google.com"


def _is_secondary(cal: str) -> bool:
    c = (cal or "").strip().lower()
    return bool(c) and "#" not in c and c.endswith(SECONDARY_SUFFIX)


def _owner_candidates(acls, cal: str) -> list:
    """All ACL owners scoped to a real user mailbox (deduped; skip the calendar's self-owner id)."""
    cid = (cal or "").strip().lower()
    out, seen = [], set()
    for a in acls:
        who = (a.scope_value or "").strip()
        if a.role == "owner" and a.scope_type == "user" and "@" in who and who.lower() != cid:
            if who.lower() not in seen:
                seen.add(who.lower())
                out.append(who)
    return out


async def _active_emails(request: Request) -> set:
    """Lowercased emails of current, non-suspended users (from the shared cache)."""
    try:
        return {u.primary_email.lower() for u in await request.app.state.gamgui.users()
                if u.primary_email and not getattr(u, "suspended", False)}
    except Exception:
        return set()


def _pick_owner(candidates: list, active: set) -> str:
    """First owner candidate that is an active (non-suspended, still-existing) account."""
    return next((c for c in candidates if c.lower() in active), "")


async def _resolve_delete_owner(request: Request, conn, cal: str):
    """(owner_to_impersonate, refusal_message). owner is '' when the calendar can't be deleted here.

    Re-reads ACLs fresh and re-validates the id — never trusts the client. The owner must currently
    be an OWNER and an ACTIVE user (an ex-employee owner is typically suspended/deleted; GAM can't
    act as them, so we refuse with guidance instead of a confusing impersonation failure)."""
    if not _is_secondary(cal):
        return "", ("Only secondary calendars can be deleted here. Primary, holiday and room "
                    "calendars can't be removed this way.")
    acls = await conn.list_calendar_acls_for(cal)
    cands = _owner_candidates(acls, cal)
    if not cands:
        return "", ("Couldn't find an owner account to act as — the calendar's owner may have been "
                    "deleted. Transfer ownership to an active user first.")
    owner = _pick_owner(cands, await _active_emails(request))
    if not owner:
        return "", (f"Owner(s) {', '.join(cands)} are suspended or no longer exist — reassign "
                    "ownership to an active user, then delete.")
    return owner, ""


def _conn(request: Request):
    return request.app.state.gamgui.connector


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


def _humanize_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 172800:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} d ago"


def _index_ready(request: Request) -> bool:
    """The index has rows AND they belong to the currently-connected domain.

    The index is one file; if the admin switched tenants, the stored domain won't match the active
    one — treat that as "needs rebuild" so we never serve (or delete against) another tenant's data.
    """
    st = request.app.state.gamgui
    idx = st.calendar_index
    if idx is None:
        return False
    status = idx.status()
    return status.count > 0 and status.domain == st.audit_domain


def _index_ctx(request: Request) -> dict:
    """Status of the persistent calendar index for the page/status partials."""
    idx = request.app.state.gamgui.calendar_index
    ready = _index_ready(request)
    if idx is None or not ready:
        return {"count": 0, "age": "", "ready": False}
    status = idx.status()
    age = _humanize_age(time.time() - status.updated_at) if status.updated_at else ""
    return {"count": status.count, "age": age, "ready": True}


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    connected = _conn(request) is not None
    ctx = {"connected": connected}
    if connected:
        ctx["index"] = _index_ctx(request)
    return TEMPLATES.TemplateResponse(request, "calendars.html", ctx)


@router.get("/resources", response_class=HTMLResponse)
async def resources(request: Request, q: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    try:
        rs = await conn.list_resources(q.strip())
    except Exception as exc:
        return _err(request, _friendly(exc))
    items = [
        {"cal_id": r.email, "label": r.name or r.email, "meta": r.resource_type or "resource"}
        for r in rs if r.email
    ]
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    """Instant name search, served entirely from the local index (no live domain scan)."""
    if _conn(request) is None:
        return _err(request, _NOT_CONNECTED)
    idx = request.app.state.gamgui.calendar_index
    if idx is None or not _index_ready(request):  # missing, empty, or built for another domain
        return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {
            "items": [],
            "notes": ["No calendar index yet — build it once (the button above) to search every "
                      "shared calendar by name. After that, searches are instant."],
        })
    items = []
    for c in idx.search(q.strip()):
        non_room_meta = f"owned by {c.owner}" if c.owner else "shared"
        meta = "room" if c.kind == "room" else non_room_meta
        if c.kind != "room" and c.subscribers:
            meta += f" · {c.subscribers} subscriber{'' if c.subscribers == 1 else 's'}"
        items.append({"cal_id": c.id, "label": c.summary or c.id, "meta": meta})
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items, "notes": []})


async def _build_index(job, conn, idx, domain: str) -> None:
    """Background: scan the whole domain once and atomically replace the index."""
    try:
        job.current = "Scanning every user's calendars…"
        cals = await conn.scan_all_calendars()
        await asyncio.to_thread(idx.replace_all, domain, cals)
        job.applied = len(cals)
        job.log.append(f"Indexed {len(cals)} calendars.")
    except Exception as exc:  # noqa: BLE001 — surface any failure in the status partial
        job.error = str(exc)
    finally:
        job.current = ""
        job.finished = True


@router.post("/index/rebuild", response_class=HTMLResponse)
async def index_rebuild(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _err(request, _NOT_CONNECTED)
    if st.calendar_index is None:
        return _err(request, "Calendar index is unavailable.")
    existing = st.jobs.get(st.cal_index_job_id)
    if existing is not None and not existing.finished:  # don't start a second multi-minute scan
        return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": existing})
    job = start_job(st.jobs, 0)
    st.cal_index_job_id = job.id
    job.task = asyncio.create_task(_build_index(job, st.connector, st.calendar_index, st.audit_domain))
    return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": job})


@router.get("/index/status", response_class=HTMLResponse)
async def index_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job) if job else None
    if j is not None:
        # Job partial keeps polling while running, then shows a final result (no more polling).
        return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": j})
    # Unknown/pruned job -> the resting status strip (count + age).
    return TEMPLATES.TemplateResponse(request, "_calendar_index_status.html", {"index": _index_ctx(request)})


@router.get("/user", response_class=HTMLResponse)
async def user_calendars(request: Request, email: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    email = email.strip()
    if not email:
        return _err(request, "Enter a user's email.")
    try:
        cals = await conn.list_user_calendars(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    items = [
        {"cal_id": c.id, "label": c.summary or c.id,
         "meta": (("primary · " if c.primary else "") + (c.access_role or ""))}
        for c in cals if c.id
    ]
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items})


@router.get("/detail", response_class=HTMLResponse)
async def detail(request: Request, cal: str, label: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    try:
        acls = await conn.list_calendar_acls_for(cal)
    except Exception as exc:
        return _err(request, _friendly(exc))
    is_secondary = _is_secondary(cal)
    owner, delete_note = "", ""
    if is_secondary:
        cands = _owner_candidates(acls, cal)
        if not cands:
            delete_note = ("This calendar can't be deleted here: no owner account was found to act "
                           "as (its original owner may have been deleted).")
        else:
            owner = _pick_owner(cands, await _active_emails(request))
            if not owner:
                delete_note = (f"This calendar can't be deleted here: owner(s) {', '.join(cands)} are "
                               "suspended or no longer exist. Reassign ownership to an active user first.")
    return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", {
        "cal": cal, "label": label.strip(), "acls": acls, "acl_count": len(acls),
        "is_secondary": is_secondary, "owner": owner, "deletable": bool(owner),
        "delete_note": delete_note,
    })


def _delete_view(request: Request, *, cal: str, label: str, owner: str, acl_count: int = 0,
                 deleted: bool = False, error: str = "") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_calendar_delete.html", {
        "cal": cal, "label": label, "owner": owner, "acl_count": acl_count,
        "deleted": deleted, "error": error,
    })


@router.post("/delete/preview", response_class=HTMLResponse)
async def delete_preview(request: Request, cal: Annotated[str, Form()],
                         label: Annotated[str, Form()] = "",
                         acl_count: Annotated[int, Form()] = 0) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    cal = cal.strip()
    try:
        owner, refusal = await _resolve_delete_owner(request, conn, cal)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if not owner:
        return _err(request, refusal)
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, acl_count=acl_count)


@router.post("/delete", response_class=HTMLResponse)
async def delete_cal(request: Request, cal: Annotated[str, Form()],
                     confirm: Annotated[str, Form()] = "",
                     label: Annotated[str, Form()] = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    cal = cal.strip()
    # Re-resolve owner + re-validate the id server-side — never act on a client-supplied identity.
    try:
        owner, refusal = await _resolve_delete_owner(request, conn, cal)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if not owner:
        return _err(request, refusal)
    if confirm.strip() != "DELETE":  # exact-case gate
        return _delete_view(request, cal=cal, label=label.strip(), owner=owner,
                            error="Type DELETE (in capitals) to confirm.")
    result = await conn.delete_calendar(owner, cal)
    if not result.ok:
        return _delete_view(request, cal=cal, label=label.strip(), owner=owner,
                            error=f"Couldn't delete the calendar: {result.detail}")
    idx = request.app.state.gamgui.calendar_index
    if idx is not None:
        # Off the event loop — a sync SQLite write could block briefly if a rebuild is mid-write.
        await asyncio.to_thread(idx.remove, cal)  # drop it from search immediately (no full rebuild)
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, deleted=True)


@router.get("/events", response_class=HTMLResponse)
async def events(request: Request, cal: str, q: str = "", after: str = "", before: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    q, after, before = q.strip(), after.strip(), before.strip()
    if not (q or after or before):  # never an unbounded all-events scan
        return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": [], "need_filter": True})
    try:
        evs = await conn.search_events(cal, query=q, after=after, before=before, cap=EVENT_CAP)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": evs, "capped": len(evs) >= EVENT_CAP})


@router.post("/event/preview", response_class=HTMLResponse)
async def event_preview(request: Request, cal: Annotated[str, Form()], event_id: Annotated[str, Form()]) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    try:
        ev = await conn.get_event(cal, event_id)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if ev is None:
        return _err(request, "That event no longer exists.")
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": ev, "deleted": False})


@router.post("/event/delete", response_class=HTMLResponse)
async def event_delete(request: Request, cal: Annotated[str, Form()], event_id: Annotated[str, Form()]) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, _NOT_CONNECTED)
    result = await conn.delete_event(cal, event_id)
    if not result.ok:
        return _err(request, f"Couldn't delete the event: {result.detail}")
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": None, "deleted": True})
