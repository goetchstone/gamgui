"""Onboarding runbooks (/onboard).

Define role → task-list templates (editable, persisted locally), a per-role signature template + OU +
groups + shared calendars, and a welcome-email template. Generating for a new hire can **create the
Google account** (a one-time temp password on a printable sheet, or — when a notify address is given —
delivered by GAM/Google straight to the hire; never audited in the clear), apply the role's signature,
add the hire to the role's groups, subscribe them to its shared calendars, fire the setup checklist to
the assignee's Google Tasks, and send the welcome email. A **CSV of hires** runs the same per-hire
provisioning as a polled BatchJob. Every mutation goes through the guarded, audited connector writes.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated, List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...core import guard, onboarding
from ...core import signatures as sig
from ...core.connectors.base import RiskLevel
from ...core.gam.models import GAMUser
from ...core.onboarding import RunbookStore
from ...core.signatures import SignatureStore
from ..jobs import register_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES

router = APIRouter(prefix="/onboard")

# Live-feed bounds (invariant #9 — bound anything polled). The status poll renders only the last
# _RECENT_WINDOW per-hire rows and a capped failure sample; the credentials sheet is rendered once, in
# the final panel, not on every poll.
_RECENT_WINDOW = 12
_FAILED_SAMPLE_CAP = 200
_CREDS_TTL = 15 * 60   # keep the bulk credentials sheet re-fetchable for 15 min, or until Done
_MAX_CSV_BYTES = 1024 * 1024   # a hire list is kilobytes; refuse a huge upload before decoding it


def _st(request: Request):
    return request.app.state.gamgui


def _store(request: Request) -> RunbookStore:
    st = _st(request)
    if st.runbooks is None:
        st.runbooks = RunbookStore()
    return st.runbooks


def _sig_store(request: Request) -> SignatureStore:
    st = _st(request)
    if st.sig_templates is None:
        st.sig_templates = SignatureStore()
    return st.sig_templates


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


def _ctx(name: str, email: str, role: str, manager: str) -> dict:
    return {"name": name, "email": email, "role": role, "manager": manager}


def _split_name(name: str, first: str, last: str) -> "tuple[str, str]":
    """Prefer explicit first/last; otherwise split the display name on the first space."""
    first, last = first.strip(), last.strip()
    if not first and not last and name.strip():
        parts = name.strip().split()
        first = parts[0]
        last = " ".join(parts[1:])
    return first, last


# --- shared per-hire provisioning steps (used by the single /run and the bulk executor) ---

async def _apply_signature(conn, sig_store, cfg, email: str, first: str, last: str) -> Optional[str]:
    """Apply the role's signature template to ``email``; return the template name if applied, else None."""
    if not cfg.signature:
        return None
    body = sig_store.get(cfg.signature)
    if not body:
        return None
    user = GAMUser(primary_email=email, given_name=first, family_name=last)
    try:
        r = await conn.set_signature(email, sig.render_signature(body, user), html=True)
        return cfg.signature if r.ok else None
    except Exception:  # noqa: BLE001 — signature is best-effort
        return None


async def _apply_groups(conn, email: str, groups: List[str]) -> "tuple[int, list]":
    ok, failed = 0, []
    for g in groups:
        try:
            r = await conn.add_group_member(g, email)
            if r.ok:
                ok += 1
            else:
                failed.append(g)
        except Exception:  # noqa: BLE001
            failed.append(g)
    return ok, failed


async def _apply_calendars(conn, email: str, calendars: List[str]) -> "tuple[int, list]":
    ok, failed = 0, []
    for c in calendars:
        try:
            r = await conn.subscribe_calendar_for(email, c)
            if r.ok:
                ok += 1
            else:
                failed.append(c)
        except Exception:  # noqa: BLE001
            failed.append(c)
    return ok, failed


async def _provision_hire(conn, sig_store, store, cfg, hire: dict) -> dict:
    """Run one hire's onboarding end-to-end and return a structured result (no HTML, never raises).

    ``hire`` is a parsed CSV row; ``cfg`` its resolved role template. Best-effort per sub-step. When an
    account is created, a blank ``notify`` puts the temp password on ``credential`` (printable sheet);
    a ``notify`` email hands sign-in delivery to GAM/Google and only sets ``notified``."""
    email = (hire.get("email") or "").strip()
    name = (hire.get("name") or "").strip()
    first, last = _split_name(name, hire.get("first", ""), hire.get("last", ""))
    res = {"email": email, "name": name or (first + " " + last).strip() or email, "role": hire["role"],
           "ok": True, "account_created": False, "notified": False, "credential": None,
           "signature": None, "groups": None, "calendars": None, "tasklist": None,
           "email_sent": None, "errors": []}

    if hire.get("create_account"):
        if not email:
            res["ok"] = False; res["errors"].append("no email to create the account"); return res
        if not first or not last:
            res["ok"] = False; res["errors"].append("need a first & last name to create the account"); return res
        notify = (hire.get("notify") or "").strip() or None
        pw = onboarding.generate_temp_password()
        try:
            cr = await conn.create_user(email, first, last, pw, change_password=True,
                                        org_unit=(cfg.org_unit or None), notify=notify)
        except Exception as exc:  # noqa: BLE001
            res["ok"] = False; res["errors"].append("create: " + str(getattr(exc, "remediation", exc))); return res
        if not cr.ok:
            res["ok"] = False; res["errors"].append("create: " + (cr.detail or "failed")); return res
        res["account_created"] = True
        if notify:
            res["notified"] = True          # GAM emailed the sign-in info; the pw lives only in that email
        else:
            res["credential"] = {"name": res["name"], "email": email, "password": pw,
                                 "org_unit": cfg.org_unit or "/"}

    if email:
        # Signature only for an account THIS run created — matches the single /run flow and never
        # clobbers an existing user's customized signature (or renders a blank {name} for an
        # existing-account row that has no name in the CSV).
        if res["account_created"]:
            res["signature"] = await _apply_signature(conn, sig_store, cfg, email, first, last)
            if cfg.signature and not res["signature"]:
                res["errors"].append("signature: not applied")
        if cfg.groups:
            g_ok, g_fail = await _apply_groups(conn, email, cfg.groups)
            res["groups"] = {"added": g_ok, "total": len(cfg.groups), "failed": g_fail}
            if g_fail:
                res["errors"].append("groups: couldn't add " + ", ".join(g_fail))
        if cfg.calendars:
            c_ok, c_fail = await _apply_calendars(conn, email, cfg.calendars)
            res["calendars"] = {"added": c_ok, "total": len(cfg.calendars), "failed": c_fail}
            if c_fail:
                res["errors"].append("calendars: couldn't subscribe " + ", ".join(c_fail))

    assignee = (hire.get("assignee") or email).strip()
    if assignee:
        title = "Onboard {} — {}".format(name or email or "new hire", hire["role"])
        try:
            tl = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
            res["tasklist"] = {"assignee": assignee, "tasklist_id": tl.get("tasklist_id", ""),
                               "created": tl.get("created"), "total": tl.get("total"),
                               "failed": tl.get("failed", []), "ok": bool(tl.get("tasklist_id"))}
            if not tl.get("tasklist_id"):
                res["errors"].append("tasks: no tasklist id came back")
        except Exception as exc:  # noqa: BLE001
            res["errors"].append("tasks: " + str(getattr(exc, "remediation", exc)))

    if hire.get("send_welcome") and email:
        # The single flow holds the welcome template its preview rendered; a bulk row uses the store's.
        w, ctx = hire.get("welcome") or store.welcome(), _ctx(name, email, hire["role"], hire.get("manager", ""))
        try:
            we = await conn.send_welcome_email(email, onboarding.render(w["subject"], ctx),
                                               onboarding.render(w["body"], ctx))
            res["email_sent"] = bool(we.ok)
        except Exception:  # noqa: BLE001
            res["email_sent"] = False
    if res["email_sent"] is False:
        res["errors"].append("welcome email: failed to send")
    # A hire is only "ok" if every best-effort sub-step also succeeded (not just the create).
    res["ok"] = not res["errors"]
    return res


@dataclass
class OnboardJob:
    """Polled progress for a bulk CSV run. The live feed (``recent``, ``failed``) is bounded per
    invariant #9; ``credentials`` accumulates the printable-sheet rows and is rendered once, in the
    final panel, not on every poll."""
    id: str
    total: int
    done: int = 0
    ok: int = 0
    account_created: int = 0
    notified: int = 0
    failed_total: int = 0
    failed: List[str] = field(default_factory=list)      # capped sample of "email — reason"
    recent: List[dict] = field(default_factory=list)     # capped feed of per-hire result dicts
    credentials: List[dict] = field(default_factory=list)  # printable-sheet rows (blank-notify accounts)
    finished: bool = False
    finished_at: float = 0.0
    error: Optional[str] = None
    task: object = field(default=None, repr=False)

    def record(self, res: dict) -> None:
        self.done += 1
        if res.get("ok"):
            self.ok += 1
        else:
            self.failed_total += 1
            if len(self.failed) < _FAILED_SAMPLE_CAP:
                self.failed.append("{} — {}".format(res.get("email") or "?",
                                                     "; ".join(res.get("errors") or ["failed"])))
        if res.get("account_created"):
            self.account_created += 1
        if res.get("notified"):
            self.notified += 1
        if res.get("credential"):
            self.credentials.append(res["credential"])
        # The live feed shows only email/name/ok/errors — never keep the temp password here
        # (it lives, briefly, only in `credentials` for the printable sheet).
        self.recent.append({k: res.get(k) for k in ("email", "name", "ok", "errors")})
        del self.recent[:-_RECENT_WINDOW]


async def _run_bulk_onboard(job: OnboardJob, conn, sig_store, store, rows: List[dict], cfgs: dict) -> None:
    """Background executor: onboard each parsed row via its role cfg. Never raises out."""
    try:
        for hire in rows:
            cfg = cfgs.get(hire["role"])
            if cfg is None or not cfg.steps:
                job.record({"email": hire.get("email"), "name": hire.get("name") or hire.get("email"),
                            "role": hire["role"], "ok": False,
                            "errors": ["unknown role or role has no steps"]})
                continue
            job.record(await _provision_hire(conn, sig_store, store, cfg, hire))
    except Exception as exc:  # noqa: BLE001 — a loop-level failure shouldn't wedge the job
        job.error = str(exc)
    finally:
        job.finished = True
        job.finished_at = time.monotonic()


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "onboarding.html", {"connected": False})
    store = _store(request)
    return TEMPLATES.TemplateResponse(request, "onboarding.html", {
        "connected": True, "roles": store.roles(), "welcome": store.welcome(),
        "vars": onboarding.WELCOME_VARS, "sig_templates": _sig_store(request).names(),
    })


@router.post("/role", response_class=HTMLResponse)
async def save_role(request: Request, name: Annotated[str, Form()], steps: Annotated[str, Form()] = "",
                    signature: Annotated[str, Form()] = "", org_unit: Annotated[str, Form()] = "",
                    groups: Annotated[str, Form()] = "", calendars: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    try:
        store.set_role(name, steps.splitlines(), signature=signature, org_unit=org_unit,
                       groups=groups.splitlines(), calendars=calendars.splitlines())
    except ValueError as exc:
        return _err(request, str(exc))
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/role/delete", response_class=HTMLResponse)
async def delete_role(request: Request, name: Annotated[str, Form()]) -> HTMLResponse:
    store = _store(request)
    store.delete_role(name)
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/welcome", response_class=HTMLResponse)
async def save_welcome(request: Request, subject: Annotated[str, Form()] = "", body: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    store.set_welcome(subject, body)
    return TEMPLATES.TemplateResponse(request, "_onboard_welcome.html",
                                      {"welcome": store.welcome(), "vars": onboarding.WELCOME_VARS, "saved": True})


# Run executes the hire its preview showed: the form's values, the role template and the welcome email
# as they were at Preview, held under a single-use token (web/previews.py). Run once posted the live
# form, so ticking "Create the Google account" after a tasks-only preview created an account nobody
# had previewed (failure-log 2026-09-23).
_FLOW = "onboard"


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
    if email.strip() and not onboarding.looks_like_email(email):
        return _err(request, "That does not look like a valid email address for the new hire.")
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return _err(request, "That role has no steps yet — add some in Role templates.")
    w, ctx = store.welcome(), _ctx(name, email, role, manager)
    f, l = _split_name(name, first, last)
    hire = {"role": role, "name": name, "email": email.strip(), "manager": manager, "assignee": assignee,
            "create_account": bool(create_account), "first": first, "last": last,
            "send_welcome": bool(send_welcome), "notify": "", "welcome": w}
    token = _st(request).previews.hold(
        _FLOW, _form_key(role, name, email, manager, assignee, send_welcome, create_account, first, last),
        (hire, cfg))
    return TEMPLATES.TemplateResponse(request, "_onboard_preview.html", {
        "token": token,
        "role": role, "steps": cfg.steps, "assignee": (assignee or email).strip(), "name": name, "email": email,
        "manager": manager, "send_welcome": bool(send_welcome),
        "create_account": bool(create_account), "first": f, "last": l,
        "org_unit": cfg.org_unit or "/", "signature": cfg.signature,
        "groups": cfg.groups, "calendars": cfg.calendars,
        "subject": onboarding.render(w["subject"], ctx), "body": onboarding.render(w["body"], ctx),
    })


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, role: Annotated[str, Form()], name: Annotated[str, Form()] = "",
              email: Annotated[str, Form()] = "", manager: Annotated[str, Form()] = "",
              assignee: Annotated[str, Form()] = "", send_welcome: Annotated[str, Form()] = "",
              create_account: Annotated[str, Form()] = "", first: Annotated[str, Form()] = "",
              last: Annotated[str, Form()] = "") -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    previewed = _form_key(role, name, email, manager, assignee, send_welcome, create_account, first, last)
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return _err(request, "That role has no steps.")
    email = email.strip()
    if email and not onboarding.looks_like_email(email):
        return _err(request, "That does not look like a valid email address for the new hire.")
    make_account = bool(create_account)

    # Validate everything that does NOT write BEFORE any mutation, so a bad assignee (or any later
    # step) can't strand a just-created account's one-time password (it exists nowhere else).
    f = l = ""
    if make_account:
        if not email:
            return _err(request, "Enter the new hire's email to create the account.")
        f, l = _split_name(name, first, last)
        if not f or not l:
            return _err(request, "Enter the new hire's first and last name to create the account.")
    assignee = assignee.strip() or email
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")
    if not onboarding.looks_like_email(assignee):
        return _err(request, "The assignee does not look like a valid email address.")
    # Every run writes (a task list at least; maybe an account): only via the preview's Run button.
    form = await request.form()
    refusal = guard.enforce(guard.changes([email or assignee], RiskLevel.LOW, "Onboard"), form, confirm_step=True)
    if refusal:
        return _err(request, refusal)
    # ...and only the hire that preview showed: a used or expired preview, or a form edited since
    # (an account ticked, an address retyped), is refused rather than run.
    held, refusal = st.previews.take(_FLOW, str(form.get(TOKEN_FIELD) or ""), previewed, again="click Preview again")
    if refusal:
        return _err(request, refusal)

    # Delegate the actual provisioning to the shared per-hire path — ONE implementation for the single
    # and bulk flows (their divergence is exactly what stranded a temp password before). The single
    # flow never uses `notify` (always the printable sheet) and turns a hard failure into an error page
    # while the bulk feed shows it as a per-hire row.
    hire, cfg = held   # the previewed hire, role template and welcome email — never re-read from the store
    res = await _provision_hire(conn, _sig_store(request), store, cfg, hire)
    if make_account and not res["account_created"]:
        detail = next((e[len("create: "):] for e in res["errors"] if e.startswith("create:")), "unknown error")
        return _err(request, "Couldn't create the account: " + detail)

    # Map the structured result onto the single-hire result panel's contract.
    credentials = {**res["credential"], "signature": res["signature"]} if res["credential"] else None
    memberships = None
    if res["groups"] or res["calendars"]:
        g = res["groups"] or {"added": 0, "total": 0, "failed": []}
        c = res["calendars"] or {"added": 0, "total": 0, "failed": []}
        memberships = {"groups_added": g["added"], "groups_total": g["total"], "groups_failed": g["failed"],
                       "cals_added": c["added"], "cals_total": c["total"], "cals_failed": c["failed"]}
    result = res["tasklist"] or {"tasklist_id": "", "created": 0, "total": len(cfg.steps), "failed": []}
    tl_err = next((e[len("tasks: "):] for e in res["errors"] if e.startswith("tasks:")), "")
    if tl_err and not result.get("tasklist_id"):
        result = {**result, "error": tl_err}
    if not credentials and not memberships and tl_err and not result.get("tasklist_id"):
        return _err(request, "Couldn't create the task list: " + tl_err)   # nothing else ran -> error page
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    return TEMPLATES.TemplateResponse(request, "_onboard_run.html", {
        "result": result, "assignee": assignee, "title": title, "email_sent": res["email_sent"],
        "email": email, "credentials": credentials, "memberships": memberships,
    })


# --- bulk CSV import ---------------------------------------------------------------------

@router.get("/bulk/template.csv", response_class=PlainTextResponse)
async def bulk_template() -> PlainTextResponse:
    return PlainTextResponse(
        onboarding.HIRE_CSV_TEMPLATE, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=onboard_template.csv"})


def _bulk_summary(rows: List[dict], store: RunbookStore):
    """Resolve each row's role and tally what a run would do. Returns (valid_rows, role_cfgs, summary,
    row_errors). A row whose role is unknown or has no steps becomes a row error, not a valid row."""
    cfgs: dict = {}
    valid: List[dict] = []
    row_errors: List[str] = []
    per_role: dict = {}
    creates = notifies = sheets = 0
    for hire in rows:
        role = hire["role"]
        if role not in cfgs:
            cfgs[role] = store.role(role)
        cfg = cfgs[role]
        if cfg is None or not cfg.steps:
            who = hire.get("email") or hire.get("name") or "a row"
            row_errors.append("{}: unknown role '{}' (or it has no steps).".format(who, role))
            continue
        valid.append(hire)
        per_role[role] = per_role.get(role, 0) + 1
        if hire["create_account"]:
            creates += 1
            if (hire.get("notify") or "").strip():
                notifies += 1
            else:
                sheets += 1
    summary = {"total": len(valid), "creates": creates, "notifies": notifies, "sheets": sheets,
               "per_role": sorted(per_role.items())}
    return valid, cfgs, summary, row_errors


@router.post("/bulk/preview", response_class=HTMLResponse)
async def bulk_preview(request: Request, csv_file: Annotated[UploadFile, File()]) -> HTMLResponse:
    if _st(request).connector is None:
        return _err(request, "Not connected.")
    try:
        data = await csv_file.read(_MAX_CSV_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        return _err(request, "Couldn't read the file: " + str(exc))
    if len(data) > _MAX_CSV_BYTES:
        return _err(request, "That file is over 1 MB — a hire list should be far smaller. Split it into "
                             "several CSVs, or check you picked the right file.")
    text = data.decode("utf-8-sig", errors="replace")
    rows, parse_errors = onboarding.parse_hire_csv(text)
    if not rows and not parse_errors:
        return _err(request, "No hires found in the CSV.")
    valid, _cfgs, summary, row_errors = _bulk_summary(rows, _store(request))
    return TEMPLATES.TemplateResponse(request, "_onboard_bulk_preview.html", {
        "summary": summary, "errors": parse_errors + row_errors,
        "csv_text": text, "can_run": bool(valid),
    })


@router.post("/bulk/run", response_class=HTMLResponse)
async def bulk_run(request: Request, csv_text: Annotated[str, Form()]) -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    rows, _errs = onboarding.parse_hire_csv(csv_text)
    valid, cfgs, _summary, _row_errors = _bulk_summary(rows, _store(request))
    # Bulk creation is gated behind the preview, like the single flow.
    previews = guard.changes([h.get("email") or h.get("name") or "" for h in valid], RiskLevel.LOW, "Onboard")
    refusal = guard.enforce(previews, await request.form(), confirm_step=True)
    if refusal:
        return _err(request, refusal)
    if not valid:
        return _err(request, "Nothing to run — every row had an unknown role or was invalid.")
    job = register_job(st.jobs, OnboardJob(id=secrets.token_urlsafe(8), total=len(valid)))
    job.task = asyncio.create_task(
        _run_bulk_onboard(job, conn, _sig_store(request), _store(request), valid, cfgs))
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": job, "credentials": None})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --- pickers: search the tenant's groups + shared calendars while editing a role -----------

@router.get("/search/groups", response_class=HTMLResponse)
async def search_groups(request: Request, q: str = "") -> HTMLResponse:
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_onboard_picker.html",
                                          {"field": "groups", "items": [], "error": "Not connected."})
    try:
        groups = await st.groups()   # cached `gam print groups`
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
                                      {"field": "groups", "items": items, "error": None})


@router.get("/search/calendars", response_class=HTMLResponse)
async def search_calendars(request: Request, q: str = "") -> HTMLResponse:
    st = _st(request)
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
    j = _st(request).jobs.get(job) if job else None
    if not isinstance(j, OnboardJob):   # st.jobs is shared; another feature's BatchJob isn't ours
        j = None
    # Credentials sheet: serve it while the job is finished and within _CREDS_TTL of finishing, then
    # drop the plaintext (an explicit "Done" clears it sooner). A GET is bookmarkable/re-fetchable, so
    # `no-store` keeps it out of the browser cache; the TTL keeps one refresh from losing every
    # password at once (the old drop-on-first-render did exactly that).
    creds = None
    if j is not None and j.finished and j.credentials:
        if time.monotonic() - j.finished_at < _CREDS_TTL:
            creds = j.credentials
        else:
            j.credentials = []
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": j, "credentials": creds})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/bulk/done", response_class=HTMLResponse)
async def bulk_done(request: Request, job: Annotated[str, Form()] = "") -> HTMLResponse:
    j = _st(request).jobs.get(job) if job else None
    if not isinstance(j, OnboardJob):
        j = None
    elif j.credentials:
        j.credentials = []   # operator confirmed they printed/saved the sheet — drop the plaintext now
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": j, "credentials": None})
    resp.headers["Cache-Control"] = "no-store"
    return resp
