"""Saved signature templates: the JSON store units + the /signatures/templates web flow."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.signatures import _DEFAULT_TEMPLATES, SignatureStore
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
    state.sig_templates = SignatureStore(tmp_path / "signatures.json")  # isolated store, not the real ~/Library file
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


# --- the store ---

def test_store_seeds_three_starters(tmp_path):
    s = SignatureStore(tmp_path / "sig.json")
    names = s.names()
    assert names == sorted(_DEFAULT_TEMPLATES)                 # exactly the seed set
    assert "Classic" in names and "Modern accent" in names and "Minimal" in names
    assert "Your Company" in s.get("Classic")                 # placeholder company text present


def test_store_round_trip(tmp_path):
    s = SignatureStore(tmp_path / "sig.json")
    s.save("Support", "<div>{name} · Support</div>")
    assert s.get("Support") == "<div>{name} · Support</div>"
    # reload from disk → persisted
    s2 = SignatureStore(tmp_path / "sig.json")
    assert "Support" in s2.names()
    s2.delete("Support")
    assert "Support" not in SignatureStore(tmp_path / "sig.json").names()


def test_store_corrupt_file_falls_back_to_seed(tmp_path):
    p = tmp_path / "sig.json"
    p.write_text("{ this is not valid json")
    s = SignatureStore(p)
    assert "Classic" in s.names()   # seeds, not a crash


def test_store_file_is_owner_only(tmp_path):
    p = tmp_path / "sig.json"
    SignatureStore(p).save("X", "<div>{name}</div>")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_store_rejects_blank_name(tmp_path):
    with pytest.raises(ValueError):
        SignatureStore(tmp_path / "sig.json").save("   ", "<div>x</div>")


def test_store_rejects_blank_body(tmp_path):
    with pytest.raises(ValueError):
        SignatureStore(tmp_path / "sig.json").save("Name", "   ")


def test_store_rejects_overlong_name(tmp_path):
    with pytest.raises(ValueError):
        SignatureStore(tmp_path / "sig.json").save("x" * 61, "<div>x</div>")


# --- the web flow ---

def test_templates_partial_lists_seeds_with_load_buttons(client):
    r = client.get("/signatures/templates")
    assert r.status_code == 200
    for name in ("Classic", "Modern accent", "Minimal"):
        assert name in r.text
    assert "data-body=" in r.text and "sigLoadTemplate" in r.text   # Load fills the editor XSS-safely


def test_save_current_as_appears(client):
    r = client.post("/signatures/templates/save",
                    data={"name": "Support desk", "template": "<div>{name} · Support</div>"})
    assert r.status_code == 200 and "Support desk" in r.text
    assert "Classic" in r.text   # seeds still listed alongside the new one


def test_delete_removes_template(client):
    client.post("/signatures/templates/save", data={"name": "Holiday promo", "template": "<div>{name}</div>"})
    r = client.post("/signatures/templates/delete", data={"name": "Holiday promo"})
    assert r.status_code == 200 and "Holiday promo" not in r.text
    assert "Classic" in r.text   # a seed survives the delete


def test_save_blank_name_shows_error(client):
    r = client.post("/signatures/templates/save", data={"name": "  ", "template": "<div>x</div>"})
    assert r.status_code == 200
    assert "required" in r.text.lower() and "bg-amber-50" in r.text   # amber error styling


def test_signatures_page_mounts_templates_container(client):
    r = client.get("/signatures")
    assert r.status_code == 200
    assert 'id="sig-templates"' in r.text and 'hx-get="/signatures/templates"' in r.text
