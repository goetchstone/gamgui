"""Shared test fixtures. Everything here runs fully offline against a mock `gam` binary."""

from __future__ import annotations

import itertools
import os
import pwd
import sys
import threading
from pathlib import Path

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.paths import app_data_dir
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault

FIXTURES = Path(__file__).parent / "fixtures"
MOCK_GAM = FIXTURES / "mock_gam.sh"
DOMAIN = "example.com"


# --- the operator's real data is off limits ---
# A store that isn't handed a path (RunbookStore(), SignatureStore(), AuditLog(), a runner with no
# base_dir, AppState.create) falls back to app_data_dir() — on this Mac the operator's real
# ~/Library/Application Support/GamGUI, holding their roles, templates and audit log. A web fixture that
# forgot one read their real onboarding roles and could append to their real audit.jsonl. So every test
# runs with HOME pointed at its own temp dir (`_hermetic_home`), and an audit hook refuses — and fails
# the test for — any open/mkdir/listdir/remove/sqlite connect under the real app-data dir or ~/.gam.


class RealAppDataAccess(RuntimeError):
    """A test reached the operator's real app-data dir or ~/.gam. Raised before the access happens."""


def _guard_variants(path: Path) -> set:
    """The spellings a path under *path* can arrive in: as given, resolved, and via the data-volume firmlink."""
    out = {os.path.normpath(str(path)), os.path.realpath(path)}
    if sys.platform == "darwin":
        out |= {"/System/Volumes/Data" + p for p in list(out) if p.startswith("/Users/")}
    return {p.casefold() if sys.platform == "darwin" else p for p in out}


def _real_homes() -> set:
    homes = {Path(os.path.expanduser("~"))}
    try:
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    return homes


# Computed at import, before any test can point HOME elsewhere: the real places, whichever spelling.
REAL_DATA_ROOTS: set = set()
for _home in _real_homes():
    REAL_DATA_ROOTS |= _guard_variants(_home / ".gam")
REAL_DATA_ROOTS |= _guard_variants(app_data_dir())
if sys.platform == "darwin":
    for _home in _real_homes():
        REAL_DATA_ROOTS |= _guard_variants(_home / "Library" / "Application Support" / "GamGUI")

REAL_DATA_VIOLATIONS: list = []         # (event, path) — every access the hook refused, in order
_WATCHED_EVENTS = frozenset({
    "open", "os.mkdir", "os.listdir", "os.scandir", "os.remove", "os.rmdir", "os.rename", "os.chmod",
    "os.chown", "os.truncate", "os.utime", "os.symlink", "os.link", "shutil.rmtree", "shutil.copyfile",
    "shutil.move", "sqlite3.connect",
})


def _under_real_data(arg) -> str:
    if not isinstance(arg, (str, bytes, os.PathLike)):
        return ""                        # an fd, a mode, a flag
    try:
        p = os.path.abspath(os.fsdecode(arg))
    except (TypeError, ValueError):
        return ""
    key = p.casefold() if sys.platform == "darwin" else p
    for root in REAL_DATA_ROOTS:
        if key == root or key.startswith(root + os.sep):
            return p
    return ""


def _real_data_guard(event: str, args: tuple) -> None:
    if event not in _WATCHED_EVENTS:
        return
    for arg in args:
        hit = _under_real_data(arg)
        if hit:
            REAL_DATA_VIOLATIONS.append((event, hit))
            raise RealAppDataAccess(f"test touched the operator's real data: {event} {hit}")


sys.addaudithook(_real_data_guard)      # can't be removed; it only ever refuses the real roots above


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """HOME is this test's tmp_path, so every app_data_dir() fallback (the audit log a /setup/verify
    connector writes, the /onboard roles, the signature templates, the runner's run/ dir, ~/.gam in the
    setup wizard) lands in a throwaway folder no other test shares. $GAMCFGDIR is cleared for the same
    reason: an operator's own would point the wizard at their real GAM config. A test that needs another
    home sets HOME itself after this (test_setup_web's ``ctx`` does)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))     # app_data_dir off macOS
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_app_data(_hermetic_home):
    """Fail any test during which the hook refused an access to the real app-data dir or ~/.gam — even
    one the code under test swallowed (a best-effort write, a background job)."""
    start = len(REAL_DATA_VIOLATIONS)
    yield
    hits = REAL_DATA_VIOLATIONS[start:]
    if hits:
        pytest.fail("reached the operator's real app data (point the store at tmp_path):\n"
                    + "\n".join(f"  {event} {path}" for event, path in hits), pytrace=False)


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
