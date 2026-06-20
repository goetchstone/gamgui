"""User management routes: list/search, detail, and the actions (signature, delegate, suspend).

Reads render full pages; actions are HTMX posts that swap a small result region. Suspend goes
through the destructive-op guard: preview (resolve + confirm) then apply.

GAM reads can raise ``GAMError`` (auth expired, rate limited, not found, …); every connector call
is wrapped so the user sees a friendly message instead of a 500. Mutations return a ``ChangeResult``
whose ``ok`` flag is always checked before reporting success.
"""

from __future__ import annotations

import asyncio
import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core.gam.errors import GAMError
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/users")

PAGE_SIZE = 50


def _conn(request: Request):
    return request.app.state.gamgui.connector


def _filter_users(users, q: str, scope: str):
    """In-memory filter over the cached list — instant, no GAM call per keystroke."""
    out = users
    if scope == "active":
        out = [u for u in out if not u.suspended]
    elif scope == "suspended":
        out = [u for u in out if u.suspended]
    q = (q or "").strip().lower()
    if q:
        out = [u for u in out if q in u.primary_email.lower() or q in u.full_name.lower() or q in (u.title or "").lower()]
    return out


def _table_context(users, q: str = "", scope: str = "all", page: int = 1) -> dict:
    filtered = _filter_users(users, q, scope)
    total = len(filtered)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    return {
        "users": filtered[start:start + PAGE_SIZE],
        "q": q, "scope": scope, "page": page, "pages": pages, "total": total,
    }


def _err(request: Request, message: str) -> HTMLResponse:
    """A small inline error fragment (for HTMX swap targets)."""
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


def _error_page(request: Request, message: str) -> HTMLResponse:
    """A full-page friendly error (for full-page GET routes)."""
    return TEMPLATES.TemplateResponse(request, "error.html", {"message": message})


def _friendly(exc: Exception) -> str:
    if isinstance(exc, GAMError):
        return exc.remediation
    return "Something went wrong talking to GAM. Please try again."


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "users.html", {"connected": False})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, "users.html",
            {"connected": True, "domain": st.connector.domain, "error": _friendly(exc), **_table_context([])},
        )
    return TEMPLATES.TemplateResponse(
        request, "users.html", {"connected": True, "domain": st.connector.domain, **_table_context(users)}
    )


@router.get("/table", response_class=HTMLResponse)
async def users_table(
    request: Request, q: str = "", scope: str = "all", page: int = 1, refresh: int = 0
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _err(request, "Not connected — run setup first.")
    try:
        users = await st.users(force=bool(refresh))
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_users_table.html", _table_context(users, q, scope, page))


@router.get("/detail", response_class=HTMLResponse)
async def user_detail(request: Request, email: str) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, "users.html", {"connected": False, "users": []})
    try:
        # Serve identity/role/security from the cached directory (reliable JSON path) so opening a
        # user is instant. Delegates and mail settings load lazily. Fall back to a direct lookup
        # only for a user not in the cached list (e.g. a deep link).
        users = await st.users()
        user = next((u for u in users if u.primary_email.lower() == email.lower()), None)
        if user is None:
            user = await conn.get_user(email)
    except Exception as exc:
        return _error_page(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request, "user_detail.html",
        {"user": user, "email": user.primary_email, "suspended": user.suspended},
    )


@router.post("/signature", response_class=HTMLResponse)
async def set_signature(
    request: Request,
    email: str = Form(...),
    signature: str = Form(""),
    html: str = Form("off"),
) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.set_signature(email, signature, html=(html == "on"))
    return TEMPLATES.TemplateResponse(
        request, "_action_result.html",
        {"ok": result.ok, "message": "Signature updated." if result.ok else result.detail},
    )


@router.get("/signature/current", response_class=HTMLResponse)
async def signature_current(request: Request, email: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    try:
        sig = await conn.get_signature(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_sig_current.html", {"signature": sig})


# --- group membership (view + add/remove; the function behind drag-and-drop) -----------
@router.get("/groups", response_class=HTMLResponse)
async def user_groups(request: Request, email: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    return await _groups_partial(request, conn, email)


async def _groups_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        member_of = await conn.list_user_groups(email)
        all_groups = await conn.list_groups()
    except Exception as exc:
        return _err(request, _friendly(exc))
    member_set = set(member_of)
    available = [g for g in all_groups if g.email not in member_set]
    return TEMPLATES.TemplateResponse(
        request, "_groups.html", {"email": email, "member_of": member_of, "available": available}
    )


@router.post("/groups/add", response_class=HTMLResponse)
async def groups_add(request: Request, email: str = Form(...), group: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.add_group_member(group.strip(), email)
    if not result.ok:
        return _err(request, f"Couldn't add to group: {result.detail}")
    return await _groups_partial(request, conn, email)


@router.post("/groups/remove", response_class=HTMLResponse)
async def groups_remove(request: Request, email: str = Form(...), group: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.remove_group_member(group.strip(), email)
    if not result.ok:
        return _err(request, f"Couldn't remove from group: {result.detail}")
    return await _groups_partial(request, conn, email)


@router.post("/delegate/add", response_class=HTMLResponse)
async def add_delegate(request: Request, email: str = Form(...), delegate: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    delegate = delegate.strip()
    if not delegate:
        return _err(request, "Enter a delegate email.")
    result = await conn.add_delegate(email, delegate)
    if not result.ok:
        return _err(request, f"Couldn't add delegate: {result.detail}")
    return await _delegates_partial(request, conn, email)


@router.post("/delegate/remove", response_class=HTMLResponse)
async def remove_delegate(request: Request, email: str = Form(...), delegate: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.remove_delegate(email, delegate.strip())
    if not result.ok:
        return _err(request, f"Couldn't remove delegate: {result.detail}")
    return await _delegates_partial(request, conn, email)


@router.post("/organization", response_class=HTMLResponse)
async def set_organization(
    request: Request, email: str = Form(...), title: str = Form(""), department: str = Form("")
) -> HTMLResponse:
    """Set a user's title (role) + department (store). Guarded write; invalidates the cache."""
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    title, department = title.strip(), department.strip()
    result = await conn.set_organization(email, title=title, department=department)
    if not result.ok:
        return _err(request, f"Couldn't update role/store: {result.detail}")
    st.invalidate_users()  # title/department changed -> cached directory is stale
    return TEMPLATES.TemplateResponse(
        request, "_org_form.html", {"email": email, "title": title, "department": department, "saved": True}
    )


# --- bulk: assign a store (department) to many users, preserving each person's title ----
async def _bulk_targets(st, group: str, emails_raw: str):
    """Resolve target ACTIVE users from a group OR a pasted email list (matched against the cache)."""
    users = await st.users()
    if group:
        members = await st.connector.list_group_members(group)
        wanted = {m.email.lower() for m in members}
    else:
        wanted = {e.strip().lower() for e in emails_raw.replace(",", "\n").splitlines() if e.strip()}
    return [u for u in users if u.primary_email.lower() in wanted and not u.suspended]


async def _run_bulk_store(job, st, conn, targets, store: str) -> None:
    """Background task: set department=store per user, KEEPING each existing title."""
    try:
        for u in targets:
            job.current = u.primary_email
            try:
                res = await conn.set_organization(u.primary_email, title=u.title or "", department=store)
                ok = bool(getattr(res, "ok", False))
            except Exception:
                ok = False
            if ok:
                job.applied += 1
            else:
                job.failed.append(u.primary_email)
            job.done += 1
    except Exception as exc:
        job.error = _friendly(exc)
    finally:
        job.current = ""
        job.finished = True
        st.invalidate_users()  # departments changed -> cached directory is stale


@router.get("/bulk", response_class=HTMLResponse)
async def bulk_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "bulk_store.html", {"connected": False})
    try:
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, "bulk_store.html", {"connected": True, "error": _friendly(exc), "groups": []})
    return TEMPLATES.TemplateResponse(request, "bulk_store.html", {"connected": True, "groups": [g.email for g in groups]})


@router.post("/bulk/preview", response_class=HTMLResponse)
async def bulk_preview(request: Request, store: str = Form(""), group: str = Form(""), emails: str = Form("")) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _err(request, "Not connected.")
    try:
        targets = await _bulk_targets(st, group.strip(), emails)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request, "_bulk_preview.html", {"targets": targets[:200], "count": len(targets), "store": store.strip()}
    )


@router.post("/bulk/apply", response_class=HTMLResponse)
async def bulk_apply(request: Request, store: str = Form(""), group: str = Form(""), emails: str = Form("")) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    store = store.strip()
    if not store:
        return _err(request, "Enter a store/department value first.")
    try:
        targets = await _bulk_targets(st, group.strip(), emails)
    except Exception as exc:
        return _err(request, _friendly(exc))
    if not targets:
        return _err(request, "No matching active users to update.")
    job = start_job(st.jobs, len(targets))
    job.task = asyncio.create_task(_run_bulk_store(job, st, conn, targets, store))
    return TEMPLATES.TemplateResponse(request, "_bulk_apply.html", {"job": job})


@router.get("/bulk/status", response_class=HTMLResponse)
async def bulk_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job)
    if j is None:
        return _err(request, "That bulk job is no longer available — re-run it.")
    return TEMPLATES.TemplateResponse(request, "_bulk_apply.html", {"job": j})


@router.get("/delegates", response_class=HTMLResponse)
async def delegates_get(request: Request, email: str) -> HTMLResponse:
    """Lazy-loaded into the detail page so the page renders before this gam call returns."""
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    return await _delegates_partial(request, conn, email)


async def _delegates_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        delegates = await conn.list_delegates(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_delegates.html", {"delegates": delegates, "email": email})


# --- calendar access (who can see/edit this user's primary calendar) --------------------
async def _calendar_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        acls = await conn.list_calendar_acls(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_calendar.html", {"acls": acls, "email": email})


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_get(request: Request, email: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    return await _calendar_partial(request, conn, email)


@router.post("/calendar/add", response_class=HTMLResponse)
async def calendar_add(
    request: Request, email: str = Form(...), target: str = Form(...), role: str = Form("reader")
) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    target = target.strip()
    if not target:
        return _err(request, "Enter an email to share with.")
    result = await conn.add_calendar_acl(email, target, role=role)
    if not result.ok:
        return _err(request, f"Couldn't share calendar: {result.detail}")
    return await _calendar_partial(request, conn, email)


@router.post("/calendar/remove", response_class=HTMLResponse)
async def calendar_remove(request: Request, email: str = Form(...), scope: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.remove_calendar_acl(email, scope.strip())
    if not result.ok:
        return _err(request, f"Couldn't remove access: {result.detail}")
    return await _calendar_partial(request, conn, email)


# --- vacation / auto-responder (lazy-loaded into the detail page) ----------------------
@router.get("/vacation", response_class=HTMLResponse)
async def vacation_get(request: Request, email: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    return await _vacation_partial(request, conn, email)


async def _vacation_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        vac = await conn.get_vacation(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_vacation.html", {"vac": vac, "email": email})


@router.post("/vacation/set", response_class=HTMLResponse)
async def vacation_set(
    request: Request,
    email: str = Form(...),
    subject: str = Form(""),
    message: str = Form(""),
    contactsonly: str = Form("off"),
    domainonly: str = Form("off"),
    start: str = Form(""),
    end: str = Form(""),
) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.set_vacation(
        email, subject, message, html=True,
        start=start.strip() or None, end=end.strip() or None,
        contacts_only=(contactsonly == "on"), domain_only=(domainonly == "on"),
    )
    if not result.ok:
        return _err(request, f"Couldn't set auto-reply: {result.detail}")
    return await _vacation_partial(request, conn, email)


@router.post("/vacation/off", response_class=HTMLResponse)
async def vacation_off(request: Request, email: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    result = await conn.clear_vacation(email)
    if not result.ok:
        return _err(request, f"Couldn't turn off auto-reply: {result.detail}")
    return await _vacation_partial(request, conn, email)


@router.get("/suspend/zone", response_class=HTMLResponse)
async def suspend_zone(request: Request, email: str, suspended: str = "false") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_suspend_zone.html", {"email": email, "suspended": suspended == "true"}
    )


@router.post("/suspend/preview", response_class=HTMLResponse)
async def suspend_preview(request: Request, email: str = Form(...)) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    # plan_suspend + guard.evaluate are pure (no GAM call) — they just resolve the target set.
    decision = guard.evaluate(conn.plan_suspend([email], suspend=True))
    return TEMPLATES.TemplateResponse(
        request, "_suspend_confirm.html", {"email": email, "decision": decision}
    )


@router.post("/suspend/apply", response_class=HTMLResponse)
async def suspend_apply(request: Request, email: str = Form(...), suspend: str = Form("on")) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected.")
    want_suspend = suspend == "on"
    try:
        results = await conn.apply(conn.plan_suspend([email], suspend=want_suspend))
    except Exception as exc:
        return _err(request, _friendly(exc))
    if not (results and all(r.ok for r in results)):
        detail = results[0].detail if results else "no change applied"
        return _err(request, f"Failed: {detail}")
    request.app.state.gamgui.invalidate_users()  # status changed -> cached list is stale
    return TEMPLATES.TemplateResponse(
        request, "_suspend_zone.html", {"email": email, "suspended": want_suspend}
    )
