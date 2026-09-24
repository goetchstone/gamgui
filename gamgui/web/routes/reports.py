"""Reports / insights routes (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import reports as reports_mod
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, friendly

router = APIRouter(prefix="/reports")

_REPORTS_PAGE = "reports.html"
_USAGE_REPORT_PARTIAL = "_usage_report.html"


@router.get("", response_class=HTMLResponse)
async def reports_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _REPORTS_PAGE, {"connected": False, "reports": []})
    try:
        users = await st.users()  # shared cache (CACHE_FIELDS superset covers REPORT_FIELDS)
    except Exception as exc:
        msg = friendly(exc, "Couldn't load users.")
        return TEMPLATES.TemplateResponse(
            request, _REPORTS_PAGE, {"connected": True, "reports": [], "error": msg, "total": 0}
        )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORTS_PAGE,
        {"connected": True, "reports": reports_mod.build_reports(users), "total": len(users)},
    )


@router.get("/usage", response_class=HTMLResponse)
async def usage(request: Request) -> HTMLResponse:
    """Lazy-loaded storage/mail usage (a separate, slower Reports-API call)."""
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": NOT_CONNECTED})
    try:
        data = await conn.usage_report(reports_mod.USAGE_PARAMS)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": friendly(exc, "Couldn't load report data.")})
    rows = reports_mod.parse_usage(data["rows"])[:25]
    return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": rows, "date": data["date"]})
