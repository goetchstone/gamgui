"""Offboarding safety: the all-users calendar-ACL sweep is best-effort (tolerates the departing
user's own-calendar / not-shared cases), and deletion is gated on data-transfer completion."""

from __future__ import annotations

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import DEFAULT_TIMEOUT, DOMAIN_WIDE_TIMEOUT


class _RaisingRunner:
    def __init__(self, exc):
        self.exc = exc

    async def run_authenticated(self, domain, argv, timeout=None, serialize=False):
        raise self.exc


def _conn(runner, tmp_path) -> GAMConnector:
    return GAMConnector(runner=runner, domain="example.com", audit=AuditLog(tmp_path / "audit.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize("line", [
    "User: bob@example.com, Service not applicable/Does not exist",               # never shared: NOT_FOUND
    "    Calendar: x@example.com, Calendar ACL: (Scope: user:x@example.com), "
    "Delete Failed: Cannot change your own access level.",                        # X's own calendar: OWN_ACL
    "User: dave@example.com, Calendar Service/App not enabled (4/120)",            # no Calendar: SERVICE_NOT_ENABLED
])
async def test_remove_from_all_calendars_tolerates_benign(line, tmp_path):
    # Expected per-user outcomes of a domain-wide sweep — it still succeeds overall. A user without
    # Calendar (a Calendar-off OU, a licence without it) used to fail the step on every run.
    exc = GAMError.from_run(50, line)
    res = await _conn(_RaisingRunner(exc), tmp_path).remove_from_all_calendars("x@example.com")
    assert res.ok
    assert "best-effort" in (res.detail or "")


@pytest.mark.asyncio
async def test_remove_from_all_calendars_does_not_tolerate_a_permission_refusal(tmp_path):
    # Only the leaver's OWN-ACL refusal is benign. Every PERMISSION_DENIED line used to be tolerated,
    # so a real 403 removing the leaver from someone's calendar counted as a clean sweep.
    stderr = ("User: bob@example.com, Service not applicable/Does not exist\n"
              "    Calendar: carol@example.com, Calendar ACL: (Scope: user:x@example.com), "
              "Delete Failed: 403: Forbidden - insufficientPermissions\n")
    res = await _conn(_RaisingRunner(GAMError.from_run(50, stderr)), tmp_path).remove_from_all_calendars("x@example.com")
    assert not res.ok and "insufficientPermissions" in res.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [GAMErrorKind.SCOPE_MISSING, GAMErrorKind.AUTH_EXPIRED, GAMErrorKind.TIMEOUT])
async def test_remove_from_all_calendars_still_fails_on_real_errors(kind, tmp_path):
    # TIMEOUT: the sweep was killed partway — never a best-effort success.
    exc = GAMError(kind=kind, exit_code=1, stderr="insufficient authentication scope")
    res = await _conn(_RaisingRunner(exc), tmp_path).remove_from_all_calendars("x@example.com")
    assert not res.ok


@pytest.mark.asyncio
async def test_remove_from_all_calendars_needs_every_line_tolerable(tmp_path):
    # The reported kind is tolerable, but one error line is not: a partial failure, never a success.
    exc = GAMError(kind=GAMErrorKind.OWN_ACL, exit_code=50,
                   kinds=frozenset({GAMErrorKind.OWN_ACL, GAMErrorKind.UNKNOWN}))
    res = await _conn(_RaisingRunner(exc), tmp_path).remove_from_all_calendars("x@example.com")
    assert not res.ok


class _RecordingRunner:
    def __init__(self):
        self.timeouts = []

    async def run_authenticated(self, domain, argv, timeout=None, serialize=False):
        self.timeouts.append((list(argv[:2]), timeout))
        return ""


@pytest.mark.asyncio
async def test_domain_wide_calls_get_the_long_timeout(tmp_path):
    # `all users …` walks every user in one gam process; under the 120s default a sweep of a few
    # hundred users was killed partway. Per-user calls keep the runner's default (None here).
    runner = _RecordingRunner()
    conn = _conn(runner, tmp_path)
    await conn.remove_from_all_calendars("x@example.com")
    await conn.scan_all_calendars()
    await conn.transfer_data("x@example.com", "drive,calendar", "m@example.com")
    domain_wide = [t for head, t in runner.timeouts if head == ["all", "users"]]
    assert domain_wide == [DOMAIN_WIDE_TIMEOUT, DOMAIN_WIDE_TIMEOUT] and DOMAIN_WIDE_TIMEOUT > DEFAULT_TIMEOUT
    assert all(t is None for head, t in runner.timeouts if head != ["all", "users"])


@pytest.mark.asyncio
async def test_incomplete_transfers_for(connector):
    pending = await connector.incomplete_transfers_for("xferpending@example.com")
    assert pending and pending[0]["application"] == "Drive and Docs"
    assert all(t["status"].lower() != "completed" for t in pending)   # the completed row is filtered out
    assert await connector.incomplete_transfers_for("nobody@example.com") == []
