"""The local FastAPI app.

It is bound to loopback only and gated by a per-launch token (set as a cookie on first load), so
no other local process or user can drive it. The native window (``gamgui/app.py``) points a
WKWebView at it; in dev you can also open the printed URL in a browser.

This module exposes an app *factory* so tests can inject a mock-backed connector and run the whole
HTTP layer offline.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.calendar_index import CalendarIndex, default_index_path
from ..core.connectors.gam_connector import GAMConnector
from ..core.gam.runner import GAMRunner
from ..core.secrets.ephemeral import sweep_stale_configs
from ..core.secrets.vault import SecretsVault
from ..core.usercache import UserCache

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
TOKEN_COOKIE = "gamgui_token"


@dataclass
class AppState:
    vault: SecretsVault
    runner: GAMRunner
    audit_domain: str = ""              # the active Workspace domain, if configured
    connector: Optional[GAMConnector] = None
    token: str = ""
    user_cache: UserCache = field(default_factory=UserCache)
    jobs: dict = field(default_factory=dict)  # id -> ApplyJob, for polled progress on long batch ops
    calendar_index: Optional[CalendarIndex] = None  # persistent calendar name-search index (derived data)
    cal_index_job_id: str = ""  # the in-flight index-rebuild job, if any (guards double-rebuilds)
    catalog: object = None  # the GAM command catalog (lazy-loaded by the Builder route)
    builder_sequence: list = field(default_factory=list)  # the working drag-built command sequence
    runbooks: object = None  # onboarding role templates + welcome email (lazy-loaded by the route)

    async def users(self, force: bool = False) -> list:
        """The cached user list (one ``gam print users`` shared by the list + reports)."""
        if self.connector is None:
            return []
        from ..core.gam.commands import CACHE_FIELDS

        return await self.user_cache.get(
            lambda: self.connector.list_users(fields=CACHE_FIELDS), force=force
        )

    def invalidate_users(self) -> None:
        self.user_cache.invalidate()

    @classmethod
    def create(cls, vault: Optional[SecretsVault] = None, token: Optional[str] = None) -> "AppState":
        sweep_stale_configs()  # clean up any credential temp dirs orphaned by a prior crash/kill
        vault = vault or SecretsVault()
        runner = GAMRunner(vault=vault)
        domains = vault.list_domains()
        domain = domains[0] if domains else ""
        connector = GAMConnector(runner=runner, domain=domain) if domain else None
        return cls(
            vault=vault,
            runner=runner,
            audit_domain=domain,
            connector=connector,
            token=token or secrets.token_urlsafe(24),
            calendar_index=CalendarIndex(default_index_path()),
        )


class TokenGateMiddleware(BaseHTTPMiddleware):
    """Allow static assets; otherwise require the launch token (cookie, else ?token= which sets it)."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    def _token_ok(self, candidate: "str | None") -> bool:
        # Constant-time compare (defence-in-depth vs. a local process timing the loopback auth).
        return candidate is not None and secrets.compare_digest(candidate, self._token)

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/static") or request.url.path == "/healthz":
            return await call_next(request)

        if self._token_ok(request.cookies.get(TOKEN_COOKIE)):
            return await call_next(request)

        if self._token_ok(request.query_params.get("token")):
            response = await call_next(request)
            response.set_cookie(
                TOKEN_COOKIE, self._token, httponly=True, samesite="strict", max_age=86400
            )
            return response

        return JSONResponse({"error": "forbidden"}, status_code=403)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="GamGUI", docs_url=None, redoc_url=None)
    app.state.gamgui = state
    app.add_middleware(TokenGateMiddleware, token=state.token)
    # Ensure the dir exists before mounting — a fresh clone or a stripped bundle may lack it,
    # and StaticFiles raises on a missing directory.
    static_dir = _WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        st: AppState = request.app.state.gamgui
        try:
            version = (await st.runner.version()).splitlines()[0] if st.runner.binary_exists() else ""
        except Exception:
            version = ""
        domains = st.vault.list_domains()
        configured = st.vault.has_credentials(st.audit_domain) if st.audit_domain else False
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "gam_version": version,
                "gam_binary": str(st.runner.gam_binary),
                "binary_present": st.runner.binary_exists(),
                "domains": domains,
                "active_domain": st.audit_domain,
                "configured": configured,
            },
        )

    # Imported here (not at module top) to avoid a cycle: routes import TEMPLATES from this module.
    from .routes.builder import router as builder_router
    from .routes.calendars import router as calendars_router
    from .routes.groups import router as groups_router
    from .routes.lifecycle import router as lifecycle_router
    from .routes.onboarding import router as onboarding_router
    from .routes.reports import router as reports_router
    from .routes.setup import router as setup_router
    from .routes.signatures import router as signatures_router
    from .routes.users import router as users_router

    app.include_router(setup_router)
    app.include_router(users_router)
    app.include_router(reports_router)
    app.include_router(groups_router)
    app.include_router(signatures_router)
    app.include_router(calendars_router)
    app.include_router(lifecycle_router)
    app.include_router(onboarding_router)
    app.include_router(builder_router)
    return app
