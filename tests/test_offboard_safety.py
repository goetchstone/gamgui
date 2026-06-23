"""Offboarding safety: the all-users calendar-ACL sweep is best-effort (tolerates the departing
user's own-calendar / not-shared cases), and deletion is gated on data-transfer completion."""

from __future__ import annotations

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.errors import GAMError, GAMErrorKind


class _RaisingRunner:
    def __init__(self, exc):
        self.exc = exc

    async def run_authenticated(self, domain, argv, timeout=None, serialize=False):
        raise self.exc


def _conn(runner, tmp_path) -> GAMConnector:
    return GAMConnector(runner=runner, domain="example.com", audit=AuditLog(tmp_path / "audit.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [GAMErrorKind.NOT_FOUND, GAMErrorKind.PERMISSION_DENIED])
async def test_remove_from_all_calendars_tolerates_benign(kind, tmp_path):
    # NOT_FOUND (user never shared with X) and PERMISSION_DENIED (cannotChangeOwnAcl on X's own
    # primary calendar) are expected per-entity outcomes — the sweep still succeeds overall.
    exc = GAMError(kind=kind, exit_code=50,
                   stderr="ERROR: 403: Cannot change your own access level. - cannotChangeOwnAcl")
    res = await _conn(_RaisingRunner(exc), tmp_path).remove_from_all_calendars("x@example.com")
    assert res.ok
    assert "best-effort" in (res.detail or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [GAMErrorKind.SCOPE_MISSING, GAMErrorKind.AUTH_EXPIRED])
async def test_remove_from_all_calendars_still_fails_on_real_errors(kind, tmp_path):
    exc = GAMError(kind=kind, exit_code=1, stderr="insufficient authentication scope")
    res = await _conn(_RaisingRunner(exc), tmp_path).remove_from_all_calendars("x@example.com")
    assert not res.ok


@pytest.mark.asyncio
async def test_incomplete_transfers_for(connector):
    pending = await connector.incomplete_transfers_for("xferpending@example.com")
    assert pending and pending[0]["application"] == "Drive and Docs"
    assert all(t["status"].lower() != "completed" for t in pending)   # the completed row is filtered out
    assert await connector.incomplete_transfers_for("nobody@example.com") == []
