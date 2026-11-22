"""One person's license seats (roadmap 4.3): what they hold, what can be added or swapped, and when a removal is refused.

Every choice comes from one read, `gam print licenses`, which with no product named passes over a product Google won't let
it read without saying so. So only what the read *found* is acted on: a seat is removed or swapped only when the read
listed it, and a license is offered to add only when the read found someone in the domain holding it (the domain has that
subscription). Nothing is concluded from a seat the read didn't list: "holds none found" is never "holds none".

Google's own words for the preview: "If you remove a user's license, you might lose their data." (Admin Help, "Assign,
remove, and reassign licenses", knowledge.workspace.google.com/admin/billing/assign-remove-and-reassign-licenses), which
also says a license unassigned "might take up to 24 hours" before it can go to someone else. A swap is "a different SKU in
the same product" (Licensing API, licenseAssignments.patch).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .gam.commands import license_sku
from .gam.models import GAMUser, LicenseSeat

SOURCE = "https://knowledge.workspace.google.com/admin/billing/assign-remove-and-reassign-licenses"
MIGHT_LOSE_DATA = "If you remove a user's license, you might lose their data."
REASSIGN_WAIT = "A license removed can take up to 24 hours before it can be given to someone else."
ONLY_SEATS_READ = "GAM passes over a product Google won't let it read without saying so, so this covers only the seats read."

Key = Tuple[str, str]   # (productId, skuId)


def key(seat: LicenseSeat) -> Key:
    return (seat.product_id, seat.sku_id)


def _by_name(s: LicenseSeat) -> Tuple[str, str]:
    return (s.product.lower(), s.sku.lower())


def held_by(seats: Sequence[LicenseSeat], email: str) -> List[LicenseSeat]:
    """The seats the read lists for ``email``, one per license, by product then license name."""
    who = email.strip().lower()
    return sorted({key(s): s for s in seats if s.user == who}.values(), key=_by_name)


def in_domain(seats: Sequence[LicenseSeat]) -> Dict[Key, LicenseSeat]:
    """Each license someone in the domain holds, by its IDs (the seat stands in for its names)."""
    out: Dict[Key, LicenseSeat] = {}
    for s in seats:
        out.setdefault(key(s), s)
    return out


@dataclass(frozen=True)
class Choices:
    """What one read offers for one person, worked out in one pass: the seats they hold, each with what it can change to
    (another license of the same product the domain holds), and what can be added (a license the domain holds of a product
    they hold none of: the Licensing API moves a user to "a different SKU in the same product" with a patch, so another SKU
    of a product they hold is offered as a change, not an add)."""
    held: List[Tuple[LicenseSeat, List[LicenseSeat]]]
    adds: List[LicenseSeat]


def choices(seats: Sequence[LicenseSeat], email: str) -> Choices:
    mine = held_by(seats, email)
    products, keys = {s.product_id for s in mine}, {key(s) for s in mine}
    others = sorted((s for k, s in in_domain(seats).items() if k not in keys), key=_by_name)
    return Choices(held=[(m, [s for s in others if s.product_id == m.product_id]) for m in mine],
                   adds=[s for s in others if s.product_id not in products])


def nameable(seat: LicenseSeat) -> bool:
    """Whether GAM can be given the seat's IDs as one ``nv:`` seat (``license_sku``): one it can't is never offered."""
    try:
        license_sku(seat.product_id, seat.sku_id)
    except ValueError:
        return False
    return True


def on_suspended(seats: Sequence[LicenseSeat], users: Sequence[GAMUser]) -> List[Tuple[str, LicenseSeat]]:
    """Each seat the read lists on an account the directory read says is suspended, as ``(their primary address, the
    seat)``, one per license, by address then license name (roadmap 4.3, D3). A seat on an address the directory read
    doesn't list isn't one: who holds it, and whether they're suspended, is unknown."""
    suspended = {u.primary_email.strip().lower(): u.primary_email.strip() for u in users if u.suspended}
    held = {(s.user, key(s)): s for s in seats if s.user in suspended}
    return sorted(((suspended[user], s) for (user, _), s in held.items()), key=lambda p: (p[0].lower(), *_by_name(p[1])))


def _failed(transfer: dict) -> bool:
    return str(transfer.get("status", "")).strip().lower() == "failed"


def transfer_refusal(email: str, pending: Optional[list]) -> str:
    """Why a removal is refused while a data transfer from ``email`` is unfinished, or can't be checked (``None``, as
    ``incomplete_transfers_for`` answers a failed read): unknown is never a reason to go ahead (invariant 10). A transfer
    that *failed* has stopped, and stays listed after a later one completes, so it doesn't refuse: the confirm step names
    it (``failed_transfers``)."""
    if pending is None:
        return (f"Couldn't check whether a data transfer from {email} is still running, so the license isn't removed. "
                "Try again in a moment.")
    running = [p for p in pending if not _failed(p)]
    if running:
        what = ", ".join(f"{p.get('application', 'data')} ({p.get('status', 'unfinished')})" for p in running)
        return (f"A data transfer from {email} hasn't finished ({what}), so the license isn't removed: "
                "remove it once the transfer completes.")
    return ""


def failed_transfers(pending: Optional[list]) -> List[str]:
    """The applications whose transfer from them failed, for the confirm step to name."""
    return [str(p.get("application") or "data") for p in (pending or []) if _failed(p)]


def many_refusal(email: str, pending: Optional[list]) -> str:
    """Why a removal in a run over many people leaves ``email``'s seats in place: ``transfer_refusal``, and a transfer that
    *failed* too. On their page the confirm step names a failed one and the operator decides; a run has no one to ask, so
    it keeps the seat and says why, and their page can still remove it."""
    if refusal := transfer_refusal(email, pending):
        return refusal
    if failed := failed_transfers(pending):
        return (f"A data transfer from {email} failed ({', '.join(failed)}): what it was moving may still be only on their "
                "account, so the license isn't removed. Transfer it again, or remove it from their page.")
    return ""
