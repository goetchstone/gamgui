"""Onboarding runbooks (/onboard).

Define role → task-list templates (editable, persisted locally), a per-role signature template + OU,
and a welcome-email template. Generating for a new hire can **create the Google account** (with a
one-time temp password the operator prints and hands over — never emailed or audited), apply the
role's signature, fire the setup checklist to the assignee's Google Tasks, and send the welcome
email. Every mutation goes through the guarded, audited connector writes.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import onboarding
from ...core import signatures as sig
from ...core.gam.models import GAMUser
from ...core.onboarding import RunbookStore
from ...core.signatures import SignatureStore
from ..server import TEMPLATES

router = APIRouter(prefix="/onboard")


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
                    signature: Annotated[str, Form()] = "", org_unit: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    try:
        store.set_role(name, steps.splitlines(), signature=signature, org_unit=org_unit)
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

    if make_account:
        # Creating a real account is gated behind the preview: its Run button posts confirm=1.
        if confirm != "1":
            return _err(request, "Preview first — creating an account needs confirmation.")
        if not email:
            return _err(request, "Enter the new hire's email to create the account.")
        f, l = _split_name(name, first, last)
        if not f or not l:
            return _err(request, "Enter the new hire's first and last name to create the account.")
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
        if cfg.signature:
            body = _sig_store(request).get(cfg.signature)
            if body:
                user = GAMUser(primary_email=email, given_name=f, family_name=l)
                try:
                    sres = await conn.set_signature(email, sig.render_signature(body, user), html=True)
                    credentials["signature"] = cfg.signature if sres.ok else None
                except Exception:  # noqa: BLE001
                    credentials["signature"] = None

    assignee = (assignee or email).strip()
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    try:
        result = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
    except Exception as exc:  # noqa: BLE001
        return _err(request, "Couldn't create the task list: " + str(getattr(exc, "remediation", exc)))
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
        "credentials": credentials,
    })
