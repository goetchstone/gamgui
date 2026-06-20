from __future__ import annotations

import json
from pathlib import Path

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import FILENAMES, InMemoryBackend, SecretsVault
from gamgui.core.setup import SetupService, _extract_auth_url, _parse_check


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
    cmds = info["commands"]
    assert "GAMCFGDIR" in info["env"]
    # `create project` takes the admin; oauth create / svcacct must NOT (that bug dropped oauth2.txt)
    assert any("create project admin@ex.com" in c for c in cmds)
    assert any(c.endswith("oauth create") for c in cmds)
    assert any(c.endswith("create svcacct") for c in cmds)
    # oauth create (writes oauth2.txt) must come before svcacct
    order = [i for i, c in enumerate(cmds) if "oauth create" in c or "create svcacct" in c]
    assert cmds[order[0]].endswith("oauth create")


def test_parse_check_pulls_pass_fail():
    out = "System time status: PASS\nSome scope: FAIL\nno colon line\n"
    lines = _parse_check(out)
    assert ("System time status", "PASS") in lines
    assert ("Some scope", "FAIL") in lines
    assert len(lines) == 2


def test_parse_check_handles_scope_table_format():
    # GAM's real `check serviceaccount` output: "<scope-url>   FAIL (n/m)"
    out = (
        "Domain-wide Delegation authentication:, User: a@e.com, Scopes: 2\n"
        "  https://mail.google.com/                         FAIL (1/2)\n"
        "  https://www.googleapis.com/auth/calendar         PASS (2/2)\n"
    )
    lines = _parse_check(out)
    assert ("https://mail.google.com/", "FAIL") in lines
    assert ("https://www.googleapis.com/auth/calendar", "PASS") in lines


def test_extract_auth_url():
    out = "Some scopes FAILED!\nplease go to:\n    https://gam-shortn.appspot.com/qhhmzr\nthen retry"
    assert _extract_auth_url(out) == "https://gam-shortn.appspot.com/qhhmzr"
    assert _extract_auth_url("all good, no link") == ""


async def test_engine_version(runner, vault):
    assert EXPECTED_GAM_VERSION in await SetupService(vault, runner).engine_version()


async def test_engine_version_warning_silent_when_matched(runner, vault):
    # The mock reports EXPECTED_GAM_VERSION -> no warning.
    assert await SetupService(vault, runner).engine_version_warning() == ""


async def test_engine_version_warning_on_mismatch(runner, vault, monkeypatch):
    svc = SetupService(vault, runner)

    async def fake_version() -> str:
        return "GAM 9.99.99 - mock"

    monkeypatch.setattr(svc, "engine_version", fake_version)
    warning = await svc.engine_version_warning()
    assert "9.99.99" in warning and EXPECTED_GAM_VERSION in warning  # fail-soft: warns, never blocks


async def test_verify_passes_with_mock(runner, vault, domain):
    result = await SetupService(vault, runner).verify(domain, "admin@example.com")
    assert result.ok is True
    assert any(status == "PASS" for _, status in result.lines)


async def test_verify_without_credentials(runner):
    empty = SecretsVault(InMemoryBackend())
    result = await SetupService(empty, runner).verify("nope.com", "a@nope.com")
    assert result.ok is False
    assert "No credentials" in result.summary
