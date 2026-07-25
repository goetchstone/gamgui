from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.setup import SetupService
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


def test_import_leaves_a_user_chosen_dirs_plaintext_alone(ctx):
    # The post-import wipe applies ONLY to our own managed staging dir. A folder the operator
    # picked — their own GAM install — keeps its files; we don't destroy someone else's config.
    client, base, vault, _ = ctx
    cfg = base / "their-gam"
    cfg.mkdir()
    (cfg / "oauth2.txt").write_text("tok")
    (cfg / "oauth2service.json").write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    r = client.post("/setup/import", data={"domain": "ex.com", "admin": "a@ex.com", "config_dir": str(cfg)})
    assert r.status_code == 200
    assert vault.has_credentials("ex.com")
    assert (cfg / "oauth2.txt").read_text() == "tok"
    assert (cfg / "oauth2service.json").is_file()


# --- the config_dir a form supplies has to be a real directory -------------------------------
# The operator choosing the folder is the feature, so there's no allow-list — but a path that
# isn't an existing directory is refused with a message, not reported as an empty import.


def _svc(state) -> SetupService:
    return SetupService(state.vault, state.runner)


def test_import_rejects_nonexistent_dir(ctx):
    _, base, vault, state = ctx
    with pytest.raises(ValueError, match="No such folder"):
        _svc(state).import_dir(base / "typo-not-here", "ex.com")
    assert not vault.has_credentials("ex.com")


def test_import_rejects_a_file(ctx):
    _, base, vault, state = ctx
    f = base / "oauth2service.json"
    f.write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    with pytest.raises(ValueError, match="not a folder"):
        _svc(state).import_dir(f, "ex.com")     # the file itself, not its parent
    assert not vault.has_credentials("ex.com")
    assert f.is_file()                          # and nothing of theirs was touched


def test_import_rejects_blank_dir(ctx):
    _, _, vault, state = ctx
    with pytest.raises(ValueError, match="Choose the folder"):
        _svc(state).import_dir("   ", "ex.com")
    assert not vault.has_credentials("ex.com")


# The three above call the service directly. These go over HTTP, because a rejected path has to
# reach the operator as a message they can act on — raising into the route turns a typo into a 500.
def test_import_route_reports_a_bad_path_instead_of_crashing(ctx):
    client, base, vault, _ = ctx
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(base / "typo-not-here")})
    assert r.status_code == 200
    assert "No such folder" in r.text
    assert not vault.has_credentials("ex.com")


def test_import_route_reports_a_file_instead_of_crashing(ctx):
    client, base, vault, _ = ctx
    f = base / "oauth2.txt"
    f.write_text("tok")
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(f)})
    assert r.status_code == 200
    assert "not a folder" in r.text
    assert not vault.has_credentials("ex.com")
    assert f.is_file()


def test_resolve_dir_accepts_a_real_dir_and_expands_home(ctx):
    _, base, _, state = ctx
    nested = base / "cfg" / "sub" / ".."
    (base / "cfg" / "sub").mkdir(parents=True)
    assert _svc(state).resolve_dir(nested) == (base / "cfg").resolve()
    assert _svc(state).resolve_dir("~").is_dir()


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
