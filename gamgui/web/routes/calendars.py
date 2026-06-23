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
    items, seen, notes = [], set(), []
    # Two independent sources — a failure in one (e.g. no Calendar Resource API) must not hide the other.
    try:  # Room/resource calendars (matched locally).
        for r in await conn.list_resources(q):
            if r.email and r.email not in seen:
                seen.add(r.email)
                items.append({"cal_id": r.email, "label": r.name or r.email, "meta": "room"})
    except Exception as exc:
        notes.append(f"Room calendars unavailable: {_friendly(exc)}")
    if q:  # Secondary calendars across the domain — only scan when there's a query (avoid pulling all).
        try:
            for c in await conn.search_calendars(q):
                if c["id"] not in seen:
                    seen.add(c["id"])
                    meta = f"owned by {c['owner']}" if c.get("owner") else (c.get("role") or "shared")
                    items.append({"cal_id": c["id"], "label": c["summary"] or c["id"], "meta": meta})
        except Exception as exc:
            notes.append(f"Domain calendar search unavailable: {_friendly(exc)}")
    return TEMPLATES.TemplateResponse(request, "_calendar_list.html", {"items": items, "notes": notes})


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
async def detail(request: Request, cal: str, label: str = "") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
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
async def delete_preview(request: Request, cal: str = Form(...), label: str = Form(""),
                         acl_count: int = Form(0)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    cal = cal.strip()
    try:
        owner, refusal = await _resolve_delete_owner(request, conn, cal)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if not owner:
        return _err(request, refusal)
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, acl_count=acl_count)


@router.post("/delete", response_class=HTMLResponse)
async def delete_cal(request: Request, cal: str = Form(...), confirm: str = Form(""),
                     label: str = Form("")) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
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
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, deleted=True)


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
