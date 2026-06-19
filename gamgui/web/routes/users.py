"""User management routes: list/search, detail, and the actions (signature, delegate, suspend).

Reads render full pages; actions are HTMX posts that swap a small result region. Suspend goes
through the destructive-op guard: preview (resolve + confirm) then apply.

GAM reads can raise ``GAMError`` (auth expired, rate limited, not found, …); every connector call
is wrapped so the user sees a friendly message instead of a 500. Mutations return a ``ChangeResult``
whose ``ok`` flag is always checked before reporting success.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/users")


def _conn(request: Request):
    return request.app.state.gamgui.connector


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
    conn = _conn(request)
    if conn is None:
        return TEMPLATES.TemplateResponse(request, "users.html", {"connected": False, "users": []})
    try:
        users = await conn.list_users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, "users.html",
            {"connected": True, "users": [], "domain": conn.domain, "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request, "users.html", {"connected": True, "users": users, "domain": conn.domain}
    )


@router.get("/table", response_class=HTMLResponse)
async def users_table(request: Request, q: str = "", scope: str = "all") -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return _err(request, "Not connected — run setup first.")
    try:
        users = await conn.list_users(search=q.strip(), include_suspended=(scope != "active"))
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_users_table.html", {"users": users})


@router.get("/detail", response_class=HTMLResponse)
async def user_detail(request: Request, email: str) -> HTMLResponse:
    conn = _conn(request)
    if conn is None:
        return TEMPLATES.TemplateResponse(request, "users.html", {"connected": False, "users": []})
    try:
        user = await conn.get_user(email)
        delegates = await conn.list_delegates(email)
    except Exception as exc:
        return _error_page(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request, "user_detail.html",
        {"user": user, "delegates": delegates, "email": email, "suspended": user.suspended},
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


async def _delegates_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        delegates = await conn.list_delegates(email)
    except Exception as exc:
        return _err(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_delegates.html", {"delegates": delegates, "email": email})


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
    return TEMPLATES.TemplateResponse(
        request, "_suspend_zone.html", {"email": email, "suspended": want_suspend}
    )
