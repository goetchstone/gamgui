"""Drag-and-drop group membership board (/groups)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..server import TEMPLATES
from ._common import NOT_CONNECTED, friendly

_GROUPS_PAGE = "groups.html"
_BOARD_MEMBERS_PARTIAL = "_board_members.html"

router = APIRouter(prefix="/groups")


@router.get("", response_class=HTMLResponse)
async def board(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE, {"connected": False})
    try:
        users = await st.users()
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, _GROUPS_PAGE, {"connected": True, "users": [], "groups": [], "error": friendly(exc)}
        )
    return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE, {"connected": True, "users": users, "groups": groups})


async def _members_partial(request: Request, conn, group: str, error: str = "", details: str = "") -> HTMLResponse:
    if not group:
        return TEMPLATES.TemplateResponse(request, _BOARD_MEMBERS_PARTIAL, {"group": "", "members": [], "empty": True})
    try:
        members = await conn.list_group_members(group)
    except Exception as exc:  # a failed add/remove's own message outranks the re-read's
        return TEMPLATES.TemplateResponse(
            request, _BOARD_MEMBERS_PARTIAL, {"group": group, "members": [], "error": error or friendly(exc), "details": details}
        )
    return TEMPLATES.TemplateResponse(
        request, _BOARD_MEMBERS_PARTIAL, {"group": group, "members": members, "error": error, "details": details}
    )


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, group: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _BOARD_MEMBERS_PARTIAL, {"group": group, "members": [], "error": NOT_CONNECTED})
    return await _members_partial(request, st.connector, group)


@router.post("/members", response_class=HTMLResponse)
async def members_mutate(
    request: Request,
    group: Annotated[str, Form()],
    email: Annotated[str, Form()],
    op: Annotated[str, Form()] = "add",
) -> HTMLResponse:
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, _BOARD_MEMBERS_PARTIAL, {"group": group, "members": [], "error": NOT_CONNECTED})
    removing = op == "remove"
    result = await (conn.remove_group_member(group, email) if removing else conn.add_group_member(group, email))
    if result.ok:
        return await _members_partial(request, conn, group)
    what = f"Couldn't remove {email} from {group}." if removing else f"Couldn't add {email} to {group}."
    return await _members_partial(request, conn, group, error=f"{what} {result.remediation}".strip(),
                                  details=result.detail)
