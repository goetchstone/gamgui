"""Check an account: what an attacker commonly changes, read and said in words. Changes nothing.

Each read stands alone: one that failed is said beside its list, never taken as "none". ``findings`` says what to look at
first. Pure: the reads are on ``GAMConnector``. Ported from SwiftGamGui's ``Compromised.swift``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar

from ..gam.models import Vacation
from ..phishing import addresses_in

T = TypeVar("T")

# What Check Account reads with `info user`: who the person is, whether they're locked or an admin, their 2-Step Verification and
# recovery details (GamCommands.txt USER_FIELDS_CHOICE_MAP's names).
ACCOUNT_FIELDS = ("primaryEmail", "name", "suspended", "isAdmin", "isDelegatedAdmin", "isEnrolledIn2Sv", "isEnforcedIn2Sv",
                  "recoveryEmail", "recoveryPhone")
DRAFT_CAP = 5000            # an attacker's session can leave thousands
SCHEDULED_CAP = 500         # above the 100 guides to Gmail say one person can schedule, so a check lists every one
SCHEDULED_LABEL = "SCHEDULED"
SCHEDULED_QUERY = "in:scheduled"
DRAFTS_QUERY = "in:drafts"


def _text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _flag(value: Any) -> bool:
    """GAM's JSON has a bool, its CSV the word."""
    return value is True or str(value).strip().lower() == "true"


def _labels(record: Mapping[str, Any]) -> Tuple[List[str], bool]:
    """A message's labels, and whether they can be told apart: a user label's name may hold a space, or the word."""
    words = _text(record, "Labels").split()
    return words, _text(record, "LabelsCount") == str(len(words))


@dataclass(frozen=True)
class Read(Generic[T]):
    """One read of the check: its answer, or what went wrong in words."""

    value: Optional[T] = None
    problem: str = ""


@dataclass(frozen=True)
class Account:
    primary_email: str
    full_name: str = ""
    suspended: bool = False
    is_admin: bool = False
    is_delegated_admin: bool = False
    enrolled_2sv: bool = False
    enforced_2sv: bool = False
    recovery_email: str = ""
    recovery_phone: str = ""

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Account":
        email = _text(record, "primaryEmail")
        if not email:
            raise ValueError("GAM's answer wasn't this person's record")
        name = record.get("name")
        full = ""
        if isinstance(name, Mapping):
            full = _text(name, "fullName") or " ".join(filter(None, (_text(name, "givenName"), _text(name, "familyName"))))
        return cls(email, full, _flag(record.get("suspended")), _flag(record.get("isAdmin")), _flag(record.get("isDelegatedAdmin")),
                   _flag(record.get("isEnrolledIn2Sv")), _flag(record.get("isEnforcedIn2Sv")),
                   _text(record, "recoveryEmail"), _text(record, "recoveryPhone"))

    def same_as(self, other: "Account") -> bool:
        """Still the account the operator previewed: the run acts on nothing that's changed under it."""
        return (self.primary_email.lower(), self.suspended, self.is_admin, self.is_delegated_admin) == \
            (other.primary_email.lower(), other.suspended, other.is_admin, other.is_delegated_admin)


@dataclass(frozen=True)
class Filter:
    """One Gmail filter: what mail it matches and what it does with it."""

    id: str
    matches: Tuple[str, ...]
    does: Tuple[str, ...]
    hides_or_forwards: bool     # what an attacker's filter does to hide replies or take mail

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Filter":
        criteria, action = _mapping(record.get("criteria")), _mapping(record.get("action"))
        matches: List[str] = []
        for key in ("from", "to", "subject", "query", "negatedQuery"):
            value = _text(criteria, key)
            if value:
                matches.append(f"mail matching “{value}”" if key == "query" else f"not matching “{value}”" if key == "negatedQuery"
                               else f"{key} {value}")
        if _flag(criteria.get("hasAttachment")):
            matches.append("has an attachment")
        if _text(criteria, "size"):
            matches.append(f"{'smaller' if _text(criteria, 'sizeComparison') == 'smaller' else 'larger'} than {_text(criteria, 'size')} bytes")
        if _flag(criteria.get("excludeChats")):
            matches.append("not chats")
        does: List[str] = []
        hides = False
        if _text(action, "forward"):
            does.append(f"forwards it to {_text(action, 'forward')}")
            hides = True
        for label in action.get("addLabelIds") or ():
            label = str(label)
            if label == "TRASH":
                does.append("deletes it (to Trash)")
                hides = True
            elif label == "SPAM":
                does.append("sends it to Spam")
                hides = True
            elif label == "STARRED":
                does.append("stars it")
            elif label == "IMPORTANT":
                does.append("marks it important")
            else:
                does.append(f"files it under {label[9:].lower()}" if label.startswith("CATEGORY_") else f"adds the label {label}")
        for label in action.get("removeLabelIds") or ():
            label = str(label)
            if label == "INBOX":
                does.append("archives it (skips the inbox)")
                hides = True
            elif label == "UNREAD":
                does.append("marks it read")
                hides = True
            elif label == "SPAM":
                does.append("never sends it to Spam")
            elif label == "IMPORTANT":
                does.append("never marks it important")
            else:
                does.append(f"removes the label {label}")
        return cls(_text(record, "id"), tuple(matches) or ("every message",), tuple(does) or ("does nothing Gmail shows",), hides)

    @property
    def words(self) -> str:
        when = ", ".join(self.matches)
        return f"{when[:1].upper()}{when[1:]}: {', '.join(self.does)}"


_KEPT_COPY = {"leaveInInbox": "a copy stays in the inbox", "archive": "the copy is archived", "trash": "the copy is deleted",
              "markRead": "the copy is marked read"}


@dataclass(frozen=True)
class Forwarding:
    enabled: bool
    to: str = ""
    disposition: str = ""

    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]]) -> Optional["Forwarding"]:
        """The person's row; none (Gmail off for them prints the titles alone) reads as None, said as unknown."""
        if not records:
            return None
        row = records[0]
        return cls(_flag(row.get("forwardEnabled")), _text(row, "forwardTo"), _text(row, "disposition"))

    @property
    def words(self) -> str:
        if not self.enabled:
            return "Off"
        kept = _KEPT_COPY.get(self.disposition, self.disposition)
        return f"On: every message goes to {self.to}" + (f", and {kept}" if kept else "")


@dataclass(frozen=True)
class ForwardingAddress:
    """An address forwarding may send to. An accepted one lets forwarding be turned back on to it without a new code."""

    address: str
    status: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ForwardingAddress":
        return cls(_text(record, "forwardingEmail"), _text(record, "verificationStatus"))


@dataclass(frozen=True)
class SendAs:
    address: str
    name: str = ""
    is_primary: bool = False
    is_default: bool = False
    reply_to: str = ""
    status: str = ""
    has_signature: bool = False

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "SendAs":
        return cls(_text(record, "sendAsEmail"), _text(record, "displayName"), _flag(record.get("isPrimary")), _flag(record.get("isDefault")),
                   _text(record, "replyToAddress"), _text(record, "verificationStatus"), _flag(record.get("signature")))

    @property
    def words(self) -> str:
        parts = [f"{self.name} <{self.address}>" if self.name else self.address]
        if self.is_primary:
            parts.append("their own address")
        if self.is_default:
            parts.append("sent from by default")
        if self.reply_to:
            parts.append(f"replies go to {self.reply_to}")
        if self.status:
            parts.append(f"verification {self.status}")
        if self.has_signature:
            parts.append("has a signature")
        return " · ".join(parts)


@dataclass(frozen=True)
class AdminRole:
    role: str
    scope: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "AdminRole":
        name = _text(record, "role") or _text(record, "roleId")
        return cls("Super Admin" if name == "_SEED_ADMIN_ROLE" else name,
                   f"the org unit {_text(record, 'orgUnit')}" if _text(record, "scopeType") == "ORG_UNIT" else "the whole domain")

    @property
    def words(self) -> str:
        return f"{self.role}, for {self.scope}"


@dataclass(frozen=True)
class Draft:
    id: str
    date: str = ""
    subject: str = ""

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Draft":
        return cls(_text(record, "id"), _text(record, "Date"), _text(record, "Subject"))


def drafts_matching(drafts: Iterable[Draft], text: str) -> List[Draft]:
    """The drafts whose subject holds ``text``, case and surrounding spaces aside."""
    needle = (text or "").strip().lower()
    return [d for d in drafts if needle in d.subject.lower()] if needle else []


@dataclass(frozen=True)
class ScheduledSend:
    """An email waiting in Gmail's Scheduled. Gmail sends it at its time with nobody signed in, and it isn't a draft."""

    id: str
    subject: str = ""
    date: str = ""
    recipients: Tuple[str, ...] = ()
    is_scheduled: bool = False      # plainly labelled so by Gmail: the only kind the step deletes

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ScheduledSend":
        words, apart = _labels(record)
        seen: List[str] = []
        for column in sorted(record):
            head = str(column).split(" ")[0].lower()
            value = record[column]
            if head in ("to", "cc", "bcc") and isinstance(value, str):
                for address in addresses_in(value)[0]:
                    if address.lower() not in {s.lower() for s in seen}:
                        seen.append(address)
        # Never SENT: its copy holds the only whole Bcc list.
        return cls(_text(record, "id"), _text(record, "Subject"), _text(record, "Date"), tuple(seen),
                   apart and SCHEDULED_LABEL in words and "SENT" not in words)


def scheduled_recipients(sends: Iterable[ScheduledSend]) -> List[str]:
    seen: List[str] = []
    for send in sends:
        for address in send.recipients:
            if address.lower() not in {s.lower() for s in seen}:
                seen.append(address)
    return seen


@dataclass
class Check:
    """What Check Account read for one person, on this connector."""

    asked: str
    account: Account
    filters: Read[List[Filter]] = field(default_factory=Read)
    forwarding: Read[Optional[Forwarding]] = field(default_factory=Read)
    forwarding_addresses: Read[List[ForwardingAddress]] = field(default_factory=Read)
    delegates: Read[List[str]] = field(default_factory=Read)
    send_as: Read[List[SendAs]] = field(default_factory=Read)
    admin_roles: Read[List[AdminRole]] = field(default_factory=Read)
    scheduled: Read[List[ScheduledSend]] = field(default_factory=Read)
    drafts: Read[List[Draft]] = field(default_factory=Read)
    auto_reply: Read[Vacation] = field(default_factory=Read)
    findings: List[str] = field(default_factory=list)
    cut_off: str = ""       # why nothing here can run: this is the admin GamGUI signs in as
    done: frozenset = frozenset()       # the switched steps the last run of this person finished
    run: str = field(default_factory=lambda: secrets.token_hex(8))      # what a preview is bound to: this check, never another

    @property
    def scheduled_sends(self) -> List[ScheduledSend]:
        return [s for s in self.scheduled.value or [] if s.is_scheduled]

    @property
    def other_send_as(self) -> List[SendAs]:
        return [s for s in self.send_as.value or [] if not s.is_primary]


def findings(account: Account, *, forwarding: Optional[Forwarding] = None, filters: Optional[Sequence[Filter]] = None,
             delegates: Optional[Sequence[str]] = None, send_as: Optional[Sequence[SendAs]] = None,
             roles: Optional[Sequence[AdminRole]] = None, scheduled: Optional[Sequence[ScheduledSend]] = None,
             drafts: Optional[Sequence[Draft]] = None, auto_reply: Optional[Vacation] = None) -> List[str]:
    """What the check found worth saying, before the lists, in the order to deal with them. None is a read that failed: it's
    said beside its list, not here."""
    said: List[str] = []
    user = account.primary_email
    if account.suspended:
        said.append(f"{user} is suspended: nobody can sign in to it until it's restored.")
    if scheduled:       # first of what to undo: these go out on their own, on Gmail's clock
        sends = [s for s in scheduled if s.is_scheduled]
        if sends:
            one, people = len(sends) == 1, len(scheduled_recipients(sends))
            at_least = "At least " if len(scheduled) >= SCHEDULED_CAP else ""
            said.append(f"{at_least}{'1 email is' if one else f'{len(sends)} emails are'} scheduled to send from {user}, to {people} "
                        f"recipient{'' if people == 1 else 's'}: Gmail sends {'it' if one else 'them'} at {'its' if one else 'their'} "
                        f"time on its own, with nobody signed in. Delete {'it' if one else 'them'} unless {user} scheduled {'it' if one else 'them'}.")
    if forwarding and forwarding.enabled:
        said.append(f"Forwarding is on, to {forwarding.to}: every message goes there. The lock turns it off.")
    hiding = [f for f in filters or () if f.hides_or_forwards]
    if hiding:
        said.append(f"{'1 filter forwards, deletes, archives or marks' if len(hiding) == 1 else f'{len(hiding)} filters forward, delete, archive or mark'} "
                    f"mail read: an attacker's filter hides the replies to what they sent. Tick each that isn't {user}'s own.")
    if delegates:
        said.append(f"{'1 person' if len(delegates) == 1 else f'{len(delegates)} people'} can read {user}'s mail as a delegate: remove any {user} didn't add.")
    others = [s for s in send_as or () if not s.is_primary]
    if others:
        said.append(f"{user} can send as {len(others)} other address{'' if len(others) == 1 else 'es'}: remove any they didn't add.")
    if auto_reply and auto_reply.enabled:
        said.append(f"An auto-reply is on (“{auto_reply.subject}”): an attacker's sends their message, a link say, to everyone who writes. "
                    f"Tick it to turn it off unless it's {user}'s own.")
    if drafts:
        count = f"at least {DRAFT_CAP}" if len(drafts) >= DRAFT_CAP else str(len(drafts))
        said.append(f"{user} has {count} draft{'' if len(drafts) == 1 else 's'}: an attacker's leftover drafts can still be sent by anyone "
                    f"signed in. Delete them, all or by subject.")
    recovery = [w for w in (f"email {account.recovery_email}" if account.recovery_email else "",
                            f"phone {account.recovery_phone}" if account.recovery_phone else "") if w]
    if recovery:
        said.append(f"Recovery {' and '.join(recovery)}: check with {user} that {'it is' if len(recovery) == 1 else 'they are'} theirs. An attacker's "
                    f"would let them take the account back through “Forgot password?”: if not, switch on clearing them.")
    if not account.enrolled_2sv:
        said.append(f"2-Step Verification is off for {user}: once they sign in again, have them turn it on, or enforce it for their org "
                    f"unit in the Admin console.")
    if account.is_admin:
        said.append(f"{user} is a super admin: an attacker could have changed anything in the domain. Check the Admin console's audit log.")
    elif account.is_delegated_admin or roles:
        said.append(f"{user} holds an admin role: check the Admin console's audit log for what it was used for.")
    return said
