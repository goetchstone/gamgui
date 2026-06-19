"""Setup wizard routes.

A small HTMX flow: collect domain + admin, then either import an existing GAM config dir or follow
the guided fresh-setup commands; do the manual Domain-Wide Delegation step; verify. On a passing
verify the Google Workspace connector is activated on the app state.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.connectors.gam_connector import GAMConnector
from ...core.setup import SetupService
from ..server import TEMPLATES

router = APIRouter(prefix="/setup")


def _service(request: Request) -> SetupService:
    st = request.app.state.gamgui
    return SetupService(st.vault, st.runner)


@router.get("", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    svc = _service(request)
    return TEMPLATES.TemplateResponse(
        request,
        "setup.html",
        {
            "gam_version": await svc.engine_version(),
            "binary_present": st.runner.binary_exists(),
            "candidate_dirs": svc.candidate_dirs(),
        },
    )


@router.post("/import", response_class=HTMLResponse)
async def do_import(
    request: Request,
    domain: str = Form(""),
    admin: str = Form(""),
    config_dir: str = Form(""),
) -> HTMLResponse:
    domain, admin, config_dir = domain.strip(), admin.strip(), config_dir.strip()
    if not domain or not admin or not config_dir:
        return TEMPLATES.TemplateResponse(
            request, "_error.html",
            {"message": "Enter the domain and super-admin email, then choose a credentials folder."},
        )
    svc = _service(request)
    imported = svc.import_dir(config_dir, domain)
    return TEMPLATES.TemplateResponse(
        request, "_dwd.html",
        {
            "imported": imported,
            "ready": svc.is_ready(domain),
            "domain": domain,
            "admin": admin,
            "dwd": svc.dwd_details(domain),
        },
    )


@router.post("/fresh", response_class=HTMLResponse)
async def fresh(request: Request, domain: str = Form(""), admin: str = Form("")) -> HTMLResponse:
    svc = _service(request)
    info = svc.setup_commands(admin.strip() or "admin@yourdomain.com")
    return TEMPLATES.TemplateResponse(
        request, "_commands.html",
        {"info": info, "domain": domain.strip(), "admin": admin.strip()},
    )


@router.post("/verify", response_class=HTMLResponse)
async def verify(request: Request, domain: str = Form(""), admin: str = Form("")) -> HTMLResponse:
    domain, admin = domain.strip(), admin.strip()
    if not domain or not admin:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "Domain and super-admin email are required to verify."}
        )
    st = request.app.state.gamgui
    svc = _service(request)
    result = await svc.verify(domain, admin)
    if result.ok:
        st.connector = GAMConnector(runner=st.runner, domain=domain)
        st.audit_domain = domain
    return TEMPLATES.TemplateResponse(
        request, "_verify.html", {"result": result, "domain": domain, "admin": admin}
    )
