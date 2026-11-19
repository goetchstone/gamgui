"""Reports / insights routes (read-only)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import bulk, clock, guard
from ...core import reports as reports_mod
from ...core.catalog.catalog import load_catalog
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.setup import admin_token_scopes
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, as_of, error_partial, friendly

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
    named = _named_reports(st)
    try:
        # The shared cache (CACHE_FIELDS covers REPORT_FIELDS); the header says how old it is.
        users = await st.users(force=bool(refresh), stale_ok=True)
    except Exception as exc:
        msg = friendly(exc, "Couldn't load users.")
        return TEMPLATES.TemplateResponse(
            request, _REPORTS_PAGE, {"connected": True, "reports": [], "error": msg, "total": 0, "named": named}
        )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORTS_PAGE,
        {"connected": True, "reports": reports_mod.build_reports(users), "total": len(users), "named": named,
         **as_of(st.user_cache)},
    )


def _named_reports(st) -> list:
    """Roadmap 3.1: each named read, the Builder command it opens (None when a GAM bump lost it), and whether the admin
    token holds its scope (None: a service-account read, or a token whose scopes can't be read)."""
    if st.catalog is None:
        st.catalog = load_catalog()
    granted = admin_token_scopes(st.vault, st.audit_domain) if st.vault is not None else None
    return [{"report": r, "cmd": reports_mod.named_report_command(st.catalog, r),
             "held": reports_mod.admin_scope_held(r, granted)} for r in reports_mod.NAMED_REPORTS]


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


# --- Inactive → suspend (roadmap 3.3, D3) --------------------------------------------------------------------------------
# The card's list comes from the cached directory, so the preview reads the directory again and holds the people that read
# found, under a single-use token (web/previews.py): Apply suspends exactly those, never a list rebuilt since. A new hire
# who hasn't signed in yet looks inactive, so each one made inside the window is flagged; the admin GamGUI signs in as is
# left out by name (the chokepoint refuses it whatever is sent).
_SUSPEND_FLOW = "suspend_inactive"
_SUSPEND_PARTIAL = "_inactive_suspend.html"
_SUSPEND_SHOWN = 200   # people the preview lists; the count is the whole of it (bounded page)
_ADMIN_UNCONFIRMED = ("GamGUI can't confirm which account it signs in as, so it can't leave that account out of a suspend. "
                      "Import the credentials again on Setup, then preview again.")


def _suspend_key(days) -> tuple:
    return ("inactive", str(days or "").strip())


@router.post("/inactive/preview", response_class=HTMLResponse)
async def inactive_preview(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    admin, admin_id, _why_not = await conn.connected_admin()
    if not admin:
        return error_partial(request, _ADMIN_UNCONFIRMED)
    try:
        users = await st.users(force=True)   # read now: the card counted a cached list
    except Exception as exc:  # noqa: BLE001 — a failed read is said, and nothing is offered
        return error_partial(request, "Couldn't read the directory again, so nothing is offered. " + friendly(exc))
    targets, left_out = reports_mod.inactive_to_suspend(users, admin, admin_id)
    emails = [t.email for t in targets]
    # Keyed on the window the preview used, posted back with Apply: a form edited since is refused, never re-read.
    token = st.previews.hold(_SUSPEND_FLOW, _suspend_key(reports_mod.INACTIVE_DAYS), emails, tenant=tenant) if emails else ""
    decision = guard.evaluate(conn.plan_suspend(emails), typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    return TEMPLATES.TemplateResponse(request, _SUSPEND_PARTIAL, {
        "targets": targets[:_SUSPEND_SHOWN], "count": len(targets), "new": sum(t.new for t in targets),
        "left_out": left_out, "days": reports_mod.INACTIVE_DAYS, "decision": decision, "token": token})


@router.post("/inactive/apply", response_class=HTMLResponse)
async def inactive_apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    emails, refusal = st.previews.take(_SUSPEND_FLOW, str(form.get(TOKEN_FIELD) or ""), _suspend_key(form.get("days")),
                                       again="click Suspend these… again")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(conn.plan_suspend(emails), form, confirm_step=True, typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return error_partial(request, refusal)
    n = len(emails)
    job = start_job(st.jobs, n, kind="suspend_inactive", title=f"Suspend {n} inactive account{'s' if n != 1 else ''}")
    job.task = asyncio.create_task(_run_suspend(job, st, conn, emails))
    return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"job": job})


async def _run_suspend(job, st, conn, emails) -> None:
    """The job's task: each suspend GAM accepted is patched into the cached directory as it lands. A failed write's outcome
    isn't known (a timeout may still have applied it), so any failure, or the loop dying, drops the whole list instead."""
    try:
        await bulk.suspend_users(job, conn, emails, on_set=lambda e: st.patch_user(e, suspended=True))
    finally:
        if job.failed_total or job.error:
            st.invalidate_users()


@router.get("/inactive/status", response_class=HTMLResponse)
async def inactive_status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != "suspend_inactive":
        return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"error": "That run is no longer here."})
    return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"job": found})
