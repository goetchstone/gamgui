"""Signature designer routes: scoped template -> preview -> apply (with live progress)."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Annotated, List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import signatures as sig
from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/signatures")


@dataclass
class ApplyJob:
    """In-memory progress for one bulk signature apply, polled by the UI."""

    id: str
    total: int
    applied: int = 0
    done: int = 0
    failed: List[str] = field(default_factory=list)
    current: str = ""
    finished: bool = False
    error: Optional[str] = None
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run


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


def _prune_jobs(st, keep: int = 10) -> None:
    """Drop the oldest finished jobs so the registry can't grow without bound."""
    finished = [jid for jid, j in st.jobs.items() if j.finished]
    for jid in finished[:-keep] if len(finished) > keep else []:
        st.jobs.pop(jid, None)


async def _run_apply(job: ApplyJob, conn, matched, template: str) -> None:
    """Background task: set each user's signature, updating ``job`` as it goes."""
    try:
        for u in matched:
            job.current = u.primary_email
            try:
                result = await conn.set_signature(u.primary_email, sig.render_signature(template, u), html=True)
                ok = bool(getattr(result, "ok", False))
            except Exception:
                ok = False
            if ok:
                job.applied += 1
            else:
                job.failed.append(u.primary_email)
            job.done += 1
    except Exception as exc:  # whole-batch failure (e.g. auth expired mid-run)
        job.error = _friendly(exc)
    finally:
        job.current = ""
        job.finished = True


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
            {"connected": True, "error": _friendly(exc), "options": {"ous": [], "departments": [], "locations": [], "users": []}, "groups": [], "variables": sig.VARIABLES},
        )
    return TEMPLATES.TemplateResponse(
        request, "signatures.html",
        {"connected": True, "options": sig.scope_options(users), "groups": [g.email for g in groups], "variables": sig.VARIABLES},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "company",
    scope_value: Annotated[str, Form()] = "",
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
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "company",
    scope_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": "Not connected."})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": _friendly(exc)})
    matched = await _matched(st, users, scope_type, scope_value)
    if not matched:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": "No active users match this scope."})

    # Run the (potentially minutes-long) per-user loop in the background and report progress by polling,
    # so the UI never looks frozen on a large apply.
    job = ApplyJob(id=secrets.token_urlsafe(8), total=len(matched))
    _prune_jobs(st)
    st.jobs[job.id] = job
    job.task = asyncio.create_task(_run_apply(job, st.connector, matched, template))
    return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"job": job})


@router.get("/apply/status", response_class=HTMLResponse)
async def apply_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job)
    if j is None:
        return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"error": "That apply job is no longer available — re-run apply."})
    return TEMPLATES.TemplateResponse(request, "_sig_apply.html", {"job": j})
