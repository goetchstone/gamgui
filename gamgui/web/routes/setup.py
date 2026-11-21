"""Setup wizard routes.

A small HTMX flow: collect domain + admin, then either import an existing GAM config dir or follow
the guided fresh-setup commands; do the manual Domain-Wide Delegation step; verify. On a passing
verify the Google Workspace connector is activated on the app state. With credentials for more than
one domain in the Keychain, the page also offers a switcher, which activates another through that
same verify (plan U10b).
"""

from __future__ import annotations

import logging

import asyncio
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from keyring.errors import KeyringError

from ...core.audit import AuditLog
from ...core.connectors.gam_connector import GAMConnector
from ...core.setup import SetupService, VerifyResult
from ..jobs import ONE_STEP, UNSTOPPABLE
from ..server import SCREENS, TEMPLATES

logger = logging.getLogger(__name__)
_UNREADABLE_FOLDER = "That folder could not be read — check the path and try again."
_REFUSED_FOLDER = ("macOS refused access to that folder. Check its permissions, or the app's Files and Folders "
                   "access in System Settings, then try again.")

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
            "gam_version_warning": await svc.engine_version_warning(),
            "binary_present": st.runner.binary_exists(),
            "candidate_dirs": svc.candidate_dirs(),
            "domains": st.vault.list_domains(),
            "active": st.connector.domain if st.connector else "",
        },
    )


@router.post("/import", response_class=HTMLResponse)
async def do_import(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
    config_dir: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # Lowercased as import_dir stores it, so the DWD step and its verify name the same key.
    domain, admin, config_dir = domain.strip().lower(), admin.strip(), config_dir.strip()
    if not domain or not admin or not config_dir:
        return TEMPLATES.TemplateResponse(
            request, "_error.html",
            {"message": "Enter the domain and super-admin email, then choose a credentials folder."},
        )
    svc = _service(request)
    try:
        # Synchronous filesystem + Keychain work: off the event loop, or it stalls every request.
        imported = await asyncio.to_thread(svc.import_dir, config_dir, domain)
    except ValueError as exc:
        # A typo'd or non-directory path is operator error, not a crash — say which, and let them
        # correct it in place. The import path is written to raise only operator-facing ValueError,
        # but it stands on the filesystem and on $HOME: an unreadable folder (OSError) or an
        # environment with no determinable home (RuntimeError from Path.home()/expanduser) are
        # conditions the operator can act on, and a 500 tells them nothing. Deliberately narrow —
        # a programming error is not an OSError, and still surfaces as a 500.
        return TEMPLATES.TemplateResponse(
            request, "_error.html",
            {"message": str(exc) or _UNREADABLE_FOLDER},
        )
    except (OSError, RuntimeError) as exc:
        logger.warning("importing a credentials folder failed", exc_info=exc)
        refused = isinstance(exc, PermissionError)
        return TEMPLATES.TemplateResponse(request, "_error.html", {"message": _REFUSED_FOLDER if refused else _UNREADABLE_FOLDER})
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
async def fresh(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
) -> HTMLResponse:
    svc = _service(request)
    info = svc.setup_commands(admin.strip() or "admin@yourdomain.com")
    return TEMPLATES.TemplateResponse(
        request, "_commands.html",
        {"info": info, "domain": domain.strip().lower(), "admin": admin.strip()},
    )


@router.post("/verify", response_class=HTMLResponse)
async def verify(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
) -> HTMLResponse:
    domain, admin = domain.strip().lower(), admin.strip()     # the key import stored
    if not domain or not admin:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "Domain and super-admin email are required to verify."}
        )
    return await _verify_and_activate(request, domain, admin)


@router.post("/switch", response_class=HTMLResponse)
async def switch(
    request: Request,
    tenant: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
    where: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
    confirmed: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Activate a domain the Keychain holds credentials for — another, or the active one again (Setup's
    Check access) — by the same verify, so a switch
    is a `check serviceaccount` read, the connector only changes on a pass, and the caches are busted.
    The admin is the one its ``oauth2.txt`` names, else the one typed in "Your domain".

    The header's domain menu (plan F6) posts here too, with ``where=menu``: its answers land in the menu, and a pass
    reloads ``back``, the screen it was on, for the new domain. From either place, a switch to another domain while a
    run is going says first what it stops (``_switch_warning.html``) and goes ahead only once ``confirmed`` names every
    running job's ID."""
    st = request.app.state.gamgui
    tenant, menu = tenant.strip(), where == "menu"
    active = st.connector.domain if st.connector else ""
    try:
        # Keychain reads: a locked Keychain asks for its password here, as Setup's own verify does.
        listed = st.vault.list_domains()
        named = st.vault.oauth_admin_email(tenant) if tenant in listed else ""
    except KeyringError as exc:
        logger.warning("the Keychain refused a domain switch's read", exc_info=exc)
        return _not_checked(request, tenant, active, menu, switched=True, keychain=True)
    if tenant not in listed:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "No credentials for that domain in the Keychain."}
        )
    admin = named or admin.strip()
    if not admin:
        message = (f"{tenant}'s oauth2.txt names no admin, so switch to it on Setup, where you type its super-admin email."
                   if menu else f"Enter {tenant}'s super-admin email in \"Your domain\" above, then try again.")
        return TEMPLATES.TemplateResponse(request, "_error.html", {"message": message})
    running = [j for j in st.jobs.values() if not j.finished and j.kind != ONE_STEP]
    # Confirmed means "stop these": a run started since the warning (from another window) is warned about first.
    unwarned = {j.id for j in running} - set(confirmed.split(","))
    if unwarned and tenant.lower() != active.lower():
        return TEMPLATES.TemplateResponse(request, "_switch_warning.html", {
            "tenant": tenant, "active": active.lower(), "running": running, "unstoppable": UNSTOPPABLE,
            "where": where, "back": back, "zone": "domain-result" if menu else "tenant-result",
            "confirm": ",".join(j.id for j in running)})
    return await _verify_and_activate(request, tenant, admin, switched=True, menu=menu, back=back)


def _back_to(back: str) -> str:
    """Where a pass from the menu reloads: the top of the screen it was on (``/users``, not one user's page, which may
    name the old domain's account). Only one of the header's screens, or Setup: never another path or origin."""
    return back if back == "/setup" or back in {href for href, _ in SCREENS} else "/"


def _not_checked(request: Request, domain: str, active: str, menu: bool, switched: bool, keychain: bool) -> HTMLResponse:
    """The Keychain didn't hand the credentials over (``keychain``: locked and its prompt cancelled, or access denied),
    or the check couldn't run at all (no room for its temporary folder): an answer in fixed words, the active domain
    kept. The caller logs the exception; its text is never shown, as it could carry the Keychain's own."""
    if keychain:
        summary = (f"The Keychain didn't hand over {domain}'s credentials: it may be locked, or its password prompt was "
                   "cancelled. Nothing changed. Try again.")
    else:
        summary = (f"{domain}'s access check couldn't run, so nothing changed. Check the disk has room, then try "
                   "again.")
    result = VerifyResult(ok=False, summary=summary)
    template = "_switch_failed.html" if menu else "_verify.html"
    return TEMPLATES.TemplateResponse(request, template, {"result": result, "domain": domain, "switched": switched,
                                                          "active": active.lower(), "unchecked": True})


async def _verify_and_activate(request: Request, domain: str, admin: str, switched: bool = False, menu: bool = False,
                               back: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    try:
        result = await _service(request).verify(domain, admin)
    except Exception as exc:  # noqa: BLE001 — GAM's own failures are verify's answer; anything else is said, not a 500
        logger.warning("a domain check couldn't run", exc_info=exc)
        return _not_checked(request, domain, st.connector.domain if st.connector else "", menu, switched,
                            keychain=isinstance(exc, KeyringError))
    if result.ok:
        audit = st.connector.audit if st.connector else None          # one audit log across a switch
        st.activate(GAMConnector(runner=st.runner, domain=domain, audit=audit))   # busts what the old tenant left
        if menu:      # every screen read the old domain: reload this one, for the new domain
            return HTMLResponse("", headers={"HX-Redirect": _back_to(back)})
    if menu:
        return TEMPLATES.TemplateResponse(request, "_switch_failed.html", {
            "result": result, "domain": domain, "active": st.connector.domain.lower() if st.connector else ""})
    return TEMPLATES.TemplateResponse(
        request, "_verify.html",
        {"result": result, "domain": domain, "admin": admin, "switched": switched,
         "domains": st.vault.list_domains(), "active": st.connector.domain if st.connector else ""},
    )


@router.post("/remove", response_class=HTMLResponse)
async def remove(request: Request, remove: Annotated[str, Form()] = "") -> HTMLResponse:
    """Delete a listed, inactive domain's credentials from the Keychain (``SetupService.remove_domain``)
    and re-render the tenant panel with what happened. Local only: no gam call. ``remove`` is the
    index's exact spelling, never lowercased, so a capitalized entry from before can be removed.
    ``remove_domain`` writes the audit record, to the log the Audit page reads."""
    st = request.app.state.gamgui
    active = st.connector.domain if st.connector else ""
    audit = st.connector.audit if st.connector else AuditLog()
    domain = remove.strip()
    notice = error = ""
    try:
        # Keychain deletes and the case probe are synchronous: off the event loop.
        removal = await asyncio.to_thread(_service(request).remove_domain, domain, active, audit=audit)
        notice = removal.message
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — a refused or locked Keychain: the items may remain
        # It may be the case probe or a delete that failed: say only that the Keychain refused.
        logger.warning("removing a domain from the Keychain failed", exc_info=exc)
        error = f"The Keychain refused the change, so {domain} stays listed. Try Remove again."
    return TEMPLATES.TemplateResponse(
        request, "_tenant_panel.html",
        {"domains": st.vault.list_domains(), "active": active, "notice": notice, "error": error},
    )
