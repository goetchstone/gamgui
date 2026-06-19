"""The destructive-operation guard.

Every mutation passes through here before it runs. Given the list of :class:`ChangePreview`
objects a connector produced (its dry-run), the guard decides what confirmation the UI must
require. This is the single chokepoint that makes "show exactly which accounts will be affected,
and make me confirm" a property of the whole app rather than something each screen reimplements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .connectors.base import ChangePreview, RiskLevel

# A bulk action at/above this count needs typed confirmation, not just a click.
DEFAULT_BULK_THRESHOLD = 10
# Above this count we additionally flag the operation as unusually large.
DEFAULT_HARD_CAP = 200


@dataclass
class GuardDecision:
    max_risk: RiskLevel
    affected: List[str]
    requires_confirmation: bool
    requires_typed_confirmation: bool
    over_hard_cap: bool
    summary: str
    warnings: List[str] = field(default_factory=list)

    @property
    def affected_count(self) -> int:
        return len(self.affected)


def evaluate(
    previews: Sequence[ChangePreview],
    bulk_threshold: int = DEFAULT_BULK_THRESHOLD,
    hard_cap: int = DEFAULT_HARD_CAP,
) -> GuardDecision:
    """Decide the confirmation policy for a planned set of changes."""
    if not previews:
        return GuardDecision(
            max_risk=RiskLevel.READ_ONLY,
            affected=[],
            requires_confirmation=False,
            requires_typed_confirmation=False,
            over_hard_cap=False,
            summary="No changes.",
        )

    max_risk = max(p.risk for p in previews)
    affected = [p.target for p in previews]
    count = len(affected)
    is_destructive = max_risk == RiskLevel.DESTRUCTIVE
    is_bulk = count >= bulk_threshold

    # Confirmation rules:
    #   - destructive: always confirm.
    #   - bulk mutation (>= threshold) of any non-read risk: confirm.
    #   - destructive AND bulk: require typing to confirm.
    requires_confirmation = is_destructive or (is_bulk and max_risk >= RiskLevel.LOW)
    requires_typed_confirmation = is_destructive and is_bulk
    over_hard_cap = count > hard_cap

    warnings: List[str] = []
    if over_hard_cap:
        warnings.append(
            f"This affects {count} accounts (over the {hard_cap} safety threshold). Double-check the target set."
        )

    verb = {
        RiskLevel.READ_ONLY: "Read",
        RiskLevel.LOW: "Change",
        RiskLevel.DESTRUCTIVE: "DESTRUCTIVE change",
    }[max_risk]
    summary = f"{verb}: {count} target{'s' if count != 1 else ''} affected."

    return GuardDecision(
        max_risk=max_risk,
        affected=affected,
        requires_confirmation=requires_confirmation,
        requires_typed_confirmation=requires_typed_confirmation,
        over_hard_cap=over_hard_cap,
        summary=summary,
        warnings=warnings,
    )
