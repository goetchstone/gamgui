from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    client = TestClient(create_app(state))
    client.get("/?token=t")  # establish the token cookie
    return client, tmp_path, vault, state


def test_setup_page_renders(ctx):
    client = ctx[0]
    r = client.get("/setup")
    assert r.status_code == 200
    assert "Connect Google Workspace" in r.text


def test_import_shows_dwd_and_stores_creds(ctx):
    client, base, vault, _ = ctx
    cfg = base / "cfg"
    cfg.mkdir()
    (cfg / "oauth2.txt").write_text("tok")
    (cfg / "oauth2service.json").write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    r = client.post("/setup/import", data={"domain": "ex.com", "admin": "a@ex.com", "config_dir": str(cfg)})
    assert r.status_code == 200
    assert "CID.apps" in r.text                 # DWD client id surfaced
    assert "copyEl(" in r.text                   # client id has a copy button
    assert vault.has_credentials("ex.com")


def test_import_requires_fields(ctx):
    client = ctx[0]
    r = client.post("/setup/import", data={"domain": "", "admin": "", "config_dir": ""})
    assert "domain" in r.text.lower()


def test_verify_activates_connector(ctx):
    client, _, vault, state = ctx
    vault.set_all("ex.com", {"oauth2": "tok", "oauth2service": json.dumps({"client_id": "x"})})
    r = client.post("/setup/verify", data={"domain": "ex.com", "admin": "a@ex.com"})
    assert r.status_code == 200
    assert "connected" in r.text.lower()
    assert state.connector is not None and state.audit_domain == "ex.com"


def test_fresh_shows_commands(ctx):
    client = ctx[0]
    r = client.post("/setup/fresh", data={"domain": "ex.com", "admin": "a@ex.com"})
    assert "create project" in r.text
    assert "GAMCFGDIR" in r.text
    assert "copyEl(" in r.text  # each command has a copy button
