"""The local FastAPI app.

It is bound to loopback only and gated by a per-launch token (set as a cookie on first load), so
no other local process or user can drive it. The native window (``gamgui/app.py``) points a
WKWebView at it; in dev you can also open the printed URL in a browser.

This module exposes an app *factory* so tests can inject a mock-backed connector and run the whole
HTTP layer offline.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
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
    builder_last_result: Optional[dict] = None  # last read-command result set, for the CSV download
    runbooks: object = None  # onboarding role templates + welcome email (lazy-loaded by the route)
    sig_templates: object = None  # saved HTML signature templates (lazy-loaded by the signatures route)

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
    """Reject cross-origin callers, allow static assets, else require the launch token
    (cookie, else ?token= which sets it).

    The origin check comes first because the cookie alone is not enough: cookies are not port-scoped
    (RFC 6265 §8.5), so SameSite=Strict treats *every* port on 127.0.0.1 as the same site. Without
    this, a page served by any other local process could fire a form POST at us — a simple request,
    so no preflight — and the browser would helpfully attach our token cookie.

    Also stamps security headers on every response: a CSP locking down object/base/frame/form
    vectors (no script-src directive — the app uses inline handlers and all scripts are now same-origin
    vendored, so there is no remote script to constrain), nosniff, and no-referrer (so the ?token= in
    the first URL can't leak to fonts.googleapis via the Referer header)."""

    SECURITY_HEADERS = {
        "Content-Security-Policy": "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    # Sec-Fetch-Site values that mean another origin initiated this. "same-site" is included on
    # purpose: that is exactly the other-port-on-loopback case. Anything else (including a missing
    # header, or a value a future browser invents) falls through to the Origin check below.
    CROSS_ORIGIN_FETCH_SITES = frozenset({"cross-site", "same-site"})

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    def _token_ok(self, candidate: "str | None") -> bool:
        # Constant-time compare (defence-in-depth vs. a local process timing the loopback auth).
        # Compared as UTF-8 bytes: compare_digest's str form rejects codepoints > 127 with a
        # TypeError, and a cookie or query string can carry those, which would turn a bad token
        # into a 500 instead of a 403.
        if candidate is None:
            return False
        return secrets.compare_digest(candidate.encode("utf-8"), self._token.encode("utf-8"))

    def _same_origin(self, request: Request) -> bool:
        if request.headers.get("sec-fetch-site", "").lower() in self.CROSS_ORIGIN_FETCH_SITES:
            return False
        origin = request.headers.get("origin")
        if origin is None:
            # Browsers always send Origin cross-origin, so absence means a same-origin navigation
            # or a non-browser client (the native WKWebView's initial load, curl, the test client).
            return True
        # The listening port is picked at runtime, so our own authority is whatever Host says.
        return origin.casefold() == f"{request.url.scheme}://{request.headers.get('host', '')}".casefold()

    def _secure(self, response):
        for key, value in self.SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    async def dispatch(self, request: Request, call_next):
        if not self._same_origin(request):
            return self._secure(JSONResponse({"error": "forbidden"}, status_code=403))

        if request.url.path.startswith("/static") or request.url.path == "/healthz":
            return self._secure(await call_next(request))

        if self._token_ok(request.cookies.get(TOKEN_COOKIE)):
            return self._secure(await call_next(request))

        if self._token_ok(request.query_params.get("token")):
            response = await call_next(request)
            # A session cookie: the token is regenerated every launch, so persisting it for a day
            # only widens the window in which it can be lifted out of the browser's cookie jar.
            response.set_cookie(TOKEN_COOKIE, self._token, httponly=True, samesite="strict")
            return self._secure(response)

        return self._secure(JSONResponse({"error": "forbidden"}, status_code=403))


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    yield
    # Bulk apply, offboarding, and the calendar scan are fire-and-forget `asyncio.create_task` jobs
    # (return fast, poll for progress). On shutdown, cancel any still in flight so the loop can close
    # instead of blocking on their in-flight gam subprocesses — a graceful quit for the real app, and
    # (with a context-managed TestClient) a teardown that doesn't hang on the asyncio child-watcher,
    # which is what made the "job started" route tests flake on Linux/py3.12.
    st = getattr(app.state, "gamgui", None)
    for job in list(getattr(st, "jobs", {}).values()) if st else []:
        task = getattr(job, "task", None)
        if task is not None and not task.done():
            task.cancel()


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="GamGUI", docs_url=None, redoc_url=None, lifespan=_lifespan)
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
    from .routes.audit import router as audit_router
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
    app.include_router(audit_router)
    return app
