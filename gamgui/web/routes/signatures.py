"""Signature designer routes: scoped template -> preview -> apply (with live progress)."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Annotated, List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core import signatures as sig
from ...core.connectors.base import RiskLevel
from ...core.signatures import SignatureStore
from ..jobs import stop_reason
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, friendly, signature_store

router = APIRouter(prefix="/signatures")

_SIGNATURES_PAGE = "signatures.html"
_PREVIEW_PARTIAL = "_sig_preview.html"
_APPLY_PARTIAL = "_sig_apply.html"
_TEMPLATES_PARTIAL = "_sig_templates.html"


def _tctx(store: SignatureStore, **extra) -> dict:
    """Context for ``_sig_templates.html``: each template as {name, body}, plus any saved/error flag."""
    ctx: dict = {"templates": [{"name": n, "body": store.get(n)} for n in store.names()]}
    ctx.update(extra)
    return ctx


@dataclass
class SigResult:
    """One user's outcome in a bulk apply — the unit of the live "as they get set" feed."""

    email: str
    ok: bool
    reason: str = ""   # a failure's remediation, in words (a short fixed text per GAMErrorKind)
    detail: str = ""   # a failure's raw GAM error, cut to _DETAIL_CAP


_RECENT_WINDOW = 12       # most-recent per-user results kept for the live feed (bounds the polled HTML)
_FAILED_SAMPLE_CAP = 200  # cap the retained failed list so a mostly-failing run can't bloat the poll
_DETAIL_CAP = 300         # chars of raw error kept per failure: GAM echoes the argv (the whole body) on a usage error


@dataclass
class ApplyJob:
    """In-memory progress for one bulk signature apply, polled by the UI."""

    id: str
    total: int
    applied: int = 0
    done: int = 0
    failed_total: int = 0
    failed: List[SigResult] = field(default_factory=list)   # capped sample of failures, with why (see _FAILED_SAMPLE_CAP)
    recent: List[SigResult] = field(default_factory=list)   # rolling window, newest last; drives the live feed
    current: str = ""
    finished: bool = False
    error: Optional[str] = None
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run

    def record(self, email: str, ok: bool, reason: str = "", detail: str = "") -> None:
        """Log one user's outcome: tallies, the capped failed sample, and the rolling live feed.

        Both the failed list and the feed are bounded — in length and, per failure, in the raw error
        kept — so the polled status partial stays small even on a domain-wide (thousands of users)
        apply; the feed shows only the most recent handful.
        """
        result = SigResult(email, ok, reason, detail[:_DETAIL_CAP])
        if ok:
            self.applied += 1
        else:
            self.failed_total += 1
            if len(self.failed) < _FAILED_SAMPLE_CAP:
                self.failed.append(result)
        self.recent.append(result)
        if len(self.recent) > _RECENT_WINDOW:
            del self.recent[0]
        self.done += 1


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


def _previews(matched):
    """What an apply changes, for the guard: it overwrites each matched signature (no backup)."""
    return guard.changes([u.primary_email for u in matched], RiskLevel.LOW, "Set signature")


# Apply writes what the preview showed: its template to the people it resolved, held under a single-use
# token (web/previews.py). Apply once re-resolved the live form, so a scope widened after Preview
# overwrote everyone it now matched while the button still said "Apply to 1 user".
_FLOW = "signatures"


def _form_key(template: str, scope_type: str, scope_value: str) -> tuple:
    return (template, scope_type, scope_value.strip())


def _prune_jobs(st, keep: int = 10) -> None:
    """Drop the oldest finished jobs so the registry can't grow without bound."""
    finished = [jid for jid, j in st.jobs.items() if j.finished]
    for jid in finished[:-keep] if len(finished) > keep else []:
        st.jobs.pop(jid, None)


async def _run_apply(job: ApplyJob, conn, matched, template: str) -> None:
    """Background task: set each user's signature, updating ``job`` as it goes. Stops at a failure
    every later user would share (``stop_reason``: sign-in expired, a scope missing)."""
    try:
        for u in matched:
            job.current = u.primary_email
            try:
                result = await conn.set_signature(u.primary_email, sig.render_signature(template, u), html=True)
                job.record(u.primary_email, result.ok, result.remediation, result.detail)
                kind, why = (None if result.ok else result.kind), result.remediation
            except Exception as exc:  # noqa: BLE001 — _run_write reports GAM's failures; this is anything else
                job.record(u.primary_email, False, friendly(exc), str(exc))
                kind, why = getattr(exc, "kind", None), friendly(exc)
            stop = stop_reason(kind, why, job.total - job.done)
            if stop:
                job.error = stop
                break
    except Exception as exc:  # whole-batch failure (e.g. auth expired mid-run)
        job.error = friendly(exc)
    finally:
        job.current = ""
        job.finished = True


def _test_user(st, options: dict) -> str:
    """Who "Specific user (test)" opens on: the connected admin's own address when it is an active
    user, else "" (a blank choice). It once opened on whoever sorted first — a colleague, whose live
    signature the page's default path (Preview, Apply) then overwrote."""
    try:
        admin = st.vault.oauth_admin_email(st.connector.domain) if st.vault is not None else ""
    except Exception:  # noqa: BLE001 — an unreadable credential just means "no default"
        admin = ""
    return next((e for e in options["users"] if admin and e.lower() == admin), "")


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
            {"connected": True, "error": friendly(exc), "options": {"ous": [], "departments": [], "locations": [], "users": []}, "groups": [], "variables": sig.VARIABLES, "test_user": ""},
        )
    options = sig.scope_options(users)
    return TEMPLATES.TemplateResponse(
        request, _SIGNATURES_PAGE,
        {"connected": True, "options": options, "groups": [g.email for g in groups], "variables": sig.VARIABLES,
         "test_user": _test_user(st, options)},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "user",
    scope_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": NOT_CONNECTED})
    try:
        users = await st.users()
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": friendly(exc)})
    matched = await _matched(st, users, scope_type, scope_value)
    sample = matched[0] if matched else None
    decision = guard.evaluate(_previews(matched), typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    token = (st.previews.hold(_FLOW, _form_key(template, scope_type, scope_value), (template, matched))
             if matched else "")
    return TEMPLATES.TemplateResponse(
        request, _PREVIEW_PARTIAL,
        {"rendered": sig.render_signature(template, sample) if sample else "", "count": len(matched),
         "sample": sample, "warning": sig.smart_quote_warning(template),
         "typed_count": decision.requires_typed_count, "token": token},
    )


@router.post("/apply", response_class=HTMLResponse)
async def apply(
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "user",
    scope_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": NOT_CONNECTED})
    form = await request.form()
    # The previewed template and people, or nothing: a used, expired or missing preview, or a scope or
    # template edited since, is refused rather than run on values the preview never showed.
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""),
                                     _form_key(template, scope_type, scope_value), again="click Preview again")
    if refusal:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": refusal})
    template, matched = held
    # Only via the preview's Apply button, and over COUNT_CONFIRM_ABOVE people with the previewed count typed.
    refusal = guard.enforce(_previews(matched), form, confirm_step=True, typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": refusal})

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
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(signature_store(request)))


@router.post("/templates/save", response_class=HTMLResponse)
async def save_template(
    request: Request,
    name: Annotated[str, Form()] = "",
    template: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # `template` rides in via hx-include="#sig-form" — the CURRENT editor content — while `name`
    # comes from the save form's own input.
    store = signature_store(request)
    try:
        store.save(name, template)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, error=str(exc)))
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, saved=name.strip()))


@router.post("/templates/delete", response_class=HTMLResponse)
async def delete_template(request: Request, name: Annotated[str, Form()] = "") -> HTMLResponse:
    store = signature_store(request)
    store.delete(name)
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store))
