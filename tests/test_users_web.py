from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


@pytest.fixture
def unconnected_client(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


def test_users_list(client):
    r = client.get("/users")
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "bob@example.com" in r.text
    assert "Suspended" in r.text  # bob is suspended in the fixture


def test_users_table_search_filters(client):
    r = client.get("/users/table", params={"q": "ali", "scope": "all"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "carol@example.com" not in r.text  # in-memory filter excludes non-matches


def test_user_detail_shows_info_and_delegates(client):
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Alice Anders" in r.text
    assert "a.anders@example.com" in r.text          # alias
    assert "assistant@example.com" in r.text          # delegate from fixture
    assert "Gmail signature" in r.text
    assert "IT Director" in r.text                    # title / role surfaced
    assert "Vacation responder" in r.text


def test_vacation_get_renders_current_state(client):
    r = client.get("/users/vacation", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Out of office" in r.text                  # current subject from mock
    assert "Save auto-reply" in r.text


def test_vacation_set_and_off(client):
    r = client.post("/users/vacation/set", data={"email": "alice@example.com", "subject": "OOO", "message": "away"})
    assert r.status_code == 200
    r2 = client.post("/users/vacation/off", data={"email": "alice@example.com"})
    assert r2.status_code == 200


def test_users_list_has_title_column(client):
    r = client.get("/users")
    assert "Title" in r.text and "IT Director" in r.text


def test_reports_page_renders(client):
    r = client.get("/reports")
    assert r.status_code == 200
    assert "No 2-step verification" in r.text
    assert "carol@example.com" in r.text  # carol: active, no 2SV


def test_reports_requires_connection(unconnected_client):
    r = unconnected_client.get("/reports")
    assert r.status_code == 200
    assert "Connect a domain first" in r.text


def test_groups_board_renders(client):
    r = client.get("/groups")
    assert r.status_code == 200
    assert "alice@example.com" in r.text       # draggable person card
    assert "sales@example.com" in r.text        # group option


def test_groups_board_members_view_and_mutate(client):
    r = client.get("/groups/members", params={"group": "sales@example.com"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text        # member from the group-members fixture
    add = client.post("/groups/members", data={"group": "sales@example.com", "email": "carol@example.com", "op": "add"})
    assert add.status_code == 200
    rem = client.post("/groups/members", data={"group": "sales@example.com", "email": "alice@example.com", "op": "remove"})
    assert rem.status_code == 200


def test_signature_current_renders(client):
    r = client.get("/users/signature/current", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Best," in r.text  # current signature read from the mailbox


def test_user_groups_view_add_remove(client):
    r = client.get("/users/groups", params={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "sales@example.com" in r.text            # current membership
    assert "it@example.com" in r.text               # available group in the add picker
    add = client.post("/users/groups/add", data={"email": "alice@example.com", "group": "it@example.com"})
    assert add.status_code == 200
    rem = client.post("/users/groups/remove", data={"email": "alice@example.com", "group": "sales@example.com"})
    assert rem.status_code == 200


def test_suspended_user_detail_shows_unsuspend(client):
    # Regression: the _suspend_zone include must receive `suspended` from the user.
    r = client.get("/users/detail", params={"email": "bob@example.com"})
    assert r.status_code == 200
    assert "Suspended" in r.text
    assert "Unsuspend" in r.text


def test_set_signature(client):
    r = client.post("/users/signature", data={"email": "alice@example.com", "signature": "Best,\nAlice", "html": "on"})
    assert r.status_code == 200
    assert "Signature updated." in r.text


def test_add_delegate_returns_list(client):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "new@example.com"})
    assert r.status_code == 200
    assert "assistant@example.com" in r.text  # refreshed delegate list
    assert "Remove" in r.text


def test_suspend_preview_is_guarded(client):
    r = client.post("/users/suspend/preview", data={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Confirm suspend" in r.text
    assert "alice@example.com" in r.text
    assert "DESTRUCTIVE" in r.text or "destructive" in r.text.lower()


def test_suspend_apply_toggles_zone(client):
    r = client.post("/users/suspend/apply", data={"email": "alice@example.com", "suspend": "on"})
    assert r.status_code == 200
    assert "Unsuspend" in r.text  # now shows the suspended-state control


def test_users_requires_connection(unconnected_client):
    r = unconnected_client.get("/users")
    assert r.status_code == 200
    assert "Connect a domain first" in r.text


def test_user_detail_passes_email_into_suspend_zone(client):
    # Regression for the include-context fix: the suspend button must carry the email.
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert '"email": "alice@example.com"' in r.text


def test_users_page_shows_friendly_error_not_500(client, monkeypatch):
    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    async def boom(*a, **k):
        raise GAMError(GAMErrorKind.AUTH_EXPIRED, exit_code=1, stderr="invalid_grant")

    monkeypatch.setattr(client.app.state.gamgui.connector, "list_users", boom)
    r = client.get("/users")
    assert r.status_code == 200
    assert "Re-run setup" in r.text  # GAMError.remediation, not a 500


def test_table_not_connected_shows_message(unconnected_client):
    r = unconnected_client.get("/users/table", params={"q": "x"})
    assert r.status_code == 200
    assert "Not connected" in r.text


def test_add_delegate_failure_is_reported(client, monkeypatch):
    from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel

    async def fail(email, delegate):
        preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=email, summary="x", risk=RiskLevel.LOW)
        return ChangeResult(preview=preview, ok=False, detail="permission denied")

    monkeypatch.setattr(client.app.state.gamgui.connector, "add_delegate", fail)
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "x@example.com"})
    assert "add delegate: permission denied" in r.text  # apostrophe is HTML-escaped by Jinja


def test_add_delegate_empty_rejected(client):
    r = client.post("/users/delegate/add", data={"email": "alice@example.com", "delegate": "   "})
    assert "Enter a delegate email." in r.text
