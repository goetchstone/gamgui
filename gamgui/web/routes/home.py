"""Home (plan U4): a dashboard that fits the window and makes no directory call of its own.

The counts come from the directory cache as it is — any age, labelled — and a cold cache shows a Load
control instead of holding the page on ``gam print users``. Jobs come from the tray's registry and
failures from the local audit log. The only ``gam`` on load is ``gam version``, which is local — and
held on AppState (``gam_version``) until the binary changes or setup activates a tenant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import islice, takewhile
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import reports as reports_mod
from ...core.audit import iter_records
from ..jobs import tray
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, as_of, friendly
from .audit import _audit_path

router = APIRouter()

HOME_JOBS = 4        # the tray lists more; Home shows the newest few (running first)
HOME_FAILURES = 3    # the last few failed writes; Audit has the rest
HOME_FAILURE_DAYS = 30   # "recent": June's already-fixed failures once headlined Home in September


def _directory(st, users) -> Dict[str, Any]:
    """The Directory card's context from a user list (None: nothing cached — the card offers Load)."""
    if users is None:
        return {"loaded": False}
    groups = st.group_cache.cached
    return {
        "loaded": True, "total": len(users), "suspended": sum(1 for u in users if u.suspended),
        "groups": None if groups is None else len(groups),
        "reports": reports_mod.build_reports(users), **as_of(st.user_cache),
    }


def recent_failures(request: Request, n: int = HOME_FAILURES, days: int = HOME_FAILURE_DAYS) -> List[Dict[str, Any]]:
    """The newest ``n`` failed audit records from the last ``days``, newest first. The log streams
    newest-first, so reading stops at the first record older than the window (or once ``n`` are found)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = takewhile(lambda r: str(r.get("ts") or "") >= cutoff, iter_records(_audit_path(request)))
    return list(islice((r for r in recent if r.get("ok") is False), n))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    version = await st.gam_version()
    configured = st.vault.has_credentials(st.audit_domain) if st.audit_domain else False
    rows, running, _ = tray(st.jobs)
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "gam_version": version,
        "binary_present": st.runner.binary_exists(),
        "configured": configured,
        "connected": st.connector is not None,
        "jobs": rows[:HOME_JOBS], "running": running, "jobs_more": max(0, len(st.jobs) - HOME_JOBS),
        "failures": recent_failures(request),
        **_directory(st, st.user_cache.cached),
    })


@router.get("/home/directory", response_class=HTMLResponse)
async def directory(request: Request, refresh: int = 0) -> HTMLResponse:
    """The Directory card's Load / Refresh: one ``gam print users`` (or none, when a fresh list is cached)."""
    st = request.app.state.gamgui
    ctx: Dict[str, Any] = {"connected": st.connector is not None, "loaded": False}
    if st.connector is None:
        ctx["error"] = NOT_CONNECTED
    else:
        try:
            ctx.update(_directory(st, await st.users(force=bool(refresh), stale_ok=True)))
        except Exception as exc:
            ctx["error"] = friendly(exc, "Couldn't load the directory.")
    return TEMPLATES.TemplateResponse(request, "_home_directory.html", ctx)
