"""The destructive-operation guard: what confirmation a mutation needs, and the check that it got it.

:func:`evaluate` decides, from the :class:`ChangePreview` list a mutation would make: destructive →
a Confirm click; bulk (>= ``DEFAULT_BULK_THRESHOLD``) → a Confirm click; destructive *and* bulk →
the operator types "confirm"; and, for a route that opts in with ``typed_count_above``, more targets
than that → the operator types how many. An account delete — any preview whose argv is
``GAMCommands.delete_user`` — also needs its exact address typed, on every route that deletes one.
It is pure, and a template renders its decision.

:func:`enforce` is the server-side half, and the one that counts: a mutating route calls it with the
posted form before its first GAM write and refuses when the form lacks what the decision requires.
A template showing a Confirm button proves nothing about the POST that comes back — five routes
once ran a suspend, an event delete, a company-wide signature overwrite and a whole offboarding on a
bare POST because only their templates asked (docs/failure-log.md, 2026-09-23).

What does not call it: single-target LOW writes (by this policy they need no confirmation), and
calendar delete, whose route demands a stronger typed value (``DELETE``). Account delete does call
it: the typed-email rule is here, so the Users delete zone, the Builder's "Delete account" and a
Builder sequence all get it (the Builder once deleted an account on one Confirm click).
``tests/test_write_routes_guarded.py`` enumerates every POST route and holds each to one of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from .connectors.base import ChangePreview, ConnectorID, RiskLevel
from .gam.commands import GAMCommands

# At/above this count a change is bulk: it needs a Confirm click, and the typed word if destructive.
DEFAULT_BULK_THRESHOLD = 10
# Above this count we additionally flag the operation as unusually large.
DEFAULT_HARD_CAP = 200
# Above this count, a route that opts in (``typed_count_above=``) makes the operator type the count: a
# number read off the preview, so a mis-scoped overwrite of many accounts can't be clicked through.
COUNT_CONFIRM_ABOVE = 25

# What a confirm step posts back — the templates must send exactly these, and `enforce` checks them.
CONFIRMED_FIELD = "confirmed"   # the Confirm button (hx-vals) or a hidden input: "1"
TYPED_FIELD = "confirm"         # a destructive bulk change: the operator types TYPED_WORD
TYPED_WORD = "confirm"
COUNT_FIELD = "confirm_count"   # a large opted-in change: the operator types the affected count
EMAIL_FIELD = "confirm_email"   # an account delete: the operator types each deleted address (repeatable)


@dataclass
class GuardDecision:
    max_risk: RiskLevel
    affected: List[str]
    requires_confirmation: bool
    requires_typed_confirmation: bool
    over_hard_cap: bool
    summary: str
    warnings: List[str] = field(default_factory=list)
    requires_typed_count: bool = False
    typed_emails: List[str] = field(default_factory=list)   # accounts deleted: each address must be typed

    @property
    def affected_count(self) -> int:
        return len(self.affected)


def deleted_account(preview: ChangePreview) -> Optional[str]:
    """The address ``preview`` deletes when its argv is an account delete (``GAMCommands.delete_user``)."""
    argv = list(preview.argv or [])
    return argv[-1] if argv and argv == GAMCommands.delete_user(argv[-1]) else None


def evaluate(
    previews: Sequence[ChangePreview],
    bulk_threshold: int = DEFAULT_BULK_THRESHOLD,
    hard_cap: int = DEFAULT_HARD_CAP,
    typed_count_above: Optional[int] = None,
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
        requires_typed_count=typed_count_above is not None and count > typed_count_above,
        typed_emails=list(dict.fromkeys(a for a in map(deleted_account, previews) if a)),
    )


def changes(targets: Iterable[str], risk: RiskLevel, summary: str) -> List[ChangePreview]:
    """Previews for a route whose connector call builds its own argv. Only ``evaluate``/``enforce``
    read them (target + risk); they are never applied."""
    return [ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=t, summary=summary, risk=risk)
            for t in targets]


def enforce(previews: Sequence[ChangePreview], form: Mapping[str, Any], *,
            confirm_step: bool = False, typed_count_above: Optional[int] = None) -> Optional[str]:
    """Why ``form`` may not run ``previews`` (an operator-facing message), or None when it may.

    ``confirm_step`` is for a route whose UI always previews first (a bulk job, a multi-step
    routine): it needs ``confirmed=1`` whatever the count or risk. ``typed_count_above`` also asks,
    above that many targets, for the count typed — the count the server resolved *now*, so a scope
    that grew since the preview is refused rather than run on the operator's older number.
    """
    decision = evaluate(previews, typed_count_above=typed_count_above)
    if decision.requires_typed_confirmation:
        if str(form.get(TYPED_FIELD) or "").strip().lower() != TYPED_WORD:
            return "Type confirm to run this destructive bulk change."
    elif (decision.requires_confirmation or confirm_step) and form.get(CONFIRMED_FIELD) != "1":
        return "This change needs confirmation — preview it, then confirm."
    n = decision.affected_count
    if decision.requires_typed_count and str(form.get(COUNT_FIELD) or "").strip() != str(n):
        return f"This changes {n} accounts: preview again, and type {n} to confirm."
    typed = {str(v).strip().lower() for v in _values(form, EMAIL_FIELD)}
    if any(address.strip().lower() not in typed for address in decision.typed_emails):
        return ("Type the exact email address to confirm." if len(decision.typed_emails) == 1 else
                "Type the exact email address of each account to delete to confirm.")
    return None


def _values(form: Mapping[str, Any], key: str) -> List[Any]:
    """Every value posted under ``key`` (a form may repeat a field; a plain dict holds one)."""
    if hasattr(form, "getlist"):
        return list(form.getlist(key))
    value = form.get(key)
    return [] if value is None else list(value) if isinstance(value, (list, tuple)) else [value]
