"""Shared test fixtures. Everything here runs fully offline against a mock `gam` binary."""

from __future__ import annotations

import itertools
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


@pytest.fixture(scope="session")
def _sent_argv(tmp_path_factory):
    """Every distinct argv the tests sent the mock `gam`, with the first test that sent it. When the session
    ends each must be a shape the grammar vouches for (test_command_contract.py `unshaped_argv`) — reported
    as an error at the last test's teardown, naming every offender. A test that sends argv no builder emits
    on purpose (a malformed shape the mock must refuse, a partial one seeding its state) says so with
    @pytest.mark.hand_built_argv."""
    sent: dict = {}
    yield tmp_path_factory.mktemp("gam_argv"), itertools.count(), sent
    from .test_command_contract import unshaped_argv

    offenders = unshaped_argv(sent)
    if offenders:
        raise AssertionError(
            f"{len(offenders)} argv sent to the mock gam aren't shapes the vendored grammar vouches for. Build "
            "the argv with a GAMCommands builder (fix it against GamCommands.txt), or mark a test that sends "
            "one on purpose @pytest.mark.hand_built_argv:\n" + "\n".join(offenders))


@pytest.fixture(autouse=True)
def _gam_argv_log(request, _sent_argv, monkeypatch):
    """Record every argv the mock `gam` receives during each test (GAM_MOCK_ARGV_LOG)."""
    from .helpers import read_gam_calls

    logs, n, sent = _sent_argv
    log = logs / f"{next(n)}.log"
    monkeypatch.setenv("GAM_MOCK_ARGV_LOG", str(log))
    yield log
    if request.node.get_closest_marker("hand_built_argv") is None:
        for argv in read_gam_calls(log):
            sent.setdefault(tuple(argv), request.node.nodeid)


@pytest.fixture
def gam_calls(_gam_argv_log: Path):
    """Every argv the mock `gam` has received in this test so far; call the fixture to read them."""
    from .helpers import read_gam_calls

    return lambda: read_gam_calls(_gam_argv_log)


@pytest.fixture
def gam_state(tmp_path: Path, monkeypatch) -> Path:
    """Let the mock `gam` keep, between calls, the settings real GAM merges into (GAM_MOCK_STATE):
    `vacation` then updates only the fields it names, as GAM does, and `show vacation` shows what
    survived. Opt-in — without it the mock is stateless and `show vacation` is canned."""
    state = tmp_path / "gam_state"
    state.mkdir()
    monkeypatch.setenv("GAM_MOCK_STATE", str(state))
    return state


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
