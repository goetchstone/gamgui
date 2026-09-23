"""Shared test fixtures. Everything here runs fully offline against a mock `gam` binary."""

from __future__ import annotations

import os
import threading
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


@pytest.fixture
def gam_calls(tmp_path: Path, monkeypatch):
    """Record every argv the mock `gam` receives from here on; call the fixture to read them."""
    from .helpers import read_gam_calls

    log = tmp_path / "gam_argv.log"
    monkeypatch.setenv("GAM_MOCK_ARGV_LOG", str(log))
    return lambda: read_gam_calls(log)


FIFO_WATCHDOG_SECONDS = 3.0


@pytest.fixture
def fifo():
    """Factory: ``os.mkfifo`` at a path, with a watchdog for code that wrongly blocks opening it.

    A FIFO's read-side open blocks until a writer appears, so a regression would hang the suite (a
    worker thread stuck in ``open`` outlives even a pytest-timeout). After ``FIFO_WATCHDOG_SECONDS``
    the watchdog opens the write end, which releases any blocked reader; a test asserting it finished
    well inside that window then fails instead of hanging.
    """
    timers = []

    def make(path: Path) -> Path:
        os.mkfifo(path)

        def release() -> None:
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass            # ENXIO: nobody is blocked reading it, the good case

        timer = threading.Timer(FIFO_WATCHDOG_SECONDS, release)
        timer.daemon = True
        timer.start()
        timers.append(timer)
        return path

    yield make
    for timer in timers:
        timer.cancel()
