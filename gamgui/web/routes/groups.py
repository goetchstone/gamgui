"""The Groups board (/groups): find a group, see its members and their roles, add someone by name or
email with a role, and remove a member after a confirm step (plan U7, A6).

Every list here is capped server-side: the group finder and the people type-ahead return the top
matches from the cached directory, never the whole of it, and a group's members come a page at a time.

The add field is free text, and GAM reads the member as a <UserTypeEntity>: ``oauthuser`` is the
authorizing admin, a bare name becomes name@<domain> and a comma adds two people. So a group or member
that isn't one full address is refused in words before GAM (``looks_like_email``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import GROUP_ROLES
from ...core.onboarding import looks_like_email
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, error_partial, friendly

PICK_LIMIT = 15      # group and people suggestions shown at once — keep typing to narrow
MEMBERS_PAGE = 50    # member rows per page; the list scrolls inside the fixed window
_ROLE_RANK = {"OWNER": 0, "MANAGER": 1, "MEMBER": 2}

_GROUPS_PAGE = "groups.html"
_LIST = "_board_members.html"

router = APIRouter(prefix="/groups")


def _pick(items, q: str, *fields) -> tuple[list, bool]:
    """The first PICK_LIMIT of ``items`` whose ``fields`` contain ``q`` (case-insensitive), and whether
    there were more."""
    ql = q.strip().lower()
    hits = [it for it in items if not ql or any(ql in (getattr(it, f, "") or "").lower() for f in fields)]
    return hits[:PICK_LIMIT], len(hits) > PICK_LIMIT


async def _group_choices(st, q: str = "") -> dict:
    try:
        groups = await st.groups()   # cached `gam print groups`, shared with onboarding's picker
    except Exception as exc:  # noqa: BLE001 — any failure is shown in words, not a 500
        return {"groups": [], "more": False, "find_q": q, "find_error": "Couldn't list groups. " + friendly(exc)}
    shown, more = _pick(groups, q, "email", "name")
    return {"groups": shown, "more": more, "find_q": q, "find_error": ""}


async def _list_ctx(conn, group: str, q: str = "", page: int = 1, **extra) -> dict:
    """What ``_board_members.html`` shows: one page of ``group``'s members matching ``q``, owners and
    managers first. ``extra`` carries a write's result (message/error/details); ``focus=False`` keeps an
    operator's click from moving focus into the list (an add leaves it in the add form)."""
    ctx: dict = {"group": group, "q": q, "members": [], "total": 0, "page": 1, "pages": 1, "start": 0,
                 "message": "", "error": "", "details": "", "focus": True, **extra}
    try:
        members = await conn.list_group_members(group)
    except Exception as exc:  # noqa: BLE001 — a failed write's own message outranks the re-read's
        ctx["error"] = ctx["error"] or friendly(exc)
        ctx["unread"] = True
        return ctx
    ql = q.strip().lower()
    rows = sorted((m for m in members if not ql or ql in m.email.lower() or ql in m.role.lower()),
                  key=lambda m: (_ROLE_RANK.get(m.role, 3), m.email.lower()))
    pages = max(1, -(-len(rows) // MEMBERS_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * MEMBERS_PAGE
    ctx.update(members=rows[start:start + MEMBERS_PAGE], total=len(rows), all_count=len(members),
               page=page, pages=pages, start=start)
    return ctx


async def _panel_ctx(st, group: str) -> dict:
    names = {g.email.lower(): g.name for g in await _cached_groups(st)}
    return {"group": group, "group_name": names.get(group.lower(), ""), "roles": GROUP_ROLES,
            **await _list_ctx(st.connector, group)}


async def _cached_groups(st) -> list:
    try:
        return await st.groups()
    except Exception:  # noqa: BLE001 — only the heading's display name comes from here
        return []


def _list(request: Request, ctx: dict) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _LIST, ctx)


@router.get("", response_class=HTMLResponse)
async def board(request: Request, group: str = "") -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE, {"connected": False})
    group = group.strip()
    panel = await _panel_ctx(st, group) if group else {"group": ""}
    return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE,
                                      {"connected": True, "selected": group, **await _group_choices(st), **panel})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", selected: str = "") -> HTMLResponse:
    """The group finder: the top matches by address or name from the cached group list."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_group_results.html",
                                          {"groups": [], "more": False, "find_q": q, "find_error": NOT_CONNECTED})
    ctx = await _group_choices(st, q)
    return TEMPLATES.TemplateResponse(request, "_group_results.html", {**ctx, "selected": selected})


@router.get("/people", response_class=HTMLResponse)
async def people(request: Request, q: str = "") -> HTMLResponse:
    """The add-member type-ahead: the top matches by address or name from the cached directory."""
    st = app_state(request)
    try:
        users = await st.users()
    except Exception:  # noqa: BLE001 — a directory hiccup just yields no suggestions; typing still works
        users = []
    shown, more = _pick(users, q, "primary_email", "full_name")
    return TEMPLATES.TemplateResponse(request, "_group_people.html", {"people": shown, "more": more})


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, group: str = "") -> HTMLResponse:
    """A picked group's panel: its name, the add-member form, and the first page of members."""
    st = app_state(request)
    group = group.strip()
    if st.connector is None or not group:
        return TEMPLATES.TemplateResponse(request, "_group_panel.html",
                                          {"group": "", "error": NOT_CONNECTED if st.connector is None else ""})
    return TEMPLATES.TemplateResponse(request, "_group_panel.html", await _panel_ctx(st, group))


@router.get("/members/list", response_class=HTMLResponse)
async def member_list(request: Request, group: str = "", q: str = "", page: int = 1) -> HTMLResponse:
    conn, group = app_state(request).connector, group.strip()
    if conn is None or not group:
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a group first."})
    return _list(request, await _list_ctx(conn, group, q, page))


@router.post("/members", response_class=HTMLResponse)
async def add_member(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "member",
    q: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add one person (or group) with a role — a single-target LOW write, so no confirm step."""
    conn = app_state(request).connector
    group, email, role = group.strip(), email.strip(), (role or "member").strip().lower()
    if conn is None or not looks_like_email(group):
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a group first."})
    if not email:
        return _list(request, await _list_ctx(conn, group, q, error="Enter the email address of the person to add."))
    if not looks_like_email(email):
        return _list(request, await _list_ctx(conn, group, q, error=(
            f"“{email}” isn't one email address. Pick someone from the suggestions, or type their full address.")))
    try:
        result = await conn.add_group_member(group, email, role=role)
    except ValueError:   # GAMCommands refused the role before anything ran
        return _list(request, await _list_ctx(conn, group, q, error="Pick a role: member, manager or owner."))
    if not result.ok:
        return _list(request, await _list_ctx(conn, group, q, details=result.detail,
                                              error=f"Couldn't add {email} to {group}. {result.remediation}".strip()))
    return _list(request, await _list_ctx(conn, group, q, message=f"Added {email} to {group} as {role}.", focus=False))


@router.post("/members/remove/preview", response_class=HTMLResponse)
async def remove_preview(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    page: Annotated[int, Form()] = 1,
) -> HTMLResponse:
    """The confirm step for a removal: it names who leaves which group, and writes nothing."""
    if not looks_like_email(group) or not looks_like_email(email):
        return error_partial(request, "Pick a member to remove.")
    return TEMPLATES.TemplateResponse(request, "_group_remove_confirm.html", {
        "group": group.strip(), "email": email.strip(), "role": role.strip().lower(), "q": q, "page": page})


@router.post("/members/remove", response_class=HTMLResponse)
async def remove_member(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    page: Annotated[int, Form()] = 1,
) -> HTMLResponse:
    """Remove one member — only from its confirm step (guard.enforce, confirm_step)."""
    conn = app_state(request).connector
    group, email = group.strip(), email.strip()
    if conn is None or not looks_like_email(group) or not looks_like_email(email):
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a member to remove."})
    refusal = guard.enforce(guard.changes([email], RiskLevel.LOW, f"Remove from {group}"), await request.form(),
                            confirm_step=True)
    if refusal:
        return _list(request, await _list_ctx(conn, group, q, page, error=refusal))
    result = await conn.remove_group_member(group, email)
    if not result.ok:
        return _list(request, await _list_ctx(conn, group, q, page, details=result.detail,
                                              error=f"Couldn't remove {email} from {group}. {result.remediation}".strip()))
    return _list(request, await _list_ctx(conn, group, q, page, message=f"Removed {email} from {group}."))
