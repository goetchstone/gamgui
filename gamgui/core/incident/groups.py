"""The groups an attacker's email went to, read for their members so that each person counts as themselves, not on the send to the group. Reads only. At most 20 a screen, each once though groups hold
each other. A read that failed, an address GAM finds no group at and one past the limit are said under the email, and not read again until the screen is opened again. Runbook:
docs/domains/incident.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Sequence, Set

from ..gam.errors import GAMError, GAMErrorKind
from .reads import in_words

LIMIT = 20


@dataclass
class GroupsRead:
    members: Dict[str, List[str]] = field(default_factory=dict)
    everyone: Set[str] = field(default_factory=set)     # the groups with GAM's CUSTOMER member: every person in the directory
    problems: Dict[str, str] = field(default_factory=dict)      # why a group counts on the warning's send, not by its members
    nameless: Dict[str, int] = field(default_factory=dict)      # of a group read, how many members have no address, so aren't listed
    asked: Set[str] = field(default_factory=set)
    failed: Set[str] = field(default_factory=set)
    retry: Set[str] = field(default_factory=set)

    def forget_failures(self) -> None:
        """Opening the screen again tries those again, a group inside another too, which no email names. What's said of each stays until it answers."""
        self.asked -= self.failed
        self.retry |= self.failed
        self.failed = set()


def groups_to_read(reaches: Iterable[Any]) -> List[str]:
    """A group here an email went to is no one's mailbox, inside, not reached through another group and not bounced from. Each once, in the order the emails list them."""
    return list(dict.fromkeys(r.address for reach in reaches for r in reach.recipients if r.inside and not r.mailbox and r.via is None and r.outcome != "bounced"))


def to_ask(reaches: Iterable[Any], read: GroupsRead) -> List[str]:
    """What a read should ask for now: what the emails name that wasn't asked, and what's to try again."""
    return [g for g in dict.fromkeys([*groups_to_read(reaches), *sorted(read.retry)]) if g not in read.asked]


async def read_groups(conn: Any, groups: Sequence[str], read: GroupsRead, limit: int = LIMIT, note: Callable[[], None] = lambda: None) -> None:
    """Each of ``groups`` and the groups in them, into ``read``, at most ``limit`` a call. Never raises for a read that failed: that's the group's own problem, said beside the rest. A group that lists no one
    isn't "nobody got it": members are read as they are now, not as they were when the email went, so it counts on the warning's send."""
    queue = [g.strip().lower() for g in groups]
    done = 0
    while queue:
        group = queue.pop(0)
        if not group or group in read.asked:
            continue
        read.asked.add(group)
        read.retry.discard(group)
        if done >= limit:
            read.failed.add(group)
            read.problems[group] = f"not read: this screen reads at most {limit} groups"
            continue
        done += 1
        try:
            members = await conn.list_group_members(group)
        except Exception as exc:  # noqa: BLE001 - said under the email, whatever it was
            read.failed.add(group)
            read.problems[group] = "GAM finds no group at that address" if isinstance(exc, GAMError) and exc.kind == GAMErrorKind.NOT_FOUND else in_words(exc)
            continue
        finally:
            note()
        customer = any(m.member_type.upper() == "CUSTOMER" for m in members)
        others = [m.email.strip().lower() for m in members if m.member_type.upper() != "CUSTOMER"]
        named = list(dict.fromkeys(e for e in others if "@" in e))
        if not named and not customer:
            read.failed.add(group)
            read.members.pop(group, None)
            read.problems[group] = "GAM lists no one in it now, and who was in it when the email went isn't known"
            continue
        read.problems.pop(group, None)
        read.members[group] = named
        if customer:
            read.everyone.add(group)
        if len(named) != len(others) and any("@" not in e for e in others):
            read.nameless[group] = sum("@" not in e for e in others)
        else:
            read.nameless.pop(group, None)
        queue.extend(m.email.strip().lower() for m in members if m.member_type.upper() == "GROUP" and "@" in m.email)
