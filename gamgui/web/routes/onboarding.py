"""Onboarding runbooks (/onboard).

Define role → task-list templates (editable, persisted locally) + a welcome-email template.
Generating a runbook for a new hire turns the role's steps into a Google Tasks list on the assignee
(durable, delegatable — the checklist lives in their Gmail/Tasks) and optionally sends the templated
welcome email. Task/email creation goes through the guarded, audited connector writes.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import onboarding
from ...core.onboarding import RunbookStore
from ..server import TEMPLATES

router = APIRouter(prefix="/onboard")


def _st(request: Request):
    return request.app.state.gamgui


def _store(request: Request) -> RunbookStore:
    st = _st(request)
    if st.runbooks is None:
        st.runbooks = RunbookStore()
    return st.runbooks


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


def _ctx(name: str, email: str, role: str, manager: str) -> dict:
    return {"name": name, "email": email, "role": role, "manager": manager}


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "onboarding.html", {"connected": False})
    store = _store(request)
    return TEMPLATES.TemplateResponse(request, "onboarding.html", {
        "connected": True, "roles": store.roles(), "welcome": store.welcome(),
        "vars": onboarding.WELCOME_VARS,
    })


@router.post("/role", response_class=HTMLResponse)
async def save_role(request: Request, name: str = Form(...), steps: str = Form("")) -> HTMLResponse:
    store = _store(request)
    try:
        store.set_role(name, steps.splitlines())
    except ValueError as exc:
        return _err(request, str(exc))
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/role/delete", response_class=HTMLResponse)
async def delete_role(request: Request, name: str = Form(...)) -> HTMLResponse:
    store = _store(request)
    store.delete_role(name)
    return TEMPLATES.TemplateResponse(request, "_onboard_roles.html", {"roles": store.roles()})


@router.post("/welcome", response_class=HTMLResponse)
async def save_welcome(request: Request, subject: str = Form(""), body: str = Form("")) -> HTMLResponse:
    store = _store(request)
    store.set_welcome(subject, body)
    return TEMPLATES.TemplateResponse(request, "_onboard_welcome.html",
                                      {"welcome": store.welcome(), "vars": onboarding.WELCOME_VARS, "saved": True})


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, role: str = Form(...), name: str = Form(""), email: str = Form(""),
                  manager: str = Form(""), assignee: str = Form(""), send_welcome: str = Form("")) -> HTMLResponse:
    store = _store(request)
    steps = store.steps_for(role)
    if not steps:
        return _err(request, "That role has no steps yet — add some in Role templates.")
    w, ctx = store.welcome(), _ctx(name, email, role, manager)
    return TEMPLATES.TemplateResponse(request, "_onboard_preview.html", {
        "role": role, "steps": steps, "assignee": (assignee or email).strip(), "name": name, "email": email,
        "manager": manager, "send_welcome": bool(send_welcome),
        "subject": onboarding.render(w["subject"], ctx), "body": onboarding.render(w["body"], ctx),
    })


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, role: str = Form(...), name: str = Form(""), email: str = Form(""),
              manager: str = Form(""), assignee: str = Form(""), send_welcome: str = Form("")) -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    store = _store(request)
    steps = store.steps_for(role)
    assignee = (assignee or email).strip()
    if not steps:
        return _err(request, "That role has no steps.")
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    try:
        result = await conn.create_onboarding_runbook(assignee, title, steps)
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
    })
