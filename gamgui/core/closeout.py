"""Close Out (roadmap 4.5, plan D2): what a leaver still has once their data transfer has finished, taken away as the operator
picks, then the account delete.

The offboarding routine hands the leaver's Drive and calendars over; Google's transfer runs for minutes after its command, and
taking the leaver out of their groups, moving their org unit or suspending them can switch off what it still needs. So Close
Out reads `print datatransfers` first and starts nothing while a transfer from them is unfinished, or when that can't be
read: unknown is never a reason to go ahead (invariant 10).

Every step is off until it is picked: each group, each admin role, the org unit, suspend or archive, each license, each alias
(moved to the manager). Each is offboarding's ``OffboardStep``, run by ``lifecycle.run_offboard``, and none waits on another:
a refused one is reported and the rest still run. The delete is not a step: it is the Users delete zone (roadmap 2.9), shown
under Close Out with its own refusals, its typed address and its reads again at the click.

A read that failed is said, and offers nothing in its place: groups unread are never "in no groups".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import licenses
from .gam.commands import GAMCommands, is_one_address
from .gam.models import GAMUser, LicenseSeat, RoleAssignment
from .lifecycle import OffboardStep

# Google's Admin Help, "Archive former employee accounts": archiving swaps the person's license for an Archived User one.
ARCHIVE_SOURCE = "https://knowledge.workspace.google.com/admin/users/archive-former-employee-accounts"
ARCHIVE_SWAPS = ("Archiving swaps their license for an Archived User license, and Google refuses to archive without one to "
                 "give.")
STATES = ("suspend", "archive")
RECORD_FIELDS = ("primaryEmail", "suspended", "archived", "orgUnitPath", "aliases")


@dataclass
class Found:
    """What one check read of a leaver. A list that is None wasn't read: ``unread`` says why, and nothing is offered for it."""
    user: str                                  # the account's primary address
    org_unit: str = ""
    suspended: bool = False
    archived: Optional[bool] = None            # None: the record didn't say
    aliases: Tuple[str, ...] = ()
    groups: Optional[List[str]] = None
    roles: Optional[List[RoleAssignment]] = None
    seats: Optional[List[LicenseSeat]] = None
    units: Optional[List[str]] = None          # every org unit, for the move
    pending: Optional[list] = None             # unfinished transfers from them; None: unread
    unread: Dict[str, str] = field(default_factory=dict)

    @property
    def refusal(self) -> str:
        """Why Close Out offers nothing yet, or "": a transfer from them unfinished, or one that couldn't be checked."""
        return transfer_refusal(self.user, self.pending)

    @property
    def failed_transfers(self) -> List[str]:
        return licenses.failed_transfers(self.pending)


def seat_key(seat: LicenseSeat) -> str:
    return f"{seat.product_id}|{seat.sku_id}"


def transfer_refusal(user: str, pending: Optional[list]) -> str:
    """Why nothing is closed out yet. A transfer that *failed* has stopped, so it doesn't refuse: the check names it."""
    if pending is None:
        return (f"Couldn't check whether a data transfer from {user} is still running, so nothing is offered: taking them out of "
                f"groups, moving them or suspending them can stop a transfer that needs it. Check again in a moment.")
    running = [p for p in pending if str(p.get("status", "")).strip().lower() != "failed"]
    if running:
        what = ", ".join(f"{p.get('application', 'data')} ({p.get('status', 'unfinished')})" for p in running)
        return (f"A data transfer from {user} hasn't finished ({what}), so nothing is offered yet: taking them out of groups, "
                f"moving them or suspending them can stop what it still needs. Check again once it completes.")
    return ""


async def read(conn: Any, record: GAMUser, why: Callable[[Exception], str]) -> Found:
    """Everything Close Out offers for the account ``record`` is (read with ``RECORD_FIELDS``), each read on its own and
    at once: one that fails is said in ``unread`` and offers nothing."""
    archived = record.raw.get("archived")
    found = Found(user=record.primary_email, org_unit=record.org_unit_path, suspended=record.suspended,
                  archived=archived if isinstance(archived, bool) else None, aliases=tuple(record.aliases))
    found.pending = await conn.incomplete_transfers_for(found.user)
    if found.refusal:
        return found
    reads: Sequence[Tuple[str, str]] = (("groups", "groups"), ("roles", "admin roles"), ("units", "org units"), ("seats", "licenses"))
    answers = await asyncio.gather(conn.groups_of(found.user), conn.role_assignments_of(found.user), conn.org_units(),
                                   conn.license_seats(), return_exceptions=True)
    for (name, words), value in zip(reads, answers, strict=True):
        if isinstance(value, BaseException):
            if not isinstance(value, Exception):   # a cancel (Stop, the app quitting) stays one
                raise value
            found.unread[name] = f"Couldn't read {found.user}'s {words}, so none are offered. {why(value)}"   # unknown, never none
            continue
        setattr(found, name, licenses.held_by(value, found.user) if name == "seats" else value)
    return found


# What each section's pick is called in the form: a pick in a section whose read failed is unread, never "changed".
PICK_FIELDS = {"groups": "group", "roles": "role", "units": "org_unit", "seats": "seat"}


@dataclass(frozen=True)
class Picks:
    """What the operator picked, each matched to what a read found now."""
    groups: Tuple[str, ...] = ()
    roles: Tuple[RoleAssignment, ...] = ()
    org_unit: str = ""
    state: str = ""
    seats: Tuple[LicenseSeat, ...] = ()
    aliases: Tuple[str, ...] = ()


def pick(found: Found, *, groups: Iterable[str] = (), roles: Iterable[str] = (), org_unit: str = "", state: str = "",
         seats: Iterable[str] = (), aliases: Iterable[str] = ()) -> Tuple[Picks, List[str]]:
    """The picks that name something ``found`` has, in the read's order, and the ones that don't (read since, or never
    offered): the caller refuses those, never runs them."""
    stale: List[str] = []

    def matched(asked: Iterable[str], have: Optional[Sequence[Any]], name: Callable[[Any], str]) -> Tuple[Any, ...]:
        asked = {a.strip().lower() for a in asked if a.strip()}
        known = {name(h).lower(): h for h in have or ()}
        stale.extend(sorted(a for a in asked if a not in known))
        return tuple(h for h in have or () if name(h).lower() in asked)

    unit = org_unit.strip()
    if unit and (found.units is None or unit not in found.units):
        stale.append(unit)
    state = state.strip()
    if state and (state not in STATES or (state == "suspend" and found.suspended)
                  or (state == "archive" and found.archived is not False)):
        stale.append(state)
    picks = Picks(groups=matched(groups, found.groups, str), roles=matched(roles, found.roles, lambda r: r.id),
                  org_unit=unit if unit != found.org_unit else "", state=state if state in STATES else "",
                  seats=matched(seats, found.seats, seat_key), aliases=matched(aliases, found.aliases, str))
    return picks, stale


def steps(user: str, picks: Picks, manager: str = "") -> List[OffboardStep]:
    """Every picked step in run order, for ``user``'s primary address: groups, admin roles, org unit, suspend or archive,
    licenses, aliases. ValueError when a pick can't run as asked."""
    if picks.state == "archive" and picks.seats:
        raise ValueError(f"{ARCHIVE_SWAPS} So a license isn't removed as well: pick archive or the licenses")
    if picks.aliases and not is_one_address(manager):
        raise ValueError("name the manager to move the aliases to")
    if picks.aliases and manager.strip().lower() == user.strip().lower():
        raise ValueError("an alias can't be moved to the account it's on")
    if not all(is_one_address(g) for g in picks.groups):
        raise ValueError("each group is one address")
    planned: List[OffboardStep] = []

    def add(key: str, label: str, summary: str, action, argv: List[str]) -> None:
        planned.append(OffboardStep(key, label, summary, action, [argv]))

    for group in picks.groups:
        add("group", f"Take them out of {group}",
            f"Remove {user} from {group}. An admin role held through that group goes with it.",
            lambda c, g=group: c.remove_group_member(g, user), GAMCommands.remove_group_member(group, user))
    for role in picks.roles:
        add("role", f"Remove the admin role {role.role}",
            f"Remove {user}'s {role.words} role. Only roles assigned to them directly are listed here.",
            lambda c, r=role: c.delete_admin_role(user, r), GAMCommands.delete_admin_role(role.id))
    if picks.org_unit:
        add("org_unit", f"Move them to {picks.org_unit}",
            f"Move {user} to the org unit {picks.org_unit}: its policies apply to them from then on.",
            lambda c: c.move_user_to_org_unit(user, picks.org_unit), GAMCommands.move_user_to_org_unit(picks.org_unit, user))
    if picks.state == "suspend":
        add("suspend", "Suspend the account", f"Suspend {user}: nobody can sign in to it. Unsuspending undoes it.",
            lambda c: c.suspend_user(user), GAMCommands.set_suspended(user, True))
    elif picks.state == "archive":
        add("archive", "Archive the account", f"Archive {user}: nobody can sign in to it. {ARCHIVE_SWAPS}",
            lambda c: c.archive_user(user), GAMCommands.set_archived(user))
    for seat in picks.seats:
        add("license", f"Remove the license {seat.sku}",
            f"Remove {user}'s {seat.sku} license. “{licenses.MIGHT_LOSE_DATA}” {licenses.REASSIGN_WAIT}",
            lambda c, s=seat: c.remove_license(user, s), GAMCommands.remove_license(user, seat.product_id, seat.sku_id))
    for alias in picks.aliases:
        add("alias", f"Move the alias {alias} to {manager}",
            f"Mail to {alias} goes to {manager} instead. GAM removes it from {user}, then adds it to {manager}: if the add "
            f"fails, the alias is on nobody, and you add it to {manager} on their page.",
            lambda c, a=alias: c.move_alias(user, a, manager), GAMCommands.move_alias(alias, manager))
    return planned
