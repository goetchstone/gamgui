"""Drag-and-drop group membership board (/groups)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/groups")


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


@router.get("", response_class=HTMLResponse)
async def board(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "groups.html", {"connected": False})
    try:
        users = await st.users()
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, "groups.html", {"connected": True, "users": [], "groups": [], "error": _friendly(exc)}
        )
    return TEMPLATES.TemplateResponse(request, "groups.html", {"connected": True, "users": users, "groups": groups})


async def _members_partial(request: Request, conn, group: str, error: str = "") -> HTMLResponse:
    if not group:
        return TEMPLATES.TemplateResponse(request, "_board_members.html", {"group": "", "members": [], "empty": True})
    try:
        members = await conn.list_group_members(group)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, "_board_members.html", {"group": group, "members": [], "error": _friendly(exc)}
        )
    return TEMPLATES.TemplateResponse(
        request, "_board_members.html", {"group": group, "members": members, "error": error}
    )


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, group: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_board_members.html", {"group": group, "members": [], "error": "Not connected."})
    return await _members_partial(request, st.connector, group)


@router.post("/members", response_class=HTMLResponse)
async def members_mutate(
    request: Request, group: str = Form(...), email: str = Form(...), op: str = Form("add")
) -> HTMLResponse:
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, "_board_members.html", {"group": group, "members": [], "error": "Not connected."})
    result = await (conn.remove_group_member(group, email) if op == "remove" else conn.add_group_member(group, email))
    error = "" if result.ok else result.detail
    return await _members_partial(request, conn, group, error=error)
