"""Signature designer routes: scoped template -> preview -> apply."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import signatures as sig
from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/signatures")


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


async def _matched(st, users, scope_type: str, scope_value: str):
    """Resolve the in-scope active users — group scope needs a GAM lookup, the rest is in-memory."""
    if scope_type == "group" and scope_value:
        try:
            members = await st.connector.list_group_members(scope_value)
        except Exception:
            return []
        emails = {m.email for m in members}
        return [u for u in users if u.primary_email in emails and not u.suspended]
    return sig.match_scope(users, scope_type, scope_value)


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "signatures.html", {"connected": False})
    try:
        users = await st.users()
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, "signatures.html",
            {"connected": True, "error": _friendly(exc), "options": {"ous": [], "departments": []}, "groups": [], "variables": sig.VARIABLES},
        )
    return TEMPLATES.TemplateResponse(
        request, "signatures.html",
        {"connected": True, "options": sig.scope_options(users), "groups": [g.email for g in groups], "variables": sig.VARIABLES},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request, template: str = Form(""), scope_type: str = Form("company"), scope_value: str = Form("")
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_sig_preview.html", {"error": "Not connected."})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, "_sig_preview.html", {"error": _friendly(exc)})
    matched = await _matched(st, users, scope_type, scope_value)
    sample = matched[0] if matched else None
    return TEMPLATES.TemplateResponse(
        request, "_sig_preview.html",
        {"rendered": sig.render_signature(template, sample) if sample else "", "count": len(matched), "sample": sample},
    )


@router.post("/apply", response_class=HTMLResponse)
async def apply(
    request: Request, template: str = Form(""), scope_type: str = Form("company"), scope_value: str = Form("")
) -> HTMLResponse:
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": "Not connected."})
    try:
        users = await request.app.state.gamgui.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": _friendly(exc)})
    matched = await _matched(request.app.state.gamgui, users, scope_type, scope_value)
    applied, failed = 0, []
    for u in matched:
        result = await conn.set_signature(u.primary_email, sig.render_signature(template, u), html=True)
        if result.ok:
            applied += 1
        else:
            failed.append(u.primary_email)
    return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"applied": applied, "total": len(matched), "failed": failed})
