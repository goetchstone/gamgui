from __future__ import annotations

from gamgui.core import guard
from gamgui.core.connectors.base import ChangePreview, ConnectorID, RiskLevel


def _preview(target: str, risk: RiskLevel) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=target, summary="x", risk=risk)


def test_empty_no_confirmation():
    d = guard.evaluate([])
    assert d.requires_confirmation is False
    assert d.affected_count == 0


def test_single_low_risk_no_confirmation():
    d = guard.evaluate([_preview("a@e.com", RiskLevel.LOW)])
    assert d.requires_confirmation is False
    assert d.max_risk == RiskLevel.LOW


def test_single_destructive_confirms_without_typing():
    d = guard.evaluate([_preview("a@e.com", RiskLevel.DESTRUCTIVE)])
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is False
    assert d.affected == ["a@e.com"]


def test_bulk_destructive_requires_typed_confirmation():
    previews = [_preview(f"u{i}@e.com", RiskLevel.DESTRUCTIVE) for i in range(12)]
    d = guard.evaluate(previews)
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is True


def test_bulk_low_risk_confirms_but_no_typing():
    previews = [_preview(f"u{i}@e.com", RiskLevel.LOW) for i in range(12)]
    d = guard.evaluate(previews)
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is False


def test_over_hard_cap_warns():
    previews = [_preview(f"u{i}@e.com", RiskLevel.DESTRUCTIVE) for i in range(5)]
    d = guard.evaluate(previews, hard_cap=3)
    assert d.over_hard_cap is True
    assert d.warnings
