"""Onboarding runbooks (/onboard).

Define role → task-list templates (editable, persisted locally), a per-role signature template + OU +
groups + shared calendars, and a welcome-email template. Generating for a new hire can **create the
Google account** (a one-time temp password on a printable sheet, or — when a notify address is given —
delivered by GAM/Google straight to the hire; never audited in the clear), apply the role's signature,
add the hire to the role's groups, subscribe them to its shared calendars, fire the setup checklist to
the assignee's Google Tasks, and send the welcome email. A **CSV of hires** runs the same per-hire
provisioning (``core.onboarding.provision_hire``) as a polled job. Every mutation goes through the guarded, audited connector writes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...core import clock, guard, onboarding
from ...core.bulk import stop_reason, stop_requested
from ...core.connectors.base import RiskLevel
from ...core.gam.errors import GAMErrorKind
from ...core.onboarding import RoleTemplate, RunbookStore
from ..jobs import Job, register_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state, as_of, error_partial, signature_store

router = APIRouter(prefix="/onboard")

_CREDS_TTL = 15 * 60   # keep the bulk credentials sheet re-fetchable for 15 min, or until Done
_MAX_CSV_BYTES = 1024 * 1024   # a hire list is kilobytes; refuse a huge upload before decoding it


def _store(request: Request) -> RunbookStore:
    st = app_state(request)
    if st.runbooks is None:
        st.runbooks = RunbookStore()
    return st.runbooks


@dataclass
class OnboardJob(Job):
    """Polled progress for a bulk CSV run: the bounded ``Job`` feed (one row per hire, never the temp
    password) plus the account tallies, and ``credentials`` — the printable-sheet rows, rendered once,
    in the final panel, not on every poll."""
    account_created: int = 0
    notified: int = 0
    credentials: List[dict] = field(default_factory=list)  # printable-sheet rows (blank-notify accounts)
    finishing: str = ""   # the FinishJob retrying steps Google wasn't ready for on the new accounts

    def record_hire(self, res: dict) -> None:
        notes = list(res.get("errors") or [])
        if res.get("waiting"):
            notes.append(onboarding.waiting_note(res["waiting"]))
        self.record(res.get("email") or res.get("name") or "?", bool(res.get("ok")), "; ".join(notes))
        if res.get("account_created"):
            self.account_created += 1
        if res.get("notified"):
            self.notified += 1
        if res.get("credential"):
            self.credentials.append(res["credential"])


@dataclass
class FinishJob(Job):
    """The steps Google wasn't ready for on just-created accounts (``core.onboarding.finish_new_accounts``),
    retried in the background: one feed row per step. ``next_try_at`` is when the next round starts;
    ``minutes`` is how long it keeps trying, as its panel says."""
    next_try_at: float = 0.0
    minutes: int = 0

    @property
    def next_try_in(self) -> int:
        return max(0, int(self.next_try_at - clock.now() + 0.999)) if self.next_try_at else 0


def start_finishing(jobs: dict, conn, waiting: List[Dict[str, Any]]) -> FinishJob:
    """Register a FinishJob for ``waiting`` and start it. These are writes the run's preview already
    held and ``guard.enforce`` passed — the same argv, retried, never a new change."""
    accounts = sorted({w["email"] for w in waiting})
    title = ("Finishing setup for " + accounts[0]) if len(accounts) == 1 else \
        "Finishing setup for {} new accounts".format(len(accounts))
    minutes = max(1, round(sum(onboarding.NEW_ACCOUNT_WAITS) / 60))
    job = register_job(jobs, FinishJob(total=len(waiting), title=title, kind="finish", minutes=minutes))
    job.task = asyncio.create_task(_run_finish(job, conn, waiting))
    return job


async def _run_finish(job: FinishJob, conn, waiting: List[Dict[str, Any]],
                      waits: Optional[Tuple[float, ...]] = None) -> None:
    """Background executor for a FinishJob. Never raises out; Stop ends it at once, even mid-wait."""
    async def nap(seconds: float) -> None:
        job.next_try_at = clock.now() + seconds
        for _ in range(int(seconds) + 1):   # a second at a time, so Stop ends the wait at once
            if job.cancel_requested or clock.now() >= job.next_try_at:
                break
            await asyncio.sleep(min(1.0, job.next_try_at - clock.now()))
        job.next_try_at = 0.0

    def report(step: Dict[str, Any], ok: bool, reason: str, detail: str) -> None:
        job.record(onboarding.step_label(step), ok, reason, detail)

    try:
        await onboarding.finish_new_accounts(conn, waiting, report, stopped=lambda: job.cancel_requested,
                                             nap=nap, waits=onboarding.NEW_ACCOUNT_WAITS if waits is None else waits)
        if job.cancel_requested:
            job.error = "Stopped by you."
    except Exception as exc:  # noqa: BLE001 — a loop-level failure shouldn't wedge the job
        job.error = str(exc)
    finally:
        job.finish()


async def _run_bulk_onboard(job: OnboardJob, conn, sig_store, store,
                            pairs: List[Tuple[dict, RoleTemplate]], jobs: dict) -> None:
    """Background executor: onboard each (row, role template) pair. Never raises out. Stops when an
    account create fails for a reason every later hire would share (``stop_reason``: sign-in expired,
    a scope missing); a best-effort sub-step's failure (a group, a calendar, the task list) never stops
    it, since the accounts themselves may still be created. No license left fails only the rows that
    create an account: the later ones are skipped untried, the rest still run. Stop ends it between
    hires, never mid-hire. The steps Google wasn't ready for on the new accounts go on to one FinishJob
    in ``jobs`` once every hire has run — by then the first accounts have had a head start — but not
    after a stop of any kind: those are listed instead."""
    waiting: List[Dict[str, Any]] = []
    no_license = ""
    quitting = False
    try:
        for hire, cfg in pairs:
            if stop_requested(job):
                break
            if cfg is None or not cfg.steps:
                job.record_hire({"email": hire.get("email"), "name": hire.get("name") or hire.get("email"),
                                 "role": hire["role"], "ok": False,
                                 "errors": ["unknown role or role has no steps"]})
                continue
            if no_license and hire.get("create_account"):
                job.record_hire({"email": hire.get("email"), "name": hire.get("name") or hire.get("email"),
                                 "role": hire["role"], "ok": False, "errors": ["not attempted: " + no_license]})
                continue
            res = await onboarding.provision_hire(conn, sig_store, store, cfg, hire)
            job.record_hire(res)
            waiting += res.get("waiting") or []
            kind, why = res.get("stop") or (None, "")
            if kind is GAMErrorKind.LICENSE_LIMIT:
                no_license = why
                continue
            stop = stop_reason(kind, why, job.total - job.done)
            if stop:
                job.error = stop
                break
    except asyncio.CancelledError:
        quitting = True   # the app is quitting (server._lifespan): start nothing new
        raise
    except Exception as exc:  # noqa: BLE001 — a loop-level failure shouldn't wedge the job
        job.error = str(exc)
    finally:
        job.finish()
        if waiting and job.error:
            n = len(waiting)
            job.error += (" {} step{} left waiting for Google to set up the new accounts {} not retried — "
                          "set {} from the person's page or Calendars.").format(
                              n, "" if n == 1 else "s", "was" if n == 1 else "were", "it" if n == 1 else "them")
        elif waiting and not quitting:
            job.finishing = start_finishing(jobs, conn, waiting).id


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "onboarding.html", {"connected": False})
    store = _store(request)
    return TEMPLATES.TemplateResponse(request, "onboarding.html", {
        "connected": True, "roles": store.roles(), **_welcome_ctx(store),
        "vars": onboarding.WELCOME_VARS, "sig_templates": signature_store(request).names(),
    })


def _welcome_ctx(store: RunbookStore) -> dict:
    """The named welcome emails, for the Welcome email tab and the role editor's picker."""
    names = store.welcome_names()
    return {"welcomes": [(n, store.welcome(n)) for n in names],
            "welcome_names": names, "default_welcome": onboarding.DEFAULT_WELCOME}


def _welcome_panel(request: Request, store: RunbookStore, saved: str = "", error: str = "") -> HTMLResponse:
    """The Welcome email tab, plus the role editor's picker out of band (a new name must be pickable)."""
    return TEMPLATES.TemplateResponse(request, "_onboard_welcome.html", {
        **_welcome_ctx(store), "vars": onboarding.WELCOME_VARS, "saved": saved, "error": error, "picker_oob": True})


@router.post("/role", response_class=HTMLResponse)
async def save_role(request: Request, name: Annotated[str, Form()], steps: Annotated[str, Form()] = "",
                    signature: Annotated[str, Form()] = "", org_unit: Annotated[str, Form()] = "",
                    groups: Annotated[str, Form()] = "", calendars: Annotated[str, Form()] = "",
                    welcome: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    try:
        store.set_role(name, steps.splitlines(), signature=signature, org_unit=org_unit,
                       groups=groups.splitlines(), calendars=calendars.splitlines(), welcome=welcome)
    except ValueError as exc:
        return error_partial(request, str(exc))
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/role/delete", response_class=HTMLResponse)
async def delete_role(request: Request, name: Annotated[str, Form()]) -> HTMLResponse:
    store = _store(request)
    store.delete_role(name)
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/welcome", response_class=HTMLResponse)
async def save_welcome(request: Request, subject: Annotated[str, Form()] = "", body: Annotated[str, Form()] = "",
                       name: Annotated[str, Form()] = onboarding.DEFAULT_WELCOME) -> HTMLResponse:
    """Save a named welcome email (a new name adds one; an existing name replaces it). Local only."""
    store = _store(request)
    try:
        saved = store.set_welcome(subject, body, name=name)
    except ValueError as exc:
        return _welcome_panel(request, store, error=str(exc))
    return _welcome_panel(request, store, saved=saved)


@router.post("/welcome/delete", response_class=HTMLResponse)
async def delete_welcome(request: Request, name: Annotated[str, Form()]) -> HTMLResponse:
    """Delete a named welcome email: never Default, never one a role still picks. Local only."""
    store = _store(request)
    try:
        store.delete_welcome(name.strip())
    except ValueError as exc:
        return _welcome_panel(request, store, error=str(exc))
    return _welcome_panel(request, store)


# Run executes the hire its preview showed: the form's values, the role template and the welcome email
# as they were at Preview, held under a single-use token (web/previews.py). Run once posted the live
# form, so ticking "Create the Google account" after a tasks-only preview created an account nobody
# had previewed (failure-log 2026-09-23).
_FLOW = "onboard"
_BULK_FLOW = "onboard_bulk"


def _csv_key(text: str) -> str:
    """The CSV as the preview holds it and Run compares it: LF line endings, no blank edge lines. The
    page posts it back from a <textarea>, whose value a browser rewrites (CRLF and CR become LF, a
    leading newline is dropped) — so a CSV saved by Excel or Sheets never matched its own preview."""
    return "\n".join(text.splitlines()).strip("\n")


def _form_key(role: str, name: str, email: str, manager: str, assignee: str, send_welcome: str,
              create_account: str, first: str, last: str) -> tuple:
    return (role, name.strip(), email.strip().lower(), manager.strip().lower(), assignee.strip().lower(),
            bool(send_welcome), bool(create_account), first.strip(), last.strip())


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, role: Annotated[str, Form()], name: Annotated[str, Form()] = "",
                  email: Annotated[str, Form()] = "", manager: Annotated[str, Form()] = "",
                  assignee: Annotated[str, Form()] = "", send_welcome: Annotated[str, Form()] = "",
                  create_account: Annotated[str, Form()] = "", first: Annotated[str, Form()] = "",
                  last: Annotated[str, Form()] = "") -> HTMLResponse:
    tenant = app_state(request).tenant_key()   # the tenant this preview is bound to (web/previews.py)
    if email.strip() and not onboarding.looks_like_email(email):
        return error_partial(request, "That does not look like a valid email address for the new hire.")
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return error_partial(request, "That role has no steps yet — add some in Role templates.")
    w, ctx = store.welcome_for(cfg), onboarding.welcome_context(name, email, role, manager)
    if w is None:
        if send_welcome:
            return error_partial(request, f"The role “{role}” sends the welcome email “{cfg.welcome}”, which no "
                                          f"longer exists. Pick another for the role in Role templates.")
        w = {"subject": "", "body": ""}   # not sent: nothing to show or hold
    given, family = onboarding.split_name(name, first, last)
    hire = {"role": role, "name": name, "email": email.strip(), "manager": manager, "assignee": assignee,
            "create_account": bool(create_account), "first": first, "last": last,
            "send_welcome": bool(send_welcome), "notify": "", "welcome": w}
    token = app_state(request).previews.hold(
        _FLOW, _form_key(role, name, email, manager, assignee, send_welcome, create_account, first, last),
        (hire, cfg), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_onboard_preview.html", {
        "token": token,
        "role": role, "steps": cfg.steps, "assignee": (assignee or email).strip(), "name": name, "email": email,
        "manager": manager, "send_welcome": bool(send_welcome),
        "create_account": bool(create_account), "first": given, "last": family,
        "org_unit": cfg.org_unit or "/", "signature": cfg.signature,
        "groups": cfg.groups, "calendars": cfg.calendars,
        "subject": onboarding.render(w["subject"], ctx), "body": onboarding.render(w["body"], ctx),
        "welcome_name": cfg.welcome_name,
    })


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, role: Annotated[str, Form()], name: Annotated[str, Form()] = "",
              email: Annotated[str, Form()] = "", manager: Annotated[str, Form()] = "",
              assignee: Annotated[str, Form()] = "", send_welcome: Annotated[str, Form()] = "",
              create_account: Annotated[str, Form()] = "", first: Annotated[str, Form()] = "",
              last: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    previewed = _form_key(role, name, email, manager, assignee, send_welcome, create_account, first, last)
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return error_partial(request, "That role has no steps.")
    email = email.strip()
    if email and not onboarding.looks_like_email(email):
        return error_partial(request, "That does not look like a valid email address for the new hire.")
    make_account = bool(create_account)

    # Validate everything that does NOT write BEFORE any mutation, so a bad assignee (or any later
    # step) can't strand a just-created account's one-time password (it exists nowhere else).
    if make_account:
        if not email:
            return error_partial(request, "Enter the new hire's email to create the account.")
        given, family = onboarding.split_name(name, first, last)
        if not given or not family:
            return error_partial(request, "Enter the new hire's first and last name to create the account.")
    assignee = assignee.strip() or email
    if not assignee:
        return error_partial(request, "Enter the assignee (who does the setup) or the new hire's email.")
    if not onboarding.looks_like_email(assignee):
        return error_partial(request, "The assignee does not look like a valid email address.")
    # Every run writes (a task list at least; maybe an account): only via the preview's Run button.
    form = await request.form()
    refusal = guard.enforce(guard.changes([email or assignee], RiskLevel.LOW, "Onboard"), form, confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    # ...and only the hire that preview showed: a used or expired preview, or a form edited since
    # (an account ticked, an address retyped), is refused rather than run.
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), previewed, again="click Preview again")
    if refusal:
        return error_partial(request, refusal)

    # Delegate the actual provisioning to the shared per-hire path — ONE implementation for the single
    # and bulk flows (their divergence is exactly what stranded a temp password before). The single
    # flow never uses `notify` (always the printable sheet) and turns a hard failure into an error page
    # while the bulk feed shows it as a per-hire row.
    hire, cfg = held   # the previewed hire, role template and welcome email — never re-read from the store
    res = await onboarding.provision_hire(conn, signature_store(request), store, cfg, hire)
    if make_account and not res["account_created"]:
        detail = next((e[len("create: "):] for e in res["errors"] if e.startswith("create:")), "unknown error")
        return error_partial(request, "Couldn't create the account: " + detail)

    # Map the structured result onto the single-hire result panel's contract.
    credentials = {**res["credential"], "signature": res["signature"],
                   "signature_failed": "signature: not applied" in res["errors"]} if res["credential"] else None
    memberships = None
    if res["groups"] or res["calendars"]:
        g = res["groups"] or {"added": 0, "total": 0, "failed": []}
        c = res["calendars"] or {"added": 0, "total": 0, "failed": []}
        memberships = {"groups_added": g["added"], "groups_total": g["total"], "groups_failed": g["failed"],
                       "cals_added": c["added"], "cals_total": c["total"], "cals_failed": c["failed"],
                       "cals_waiting": c.get("waiting", [])}
    result = res["tasklist"] or {"tasklist_id": "", "created": 0, "total": len(cfg.steps), "failed": []}
    tl_err = next((e[len("tasks: "):] for e in res["errors"] if e.startswith("tasks:")), "")
    if tl_err and not result.get("tasklist_id"):
        result = {**result, "error": tl_err}
    if not credentials and not memberships and tl_err and not result.get("tasklist_id"):
        return error_partial(request, "Couldn't create the task list: " + tl_err)   # nothing else ran -> error page
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    finishing = start_finishing(st.jobs, conn, res["waiting"]) if res["waiting"] else None
    return TEMPLATES.TemplateResponse(request, "_onboard_run.html", {
        "result": result, "assignee": assignee, "title": title, "email_sent": res["email_sent"],
        "email": email, "credentials": credentials, "memberships": memberships, "finishing": finishing,
        "waiting": {w["what"] for w in res["waiting"]},
    })


# --- bulk CSV import ---------------------------------------------------------------------

@router.get("/bulk/template.csv", response_class=PlainTextResponse)
async def bulk_template() -> PlainTextResponse:
    return PlainTextResponse(
        onboarding.HIRE_CSV_TEMPLATE, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=onboard_template.csv"})


@router.post("/bulk/preview", response_class=HTMLResponse)
async def bulk_preview(request: Request, csv_file: Annotated[UploadFile, File()]) -> HTMLResponse:
    tenant = app_state(request).tenant_key()   # the tenant this preview is bound to (web/previews.py)
    if app_state(request).connector is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        data = await csv_file.read(_MAX_CSV_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        return error_partial(request, "Couldn't read the file: " + str(exc))
    if len(data) > _MAX_CSV_BYTES:
        return error_partial(request, "That file is over 1 MB — a hire list should be far smaller. Split it into "
                             "several CSVs, or check you picked the right file.")
    text = _csv_key(data.decode("utf-8-sig", errors="replace"))
    rows, parse_errors = onboarding.parse_hire_csv(text)
    if not rows and not parse_errors:
        return error_partial(request, "No hires found in the CSV.")
    pairs, row_errors = onboarding.resolve_hires(rows, _store(request))
    # Run executes these rows with these role templates — not a re-parse of whatever comes back, nor a
    # role edited after the preview — under a single-use token, like every other confirm step.
    token = app_state(request).previews.hold(_BULK_FLOW, text, pairs, tenant=tenant) if pairs else ""
    return TEMPLATES.TemplateResponse(request, "_onboard_bulk_preview.html", {
        "summary": onboarding.tally_hires(pairs), "errors": parse_errors + row_errors,
        "csv_text": text, "can_run": bool(pairs), "token": token,
    })


@router.post("/bulk/run", response_class=HTMLResponse)
async def bulk_run(request: Request, csv_text: Annotated[str, Form()]) -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    rows, _errs = onboarding.parse_hire_csv(csv_text)
    resolved, _row_errors = onboarding.resolve_hires(rows, _store(request))
    # Bulk creation is gated behind the preview, like the single flow.
    previews = guard.changes([h.get("email") or h.get("name") or "" for h, _cfg in resolved], RiskLevel.LOW, "Onboard")
    form = await request.form()
    refusal = guard.enforce(previews, form, confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    held, refusal = st.previews.take(_BULK_FLOW, str(form.get(TOKEN_FIELD) or ""), _csv_key(csv_text),
                                     again="upload the CSV and preview it again", what="CSV")
    if refusal:
        return error_partial(request, refusal)
    pairs = held
    if not pairs:
        return error_partial(request, "Nothing to run — every row had an unknown role or was invalid.")
    n = len(pairs)
    job = register_job(st.jobs, OnboardJob(total=n, kind="onboard",
                                           title=f"Bulk onboarding — {n} hire{'s' if n != 1 else ''}"))
    job.task = asyncio.create_task(
        _run_bulk_onboard(job, conn, signature_store(request), _store(request), pairs, jobs=st.jobs))
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": job, "credentials": None})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --- pickers: search the tenant's groups + shared calendars while editing a role -----------

@router.get("/search/groups", response_class=HTMLResponse)
async def search_groups(request: Request, q: str = "", refresh: int = 0) -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_onboard_picker.html",
                                          {"field": "groups", "items": [], "error": NOT_CONNECTED})
    try:
        groups = await st.groups(force=bool(refresh), stale_ok=True)   # cached; the picker says how old
    except Exception as exc:  # noqa: BLE001
        return TEMPLATES.TemplateResponse(request, "_onboard_picker.html",
                                          {"field": "groups", "items": [], "error": "Couldn't list groups: " + str(exc)})
    ql = q.strip().lower()
    items = []
    for g in groups:
        if not ql or ql in g.email.lower() or ql in (g.name or "").lower():
            items.append({"value": g.email, "label": g.name or g.email, "sub": g.email if g.name else ""})
        if len(items) >= 15:
            break
    return TEMPLATES.TemplateResponse(request, "_onboard_picker.html",
                                      {"field": "groups", "items": items, "error": None, "q": q,
                                       **as_of(st.group_cache)})


@router.get("/search/calendars", response_class=HTMLResponse)
async def search_calendars(request: Request, q: str = "") -> HTMLResponse:
    st = app_state(request)
    idx = st.calendar_index
    status = idx.status() if idx is not None else None
    # Serve only an index that has rows AND belongs to the connected tenant (mirrors calendars._index_ready).
    if status is None or status.count == 0 or status.domain != st.audit_domain:
        return TEMPLATES.TemplateResponse(request, "_onboard_picker.html", {
            "field": "calendars", "items": [],
            "error": "No calendar index yet — build it on the Calendars screen, then search here."})
    items = [{"value": c.id, "label": c.summary or c.id, "sub": c.id if c.summary else ""}
             for c in idx.search(q, limit=15)]
    return TEMPLATES.TemplateResponse(request, "_onboard_picker.html",
                                      {"field": "calendars", "items": items, "error": None})


@router.get("/bulk/status", response_class=HTMLResponse)
async def bulk_status(request: Request, job: str = "") -> HTMLResponse:
    j = app_state(request).jobs.get(job) if job else None
    if not isinstance(j, OnboardJob):   # st.jobs is shared; another feature's BatchJob isn't ours
        j = None
    # Credentials sheet: serve it while the job is finished and within _CREDS_TTL of finishing, then
    # drop the plaintext (an explicit "Done" clears it sooner). A GET is bookmarkable/re-fetchable, so
    # `no-store` keeps it out of the browser cache; the TTL keeps one refresh from losing every
    # password at once (the old drop-on-first-render did exactly that).
    creds = None
    if j is not None and j.finished and j.credentials:
        if clock.now() - j.finished_at < _CREDS_TTL:
            creds = j.credentials
        else:
            j.credentials = []
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": j, "credentials": creds})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/finish/status", response_class=HTMLResponse)
async def finish_status(request: Request, job: str = "") -> HTMLResponse:
    j = app_state(request).jobs.get(job) if job else None
    if not isinstance(j, FinishJob):   # st.jobs is shared; another feature's job isn't ours
        j = None
    return TEMPLATES.TemplateResponse(request, "_onboard_finish.html", {"job": j})


@router.post("/bulk/done", response_class=HTMLResponse)
async def bulk_done(request: Request, job: Annotated[str, Form()] = "") -> HTMLResponse:
    j = app_state(request).jobs.get(job) if job else None
    if not isinstance(j, OnboardJob):
        j = None
    elif j.credentials:
        j.credentials = []   # operator confirmed they printed/saved the sheet — drop the plaintext now
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": j, "credentials": None})
    resp.headers["Cache-Control"] = "no-store"
    return resp
