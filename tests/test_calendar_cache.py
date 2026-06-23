"""The domain-wide calendar scan (`all users print calendars`) is the slow path behind calendar
name search. These tests pin the caching contract: repeat searches reuse one scan, deleting a
calendar invalidates it, and `force` bypasses it."""

from __future__ import annotations

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector


class _CountingRunner:
    """Minimal GAMRunner stub: counts calls and serves the all-calendars scan fixture."""

    def __init__(self, scan_payload: str) -> None:
        self.scan_payload = scan_payload
        self.calls: list = []

    async def run_authenticated(self, domain, argv, timeout=None, serialize=False):
        self.calls.append(list(argv))
        if "all" in argv and "print" in argv and "calendars" in argv:
            return self.scan_payload
        return "ok"

    def scans(self) -> int:
        return sum(1 for a in self.calls if "all" in a and "print" in a and "calendars" in a)


def _conn(runner, tmp_path) -> GAMConnector:
    return GAMConnector(runner=runner, domain="example.com", audit=AuditLog(path=tmp_path / "audit.jsonl"))


@pytest.mark.asyncio
async def test_search_calendars_caches_the_scan(fixtures_dir, tmp_path):
    r = _CountingRunner((fixtures_dir / "all_calendars.csv").read_text())
    c = _conn(r, tmp_path)
    first = await c.search_calendars("house")
    await c.search_calendars("ops")              # different term -> must reuse the cached scan
    assert r.scans() == 1
    assert any(d["summary"] == "House Call Calendar" for d in first)


@pytest.mark.asyncio
async def test_delete_calendar_invalidates_scan_cache(fixtures_dir, tmp_path):
    r = _CountingRunner((fixtures_dir / "all_calendars.csv").read_text())
    c = _conn(r, tmp_path)
    await c.search_calendars("house")            # scan #1 (then cached)
    await c.delete_calendar("alice@example.com", "c_house123@group.calendar.google.com")
    await c.search_calendars("house")            # cache invalidated -> scan #2
    assert r.scans() == 2


@pytest.mark.asyncio
async def test_search_calendars_force_bypasses_cache(fixtures_dir, tmp_path):
    r = _CountingRunner((fixtures_dir / "all_calendars.csv").read_text())
    c = _conn(r, tmp_path)
    await c.search_calendars("house")
    await c.search_calendars("house", force=True)
    assert r.scans() == 2
