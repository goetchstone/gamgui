"""Shared test fixtures. Everything here runs fully offline against a mock `gam` binary."""

from __future__ import annotations

from pathlib import Path

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault

FIXTURES = Path(__file__).parent / "fixtures"
MOCK_GAM = FIXTURES / "mock_gam.sh"
DOMAIN = "example.com"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def domain() -> str:
    return DOMAIN


@pytest.fixture
def vault() -> SecretsVault:
    v = SecretsVault(backend=InMemoryBackend())
    v.set_all(
        DOMAIN,
        {
            "client_secrets": '{"installed": {"client_id": "fake"}}',
            "oauth2": "fake-oauth2-token",
            "oauth2service": '{"type": "service_account", "private_key": "fake"}',
        },
    )
    return v


@pytest.fixture
def runner(vault: SecretsVault, tmp_path: Path, monkeypatch) -> GAMRunner:
    # The mock gam reads its canned responses from this directory.
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    return GAMRunner(vault=vault, gam_binary=MOCK_GAM, base_dir=tmp_path, timeout=15)


@pytest.fixture
def connector(runner: GAMRunner, tmp_path: Path) -> GAMConnector:
    return GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
