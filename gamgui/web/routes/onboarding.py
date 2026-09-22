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
from dataclasses import dataclass, field
from typing import Annotated, List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...core import onboarding
from ...core import signatures as sig
from ...core.gam.models import GAMUser
from ...core.onboarding import RunbookStore
from ...core.signatures import SignatureStore
from ..jobs import register_job
from ..server import TEMPLATES

router = APIRouter(prefix="/onboard")

# Live-feed bounds (invariant #9 — bound anything polled). The status poll renders only the last
# _RECENT_WINDOW per-hire rows and a capped failure sample; the credentials sheet is rendered once, in
# the final panel, not on every poll.
_RECENT_WINDOW = 12
_FAILED_SAMPLE_CAP = 200


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
        last = " ".join(parts[1:]) or parts[0]
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
        if cfg.groups:
            g_ok, g_fail = await _apply_groups(conn, email, cfg.groups)
            res["groups"] = {"added": g_ok, "total": len(cfg.groups), "failed": g_fail}
        if cfg.calendars:
            c_ok, c_fail = await _apply_calendars(conn, email, cfg.calendars)
            res["calendars"] = {"added": c_ok, "total": len(cfg.calendars), "failed": c_fail}

    assignee = (hire.get("assignee") or email).strip()
    if assignee:
        title = "Onboard {} — {}".format(name or email or "new hire", hire["role"])
        try:
            tl = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
            res["tasklist"] = {"assignee": assignee, "created": tl.get("created"),
                               "total": tl.get("total"), "ok": bool(tl.get("tasklist_id"))}
            if not tl.get("tasklist_id"):
                res["errors"].append("tasks: no tasklist id came back")
        except Exception as exc:  # noqa: BLE001
            res["errors"].append("tasks: " + str(getattr(exc, "remediation", exc)))

    if hire.get("send_welcome") and email:
        w, ctx = store.welcome(), _ctx(name, email, hire["role"], hire.get("manager", ""))
        try:
            we = await conn.send_welcome_email(email, onboarding.render(w["subject"], ctx),
                                               onboarding.render(w["body"], ctx))
            res["email_sent"] = bool(we.ok)
        except Exception:  # noqa: BLE001
            res["email_sent"] = False
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
        self.recent.append(res)
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


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, role: Annotated[str, Form()], name: Annotated[str, Form()] = "",
                  email: Annotated[str, Form()] = "", manager: Annotated[str, Form()] = "",
                  assignee: Annotated[str, Form()] = "", send_welcome: Annotated[str, Form()] = "",
                  create_account: Annotated[str, Form()] = "", first: Annotated[str, Form()] = "",
                  last: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return _err(request, "That role has no steps yet — add some in Role templates.")
    w, ctx = store.welcome(), _ctx(name, email, role, manager)
    f, l = _split_name(name, first, last)
    return TEMPLATES.TemplateResponse(request, "_onboard_preview.html", {
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
              last: Annotated[str, Form()] = "", confirm: Annotated[str, Form()] = "") -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    store = _store(request)
    cfg = store.role(role)
    if cfg is None or not cfg.steps:
        return _err(request, "That role has no steps.")
    email = email.strip()
    make_account = bool(create_account)
    credentials: Optional[dict] = None

    # Validate everything that does NOT write BEFORE any mutation, so a bad assignee (or any later
    # step) can't strand a just-created account's one-time password (it exists nowhere else).
    f = l = ""
    if make_account:
        # Creating a real account is gated behind the preview: its Run button posts confirm=1.
        if confirm != "1":
            return _err(request, "Preview first — creating an account needs confirmation.")
        if not email:
            return _err(request, "Enter the new hire's email to create the account.")
        f, l = _split_name(name, first, last)
        if not f or not l:
            return _err(request, "Enter the new hire's first and last name to create the account.")
    assignee = assignee.strip() or email
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")

    if make_account:
        temp = onboarding.generate_temp_password()
        try:
            res = await conn.create_user(email, f, l, temp, change_password=True,
                                         org_unit=(cfg.org_unit or None))
        except Exception as exc:  # noqa: BLE001
            return _err(request, "Couldn't create the account: " + str(getattr(exc, "remediation", exc)))
        if not res.ok:
            return _err(request, "Couldn't create the account: " + (res.detail or "unknown error"))
        credentials = {"name": name or f"{f} {l}", "email": email, "password": temp,
                       "org_unit": cfg.org_unit or "/", "signature": None}
        # Apply the role's signature template to the new account (non-fatal if it fails).
        credentials["signature"] = await _apply_signature(conn, _sig_store(request), cfg, email, f, l)

    # Add the new hire to the role's groups and subscribe them to its shared calendars. These act on
    # the new hire's own email — whether or not we just created the account — and are non-fatal per item.
    memberships = None
    if email and (cfg.groups or cfg.calendars):
        g_ok, g_fail = await _apply_groups(conn, email, cfg.groups)
        c_ok, c_fail = await _apply_calendars(conn, email, cfg.calendars)
        memberships = {"groups_added": g_ok, "groups_total": len(cfg.groups), "groups_failed": g_fail,
                       "cals_added": c_ok, "cals_total": len(cfg.calendars), "cals_failed": c_fail}

    title = "Onboard {} — {}".format(name or email or "new hire", role)
    try:
        result = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
    except Exception as exc:  # noqa: BLE001
        # An account or membership already succeeded — never bare-_err here (that strands the
        # one-time password). Surface the failure in the result panel beside the credentials sheet.
        if credentials is None and memberships is None:
            return _err(request, "Couldn't create the task list: " + str(getattr(exc, "remediation", exc)))
        result = {"tasklist_id": "", "created": 0, "failed": list(cfg.steps),
                  "total": len(cfg.steps), "error": str(getattr(exc, "remediation", exc))}
    email_sent = None
    if send_welcome and email:
        w, ctx = store.welcome(), _ctx(name, email, role, manager)
        try:
            res = await conn.send_welcome_email(email, onboarding.render(w["subject"], ctx),
                                                onboarding.render(w["body"], ctx))
            email_sent = bool(res.ok)
        except Exception:  # noqa: BLE001
            email_sent = False
    return TEMPLATES.TemplateResponse(request, "_onboard_run.html", {
        "result": result, "assignee": assignee, "title": title, "email_sent": email_sent, "email": email,
        "credentials": credentials, "memberships": memberships,
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
        text = (await csv_file.read()).decode("utf-8-sig", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return _err(request, "Couldn't read the file: " + str(exc))
    rows, parse_errors = onboarding.parse_hire_csv(text)
    if not rows and not parse_errors:
        return _err(request, "No hires found in the CSV.")
    valid, _cfgs, summary, row_errors = _bulk_summary(rows, _store(request))
    return TEMPLATES.TemplateResponse(request, "_onboard_bulk_preview.html", {
        "summary": summary, "errors": parse_errors + row_errors,
        "csv_text": text, "can_run": bool(valid),
    })


@router.post("/bulk/run", response_class=HTMLResponse)
async def bulk_run(request: Request, csv_text: Annotated[str, Form()],
                   confirm: Annotated[str, Form()] = "") -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    if confirm != "1":  # bulk creation is gated behind the preview, like the single flow
        return _err(request, "Preview first — a bulk run needs confirmation.")
    rows, _errs = onboarding.parse_hire_csv(csv_text)
    valid, cfgs, _summary, _row_errors = _bulk_summary(rows, _store(request))
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
    # One-shot credentials: render the printable sheet once (on the terminal poll), then drop the
    # plaintext temp passwords from the retained job so this GET — which is bookmarkable, kept in
    # history, and re-fetchable — can't re-serve them. `no-store` also keeps the browser from caching
    # the response. (The single-hire flow avoids this by returning credentials in a POST.)
    creds = None
    if j is not None and j.finished and j.credentials:
        creds = j.credentials
        j.credentials = []
    resp = TEMPLATES.TemplateResponse(request, "_onboard_bulk_status.html", {"job": j, "credentials": creds})
    resp.headers["Cache-Control"] = "no-store"
    return resp
