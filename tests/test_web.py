from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path) -> TestClient:
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="testtoken")
    return TestClient(create_app(state))


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_root_requires_token(client):
    assert client.get("/").status_code == 403


def test_root_with_token_renders_and_sets_cookie(client):
    r = client.get("/?token=testtoken")
    assert r.status_code == 200
    assert "Saybrook" in r.text  # Saybrook Home brand applied
    assert "Not configured" in r.text  # no creds in the in-memory vault
    # cookie now set on the client -> a token-less follow-up is allowed
    assert client.get("/").status_code == 200


def test_wrong_token_forbidden(client):
    assert client.get("/?token=nope").status_code == 403
