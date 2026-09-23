"""A mock-backed GamGUI for looking at the UI — no Google, no Keychain, no real gam.

    .venv/bin/python scripts/preview_mock.py
    open 'http://127.0.0.1:8766/?token=t'

Every gam call goes to the strict offline mock (tests/fixtures/mock_gam.sh), the vault is in memory
with fake credentials, and all state — audit log, runbooks, signature templates, calendar index, even
$HOME, so the setup screen can't see a real ~/.gam — lives in a temp dir deleted on exit. Set
GAM_MOCK_ARGV_LOG=<file> to record every argv the mock receives.

A write that "works" here only proves the mock accepted it (CLAUDE.md, "the mock lies").
"""

from __future__ import annotations

import os
import signal
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PORT = 8766
TOKEN = "t"
DOMAIN = "example.com"

# Secondary calendars carry ids this long, and they are what overflows a narrow column.
CALENDARS = [
    ("c_8f2e6a1d4b9c3e7f0a5d2b8c6e1f4a9d3b7c0e5f8a2d6b1c9e4f7a0d3b6c8e2f@group.calendar.google.com",
     "Sales Team", "alice@example.com", 14),
    ("c_3b7d1f5a9c2e6b0d4f8a1c5e9b3d7f0a2c6e8b4d1f5a9c3e7b0d2f6a8c4e1b5d@group.calendar.google.com",
     "Company Holidays & Office Closures", "alice@example.com", 212),
    ("c_e4a8c2f6b0d3e7a1c5f9b2d6e0a4c8f1b5d9e3a7c0f4b8d2e6a9c3f7b1d5e0a2@group.calendar.google.com",
     "Training Room Bookings", "carol@example.com", 3),
]


def build_app(state_dir: Path):
    from gamgui.core.audit import AuditLog
    from gamgui.core.calendar_index import CalendarIndex, IndexedCalendar
    from gamgui.core.connectors.gam_connector import GAMConnector
    from gamgui.core.gam.runner import GAMRunner
    from gamgui.core.onboarding import RunbookStore
    from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
    from gamgui.core.signatures import SignatureStore
    from gamgui.web.server import AppState, create_app, loopback_hosts

    vault = SecretsVault(backend=InMemoryBackend())
    vault.set_all(DOMAIN, {
        "client_secrets": '{"installed": {"client_id": "fake"}}',
        "oauth2": "fake-oauth2-token",
        "oauth2service": '{"type": "service_account", "private_key": "fake"}',
    })
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=state_dir)
    connector = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(state_dir / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=connector, token=TOKEN)

    state.sig_templates = SignatureStore(state_dir / "signatures.json")
    state.runbooks = RunbookStore(state_dir / "onboarding.json")
    state.runbooks.set_role(
        "Sales Associate",
        ["Order a laptop", "Set up the CRM login", "Book the product training"],
        signature="Classic", org_unit="/Sales",
        groups=["sales@example.com", "staff@example.com"],
        calendars=[CALENDARS[0][0], CALENDARS[1][0]],
    )
    state.calendar_index = CalendarIndex(state_dir / "calendar_index.db")
    state.calendar_index.replace_all(DOMAIN, [
        IndexedCalendar(cid, name, owner, "secondary", subs) for cid, name, owner, subs in CALENDARS])
    return create_app(state, allowed_hosts=loopback_hosts(PORT))


def main() -> None:
    sys.path.insert(0, str(ROOT))
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="gamgui-preview-") as tmp:
        state_dir = Path(tmp)
        os.environ["HOME"] = str(state_dir)          # app_data_dir() and the setup scan stay in here
        for var in ("GAMCFGDIR", "XDG_DATA_HOME", "GAMGUI_GAM_BINARY"):
            os.environ.pop(var, None)
        os.environ["GAM_MOCK_FIXTURES"] = str(FIXTURES)
        app = build_app(state_dir)
        print(f"GamGUI preview (mock gam, fake data): http://127.0.0.1:{PORT}/?token={TOKEN}", flush=True)
        # uvicorn re-raises the stop signal after shutting down; SIGTERM's default action would kill
        # the process before the temp dir is removed, so turn it into an exit that unwinds this block.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:   # Ctrl-C: uvicorn re-raises it after shutdown; the temp dir is gone
        pass
