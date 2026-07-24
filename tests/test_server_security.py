"""The token gate itself: cross-origin rejection, token comparison, and the bootstrap flow.

The gate is the whole authentication story for the app, so these exercise the middleware directly
rather than any one route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.calendar_index import CalendarIndex
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import TOKEN_COOKIE, AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
# TestClient's default base_url; the gate derives our authority from Host, so this is our origin.
SELF_ORIGIN = "http://testserver"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    return create_app(state)


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.get("/?token=t")  # bootstrap: sets the cookie for the rest of the session
    return c


# --- the ?token= bootstrap ---------------------------------------------------------------

def test_token_bootstrap_sets_the_cookie(app):
    c = TestClient(app)
    r = c.get("/?token=t")
    assert r.status_code == 200
    assert c.cookies.get(TOKEN_COOKIE) == "t"
    # A session cookie: it must not carry an expiry that outlives the browser session.
    set_cookie = r.headers["set-cookie"]
    assert "Max-Age" not in set_cookie and "Expires" not in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie
    assert c.get("/users").status_code == 200  # the cookie alone carries the next request


def test_no_credentials_is_forbidden(app):
    assert TestClient(app).get("/users").status_code == 403


# --- cross-origin (the port-blind cookie problem) ----------------------------------------

@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:9999",   # another local process — same *site*, so the cookie would be sent
    "http://localhost:8080",
    "http://evil.local",
    "https://testserver",      # right host, wrong scheme
    "null",                    # a sandboxed iframe / file:// document
])
def test_cross_origin_post_is_rejected(client, origin):
    r = client.post("/builder/sequence/clear", headers={"Origin": origin})
    assert r.status_code == 403
    assert r.json() == {"error": "forbidden"}


def test_same_origin_post_is_allowed(client):
    r = client.post("/builder/sequence/clear",
                    headers={"Origin": SELF_ORIGIN, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_post_without_origin_is_allowed(client):
    # The native WKWebView and non-browser clients send no Origin; that path must keep working.
    assert client.post("/builder/sequence/clear").status_code == 200


def test_cross_origin_get_is_rejected(client):
    assert client.get("/users", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403


def test_same_origin_get_navigation_and_export_still_work(app):
    st = app.state.gamgui
    st.builder_last_result = {"records": [{"primaryEmail": "alice@example.com"}]}
    c = TestClient(app)
    c.get("/?token=t")
    assert c.get("/", headers={"Sec-Fetch-Site": "none"}).status_code == 200
    r = c.get("/builder/export.csv", headers={"Origin": SELF_ORIGIN, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")


def test_cross_origin_export_is_rejected(client):
    assert client.get("/builder/export.csv", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403


def test_cross_site_fetch_metadata_is_rejected_without_origin(client):
    # Defence in depth: Sec-Fetch-Site alone is enough to spot another port on loopback.
    assert client.get("/users", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.get("/users", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_cross_origin_cannot_reach_a_mutating_route(client):
    r = client.post("/users/suspend/apply", data={"email": "alice@example.com", "suspend": "on"},
                    headers={"Origin": "http://127.0.0.1:9999"})
    assert r.status_code == 403


# --- token comparison --------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"t\xc3\xa9", b"\xff\xfe", b"caf\xc3\xa9"])
def test_non_ascii_cookie_token_is_forbidden_not_a_crash(app, raw):
    # secrets.compare_digest's str form raises TypeError above U+007F, and starlette decodes raw
    # header bytes as latin-1 — so a garbled cookie must come back 403, never a 500. Sent as raw
    # header bytes because httpx's cookie jar refuses to encode a non-ASCII value at all.
    r = TestClient(app).get("/users", headers=[(b"cookie", b"gamgui_token=" + raw)])
    assert r.status_code == 403


def test_non_ascii_query_token_is_forbidden_not_a_crash(app):
    assert TestClient(app).get("/?token=t%C3%A9").status_code == 403


def test_wrong_ascii_token_is_forbidden(app):
    c = TestClient(app)
    c.cookies.set(TOKEN_COOKIE, "nope")
    assert c.get("/users").status_code == 403


# --- unauthenticated surface -------------------------------------------------------------

def test_healthz_and_static_stay_reachable(app):
    c = TestClient(app)
    assert c.get("/healthz").json() == {"ok": True}
    r = c.get("/static/")  # the mount answers (404 for a missing asset, never the gate's 403)
    assert r.status_code != 403


def test_security_headers_on_the_forbidden_response(app):
    r = TestClient(app).get("/users", headers={"Origin": "http://evil.local"})
    assert r.status_code == 403
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
