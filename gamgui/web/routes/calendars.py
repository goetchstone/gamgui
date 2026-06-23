"""Calendars: find a calendar (a room/resource or a user's), see who has access, search its events,
and delete a stray event.

Reads are bounded (one calendar; event search requires a query or date window + a result cap).
Deletion is destructive on real calendars, so it's preview -> confirm -> audited, targeting one
specific event id (a recurring master removes the whole series).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/calendars")

EVENT_CAP = 200


def _conn(request: Request):
    return request.app.state.gamgui.connector


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "calendars.html", {"connected": _conn(request) is not None})


@router.get("/resources", response_class=HTMLResponse)
async def resources(request: Request, q: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    try:
        rs = await conn.list_resources(q.strip())
    except Exception as exc:
        return _err(request, _friendly(exc))
    items = [
        {"cal_id": r.email, "label": r.name or r.email, "meta": r.resource_type or "resource"}
        for r in rs if r.email
    ]
    return TEMPLATES.TemplateResponse(request, "_calendar_list.html", {"items": items})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    q = q.strip()
    items, seen = [], set()
    try:
        # Room/resource calendars (server-side name filter).
        for r in await conn.list_resources(q):
            if r.email and r.email not in seen:
                seen.add(r.email)
                items.append({"cal_id": r.email, "label": r.name or r.email, "meta": "room"})
        # Secondary calendars across the domain — only scan when there's a query (avoid pulling all).
        if q:
            for c in await conn.search_calendars(q):
                if c["id"] not in seen:
                    seen.add(c["id"])
                    meta = f"owned by {c['owner']}" if c.get("owner") else (c.get("role") or "shared")
                    items.append({"cal_id": c["id"], "label": c["summary"] or c["id"], "meta": meta})
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_calendar_list.html", {"items": items})


@router.get("/user", response_class=HTMLResponse)
async def user_calendars(request: Request, email: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
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
    return TEMPLATES.TemplateResponse(request, "_calendar_list.html", {"items": items})


@router.get("/detail", response_class=HTMLResponse)
async def detail(request: Request, cal: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    try:
        acls = await conn.list_calendar_acls_for(cal)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", {"cal": cal, "acls": acls})


@router.get("/events", response_class=HTMLResponse)
async def events(request: Request, cal: str, q: str = "", after: str = "", before: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    q, after, before = q.strip(), after.strip(), before.strip()
    if not (q or after or before):  # never an unbounded all-events scan
        return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": [], "need_filter": True})
    try:
        evs = await conn.search_events(cal, query=q, after=after, before=before, cap=EVENT_CAP)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": evs, "capped": len(evs) >= EVENT_CAP})


@router.post("/event/preview", response_class=HTMLResponse)
async def event_preview(request: Request, cal: str = Form(...), event_id: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    try:
        ev = await conn.get_event(cal, event_id)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if ev is None:
        return _err(request, "That event no longer exists.")
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": ev, "deleted": False})


@router.post("/event/delete", response_class=HTMLResponse)
async def event_delete(request: Request, cal: str = Form(...), event_id: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.delete_event(cal, event_id)
    if not result.ok:
        return _err(request, f"Couldn't delete the event: {result.detail}")
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": None, "deleted": True})
