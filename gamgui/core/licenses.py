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

from typing import Dict, List, Optional, Sequence, Tuple

from .gam.models import LicenseSeat

SOURCE = "https://knowledge.workspace.google.com/admin/billing/assign-remove-and-reassign-licenses"
MIGHT_LOSE_DATA = "If you remove a user's license, you might lose their data."
REASSIGN_WAIT = "A license removed can take up to 24 hours before it can be given to someone else."

Key = Tuple[str, str]   # (productId, skuId)


def key(seat: LicenseSeat) -> Key:
    return (seat.product_id, seat.sku_id)


def held_by(seats: Sequence[LicenseSeat], email: str) -> List[LicenseSeat]:
    """The seats the read lists for ``email``, one per license, by product then license name."""
    who = email.strip().lower()
    mine = {key(s): s for s in seats if s.user == who}
    return sorted(mine.values(), key=lambda s: (s.product.lower(), s.sku.lower()))


def in_domain(seats: Sequence[LicenseSeat]) -> Dict[Key, LicenseSeat]:
    """Each license someone in the domain holds, by its IDs (the seat stands in for its names)."""
    out: Dict[Key, LicenseSeat] = {}
    for s in seats:
        out.setdefault(key(s), s)
    return out


def add_choices(seats: Sequence[LicenseSeat], email: str) -> List[LicenseSeat]:
    """Licenses the domain holds that the read doesn't list for ``email``."""
    mine = {key(s) for s in held_by(seats, email)}
    return sorted((s for k, s in in_domain(seats).items() if k not in mine), key=lambda s: (s.product.lower(), s.sku.lower()))


def swap_choices(seats: Sequence[LicenseSeat], email: str, seat: LicenseSeat) -> List[LicenseSeat]:
    """What ``seat`` can become: another license of the same product the domain holds, that ``email`` doesn't."""
    return [s for s in add_choices(seats, email) if s.product_id == seat.product_id and s.sku_id != seat.sku_id]


def find(seats: Sequence[LicenseSeat], product_id: str, sku_id: str) -> Optional[LicenseSeat]:
    return in_domain(seats).get((product_id, sku_id))


def transfer_refusal(email: str, pending: Optional[list]) -> str:
    """Why a removal is refused while a data transfer from ``email`` is unfinished, or can't be checked (``None``, as
    ``incomplete_transfers_for`` answers a failed read): unknown is never a reason to go ahead (invariant 10)."""
    if pending is None:
        return (f"Couldn't check whether a data transfer from {email} is still running, so the license isn't removed. "
                "Try again in a moment.")
    if pending:
        what = ", ".join(f"{p.get('application', 'data')} ({p.get('status', 'unfinished')})" for p in pending)
        return (f"A data transfer from {email} hasn't finished ({what}), so the license isn't removed: "
                "remove it once the transfer completes.")
    return ""
