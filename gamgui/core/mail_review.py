"""Reports' mail review (roadmap 3.2): every active account's forwarding that is on, and every mail delegate.

Pure: given what GAM wrote for `all users print forward enabledonly` and `all users print delegates shownames`, account for
every account it listed. GAM reads one account at a time and announces each on stderr ("Getting Forward Enabled for <u>
(i/N)", "Getting all Delegates for <u> (i/N)", read from the vendored 7.52.03 build), warns by name for one it couldn't
read ("User: <u>, Gmail Service/App not enabled"), and skips a refused token without a word: no "Getting" line at all. So
an account is read, unread with a reason, or skipped, and only a read one says anything about forwarding or delegates. An
account that wasn't read never counts as "forwards nothing" (invariant 10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

_GETTING = re.compile(r"^Getting (?:Forward Enabled|all Delegates) for (\S+?)(?: \((\d+)/(\d+)\))?\s*$")
_WARNING = re.compile(r"^User: (\S+?), (.*?)(?: \(\d+/\d+\))?\s*$")
_GOT_USERS = re.compile(r"Got (\d+) Users?\b")
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


def account_for(stderr: str) -> Optional[Accounted]:
    """What GAM's stderr says about each account, or ``None`` when it listed none and announced none: a read with nothing
    to account for isn't an answer."""
    getting: Set[str] = set()
    warned: Dict[str, str] = {}
    listed = 0
    for raw in stderr.replace("\r", "\n").splitlines():
        line = raw.strip()
        if m := _GETTING.match(line):
            getting.add(m.group(1).lower())
            if m.group(3):
                listed = max(listed, int(m.group(3)))
        elif m := _WARNING.match(line):
            user = m.group(1).lower()
            warned[user] = GMAIL_OFF if "Service/App not enabled" in m.group(2) else NOT_READ
        elif m := _GOT_USERS.search(line):
            listed = max(listed, int(m.group(1)))
    if not listed and not getting:
        return None
    listed = max(listed, len(getting | set(warned)))
    unread = [Unread(u, why) for u, why in sorted(warned.items())]
    read = len(getting - set(warned))
    return Accounted(listed=listed, read=read, unread=unread, skipped=max(0, listed - len(getting | set(warned))))


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
