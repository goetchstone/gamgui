"""Reports / insights routes (read-only)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import clock
from ...core import reports as reports_mod
from ...core.gam.errors import GAMError, GAMErrorKind
from ..jobs import start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, as_of, friendly

router = APIRouter(prefix="/reports")

_REPORTS_PAGE = "reports.html"
_USAGE_REPORT_PARTIAL = "_usage_report.html"
_CONNECTED_APPS_PARTIAL = "_connected_apps.html"
_NAMES_SHOWN = 6   # who granted an app, before "and N more"
_READ_TIMED_OUT = ("GAM reads one account at a time and didn't finish in the hour it's allowed, so nothing was kept. "
                   "Trying again won't help on a domain this size.")


@router.get("", response_class=HTMLResponse)
async def reports_page(request: Request, refresh: int = 0) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _REPORTS_PAGE, {"connected": False, "reports": []})
    try:
        # The shared cache (CACHE_FIELDS covers REPORT_FIELDS); the header says how old it is.
        users = await st.users(force=bool(refresh), stale_ok=True)
    except Exception as exc:
        msg = friendly(exc, "Couldn't load users.")
        return TEMPLATES.TemplateResponse(
            request, _REPORTS_PAGE, {"connected": True, "reports": [], "error": msg, "total": 0}
        )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORTS_PAGE,
        {"connected": True, "reports": reports_mod.build_reports(users), "total": len(users), **as_of(st.user_cache)},
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


def _connected_apps_response(request: Request, job=None, announce: bool = False) -> HTMLResponse:
    """``announce`` only for a change the operator is waiting on: this panel arrives after the page loads, so
    app.js would speak a finished read's summary again on every visit to Reports."""
    st = request.app.state.gamgui
    ctx = {"connected": st.connector is not None, "job": job, "announce": announce, "names_shown": _NAMES_SHOWN}
    if st.connected_apps is not None:
        read, when = st.connected_apps
        ctx.update(read=read, read_age=clock.now() - when)
    return TEMPLATES.TemplateResponse(request, _CONNECTED_APPS_PARTIAL, ctx)


@router.get("/connected-apps", response_class=HTMLResponse)
async def connected_apps(request: Request, job: str = "") -> HTMLResponse:
    """The last read, a running one, or the button. Never reads by itself: one API call per account."""
    st = request.app.state.gamgui
    asked = st.jobs.get(job) if job else None
    if asked is not None and asked.kind != "connected_apps":
        asked = None
    # A finished read that isn't the latest says nothing about what's shown: its failure was another tenant's, or
    # was overtaken. It has its own line in the tray.
    if asked is not None and asked.finished and asked.id != st.connected_apps_job_id:
        asked = None
    return _connected_apps_response(request, asked or st.jobs.get(st.connected_apps_job_id), announce=asked is not None)


async def _read_connected_apps(job, st, conn) -> None:
    """Background: read every account's apps, kept only while ``conn`` is still the connected tenant's."""
    try:
        job.current = "Reading every account's apps…"
        read = await conn.connected_apps()
        if st.connector is conn:
            st.connected_apps = (read, clock.now())
            job.applied = read.app_count
        else:
            job.error = "Not kept: the connection changed during the read. Read again."
    except Exception as exc:  # noqa: BLE001 — surface any failure in the panel
        timed_out = isinstance(exc, GAMError) and exc.kind is GAMErrorKind.TIMEOUT
        job.error = _READ_TIMED_OUT if timed_out else friendly(exc)
    finally:
        job.finish()


@router.post("/connected-apps/read", response_class=HTMLResponse)
async def connected_apps_read(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _connected_apps_response(request)
    existing = st.jobs.get(st.connected_apps_job_id)
    if existing is not None and not existing.finished:  # don't start a second domain-wide read
        return _connected_apps_response(request, existing, announce=True)
    job = start_job(st.jobs, 0, kind="connected_apps", title="Connected apps read")
    st.connected_apps_job_id = job.id
    job.task = asyncio.create_task(_read_connected_apps(job, st, st.connector))
    return _connected_apps_response(request, job, announce=True)
