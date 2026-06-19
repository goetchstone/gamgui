from __future__ import annotations

import json
from pathlib import Path

from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import FILENAMES, InMemoryBackend, SecretsVault
from gamgui.core.setup import SetupService, _parse_check


def _write_config(d: Path, with_client_id: bool = True) -> None:
    (d / "client_secrets.json").write_text('{"installed": {"client_id": "x"}}')
    (d / "oauth2.txt").write_text("admin-refresh-token")
    svc = {"type": "service_account", "private_key": "k"}
    if with_client_id:
        svc["client_id"] = "123456789.apps.googleusercontent.com"
    (d / "oauth2service.json").write_text(json.dumps(svc))


def _svc(vault: SecretsVault, tmp_path: Path) -> SetupService:
    return SetupService(vault, GAMRunner(vault, gam_binary=tmp_path / "no-binary"))


def test_inspect_detects_required_files(tmp_path):
    _write_config(tmp_path)
    insp = _svc(SecretsVault(InMemoryBackend()), tmp_path).inspect(tmp_path)
    assert insp.files["oauth2service"] is True
    assert insp.has_required is True


def test_inspect_missing_is_not_ready(tmp_path):
    (tmp_path / "client_secrets.json").write_text("{}")  # only the non-critical file
    insp = _svc(SecretsVault(InMemoryBackend()), tmp_path).inspect(tmp_path)
    assert insp.any_present is True
    assert insp.has_required is False


def test_import_dir_populates_vault(tmp_path):
    _write_config(tmp_path)
    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")
    assert set(imported) == set(FILENAMES.keys())
    assert vault.has_credentials("ex.com")
    assert "service_account" in (vault.get("ex.com", "oauth2service") or "")


def test_dwd_details_extracts_client_id(tmp_path):
    _write_config(tmp_path)
    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    svc.import_dir(tmp_path, "ex.com")
    assert svc.dwd_details("ex.com")["client_id"] == "123456789.apps.googleusercontent.com"


def test_setup_commands_shape(tmp_path):
    info = _svc(SecretsVault(InMemoryBackend()), tmp_path).setup_commands("admin@ex.com")
    assert "GAMCFGDIR" in info["env"]
    assert any("admin@ex.com" in c for c in info["commands"])
    assert any("create project" in c for c in info["commands"])


def test_parse_check_pulls_pass_fail():
    out = "System time status: PASS\nSome scope: FAIL\nno colon line\n"
    lines = _parse_check(out)
    assert ("System time status", "PASS") in lines
    assert ("Some scope", "FAIL") in lines
    assert len(lines) == 2


async def test_engine_version(runner, vault):
    assert "7.46.01" in await SetupService(vault, runner).engine_version()


async def test_verify_passes_with_mock(runner, vault, domain):
    result = await SetupService(vault, runner).verify(domain, "admin@example.com")
    assert result.ok is True
    assert any(status == "PASS" for _, status in result.lines)


async def test_verify_without_credentials(runner):
    empty = SecretsVault(InMemoryBackend())
    result = await SetupService(empty, runner).verify("nope.com", "a@nope.com")
    assert result.ok is False
    assert "No credentials" in result.summary
