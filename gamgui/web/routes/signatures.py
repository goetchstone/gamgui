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
from ...core.signatures import SignatureStore
from ..server import TEMPLATES

router = APIRouter(prefix="/signatures")

_SIGNATURES_PAGE = "signatures.html"
_PREVIEW_PARTIAL = "_sig_preview.html"
_APPLY_PARTIAL = "_sig_apply.html"
_TEMPLATES_PARTIAL = "_sig_templates.html"


def _store(request: Request) -> SignatureStore:
    """The saved-template store, lazily created on first use (real ~/Library file unless a test
    pre-seeds ``st.sig_templates`` with a store pointed at a tmp path)."""
    st = request.app.state.gamgui
    if st.sig_templates is None:
        st.sig_templates = SignatureStore()
    return st.sig_templates


def _tctx(store: SignatureStore, **extra) -> dict:
    """Context for ``_sig_templates.html``: each template as {name, body}, plus any saved/error flag."""
    ctx: dict = {"templates": [{"name": n, "body": store.get(n)} for n in store.names()]}
    ctx.update(extra)
    return ctx


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
        return TEMPLATES.TemplateResponse(request, _SIGNATURES_PAGE, {"connected": False})
    try:
        users = await st.users()
        groups = await st.connector.list_groups()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request, _SIGNATURES_PAGE,
            {"connected": True, "error": _friendly(exc), "options": {"ous": [], "departments": [], "locations": [], "users": []}, "groups": [], "variables": sig.VARIABLES},
        )
    return TEMPLATES.TemplateResponse(
        request, _SIGNATURES_PAGE,
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
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": "Not connected."})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": _friendly(exc)})
    matched = await _matched(st, users, scope_type, scope_value)
    sample = matched[0] if matched else None
    return TEMPLATES.TemplateResponse(
        request, _PREVIEW_PARTIAL,
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
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "Not connected."})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": _friendly(exc)})
    matched = await _matched(st, users, scope_type, scope_value)
    if not matched:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "No active users match this scope."})

    # Run the (potentially minutes-long) per-user loop in the background and report progress by polling,
    # so the UI never looks frozen on a large apply.
    job = ApplyJob(id=secrets.token_urlsafe(8), total=len(matched))
    _prune_jobs(st)
    st.jobs[job.id] = job
    job.task = asyncio.create_task(_run_apply(job, st.connector, matched, template))
    return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"job": job})


@router.get("/apply/status", response_class=HTMLResponse)
async def apply_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job)
    if j is None:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "That apply job is no longer available — re-run apply."})
    return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"job": j})


# --- saved templates: load / save-as / delete ----------------------------------------------------

@router.get("/templates", response_class=HTMLResponse)
async def list_templates(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(_store(request)))


@router.post("/templates/save", response_class=HTMLResponse)
async def save_template(
    request: Request,
    name: Annotated[str, Form()] = "",
    template: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # `template` rides in via hx-include="#sig-form" — the CURRENT editor content — while `name`
    # comes from the save form's own input.
    store = _store(request)
    try:
        store.save(name, template)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, error=str(exc)))
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, saved=name.strip()))


@router.post("/templates/delete", response_class=HTMLResponse)
async def delete_template(request: Request, name: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    store.delete(name)
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store))
