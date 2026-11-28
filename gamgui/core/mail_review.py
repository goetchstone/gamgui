"""Reports' mail review (roadmap 3.2): every active account's forwarding that is on, and every mail delegate.

Pure: given what GAM wrote for the parallel reads (`GAMCommands.parallel_print_forward`, `parallel_print_delegates`: each
active person's own `user <u> print forward enabledonly` or `print delegates shownames`, GAM_PARALLEL workers at once,
docs/scale.md), account for every person the read was given. Each worker announces its person on stderr ("Getting Forward
Enabled for <u>", "Getting all Delegates for <u>", printShowForward and printShowDelegates), warns by name for one it
couldn't read ("User: <u>, Gmail Service/App not enabled"), and skips a refused token without a word; GAM's `showcmds`
then writes its End line with the worker's exit code. So a person is read, unread with a reason, or skipped (no End, or
an End with no word), and only a read one says anything about forwarding or delegates. A person who wasn't read never
counts as "forwards nothing" (invariant 10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Set, Tuple

_GETTING = re.compile(r"^Getting (?:Forward Enabled|all Delegates) for (\S+?)(?: \((\d+)/(\d+)\))?\s*$")
_WARNING = re.compile(r"^User: (\S+?), (.*?)(?: \(\d+/\d+\))?\s*$")
# GAM's poolCallback, with showcmds: "<ts>,<i>/<N>,End,<rc>,gam user <u> …" once a worker is done (docs/scale.md).
_END = re.compile(r"^[^,]*,\d+/\d+,End,(-?\d+),gam user (\S+)(?:\s|$)")
GMAIL_OFF = "Gmail is off for them"
NOT_READ = "GAM couldn't read them"


@dataclass(frozen=True)
class Forwarding:
    user: str
    to: str
    disposition: str


@dataclass(frozen=True)
class Delegation:
    user: str
    delegate: str
    name: str
    status: str


@dataclass(frozen=True)
class Unread:
    user: str
    why: str     # GMAIL_OFF or NOT_READ: fixed words, never GAM's line


@dataclass
class Accounted:
    """Every account GAM listed: ``read`` of ``listed`` were read; ``unread`` warned by name; ``skipped`` were passed over
    without a word (a refused token), so they can't be named here."""
    listed: int = 0
    read: int = 0
    unread: List[Unread] = field(default_factory=list)
    skipped: int = 0

    @property
    def complete(self) -> bool:
        return not self.unread and not self.skipped


@dataclass
class MailReview:
    forwarding: List[Forwarding]
    forwarding_accounts: Accounted
    delegations: List[Delegation]
    delegation_accounts: Accounted

    @property
    def complete(self) -> bool:
        return self.forwarding_accounts.complete and self.delegation_accounts.complete

    def delegated_by_user(self) -> List[Tuple[str, List[Delegation]]]:
        by: Dict[str, List[Delegation]] = {}
        for d in self.delegations:
            by.setdefault(d.user, []).append(d)
        return sorted(by.items())



def account_for_parallel(stderr: str, emails: Sequence[str]) -> Accounted:
    """What a parallel read's stderr says about each of ``emails``, the people it was given (docs/scale.md). Each worker is
    one person's read, so its own lines mean what they mean for one: its "Getting … for <u>" and an End code of 0 is read;
    a Gmail-off warning or another code is unread; an End with no "Getting" is a refused token, passed over without a word.
    A person with no End line at all never finished (cut off, or never started): skipped, never "forwards nothing"
    (invariant 10)."""
    ends: Dict[str, int] = {}
    getting: Set[str] = set()
    warned: Dict[str, str] = {}
    for raw in stderr.replace("\r", "\n").splitlines():
        line = raw.strip()
        if m := _END.match(line):
            ends[m.group(2).lower()] = int(m.group(1))
        elif m := _GETTING.match(line):
            getting.add(m.group(1).lower())
        elif m := _WARNING.match(line):
            warned[m.group(1).lower()] = GMAIL_OFF if "Service/App not enabled" in m.group(2) else NOT_READ
    people = sorted({e.strip().lower() for e in emails if e.strip()})
    unread: List[Unread] = []
    read = skipped = 0
    for e in people:
        if e in warned:
            unread.append(Unread(e, warned[e]))
        elif e not in ends:
            skipped += 1
        elif ends[e] != 0:
            unread.append(Unread(e, NOT_READ))
        elif e in getting:
            read += 1
        else:
            skipped += 1
    return Accounted(listed=len(people), read=read, unread=unread, skipped=skipped)


def forwarding_from(records: Sequence[Mapping[str, object]]) -> List[Forwarding]:
    """The accounts forwarding is on for (``enabledonly``; a row that says off is left out all the same)."""
    return sorted((Forwarding(str(r.get("User") or ""), str(r.get("forwardTo") or ""), str(r.get("disposition") or ""))
                   for r in records if str(r.get("forwardEnabled") or "").lower() == "true" and r.get("User")),
                  key=lambda f: f.user.lower())


def delegations_from(records: Sequence[Mapping[str, object]]) -> List[Delegation]:
    return sorted((Delegation(str(r.get("User") or ""), str(r.get("delegateAddress") or ""),
                              str(r.get("delegateName") or ""), str(r.get("delegationStatus") or ""))
                   for r in records if r.get("User") and r.get("delegateAddress")),
                  key=lambda d: (d.user.lower(), d.delegate.lower()))
