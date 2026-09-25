"""User management routes: list/search, detail, and the actions (signature, delegate, suspend).

Reads render full pages; actions are HTMX posts that swap a small result region. Suspend and the
bulk department job go through the guard: preview (resolve + confirm) then apply, and the apply
route itself refuses a POST without the confirmation (``guard.enforce``).

GAM reads can raise ``GAMError`` (auth expired, rate limited, not found, …); every connector call
is wrapped so the user sees a friendly message instead of a 500. Mutations return a ``ChangeResult``
whose ``ok`` flag is always checked before reporting success.
"""

from __future__ import annotations

import asyncio
import math
from typing import Annotated
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import bulk, guard
from ...core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from ...core.gam.commands import GAMCommands
from ...core.lifecycle import autoreply_html, autoreply_text
from ...core.onboarding import looks_like_email
from ...core.signatures import smart_quote_warning
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, GAM_TROUBLE, as_of, connector, error_partial, friendly, write_failed

router = APIRouter(prefix="/users")

PAGE_SIZES = (15, 25, 50)   # rows per page: 15 fit the fixed 13" window; a longer page scrolls inside the table
PAGE_SIZE = PAGE_SIZES[0]
SCOPES = ("all", "active", "suspended")
# Each sortable column's key over the cached list (plan U8). A blank (no title) sorts last either way.
SORTS = {
    "name": lambda u: u.full_name.casefold(),
    "email": lambda u: u.primary_email.casefold(),
    "title": lambda u: (u.title or "").casefold(),
    "ou": lambda u: (u.org_unit_path or "").casefold(),
    "status": lambda u: "suspended" if u.suspended else "active",
}
_DEFAULTS = {"q": "", "scope": "all", "sort": "name", "desc": 0, "size": PAGE_SIZE, "page": 1}
# The controls whose click re-renders the whole list: focus goes to its count line, not <body>.
_PAGER_IDS = ("users-prev", "users-next", "users-clear")

_USERS_PAGE = "users.html"
_DELETE_ZONE = "_delete_zone.html"
_BULK_STORE_PAGE = "bulk_store.html"
_TRY_AGAIN = f"{GAM_TROUBLE} Please try again."


def _filter_users(users, q: str, scope: str):
    """In-memory filter over the cached list — instant, no GAM call per keystroke."""
    out = users
    if scope == "active":
        out = [u for u in out if not u.suspended]
    elif scope == "suspended":
        out = [u for u in out if u.suspended]
    q = (q or "").strip().lower()
    if q:
        out = [u for u in out if any(q in (field or "").lower() for field in (
            u.primary_email, u.full_name, u.title, u.department, u.org_unit_path))]
    return out


def _sort_users(users, sort: str, desc: bool):
    """Sorted by one column, ties by name then address whichever way it runs; blanks last."""
    key = SORTS[sort]
    rows = sorted(users, key=lambda u: (u.full_name.casefold(), u.primary_email.casefold()))
    blank = [u for u in rows if not key(u)]
    rows = [u for u in rows if key(u)]
    rows.sort(key=key, reverse=desc)   # stable, so the tie order above survives a reverse
    return rows + blank


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list_state(q="", scope="all", sort="name", desc=0, size=PAGE_SIZE, page=1) -> dict:
    """The list's view with every value checked: an unknown scope, column or size is the default."""
    size = _as_int(size, PAGE_SIZE)
    return {
        "q": (q or "").strip(),
        "scope": scope if scope in SCOPES else "all",
        "sort": sort if sort in SORTS else "name",
        "desc": 1 if _as_int(desc, 0) else 0,
        "size": size if size in PAGE_SIZES else PAGE_SIZE,
        "page": max(1, _as_int(page, 1)),
    }


def _list_url(path: str, state: dict, **changes) -> str:
    """``path`` with the view as its query — only what differs from the default, so /users stays /users."""
    query = urlencode({k: v for k, v in {**state, **changes}.items() if v != _DEFAULTS[k]})
    return f"{path}?{query}" if query else path


def _list_url_from(back: str) -> str:
    """/users as a detail link's ``back`` query left it. Only the list's own keys pass, each re-checked,
    so the link can only ever reopen the list."""
    got = {k: v[-1] for k, v in parse_qs(back or "").items() if k in _DEFAULTS}
    return _list_url("/users", _list_state(**got))


def _table_context(users, q: str = "", scope: str = "all", page: int = 1, sort: str = "name",
                   desc: int = 0, size: int = PAGE_SIZE) -> dict:
    state = _list_state(q, scope, sort, desc, size, page)
    rows = _sort_users(_filter_users(users, state["q"], state["scope"]), state["sort"], bool(state["desc"]))
    size, total = state["size"], len(rows)
    pages = max(1, math.ceil(total / size))
    state["page"] = min(state["page"], pages)
    start = (state["page"] - 1) * size
    return {
        **state, "users": rows[start:start + size], "pages": pages, "total": total, "start": start,
        "sizes": PAGE_SIZES, "list_url": _list_url("/users", state), "back": _list_url("", state).lstrip("?"),
        "table_url": lambda **changes: _list_url("/users/table", state, **changes),
    }


def _error_page(request: Request, message: str) -> HTMLResponse:
    """A full-page friendly error (for full-page GET routes)."""
    return TEMPLATES.TemplateResponse(request, "error.html", {"message": message})


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request, q: str = "", scope: str = "all", page: int = 1, sort: str = "name",
                     desc: int = 0, size: int = PAGE_SIZE) -> HTMLResponse:
    """The list, opened at the view its URL names (the table's requests keep the URL current)."""
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _USERS_PAGE, {"connected": False})
    view = (q, scope, page, sort, desc, size)
    try:
        users = await st.users(stale_ok=True)   # the table footer says how old it is
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, _USERS_PAGE,
            {"connected": True, "domain": st.connector.domain, "error": friendly(exc, _TRY_AGAIN),
             **_table_context([], *view)},
        )
    return TEMPLATES.TemplateResponse(
        request, _USERS_PAGE,
        {"connected": True, "domain": st.connector.domain, **_table_context(users, *view), **as_of(st.user_cache)},
    )


@router.get("/table", response_class=HTMLResponse)
async def users_table(
    request: Request, q: str = "", scope: str = "all", page: int = 1, sort: str = "name", desc: int = 0,
    size: int = PAGE_SIZE, refresh: int = 0,
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, "Not connected — run setup first.")
    try:
        users = await st.users(force=bool(refresh), stale_ok=True)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    ctx = {**_table_context(users, q, scope, page, sort, desc, size), **as_of(st.user_cache)}
    ctx["focus"] = request.headers.get("HX-Trigger") in _PAGER_IDS
    # The address bar follows the view (plan U8), so Back from a user, or a reload, reopens it. Replaced,
    # not pushed: a search typed a pause at a time would otherwise leave a Back step per pause.
    return TEMPLATES.TemplateResponse(request, "_users_table.html", ctx, headers={"HX-Replace-Url": ctx["list_url"]})


@router.get("/detail", response_class=HTMLResponse)
async def user_detail(request: Request, email: str, back: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, _USERS_PAGE, {"connected": False, "users": []})
    try:
        # Serve identity/role/security from the cached directory (reliable JSON path) so opening a
        # user is instant. Delegates and mail settings load lazily. Fall back to a direct lookup
        # only for a user not in the cached list (e.g. a deep link).
        users = await st.users()
        user = next((u for u in users if u.primary_email.lower() == email.lower()), None)
        if user is None:
            user = await conn.get_user(email)
    except Exception as exc:
        return _error_page(request, friendly(exc, _TRY_AGAIN))
    return TEMPLATES.TemplateResponse(
        request, "user_detail.html",
        {"user": user, "email": user.primary_email, "suspended": user.suspended, "list_url": _list_url_from(back)},
    )


@router.post("/signature", response_class=HTMLResponse)
async def set_signature(
    request: Request,
    email: Annotated[str, Form()],
    signature: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "off",
) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.set_signature(email, signature, html=(html == "on"))
    if not result.ok:
        return TEMPLATES.TemplateResponse(
            request, "_action_result.html",
            {"ok": False, "message": "Couldn't set the signature. " + result.remediation, "details": result.detail},
        )
    note = smart_quote_warning(signature)
    return TEMPLATES.TemplateResponse(
        request, "_action_result.html",
        {"ok": True, "message": "Signature updated." + (" " + note if note else "")},
    )


@router.post("/signout", response_class=HTMLResponse)
async def signout_user(request: Request, email: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.signout_user(email)
    if not result.ok:
        return write_failed(request, f"Couldn't sign {email} out.", result)
    return TEMPLATES.TemplateResponse(
        request, "_action_result.html", {"ok": True, "message": f"Signed {email} out of all sessions."})


@router.get("/signature/current", response_class=HTMLResponse)
async def signature_current(request: Request, email: str) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        sig = await conn.get_signature(email)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    return TEMPLATES.TemplateResponse(request, "_sig_current.html", {"signature": sig})


# --- group membership, from the person's side (the Groups board is routes/groups.py) ----
@router.get("/groups", response_class=HTMLResponse)
async def user_groups(request: Request, email: str) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    return await _groups_partial(request, conn, email)


async def _groups_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        member_of = await conn.list_user_groups(email)
        all_groups = await conn.list_groups()
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    member_set = set(member_of)
    available = [g for g in all_groups if g.email not in member_set]
    return TEMPLATES.TemplateResponse(
        request, "_groups.html", {"email": email, "member_of": member_of, "available": available}
    )


@router.post("/groups/add", response_class=HTMLResponse)
async def groups_add(request: Request, email: Annotated[str, Form()], group: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.add_group_member(group.strip(), email)
    if not result.ok:
        return write_failed(request, f"Couldn't add {email} to {group.strip()}.", result)
    return await _groups_partial(request, conn, email)


@router.post("/groups/remove", response_class=HTMLResponse)
async def groups_remove(request: Request, email: Annotated[str, Form()], group: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.remove_group_member(group.strip(), email)
    if not result.ok:
        return write_failed(request, f"Couldn't remove {email} from {group.strip()}.", result)
    return await _groups_partial(request, conn, email)


async def _check_delegate(st, email: str, delegate: str) -> "tuple[str, str]":
    """(error, warning) for a new delegate: an error blocks the add, a warning needs the operator's OK.
    GAM reads the value as a <UserList>, so a bare name becomes name@<domain> and a comma adds several."""
    if not delegate:
        return "Enter a delegate email.", ""
    if not looks_like_email(delegate):
        return f"“{delegate}” isn't an email address — enter the delegate's full address, like name@example.com.", ""
    if delegate.lower() == email.strip().lower():
        return "A mailbox can't be delegated to its own owner.", ""
    try:
        directory = await st.users()
    except Exception as exc:  # noqa: BLE001 — can't check: ask, don't guess
        return "", f"Couldn't check {delegate} against the directory — {friendly(exc, _TRY_AGAIN)}"
    key = delegate.lower()
    found = next((u for u in directory if u.primary_email.lower() == key), None)
    if found is None:
        owner = next((u for u in directory if key in (a.lower() for a in u.aliases)), None)
        if owner:
            return f"{delegate} is an alias of {owner.primary_email} — enter the primary address.", ""
        return "", (f"{delegate} isn't in the directory. Gmail only accepts a delegate from your own "
                    f"organization — check for a typo. (An account created in the last few minutes shows "
                    f"after Users → Refresh.)")
    if found.suspended:
        return "", f"{delegate} is suspended — Gmail may refuse the delegation, and the account can't sign in to use it."
    return "", ""


@router.post("/delegate/add", response_class=HTMLResponse)
async def add_delegate(request: Request, email: Annotated[str, Form()], delegate: Annotated[str, Form()],
                       confirmed: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    delegate = delegate.strip()
    error, warning = await _check_delegate(st, email, delegate)
    if error:
        return await _delegates_partial(request, conn, email, notice={"ok": False, "message": error}, typed=delegate)
    if warning and confirmed != "1":
        return await _delegates_partial(request, conn, email, warning=warning, pending=delegate)
    result = await conn.add_delegate(email, delegate)
    if not result.ok:
        return await _delegates_partial(request, conn, email, typed=delegate, notice={
            "ok": False, "message": "Couldn't add the delegate. " + result.remediation, "details": result.detail})
    return await _delegates_partial(request, conn, email, notice={"ok": True, "message": f"Added {delegate}."})


@router.post("/delegate/remove", response_class=HTMLResponse)
async def remove_delegate(request: Request, email: Annotated[str, Form()], delegate: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    delegate = delegate.strip()
    result = await conn.remove_delegate(email, delegate)
    if not result.ok:
        return await _delegates_partial(request, conn, email, notice={
            "ok": False, "message": f"Couldn't remove {delegate}. " + result.remediation, "details": result.detail})
    return await _delegates_partial(request, conn, email, notice={"ok": True, "message": f"Removed {delegate}."})


@router.post("/organization", response_class=HTMLResponse)
async def set_organization(
    request: Request, email: Annotated[str, Form()], title: Annotated[str, Form()] = "", department: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Set a user's title (role) + department. Guarded write; patches the cached record."""
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    title, department = title.strip(), department.strip()
    result = await conn.set_organization(email, title=title, department=department)
    if not result.ok:
        return write_failed(request, "Couldn't update the title and department.", result)
    st.patch_user(email, title=title, department=department)  # `organization ... primary` sets both
    return TEMPLATES.TemplateResponse(
        request, "_org_form.html", {"email": email, "title": title, "department": department, "saved": True}
    )


# --- bulk: set the department on many users, preserving each person's title ----
# Apply sets the department its preview showed on the people it listed, held under a single-use token
# (web/previews.py). Apply once re-resolved the live form, so a list or department edited after
# Preview was written under a dialog that still named the previewed value and count.
_BULK_FLOW = "bulk_department"


def _pasted(emails_raw: str) -> set:
    return {e.strip().lower() for e in emails_raw.replace(",", "\n").splitlines() if e.strip()}


def _bulk_form_key(store: str, group: str, emails_raw: str) -> tuple:
    return (store.strip(), group.strip().lower(), tuple(sorted(_pasted(emails_raw))))


async def _bulk_targets(st, group: str, emails_raw: str):
    """Resolve target ACTIVE users from a group OR a pasted email list (matched against the cache)."""
    users = await st.users()
    if group:
        members = await st.connector.list_group_members(group)
        wanted = {m.email.lower() for m in members}
    else:
        wanted = _pasted(emails_raw)
    return [u for u in users if u.primary_email.lower() in wanted and not u.suspended]


async def _run_bulk_store(job, st, conn, targets, store: str) -> None:
    """The job's task: ``bulk.set_departments``, each person's new department patched into the cached
    directory as it lands. A failed write's outcome isn't known (a timeout may still have applied it),
    so any failure — or the loop dying — drops the whole list instead."""
    clean = False
    try:
        await bulk.set_departments(job, conn, targets, store,
                                   on_set=lambda u: st.patch_user(u.primary_email, department=store))
        clean = not job.failed_total
    finally:
        if not clean:
            st.invalidate_users()


@router.get("/bulk", response_class=HTMLResponse)
async def bulk_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _BULK_STORE_PAGE, {"connected": False})
    try:
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _BULK_STORE_PAGE, {"connected": True, "error": friendly(exc, _TRY_AGAIN), "groups": []})
    return TEMPLATES.TemplateResponse(request, _BULK_STORE_PAGE, {"connected": True, "groups": [g.email for g in groups]})


@router.post("/bulk/preview", response_class=HTMLResponse)
async def bulk_preview(request: Request, store: Annotated[str, Form()] = "", group: Annotated[str, Form()] = "", emails: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        targets = await _bulk_targets(st, group.strip(), emails)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    token = ""
    if targets and store.strip():
        token = st.previews.hold(_BULK_FLOW, _bulk_form_key(store, group, emails),
                                 (store.strip(), [u.primary_email for u in targets]))
    return TEMPLATES.TemplateResponse(
        request, "_bulk_preview.html",
        {"targets": targets[:200], "count": len(targets), "store": store.strip(), "token": token}
    )


@router.post("/bulk/apply", response_class=HTMLResponse)
async def bulk_apply(request: Request, store: Annotated[str, Form()] = "", group: Annotated[str, Form()] = "", emails: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    if not store.strip():
        return error_partial(request, "Enter a department first.")
    form = await request.form()
    held, refusal = st.previews.take(_BULK_FLOW, str(form.get(TOKEN_FIELD) or ""),
                                     _bulk_form_key(store, group, emails), again="click Preview again")
    if refusal:
        return error_partial(request, refusal)
    store, emails_held = held
    # The previewed people, with their titles as the directory has them now (the job keeps each title).
    try:
        current = {u.primary_email.lower(): u for u in await st.users()}
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    targets = [u for e in emails_held if (u := current.get(e.lower())) is not None and not u.suspended]
    if not targets or len(targets) != len(emails_held):
        return error_partial(request, "Someone in the preview is no longer an active user — click Preview again.")
    previews = guard.changes([u.primary_email for u in targets], RiskLevel.LOW, "Set department")
    refusal = guard.enforce(previews, form, confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    n = len(targets)
    job = start_job(st.jobs, n, kind="department",
                    title=f"Department “{store}” for {n} user{'s' if n != 1 else ''}")
    job.task = asyncio.create_task(_run_bulk_store(job, st, conn, targets, store))
    return TEMPLATES.TemplateResponse(request, "_bulk_apply.html", {"job": job})


@router.get("/bulk/status", response_class=HTMLResponse)
async def bulk_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job)
    if j is None:
        return error_partial(request, "That bulk job is no longer available — re-run it.")
    return TEMPLATES.TemplateResponse(request, "_bulk_apply.html", {"job": j})


@router.get("/delegates", response_class=HTMLResponse)
async def delegates_get(request: Request, email: str) -> HTMLResponse:
    """Lazy-loaded into the detail page so the page renders before this gam call returns."""
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    return await _delegates_partial(request, conn, email)


async def _delegates_partial(request: Request, conn, email: str, **extra) -> HTMLResponse:
    """The delegate list + add form. ``extra``: a ``notice`` (ok/message/details) shown above it, a
    ``warning`` + ``pending`` address awaiting "Add anyway", or the ``typed`` address to keep in the box."""
    try:
        delegates = await conn.list_delegates(email)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    return TEMPLATES.TemplateResponse(request, "_delegates.html", {"delegates": delegates, "email": email, **extra})


# --- calendar access (who can see/edit this user's primary calendar) --------------------
async def _calendar_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        acls = await conn.list_calendar_acls(email)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    return TEMPLATES.TemplateResponse(request, "_calendar.html", {"acls": acls, "email": email})


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_get(request: Request, email: str) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    return await _calendar_partial(request, conn, email)


@router.post("/calendar/add", response_class=HTMLResponse)
async def calendar_add(
    request: Request, email: Annotated[str, Form()], target: Annotated[str, Form()], role: Annotated[str, Form()] = "reader"
) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    target = target.strip()
    if not target:
        return error_partial(request, "Enter an email to share with.")
    try:
        result = await conn.add_calendar_acl(email, target, role=role)
    except ValueError as exc:  # the builder refuses a role outside the grammar's <CalendarACLRole>
        return error_partial(request, f"Couldn't share calendar: {exc}.")
    if not result.ok:
        return write_failed(request, f"Couldn't share the calendar with {target}.", result)
    return await _calendar_partial(request, conn, email)


@router.post("/calendar/remove", response_class=HTMLResponse)
async def calendar_remove(request: Request, email: Annotated[str, Form()], scope: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.remove_calendar_acl(email, scope.strip())
    if not result.ok:
        return write_failed(request, f"Couldn't remove {scope.strip()}'s access.", result)
    return await _calendar_partial(request, conn, email)


# --- delete account (irreversible — guarded, type-the-email confirm) ---------------------
@router.get("/delete/zone", response_class=HTMLResponse)
async def delete_zone(request: Request, email: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _DELETE_ZONE, {"email": email})


@router.post("/delete/confirm", response_class=HTMLResponse)
async def delete_confirm(request: Request, email: Annotated[str, Form()]) -> HTMLResponse:
    # Warn (loudly) if a Drive/calendar transfer is still running — deleting now loses that data.
    conn = connector(request)
    pending = await conn.incomplete_transfers_for(email.strip()) if conn else []
    return TEMPLATES.TemplateResponse(
        request, _DELETE_ZONE,
        {"email": email, "confirming": True, "pending_transfers": pending},
    )


@router.post("/delete/apply", response_class=HTMLResponse)
async def delete_apply(request: Request, email: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    # The exact address typed (and the confirm click): guard.enforce owns that rule for every route
    # that deletes an account, recognising the delete by the argv conn.delete_user runs.
    delete = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=email, summary="Delete account",
                           risk=RiskLevel.DESTRUCTIVE, argv=GAMCommands.delete_user(email))
    refusal = guard.enforce([delete], await request.form())
    if not refusal:
        try:
            refusal = " ".join(guard.alias_deletes({email: await conn.primary_address(email.strip())}))
        except Exception as exc:  # noqa: BLE001 - fail closed: an address GAM can't resolve can't be ruled out
            refusal = f"Couldn't confirm which account that address belongs to — {friendly(exc, _TRY_AGAIN)}"
    if refusal:
        return TEMPLATES.TemplateResponse(request, _DELETE_ZONE, {"email": email, "confirming": True, "error": refusal})
    result = await conn.delete_user(email)
    if not result.ok:
        return write_failed(request, f"Couldn't delete {email}.", result)
    request.app.state.gamgui.patch_user(email)  # no changes: the record goes
    return TEMPLATES.TemplateResponse(request, _DELETE_ZONE, {"email": email, "deleted": True})


# --- vacation / auto-responder (lazy-loaded into the detail page) ----------------------
@router.get("/vacation", response_class=HTMLResponse)
async def vacation_get(request: Request, email: str) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    return await _vacation_partial(request, conn, email)


async def _vacation_partial(request: Request, conn, email: str) -> HTMLResponse:
    try:
        vac = await conn.get_vacation(email)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    # The stored body is HTML (the form sends it so); the textarea edits its text.
    message = autoreply_text(vac.message) if vac.enabled else ""
    return TEMPLATES.TemplateResponse(request, "_vacation.html", {"vac": vac, "email": email, "message": message})


@router.post("/vacation/set", response_class=HTMLResponse)
async def vacation_set(
    request: Request,
    email: Annotated[str, Form()],
    subject: Annotated[str, Form()] = "",
    message: Annotated[str, Form()] = "",
    contactsonly: Annotated[str, Form()] = "off",
    domainonly: Annotated[str, Form()] = "off",
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.set_vacation(   # HTML, so what was typed goes out with its line breaks
        email, subject, autoreply_html(message), html=True,
        start=start.strip() or None, end=end.strip() or None,
        contacts_only=(contactsonly == "on"), domain_only=(domainonly == "on"),
    )
    if not result.ok:
        return write_failed(request, "Couldn't turn on the auto-reply.", result)
    return await _vacation_partial(request, conn, email)


@router.post("/vacation/off", response_class=HTMLResponse)
async def vacation_off(request: Request, email: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    result = await conn.clear_vacation(email)
    if not result.ok:
        return write_failed(request, "Couldn't turn off the auto-reply.", result)
    return await _vacation_partial(request, conn, email)


@router.get("/suspend/zone", response_class=HTMLResponse)
async def suspend_zone(request: Request, email: str, suspended: str = "false") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_suspend_zone.html", {"email": email, "suspended": suspended == "true"}
    )


@router.post("/suspend/preview", response_class=HTMLResponse)
async def suspend_preview(request: Request, email: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    # plan_suspend + guard.evaluate are pure (no GAM call) — they just resolve the target set.
    decision = guard.evaluate(conn.plan_suspend([email], suspend=True))
    return TEMPLATES.TemplateResponse(
        request, "_suspend_confirm.html", {"email": email, "decision": decision}
    )


@router.post("/suspend/apply", response_class=HTMLResponse)
async def suspend_apply(request: Request, email: Annotated[str, Form()], suspend: Annotated[str, Form()] = "on") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    want_suspend = suspend == "on"
    previews = conn.plan_suspend([email], suspend=want_suspend)
    refusal = guard.enforce(previews, await request.form())   # suspend is destructive: confirmed=1
    if refusal:
        return error_partial(request, refusal)
    try:
        results = await conn.apply(previews)
    except Exception as exc:
        return error_partial(request, friendly(exc, _TRY_AGAIN))
    failed = next((r for r in results if not r.ok), None)
    if not results or failed is not None:
        what = f"Couldn't {'suspend' if want_suspend else 'unsuspend'} {email}."
        return write_failed(request, what, failed) if failed is not None else error_partial(request, what)
    request.app.state.gamgui.patch_user(email, suspended=want_suspend)
    return TEMPLATES.TemplateResponse(
        request, "_suspend_zone.html", {"email": email, "suspended": want_suspend}
    )
