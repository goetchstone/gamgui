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
from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from keyring.errors import KeyringError

from ...core.audit import AuditLog
from ...core.connectors.gam_connector import GAMConnector
from ...core.schedules import StoreProblem
from ...core.setup import SetupService, VerifyResult
from ..jobs import ONE_STEP, UNSTOPPABLE
from ..server import SCREENS, TEMPLATES
from ._common import schedule_stores

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
    confirmed: Annotated[str, Form()] = "",
) -> HTMLResponse:
    domain, admin = domain.strip().lower(), admin.strip()     # the key import stored
    if not domain or not admin:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "Domain and super-admin email are required to verify."}
        )
    # A verify that passes activates the domain, so it stops what's running as a switch does: warned the same way.
    warn = _Warn("/setup/verify", {"domain": domain, "admin": admin}, zone="wizard-next")
    return await _verify_and_activate(request, domain, admin, confirmed=confirmed, warn=warn)


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
    running job's ID — asked before the check, and again after it passes (``_verify_and_activate``)."""
    st = request.app.state.gamgui
    tenant, menu = tenant.strip(), where == "menu"
    active = st.connector.domain if st.connector else ""
    if st.switching:
        return _busy(request, st)
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
    warn = _Warn("/setup/switch", {"tenant": tenant, "where": where, "back": back},
                 zone="domain-result" if menu else "tenant-result", sync="#domain-panel" if menu else "#tenant-panel",
                 include_admin=not menu)
    if running := _unwarned(st, tenant, confirmed):
        return warn.answer(request, st, tenant, running)
    return await _verify_and_activate(request, tenant, admin, switched=True, menu=menu, back=back, confirmed=confirmed,
                                      warn=warn)


@dataclass(frozen=True)
class _Warn:
    """Where a switch's warning posts its "Switch anyway" (plan F6): the route it came from, with the same values."""

    post: str
    vals: dict
    zone: str                    # the element its answer replaces, which Cancel empties
    sync: str = ""               # a second click there while one is asked drops (hx-sync)
    include_admin: bool = False  # Setup's switcher: the admin typed in "Your domain" goes with it

    def answer(self, request: Request, st, domain: str, running: list) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_switch_warning.html", {
            "tenant": domain, "active": (st.connector.domain if st.connector else "").lower(), "running": running,
            "unstoppable": UNSTOPPABLE, "warn": self, "sync": self.sync or f"#{self.zone}",
            "vals": {**self.vals, "confirmed": ",".join(j.id for j in running)}})


def _unwarned(st, domain: str, confirmed: str) -> list:
    """What making ``domain`` active would stop without a warning: every running job, when any of them isn't among the
    IDs ``confirmed`` names (a run started since the warning, in another window or during the check), else none. A
    re-check of the active domain stops nothing."""
    active = st.connector.domain if st.connector else ""
    running = [j for j in st.jobs.values() if not j.finished and j.kind != ONE_STEP]
    if domain.lower() == active.lower() or not {j.id for j in running} - set(confirmed.split(",")):
        return []
    return running


def _busy(request: Request, st) -> HTMLResponse:
    """One check at a time (plan F6): two switches in flight once ended on whichever check finished last, while the
    window had already been sent to the other."""
    return TEMPLATES.TemplateResponse(request, "_error.html", {
        "message": f"{st.switching}'s access is being checked. Wait for its answer, then try again. Nothing changed."})


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
                               back: str = "", confirmed: str = "", warn: Optional[_Warn] = None) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.switching:
        return _busy(request, st)
    st.switching = domain            # set and checked with no await between: a second check is refused until this ends
    try:
        try:
            result = await _service(request).verify(domain, admin)
        except Exception as exc:  # noqa: BLE001 — GAM's own failures are verify's answer; anything else is said, not a 500
            logger.warning("a domain check couldn't run", exc_info=exc)
            return _not_checked(request, domain, st.connector.domain if st.connector else "", menu, switched,
                                keychain=isinstance(exc, KeyringError))
        if result.ok:
            # A run started while the check ran was never warned about: say so instead. No await from here to activate.
            if warn is not None and (running := _unwarned(st, domain, confirmed)):
                return warn.answer(request, st, domain, running)
            audit = st.connector.audit if st.connector else None          # one audit log across a switch
            st.activate(GAMConnector(runner=st.runner, domain=domain, audit=audit))   # busts what the old tenant left
    finally:
        st.switching = ""
    if result.ok and menu:      # every screen read the old domain: reload this one, for the new domain
        return HTMLResponse("", headers={"HX-Redirect": _back_to(back)})
    if menu:
        return TEMPLATES.TemplateResponse(request, "_switch_failed.html", {
            "result": result, "domain": domain, "active": st.connector.domain.lower() if st.connector else ""})
    return TEMPLATES.TemplateResponse(
        request, "_verify.html",
        {"result": result, "domain": domain, "admin": admin, "switched": switched,
         "domains": st.vault.list_domains(), "active": st.connector.domain if st.connector else ""},
    )


async def _forget_schedules(st, domain: str, active: str) -> str:
    """Plan F7a: a domain removed from the list takes its schedules and the runs they kept on this Mac (its data at rest)
    with it, unless another spelling of it is still listed or active. What went, in words, to add to the notice."""
    if any(d and d.lower() == domain.lower() for d in (*st.vault.list_domains(), active)):
        return ""
    store, kept = schedule_stores(st)
    try:
        runs = await asyncio.to_thread(kept.forget_domain, domain)
        schedules = await asyncio.to_thread(store.forget_domain, domain)
    except (StoreProblem, OSError) as exc:
        logger.warning("deleting a removed domain's schedules failed", exc_info=exc)
        return (" Its schedules and the runs they kept on this Mac couldn't all be deleted: what is left is in "
                f"{store.root}.")
    if not runs and not schedules:
        return ""
    return (f" Its {schedules} schedule{'' if schedules == 1 else 's'} and {runs} kept run{'' if runs == 1 else 's'} "
            "on this Mac were deleted.")


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
        notice = removal.message + await _forget_schedules(st, domain, active)
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
