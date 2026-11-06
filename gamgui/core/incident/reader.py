"""The log reader: what the incident logs show, found by rules in code, and the fixes they point to as drafts.

Pure: events in as values, findings out, linear in the events. A draft only names a person or a Message-ID for a screen to open, where that screen's own read,
preview and confirmation do the rest. Suspect addresses are those of a sign-in Google called suspicious, and the one the operator pulled by; never an address only
because many accounts used it, which an office's does. Ported from SwiftGamGui's ``Assist/LogReading.swift``; its failure log entries are
``2026-10-22-a-guessed-device-action-status``, ``2026-10-22-googles-account-warnings-have-no-actor`` and ``2026-10-23-every-gmail-event-read-as-a-send``.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SHARED = 10             # an address more accounts than this signed in from is likely shared (an office, a VPN): its sign-ins draft no fix
WAY_IN_HOURS = 72       # how far before the attack began its emails are looked at
WAY_IN_SHOWN = 50
CANDIDATES_SHOWN = 100  # every candidate warning is offered, the most people first, at most this many
_DISTANT_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
_DISTANT_PAST = datetime.min.replace(tzinfo=timezone.utc)
SENT_TYPES = frozenset({1, 35})     # Google's "Gmail Activity Events": 1 sent, 35 the send begun. The rest aren't a send

# Most serious first.
FINDING_KINDS = ("signedInFromSuspect", "leftInPlace", "deviceAtSuspectSignIn", "appFromSuspect", "actedFromSuspect", "exportedFromSuspect", "contactsTaken",
                 "mailFromSuspect", "passwordOnPhishingSite", "failedFromSuspect", "otherAppSession", "chatFromSuspect", "agentActed", "pastPhishingWarning",
                 "passwordOnOtherSite", "breachedPassword", "sharedAddress", "suspectAddress")
# Most urgent first.
TASK_KINDS = ("secureAgain", "secure", "finishLock", "pullAgain", "sweep", "readDirectory", "removeAdded", "clearRecovery", "recoveryQuestion", "unblockSender",
              "deleteDevice", "pullEmail", "warnRecipients", "signOutApp", "warnContacts", "warnChat", "treatAsTaken", "confirmAddress", "checkPassword", "look",
              "checkShared")
# Not a lock, recovery details cleared, a computer, or a read: the logs show each of the first three done whoever did it, and this app does the reads, so a tick
# could only hide an account the attacker can still get back into, or a list left unfinished.
NOT_TICKABLE = frozenset({"secureAgain", "secure", "finishLock", "pullAgain", "sweep", "readDirectory", "clearRecovery", "deleteDevice"})
_ATTACKER_MOMENTS = frozenset({"attackerSignedIn", "attackerFailed", "attackerChanged", "deviceJoined", "attackerActivity"})
_GOOGLE_MOMENTS = frozenset({"googleBlocked", "googleDisabled", "googleSignedOut"})
_PERSON_MOMENTS = frozenset({"ownPassword", "own2SV", "ownPasskey", "ownRecovery"})


@dataclass(frozen=True)
class Event:
    """One log event as the reader sees it. The agent and the app are in words, empty when the person acted themselves."""

    log: str
    time: Optional[datetime]
    who: str
    event: str
    ip: str = ""
    flagged: bool = False
    parameters: Mapping[str, str] = field(default_factory=dict)
    agent: str = ""
    app: str = ""


def event_from_row(row: Any) -> Event:
    """A row of the logs as the reader's event (``core/incident/logs.py``). The first value of a name wins, as ``Row.value`` reads it."""
    return Event(row.application, row.date, row.who, row.event, row.ip, row.flagged, dict(reversed(row.parameters)), row.agent, row.app)


@dataclass(frozen=True)
class Whole:
    """A read of a log that holds every event of its accounts (None: every account's) from `from_time` to `to`: whatever its cap, it leaves nothing out between those
    two. A read by an address holds only that address's, so it's none."""

    accounts: Optional[FrozenSet[str]]
    from_time: datetime
    to: datetime

    def holds(self, account: str, past: datetime) -> bool:
        return (self.accounts is None or account.lower() in self.accounts) and self.from_time <= past and self.to > past


@dataclass(frozen=True)
class NobodyHas:
    """What Phishing's Find last showed of this email. Gone: no mailbox here has it, every mailbox and its Spam searched, the usual end of pulling an email. Not gone: a mailbox has it, or Find couldn't tell,
    which takes back a run or a check from before."""

    message_id: str
    at: datetime
    by: str = ""
    gone: bool = True


@dataclass(frozen=True)
class Done:
    how: str        # trashed, nobodyHasIt, warned, or byHand
    at: datetime
    by: str = ""
    said: str = ""  # byHand: how, as the operator said


@dataclass(frozen=True)
class Ticked:
    """A to-do item marked done by hand, kept with the incident: matched by its kind, its words and its basis, so one whose evidence grew is to do again."""

    kind: str
    words: str
    basis: str
    how: str
    at: datetime
    by: str = ""


def list_words(items: Sequence[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


@dataclass(frozen=True)
class Pulled:
    """Phishing's latest run on one email, as this app's audit log shows it."""

    message_id: str
    at: datetime
    by: str
    steps: int
    trashed: int
    failed: int
    mailboxes: int
    searched_everywhere: bool

    @property
    def is_done(self) -> bool:
        """Every step the run held for it ended trashed, and Find had searched everywhere: no copy Find could see is left."""
        return self.steps > 0 and self.trashed == self.steps and self.searched_everywhere

    @property
    def left(self) -> str:
        but: List[str] = []
        if self.failed > 0:
            but.append(f"{self.failed} failed")
        unfinished = max(0, self.steps - self.trashed - self.failed)
        if unfinished > 0:
            but.append(f"{unfinished} didn't finish")
        if not self.searched_everywhere:
            but.append("Find couldn't search every mailbox for it")
        return ("Phishing's last run" + (f" by {self.by}" if self.by else "")
                + (" trashed it from every mailbox it found" if self.trashed >= self.steps else f" trashed {self.trashed} of its {self.steps} steps")
                + (", but " + list_words(but) if but else "") + ": find it again there to see what's left.")


@dataclass
class Context:
    """What the pull says that its events don't: facts beside the events, never among them."""

    pulled_by: str = ""                         # the address it was pulled by: the operator's to call the attacker's
    read: Optional[Set[str]] = None             # the logs it read, by name; None: not said. A lock is seen only where the admin log was read, by the accounts
    theirs: Set[str] = field(default_factory=set)       # the sign-ins the operator marked as the person's own (`key`)
    capped: Dict[str, datetime] = field(default_factory=dict)   # the logs that stopped at their cap, each with its oldest event read
    covered: Dict[str, Set[str]] = field(default_factory=dict)  # of those, the accounts a later read holds whole past the cap, lowercased
    pulled: List[Pulled] = field(default_factory=list)
    nobody_has: List[NobodyHas] = field(default_factory=list)
    domains: Set[str] = field(default_factory=set)      # the tenant's domains, lowercased: an address at one is inside
    people: Optional[Dict[str, str]] = None     # every person's address here and each alias, lowercased, to their own; None while the directory isn't read
    groups: Dict[str, List[str]] = field(default_factory=dict)  # each group whose members were read: its address to its members'
    everyone: Set[str] = field(default_factory=set)     # the groups read with GAM's CUSTOMER member: every person in `people`
    warnings: Dict[str, Set[str]] = field(default_factory=dict) # the warnings the operator picked for each email: its bare Message-ID to theirs
    phish: Optional[str] = None                 # the email the operator picked as where it started, a bare Message-ID
    swept: Set[str] = field(default_factory=set)        # the addresses a read of every account was made from already
    gmail_whole: List[Whole] = field(default_factory=list)      # whose Gmail log the pull holds every event of, and between when

    def __post_init__(self) -> None:
        if not self.people:     # one read empty: a tenant has at least its admin
            self.people = None


def bare_message_id(text: str) -> str:
    """A Message-ID as Phishing names it: without its angle brackets (the Gmail log keeps them)."""
    trimmed = text.strip(" \t")
    return trimmed[1:-1] if len(trimmed) > 1 and trimmed.startswith("<") and trimmed.endswith(">") else trimmed


def key(account: str, address: str) -> str:
    """A sign-in the operator marked as the person's own. Exact, for whether a sign-in makes a suspect: a network may be shared with the attacker, and a mark must
    never clear them. Only the question about a new address after the lock counts a mark by its network (`_status`)."""
    return f"{account.lower()}|{address.lower()}"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sort_time(event: Event) -> datetime:
    return event.time or _DISTANT_FUTURE


def _past_time(event: Event) -> datetime:
    return event.time or _DISTANT_PAST


def grouped(events: Iterable[Event]) -> List[Tuple[str, List[Event]]]:
    """By account, case aside, in the order each first appears; each named as it first appears."""
    groups: Dict[str, List[Event]] = {}
    names: Dict[str, str] = {}
    for event in events:
        low = event.who.lower()
        names.setdefault(low, event.who)
        groups.setdefault(low, []).append(event)
    return [(names[low], theirs) for low, theirs in groups.items()]


def is_marked(event: Event) -> bool:
    """Google marked this one sign-in suspicious."""
    return (event.parameters.get("is_suspicious") or "").lower() in ("true", "1")


def is_sent(event: Event) -> bool:
    """A Gmail event that is a message sent. Every Gmail event is `delivery`, and its `event_info.mail_event_type` says what happened. From the attacker's address the
    rest are the person's own mail the attacker read, and pulling it would take real mail from everyone who has it."""
    try:
        return int(event.parameters.get("event_info.mail_event_type") or "") in SENT_TYPES
    except ValueError:
        return False


def marks_attacker(event: Event, ordinary: Set[str] = frozenset(), theirs: Set[str] = frozenset(), passkeys_added: Mapping[str, datetime] = {}) -> bool:  # type: ignore[assignment]  # noqa: B006
    """Google's own word that a sign-in was someone else's: a suspicious-sign-in event, an account disabled as hijacked, or a sign-in marked suspicious, unless that
    sign-in was proved with a passkey or security key (the person's own device), or re-proves a session from an address the account signs in from unmarked. Real logs
    mark both. Not every flag either: a person's own failed challenge, or a denied cookie, is flagged too. A sign-in the operator marked as the person's own never is.
    A passkey proves the person only when the account added none in these logs before that sign-in: an attacker who had the account can enroll their own."""
    if event.log != "login" or key(event.who, event.ip) in theirs:
        return False
    if event.event.startswith("suspicious_") or event.event in ("gov_attack_warning", "account_disabled_hijacked"):
        return True
    if not is_marked(event):
        return False
    methods = (event.parameters.get("login_challenge_method") or "").split()
    when = passkeys_added.get(event.who.lower())
    added = when is not None and (event.time is None or when <= event.time)
    if not added and any(m in ("passkey", "security_key", "security_key_otp") for m in methods):
        return False
    return not (event.parameters.get("login_type") == "reauth" and key(event.who, event.ip) in ordinary)


@dataclass(frozen=True)
class Moment:
    """One thing that happened to an account, in its timeline. `plain` is the words with nothing the logs carry as text, which an attacker can set."""

    at: datetime
    until: Optional[datetime]
    kind: str
    words: str
    count: int
    plain: str

    @property
    def who(self) -> str:
        if self.kind in _ATTACKER_MOMENTS:
            return "attacker"
        if self.kind in _GOOGLE_MOMENTS:
            return "google"
        if self.kind in _PERSON_MOMENTS:
            return "person"
        return "unknown" if self.kind == "contactsPrinted" else "admin"


@dataclass(frozen=True)
class Step:
    """A step of the lock: when, and who did it (an admin's address, or Google)."""

    at: datetime
    by: str


@dataclass(frozen=True)
class Status:
    """Where an account the attacker had stands, read from the logs in time order: the lock, and what came after it."""

    account: str
    state: str      # notLocked, partlyLocked, locked, stillIn
    attacker_got_in: bool
    attacked_at: Optional[datetime]
    reset: Optional[Step]
    sign_out: Optional[Step]
    suspended: Optional[Step]
    locked_at: Optional[datetime]
    back_in_at: Optional[datetime]
    back_in_from: str
    tried_at: Optional[datetime]
    new_addresses: Tuple[str, ...]
    unseen: Optional[str]
    earlier_lock_at: Optional[datetime]
    timeline: Tuple[Moment, ...]

    def words(self, when: Callable[[datetime], str]) -> str:
        if self.state == "stillIn":
            if not self.back_in_at or not self.locked_at:
                return ""
            return f"Signed in from {self.back_in_from} at {when(self.back_in_at)}, after the lock at {when(self.locked_at)}: the attacker may still be in."
        if self.state == "locked":      # each step at its own time: a sign-out days before the reset is said as days before
            if self.suspended and self.suspended.at == self.locked_at:
                steps = [f"suspended by {self.suspended.by} at {when(self.suspended.at)}"]
            else:
                pairs = []
                if self.reset:
                    pairs.append((self.reset.at, f"password reset by {self.reset.by} at {when(self.reset.at)}"))
                if self.sign_out:
                    pairs.append((self.sign_out.at, f"signed out by {self.sign_out.by} at {when(self.sign_out.at)}"))
                steps = [w for _, w in sorted(pairs, key=lambda p: p[0])]
            tail = (f" The attacker's address tried again at {when(self.tried_at)} and failed: the lock is holding." if self.tried_at
                    else " No sign-in from a suspect address since.")
            return "Locked: " + ", then ".join(steps) + "." + tail
        if self.state == "partlyLocked":
            if self.reset:
                return f"Password reset at {when(self.reset.at)} by {self.reset.by}, but no sign-out seen: the attacker's open sessions may still work."
            if not self.sign_out:
                return ""
            return f"Signed out at {when(self.sign_out.at)} by {self.sign_out.by}, but no password reset seen: whoever has the password can sign in again."
        if self.unseen:
            return f"No lock seen: {self.unseen}."
        if self.earlier_lock_at:
            return (f"No lock since the first sign-in from a suspect address in these logs; one came at {when(self.earlier_lock_at)}, before it. If the attack began "
                    f"earlier, pull from before it began.")
        return "No lock seen in these logs."


@dataclass(frozen=True)
class Shown:
    """Which events a to-do item is about, for the screen to narrow the logs to. Never given to a model: an app's or agent's name is the logs' text."""

    accounts: Tuple[str, ...]
    addresses: Tuple[str, ...] = ()
    logs: Tuple[str, ...] = ()
    apps: Tuple[str, ...] = ()
    agent: str = ""


@dataclass(frozen=True)
class Draft:
    """A fix a finding points to: a value for a screen, never a command. `kind` is secureAccount, pullEmail or undo."""

    kind: str
    account: str = ""
    message_id: str = ""
    subject: str = ""
    pick: str = ""                  # undo: computer or clearRecovery
    resource_id: str = ""           # undo a computer: the Devices log's ID for it, which may be Google's own
    registered_at: Optional[datetime] = None

    @property
    def title(self) -> str:
        if self.kind == "secureAccount":
            return f"Secure {self.account}…"
        if self.kind == "undo":
            return "Do It Here…"
        subject = self.subject or "(no subject)"
        return f"Pull “{subject[:40] + '…' if len(subject) > 40 else subject}” from Every Mailbox…"     # the attacker's text, of any length


def secure_draft(account: str) -> Draft:
    return Draft("secureAccount", account=account)


def pull_draft(message_id: str, subject: str) -> Draft:
    return Draft("pullEmail", message_id=message_id, subject=subject)


@dataclass(frozen=True)
class Task:
    """Something left to do, worked out by code. It leaves the list once the logs show it done, or once this app shows an email pulled or nobody here having it."""

    id: str
    kind: str
    words: str      # may hold what the logs carry as text (a subject)
    accounts: Tuple[str, ...] = ()
    drafts: Tuple[Draft, ...] = ()
    shows: Optional[Shown] = None
    pulls_again: Tuple[str, ...] = ()
    done: Optional[Done] = None
    sweeps: Optional[str] = None
    theirs: Tuple[str, ...] = ()
    basis: str = ""

    @property
    def tickable(self) -> bool:
        return self.kind not in NOT_TICKABLE


@dataclass(frozen=True)
class Finding:
    id: str
    kind: str
    words: str
    accounts: Tuple[str, ...]
    addresses: Tuple[str, ...]
    events: int
    drafts: Tuple[Draft, ...]
    markable: Tuple[str, ...] = ()  # for a suspect address Google marked: whose own sign-ins it marked there, each one the operator can mark as theirs


@dataclass
class _Pending:
    kind: str
    words: str
    accounts: Sequence[str]
    addresses: Sequence[str]
    events: int
    drafts: Sequence[Draft]
    todo: Sequence[Tuple[str, str, Sequence[Draft]]] = ()
    shows: Optional[Shown] = None
    markable: Sequence[str] = ()


@dataclass(frozen=True)
class Contact:
    """An email here that reached someone after the attacker's did: the first, and how many. Any email, picked as the warning or not, so it never says warned."""

    message_id: str
    sender: str
    subject: str
    at: datetime
    emails: int


@dataclass(frozen=True)
class Recipient:
    address: str
    inside: bool
    mailbox: bool       # a person's mailbox here: their receipt is what says a warning reached them
    outcome: str        # received, bounced, blocked, sent, notAMailbox
    at: Optional[datetime] = None
    warned: Optional[bool] = None   # the log's own word that a warning reached them. False is "not shown warned", never "not warned". None while no warning counts
    via: Optional[str] = None       # the group here the email reached them through
    expanded: bool = False          # a group whose members were read: they're listed, each counted as themselves
    contacted: Optional[Contact] = None

    @property
    def to_warn(self) -> bool:
        """Reached (all but an address that bounced and isn't a person's mailbox here) and not shown warned."""
        return not self.expanded and not (self.outcome == "bounced" and not self.mailbox) and self.warned is not True


@dataclass(frozen=True)
class Candidate:
    """An email sent after this one by an account here, from an address that isn't the attacker's, to people it reached: what the operator can pick as the warning. Or
    one picked, as the logs read show it, with why it doesn't count."""

    message_id: str
    sender: str
    subject: str
    at: Optional[datetime]
    covers: int
    warned: FrozenSet[str] = frozenset()
    problem: Optional[str] = None


REACH_COLUMNS = ("address", "where", "reached", "when", "warned", "to warn", "first contacted after", "contacted by", "contact subject", "emails from here after")


def outcome_words(recipient: Recipient) -> str:
    group = "a group here, each of its members listed as themselves"
    sent = f"sent to {recipient.via}" if recipient.via else "sent"
    if recipient.outcome == "received":
        return "received"
    if recipient.outcome == "bounced":
        return "bounced"
    if recipient.outcome == "blocked":
        return f"quarantined or rejected; {group}" if recipient.expanded else "quarantined or rejected"
    if recipient.outcome == "sent":
        return f"{sent}, no receipt in the logs read" if recipient.inside else f"{sent}, no bounce in the logs read"
    if recipient.expanded:
        return group
    return (f"in {recipient.via}, " if recipient.via else "") + "no one's mailbox here: a group or an address no account has"


def _iso(when: Optional[datetime]) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else ""


@dataclass(frozen=True)
class Reach:
    """Who one email the attacker sent reached, from the Gmail log's own events for it. Every address its sends named, Bcc included, then any mailbox that received it unnamed."""

    message_id: str
    subject: str
    senders: Tuple[str, ...]
    recipients: Tuple[Recipient, ...]
    candidates: Tuple[Candidate, ...]
    warnings: Tuple[Candidate, ...]
    unreadable: int     # entries in its sends' recipients that don't read as an address: never dropped unsaid, and never done while any is
    unnamed: int        # its sends the log shows naming no recipient at all: who they went to is unknown, so never done
    unread: Tuple[str, ...]     # people here still to warn whose Gmail log the pull doesn't show from this email on

    @property
    def reached(self) -> List[Recipient]:
        """Who to warn about it. Over-warning is safe and under-warning the harm, so only an address that bounced and isn't a person's mailbox here is left out."""
        return [r for r in self.recipients if not r.expanded and not (r.outcome == "bounced" and not r.mailbox)]

    @property
    def to_warn(self) -> List[Recipient]:
        return [r for r in self.recipients if r.to_warn]

    @property
    def rows(self) -> List[List[str]]:
        return [[r.address, "inside" if r.inside else "outside", outcome_words(r), _iso(r.at), "" if r.warned is None else ("yes" if r.warned else "not shown"),
                 "yes" if r.to_warn else "", _iso(r.contacted.at) if r.contacted else "", r.contacted.sender if r.contacted else "",
                 r.contacted.subject if r.contacted else "", str(r.contacted.emails) if r.contacted else ""] for r in self.recipients]


@dataclass(frozen=True)
class WayInEmail:
    message_id: str
    sender: str
    subject: str
    at: Optional[datetime]
    received_by: Tuple[str, ...]    # every mailbox here the logs read show receiving it, each once, the first first


@dataclass(frozen=True)
class WayIn:
    """Where it started: the emails the first account the attacker got into received from outside before its attack began, for the operator to pick the one that started it,
    and the one picked, with every mailbox here that received it. Code lists them; nothing guesses which it was."""

    account: str
    attacked_at: datetime
    candidates: Tuple[WayInEmail, ...]
    unnamed: int        # emails it received in the window whose log names no sender: said, never guessed from
    picked: Optional[WayInEmail]
    picked_unread: Optional[str]    # the Message-ID picked when the logs read don't show anyone here receiving it


@dataclass(frozen=True)
class Reading:
    """Every finding, most serious first; where each account stands; what's left to do, and what's shown done."""

    findings: Tuple[Finding, ...]
    statuses: Tuple[Status, ...]
    to_do: Tuple[Task, ...]
    done: Tuple[Task, ...]
    events: int
    reaches: Tuple[Reach, ...]
    way_in: Optional[WayIn]

    @property
    def drafts(self) -> List[Draft]:
        return list(dict.fromkeys(d for f in self.findings for d in f.drafts))

    @property
    def emails_to_pull(self) -> List[str]:
        """The Message-IDs of every email still to pull, each once, in the to-do list's order: never one done."""
        return list(dict.fromkeys(d.message_id for t in self.to_do for d in t.drafts if d.kind == "pullEmail"))

    def ticking(self, ticks: Sequence[Ticked]) -> "Reading":
        """This reading with the items `ticks` mark done by hand moved from what's left to what's done, each with how, when and by whom, and both lists numbered again. A
        tick counts only for an item that can take one, of its kind, saying exactly its words: one the logs now say more about is left to do again."""
        latest: Dict[Tuple[str, str, str], Ticked] = {}
        for one in ticks:
            at = (one.kind, one.words, one.basis)
            if at not in latest or latest[at].at <= one.at:
                latest[at] = one
        if not latest:
            return self
        left: List[Task] = []
        ticked: List[Task] = []
        for task in self.to_do:
            tick = latest.get((task.kind, task.words, task.basis)) if task.tickable else None
            if tick is None:
                left.append(task)
            else:
                ticked.append(Task("", task.kind, task.words, task.accounts, done=Done("byHand", tick.at, tick.by, tick.how), basis=task.basis))
        if not ticked:
            return self

        def numbered(tasks: Sequence[Task], prefix: str) -> Tuple[Task, ...]:
            return tuple(replace(t, id=f"{prefix}{i + 1}") for i, t in enumerate(tasks))

        return replace(self, to_do=numbered(left, "T"), done=numbered([*self.done, *ticked], "D"))


# --- the lock's words -------------------------------------------------------------------------------------------------------

# A password reset by an admin, or by the person from an address they signed in from before the attack; a sign-out by an admin or by Google; a suspension by an admin or
# Google's own disabling. GAM's `deprovision signout` also revokes apps, app passwords and backup codes, which make no lock alone.
RESETS = frozenset({"CHANGE_PASSWORD", "password_edit"})
SIGN_OUTS = frozenset({"RESET_SIGNIN_COOKIES", "user_signed_out_due_to_suspicious_session_cookie"})
SUSPENSIONS = frozenset({"SUSPEND_USER", "account_disabled_hijacked", "account_disabled_password_leak", "account_disabled_generic", "account_disabled_spamming",
                         "account_disabled_spamming_through_relay"})
LOCK_STEPS = RESETS | SIGN_OUTS | SUSPENSIONS
# What a lock leaves in place, by event, as said mid-sentence.
LEFTOVERS = {"passkey_enrolled": "a new passkey", "2sv_enroll": "2-Step Verification on a phone or app of the attacker's choosing",
             "recovery_email_edit": "a changed recovery email", "recovery_phone_edit": "a changed recovery phone", "recovery_secret_qa_edit": "a changed recovery question",
             "blocked_sender": "a blocked sender"}
_CHANGE_WORDS = {"passkey_enrolled": "added a passkey", "2sv_enroll": "turned on 2-Step Verification with a phone or app of their choosing",
                 "recovery_email_edit": "changed the recovery email", "recovery_phone_edit": "changed the recovery phone",
                 "recovery_secret_qa_edit": "changed the recovery question", "blocked_sender": "blocked{sender} in Gmail", "password_edit": "changed the password"}
TIMELINE_CAP = 200      # the earliest kept
_MOMENT_KINDS = ("attackerSignedIn", "attackerFailed", "googleBlocked", "attackerChanged", "deviceJoined", "attackerActivity", "contactsPrinted", "signedOut", "passwordReset",
                 "appsRevoked", "appPasswordsDeleted", "backupCodesDeleted", "recoveryRemoved", "suspended", "unsuspended", "adminChange", "googleDisabled", "googleSignedOut",
                 "ownPassword", "own2SV", "ownPasskey", "ownRecovery")
_DEVICE_BEFORE, _DEVICE_AFTER = -60.0, 600.0    # a device's registration tied to a sign-in: from a minute before it (the two logs' clocks) to ten minutes after
_LOG_NAMES = {"login": "Sign-ins", "token": "OAuth", "admin": "Admin", "useraccounts": "Account changes", "saml": "SAML", "takeout": "Takeout", "geminiinworkspaceapps": "Gemini",
              "drive": "Drive", "calendar": "Calendar", "contacts": "Contacts", "meet": "Meet", "keep": "Keep", "tasks": "Tasks", "gmail": "Gmail", "chat": "Chat", "mobile": "Devices",
              "datastudio": "Looker Studio", "accessevaluation": "Access evaluation"}
_DEVICE_KINDS = {"WINDOWS": "a Windows computer", "MAC": "a Mac", "MAC_OS": "a Mac", "LINUX": "a Linux computer", "CHROME_OS": "a Chromebook", "IOS": "an iPhone or iPad",
                 "IOS_SYNC": "an iPhone or iPad", "ANDROID": "an Android device"}
_ACCOUNT_LOCAL = re.compile(r"[A-Za-z0-9._%+'-]+")
_ACCOUNT_DOMAIN = re.compile(r"[A-Za-z0-9.-]+")


def is_account(who: str) -> bool:
    """Strictly an address's shape: a Chrome event's account is what the browser reports (a computer login name is not one), so nothing else reaches a status or a draft."""
    parts = who.split("@")
    if len(who) > 254 or len(parts) != 2 or not parts[0] or "." not in parts[1] or parts[1].startswith(".") or parts[1].endswith("."):
        return False
    return bool(_ACCOUNT_LOCAL.fullmatch(parts[0]) and _ACCOUNT_DOMAIN.fullmatch(parts[1]))


def unique(items: Iterable[str]) -> List[str]:
    """Each once, case aside, in order."""
    seen: Set[str] = set()
    out = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def log_name(log: str) -> str:
    return _LOG_NAMES.get(log, log)


def _sentence(name: str) -> str:
    words = name.replace("_", " ").lower()
    return words[:1].upper() + words[1:]


def device_words(event: Event) -> str:
    """A registered device as people say it, mid-sentence: "a Windows computer (Virtual Machine, Windows 10.0.19045)"."""
    kind = _DEVICE_KINDS.get((event.parameters.get("DEVICE_TYPE") or "").upper(), "a device")
    about = [w for w in (event.parameters.get("DEVICE_MODEL") or "", event.parameters.get("OS_VERSION") or "") if w]
    return kind + (f" ({', '.join(about)})" if about else "")


def device_word(event: Event, ids: Set[str], account: str) -> Optional[bool]:
    """What ``event`` says of the account's device ``ids``: True there, False gone, None nothing. A Devices-log action counts only once `EXECUTED`: a guessed `SUCCESS`
    matched nothing Google sends, so a computer an admin blocked stayed on the list (failure log 2026-10-22)."""
    p = event.parameters
    if not (p.get("DEVICE_ID") in ids or p.get("RESOURCE_ID") in ids):
        return None
    if event.log == "mobile":
        if event.who.lower() != account.lower():
            return None
        if event.event == "DEVICE_REGISTER_UNREGISTER_EVENT":
            return p.get("ACCOUNT_STATE") != "UNREGISTERED"
        if event.event == "DEVICE_ACTION_EVENT":
            if p.get("ACTION_EXECUTION_STATUS") != "EXECUTED":
                return None
            action = p.get("ACTION_TYPE") or ""
            if action in ("BLOCK", "DISALLOW_ACCESS", "ACCOUNT_WIPE", "DEVICE_WIPE", "UNENROLL"):
                return False
            return True if action in ("APPROVE", "ALLOW_ACCESS") else None
        if event.event == "DEVICE_COMPLIANCE_CHANGED_EVENT":
            return False if p.get("DEVICE_DEACTIVATION_REASON") == "DEVICE_BLOCKED_BY_ADMIN" else None
        return None
    if event.log == "admin":
        if (p.get("USER_EMAIL") or "").lower() != account.lower():
            return None
        if event.event in ("MOBILE_DEVICE_BLOCK", "MOBILE_DEVICE_DELETE", "MOBILE_DEVICE_WIPE", "MOBILE_ACCOUNT_WIPE", "MOBILE_DEVICE_CANCEL_WIPE_THEN_BLOCK"):
            return False
        return True if event.event in ("MOBILE_DEVICE_APPROVE", "MOBILE_DEVICE_CANCEL_WIPE_THEN_APPROVE") else None
    return None


def network(ip: str) -> str:
    """The network an address is in, to tell a person's own from a new one: an IPv4 address itself; an IPv6 address's first 64 bits, which a home keeps while its devices change
    the rest (privacy addresses)."""
    if ":" not in ip:
        return ip
    text = ip.lower().split("%", 1)[0]
    if text.startswith("::ffff:") and "." in text:      # an IPv4 address written as IPv6 is that IPv4 address
        return text[7:]
    halves = text.split("::")
    if len(halves) > 2:
        return text

    def groups(half: str) -> List[str]:
        out: List[str] = []
        for group in (g for g in half.split(":") if g):
            octets = group.split(".")
            if "." in group and len(octets) == 4 and all(o.isdigit() and int(o) < 256 for o in octets):
                out += [format(int(octets[0]) << 8 | int(octets[1]), "x"), format(int(octets[2]) << 8 | int(octets[3]), "x")]
            else:
                out.append(group)
        return out

    head, tail = groups(halves[0]), groups(halves[1]) if len(halves) == 2 else []
    full = head + ["0"] * max(0, 8 - len(head) - len(tail)) + tail if len(halves) == 2 else head

    def value(group: str) -> str:
        try:
            return format(int(group, 16), "x")
        except ValueError:
            return "0"

    return ":".join(value(g) for g in full[:4])


def _proof(event: Event) -> str:
    """How an attacker's sign-in got past Google's challenge, mid-sentence: the means of the takeover."""
    methods = (event.parameters.get("login_challenge_method") or "").split()
    if "knowledge_historical_password" in methods:
        return ", answering Google's question for a previous password"
    if "device_prompt" in methods:
        return ": someone approved the sign-in prompt on the person's phone"
    if any(m.startswith("idv_") for m in methods):
        return ": someone entered the code sent to the person's phone"
    return ", with a passkey" if "passkey" in methods else ""


def address(text: str) -> Optional[str]:
    """An email address as Who it reached lists one, lowercased: text around its last `@`, neither part empty, no space or control character, at most 320 characters. Looser than
    `is_account`: a recipient's address can hold `&`, `#` or letters beyond ASCII, and dropping one would hide someone still to warn."""
    found = text.strip(" \t").lower()
    at = found.rfind("@")
    if not 3 <= len(found) <= 320 or at <= 0 or at == len(found) - 1:
        return None
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in found):
        return None
    return found


def destinations(event: Event, read: Callable[[str], Optional[str]] = address) -> Tuple[List[str], int]:
    """`message_info.flattened_destinations` as addresses (`service:selector:address` entries, comma-separated: Google's schema for Gmail logs), each once, and how many entries
    didn't end in one."""
    field_text = event.parameters.get("message_info.flattened_destinations") or ""
    addresses: List[str] = []
    seen: Set[str] = set()
    unreadable = 0
    for entry in field_text.split(","):
        entry = entry.strip(" \t")
        if not entry:
            continue
        found = read(entry.rsplit(":", 1)[-1])
        if found is None:
            unreadable += 1
        elif found not in seen:
            seen.add(found)
            addresses.append(found)
    return addresses, unreadable


class _Accounts:
    """Each account's events, case aside, in `events`' order: its own and the admin log's steps done to it, each once. Every rule about one account reads only these: a whole pull
    for every account holds hundreds of thousands, and reading them all once per account took minutes (failure log 2026-10-23)."""

    def __init__(self, events: Sequence[Event]) -> None:
        self._events = events
        self._indices: Dict[str, List[int]] = {}
        for i, event in enumerate(events):
            who = event.who.lower()
            self._indices.setdefault(who, []).append(i)
            target = (event.parameters.get("USER_EMAIL") or "").lower()
            if target and target != who:
                self._indices.setdefault(target, []).append(i)

    def __getitem__(self, account: str) -> List[Event]:
        return [self._events[i] for i in self._indices.get(account.lower(), ())]


def _timeline(me: str, events: Sequence[Event], acting: Set[str], since: Optional[datetime], known: Callable[[str], bool], joined: Sequence[Event]) -> Tuple[Moment, ...]:
    """`me`'s timeline, from the attack on: each attacker sign-in, failure and change from a suspect address, the device that joined at one, and the rest the attacker did a line per
    log; each step of every lock an admin took; what Google did; and the person's own changes, said with the address when it's not one they used before the attack."""
    start = since or _DISTANT_PAST
    moments: List[Moment] = []

    def add(at: Optional[datetime], kind: str, words: str, until: Optional[datetime] = None, count: int = 1, plain: Optional[str] = None) -> None:
        if at is not None:
            moments.append(Moment(at, until, kind, words, count, plain if plain is not None else words))

    activity: Dict[str, List[Event]] = {}
    for event in events:
        if event.who.lower() != me or event.ip not in acting:
            continue
        p = event.parameters
        pair = (event.log, event.event)
        if pair == ("login", "login_success"):
            add(event.time, "attackerSignedIn", f"The attacker signed in from {event.ip}{_proof(event)}")
        elif pair == ("saml", "login_success"):
            add(event.time, "attackerSignedIn", f"The attacker signed in to {p.get('application_name') or 'another app'} through Google from {event.ip}",
                plain=f"The attacker signed in to another app through Google from {event.ip}")
        elif pair == ("login", "login_failure"):
            add(event.time, "attackerFailed", f"The attacker's sign-in from {event.ip} failed")
        elif event.log == "login" and event.event.startswith("suspicious_"):
            add(event.time, "googleBlocked", f"Google blocked a suspicious sign-in from {event.ip}")
        elif pair == ("token", "authorize"):
            add(event.time, "attackerChanged", f"The attacker gave {p.get('app_name') or 'an app'} access", plain="The attacker gave an app access")
        elif event.event in _CHANGE_WORDS:
            affected = p.get("affected_email_address")
            sender = f" {affected}" if affected else " a sender"
            add(event.time, "attackerChanged", "The attacker " + _CHANGE_WORDS[event.event].replace("{sender}", sender),
                plain="The attacker " + _CHANGE_WORDS[event.event].replace("{sender}", " a sender"))
        elif event.log in ("login", "accessevaluation"):
            pass    # a sign-in's challenges and token checks are part of the sign-in
        else:
            activity.setdefault(event.log, []).append(event)
    for log in sorted(activity):
        theirs = activity[log]
        times = sorted(e.time for e in theirs if e.time)
        add(times[0] if times else None, "attackerActivity", f"{len(theirs)} {log_name(log)} event{'' if len(theirs) == 1 else 's'} from the attacker's address",
            until=times[-1] if len(times) > 1 else None, count=len(theirs))
    for device in joined:
        if device.who.lower() == me:
            words = device_words(device)
            add(device.time, "deviceJoined", f"{words[:1].upper()}{words[1:]} joined the account", plain="A computer joined the account at the attacker's sign-in")
    for event in events:
        if event.log == "contacts" and event.event == "print_contacts" and event.who.lower() == me and _past_time(event) >= start:
            count = event.parameters.get("CONTACTS_COUNT") or ""
            add(event.time, "contactsPrinted", "Their contacts" + (f" ({int(count):,})" if count.isdigit() else "") + " were printed")
    # An admin's steps, never from a suspect address; an app's access revoked many at once is said once.
    revoked: List[Tuple[datetime, str]] = []
    simple = {"CHANGE_PASSWORD": ("passwordReset", "Password reset by {}"), "RESET_SIGNIN_COOKIES": ("signedOut", "Signed out of every session by {}"),
              "REVOKE_ASP": ("appPasswordsDeleted", "An app password deleted by {}"), "DELETE_2SV_SCRATCH_CODES": ("backupCodesDeleted", "2-Step Verification backup codes deleted by {}"),
              "REMOVE_RECOVERY_EMAIL": ("recoveryRemoved", "Recovery email removed by {}"), "REMOVE_RECOVERY_PHONE": ("recoveryRemoved", "Recovery phone removed by {}"),
              "SUSPEND_USER": ("suspended", "Suspended by {}"), "UNSUSPEND_USER": ("unsuspended", "Unsuspended by {}"), "MOBILE_DEVICE_BLOCK": ("adminChange", "A device blocked by {}"),
              "MOBILE_DEVICE_DELETE": ("adminChange", "A device removed by {}"), "MOBILE_DEVICE_WIPE": ("adminChange", "A device wiped by {}"), "MOBILE_ACCOUNT_WIPE": ("adminChange", "A device wiped by {}")}
    for event in events:
        if event.log != "admin" or (event.parameters.get("USER_EMAIL") or "").lower() != me or event.ip in acting or _past_time(event) < start:
            continue
        by = event.who or "an admin"
        if event.event in ("REVOKE_3LO_TOKEN", "REVOKE_3LO_DEVICE_TOKENS"):
            if event.time:
                revoked.append((event.time, by))
        elif event.event in simple:
            kind, words = simple[event.event]
            add(event.time, kind, words.format(by))
        elif event.event != "CHANGE_PASSWORD_ON_NEXT_LOGIN":    # said with the reset beside it
            add(event.time, "adminChange", f"{_sentence(event.event)} by {by}")
    batches: List[List[Any]] = []       # one batch: the same admin's revocations within a minute of the batch's first
    for at, by in sorted(revoked, key=lambda r: r[0]):
        last = next((b for b in reversed(batches) if b[2] == by), None)
        if last is not None and (at - last[0]).total_seconds() <= 60:
            last[1] += 1
        else:
            batches.append([at, 1, by])
    for at, count, by in batches:
        add(at, "appsRevoked", f"{count} app{chr(39) + 's' if count == 1 else 's' + chr(39)} access revoked by {by}", count=count)
    # Google, and the person: their own changes from addresses that aren't a suspect's.
    for event in events:
        if event.log not in ("login", "useraccounts") or event.who.lower() != me or event.ip in acting or _past_time(event) < start:
            continue
        said = "" if not event.ip or known(event.ip) else f", from {event.ip}, an address not seen before the attack"
        name = event.event
        if name.startswith("account_disabled_"):
            add(event.time, "googleDisabled", "Google disabled the account" + (" as hijacked" if name == "account_disabled_hijacked"
                                                                                 else ": its password leaked" if name == "account_disabled_password_leak" else ""))
        elif name == "user_signed_out_due_to_suspicious_session_cookie":
            add(event.time, "googleSignedOut", "Google signed them out: a suspicious session cookie")
        elif name == "password_edit":   # from an address not theirs before the attack it may not be them: said without saying who
            add(event.time, "ownPassword", "They changed their own password" if not said else "The password was changed" + said)
        elif name == "2sv_enroll":
            add(event.time, "own2SV", "They turned on 2-Step Verification" + said)
        elif name == "passkey_enrolled":
            add(event.time, "ownPasskey", "They added a passkey" + said)
        elif name == "recovery_phone_edit":
            add(event.time, "ownRecovery", "They changed their recovery phone" + said)
        elif name == "recovery_email_edit":
            add(event.time, "ownRecovery", "They changed their recovery email" + said)
    # Once each (a password change is in both the sign-in and account changes logs), in time order.
    seen: Set[Tuple[datetime, str]] = set()
    out: List[Moment] = []
    for moment in sorted(moments, key=lambda m: (m.at, _MOMENT_KINDS.index(m.kind), m.words)):
        if (moment.at, moment.words) not in seen:
            seen.add((moment.at, moment.words))
            out.append(moment)
    return tuple(out[:TIMELINE_CAP])


def _status(account: str, events: Sequence[Event], acting: Set[str], context: Context, joined: Sequence[Event] = (), phished_at: Optional[datetime] = None,
            held: Optional[Mapping[str, Tuple[datetime, datetime]]] = None) -> Status:
    """Where `account` stands: the first password reset, sign-out and suspension done to it since the attacker's first event (by an admin, never from a suspect address, which would be
    an attacker with an admin's account; or by Google), the lock they make, and the sign-ins after it. A sign-in from a suspect address after a lock starts it over."""
    held = held or {}
    me = account.lower()
    theirs = [e for e in events if e.who.lower() == me]
    times = [e.time for e in theirs if e.ip in acting and e.time]
    if phished_at:
        times.append(phished_at)
    attacked_at = min(times) if times else None     # nothing before counts as its lock, and without it nothing does
    # The addresses it signed in from before the attack: the person's own. A later sign-in counts as theirs by network (a home's IPv6 address changes), unless a suspect address shares
    # that network; a password change only from the very address.
    before_exact = {e.ip for e in theirs if e.log == "login" and e.event == "login_success" and e.time and attacked_at and e.time < attacked_at}
    suspect_networks = {network(ip) for ip in acting}
    known_networks = {network(ip) for ip in before_exact} - suspect_networks
    mine = me + "|"
    marked = {k[len(mine):] for k in context.theirs if k.startswith(mine)}
    marked_networks = {network(ip) for ip in marked} - suspect_networks

    def known(ip: str) -> bool:
        return ip in before_exact or network(ip) in known_networks or ip.lower() in marked or network(ip) in marked_networks

    unsuspended = [e.time for e in events if e.log == "admin" and e.event == "UNSUSPEND_USER" and (e.parameters.get("USER_EMAIL") or "").lower() == me and e.time]
    done: List[Event] = []
    for event in events:        # done to the account, oldest first
        at = event.time
        if at is None:
            continue
        if event.event in SUSPENSIONS and any(u > at for u in unsuspended):     # a suspension later undone isn't a lock
            continue
        if event.log == "admin":
            span = held.get(event.who.lower())
            if (event.parameters.get("USER_EMAIL") or "").lower() == me and event.ip not in acting and not (span and span[0] <= at <= span[1]):
                done.append(event)
        elif event.log in ("login", "useraccounts") and event.who.lower() == me:
            if event.event != "password_edit" or (event.ip not in acting and event.ip in before_exact):
                done.append(event)
    done.sort(key=_past_time)
    entries = sorted((e for e in theirs if e.ip in acting and e.log in ("login", "saml") and e.event == "login_success" and e.time), key=_past_time)

    def first(names: FrozenSet[str], after: Optional[datetime]) -> Optional[Step]:
        for event in done:
            if event.event in names and (after is None or _past_time(event) >= after) and event.time:
                return Step(event.time, event.who if event.log == "admin" or event.event == "password_edit" else "Google")
        return None

    since = attacked_at
    reset = sign_out = suspended = None
    whole = locked_at = None
    back_in: Optional[Event] = None
    while attacked_at is not None:
        reset, sign_out, suspended = first(RESETS, since), first(SIGN_OUTS, since), first(SUSPENSIONS, since)
        options = [o for o in (suspended.at if suspended else None, max(reset.at, sign_out.at) if reset and sign_out else None) if o]
        whole = min(options) if options else None
        either = reset or sign_out
        locked_at = whole or (either.at if either else None)
        if locked_at is None:
            back_in = None
            break
        back_in = next((e for e in entries if _past_time(e) > locked_at), None)
        back = back_in.time if back_in else None
        if back is None or not any(e.event in LOCK_STEPS and _past_time(e) > back for e in done):
            break
        since = back
    after = [e for e in theirs if e.time and e.time > locked_at] if locked_at else []
    tried = [e.time for e in after if e.ip in acting and e.log == "login" and (e.event == "login_failure" or e.event.startswith("suspicious_")) and e.time]
    places: Set[str] = set()
    new_addresses: List[str] = []
    for e in after:
        if e.log == "login" and e.event == "login_success" and e.ip and e.ip not in acting and not known(e.ip):
            place = e.ip if network(e.ip) in suspect_networks else network(e.ip)
            if place not in places:
                places.add(place)
                new_addresses.append(e.ip)
    unseen: Optional[str] = None
    if locked_at is None:
        if context.pulled_by:
            unseen = "a pull by one address doesn't show it (the lock is done from yours): pull by the accounts"
        elif context.read is not None and "admin" not in context.read:
            unseen = "the admin log, where it would be, wasn't read"
    # A lock before the first suspect event these logs hold: the window may start after the attack did.
    earlier = None
    if locked_at is None:
        last = [e for e in done if e.event in LOCK_STEPS]
        earlier = last[-1].time if last else None
    state = "stillIn" if back_in else "locked" if whole else "partlyLocked" if locked_at else "notLocked"
    return Status(account, state, bool(entries), attacked_at, reset, sign_out, suspended, locked_at, back_in.time if back_in else None, back_in.ip if back_in else "",
                  max(tried) if tried else None, tuple(new_addresses), unseen, earlier, _timeline(me, events, acting, attacked_at, known, joined))


# --- who it reached ---------------------------------------------------------------------------------------------------------


def _mail_type(event: Event) -> Optional[int]:
    try:
        return int(event.parameters.get("event_info.mail_event_type") or "")
    except ValueError:
        return None


class _Mail:
    """What the Gmail log says of one email: its sends and sends begun, oldest first; every address they named, then every mailbox that received it unnamed, each once, an alias as its
    person; who a send Google didn't mark failed named; who received it; and what failed, each with when."""

    def __init__(self) -> None:
        self.subject = ""
        self.sends: List[Event] = []
        self.begun: List[Event] = []
        self.order: List[str] = []
        self.sent_ok: Set[str] = set()
        self.received: Dict[str, Optional[datetime]] = {}
        self.failed: Dict[str, Tuple[bool, Optional[datetime]]] = {}    # (bounced, at)
        self.last_received: Dict[str, datetime] = {}
        self.last_failed: Dict[str, datetime] = {}
        self.unreadable = 0
        self.unnamed = 0        # its Sends with no recipient named at all
        self.whole_failed = False   # a failure naming no recipient, nor a recipient's own mailbox: the whole email's
        self._seen: Set[str] = set()

    def ours(self, attacker: Set[str]) -> Optional[Event]:
        """Its first send Google didn't mark failed, by an account here, from an address that isn't the attacker's: what makes it a warning."""
        return next((e for e in self.sends if is_account(e.who) and e.ip not in attacker and (e.parameters.get("event_info.success") or "").lower() != "false"), None)

    def received_last(self, who: str) -> bool:
        """Its last word for ``who`` is a receipt: one received, then quarantined, wasn't reached; one quarantined, then released, was."""
        at = self.last_received.get(who)
        if at is None:
            return False
        failed = self.last_failed.get(who)
        return failed is None or at > failed

    @property
    def addressed(self) -> Set[str]:
        """Who it went to, failed aside: what a candidate warning is offered and counted by."""
        return {a for a in self.order if self.received_last(a) or (not self.whole_failed and a in self.sent_ok and a not in self.failed)}

    def add(self, who: str) -> None:
        if who not in self._seen:
            self._seen.add(who)
            self.order.append(who)

    def fail(self, who: str, bounced: bool, at: Optional[datetime]) -> None:
        self.add(who)
        # A failure that isn't a bounce wins over one that is: Gmail trying again after a bounce leaves someone who may have it, to warn.
        known = self.failed.get(who)
        if known is None or (known[0] and not bounced):
            self.failed[who] = (bounced, at)
        self.last_failed[who] = max(self.last_failed.get(who, _DISTANT_PAST), at or _DISTANT_FUTURE)


def _mails(events: Iterable[Event], person: Callable[[str], str], read: Callable[[str], Optional[str]]) -> Dict[str, _Mail]:
    by_id: Dict[str, List[Event]] = {}
    for event in events:
        raw = event.parameters.get("message_info.rfc2822_message_id")
        if raw:
            by_id.setdefault(bare_message_id(raw), []).append(event)
    out: Dict[str, _Mail] = {}
    for message, theirs in by_id.items():
        mail = _Mail()
        ordered = sorted(theirs, key=_past_time)
        mail.subject = next((e.parameters["message_info.subject"] for e in ordered if e.parameters.get("message_info.subject")), "")
        unnamed_failures: List[Tuple[str, bool, Optional[datetime]]] = []
        for event in ordered:
            kind = _mail_type(event)
            failed_event = (event.parameters.get("event_info.success") or "").lower() == "false"
            if kind in (1, 35):
                named, unreadable = destinations(event, read)
                (mail.sends if kind == 1 else mail.begun).append(event)
                mail.unreadable += unreadable
                if kind == 1 and not named and unreadable == 0:     # a Send names its recipients: one that names none can't say who it went to
                    mail.unnamed += 1
                    if failed_event:
                        mail.whole_failed = True
                for who in map(person, named):
                    mail.add(who)
                    if kind == 1 and failed_event:
                        mail.fail(who, False, event.time)
                    elif kind == 1:
                        mail.sent_ok.add(who)
            elif kind in (2, 6) and not failed_event:
                mailbox = read(event.who)
                if mailbox is None:
                    continue
                mailbox = person(mailbox)
                mail.add(mailbox)
                if mailbox not in mail.received:
                    mail.received[mailbox] = event.time
                mail.last_received[mailbox] = max(mail.last_received.get(mailbox, _DISTANT_PAST), event.time or _DISTANT_PAST)
            elif kind in (30, 5, 34) or failed_event:
                # A failure names its recipients, or is the mailbox's own, or, from a sender, the whole email's: decided once every send is known, below.
                named = list(map(person, destinations(event, read)[0]))
                if not named:
                    own = read(event.who)
                    if own is None:
                        mail.whole_failed = True
                    else:
                        unnamed_failures.append((person(own), kind == 30, event.time))
                for who in named:
                    mail.fail(who, kind == 30, event.time)
        senders = {person(found) for found in (read(e.who) for e in (*mail.sends, *mail.begun)) if found}
        for own, bounced, when in unnamed_failures:
            if own in senders:
                mail.whole_failed = True
            else:
                mail.fail(own, bounced, when)
        out[message] = mail
    return out


def _reaches(ids: Sequence[str], events: Sequence[Event], attacker: Set[str], context: Context) -> Tuple[Reach, ...]:
    """Who each email in `ids` reached, and the emails an account here sent after it to the same people. Two passes over the Gmail events: the first finds whose receipts the pull has and
    which emails matter; the second sums only those, each once. A whole pull holds hundreds of thousands of events (failure log 2026-10-23)."""
    if not ids:
        return ()
    attackers = {bare_message_id(i) for i in ids}
    picks = {p for values in context.warnings.values() for p in values}
    people = context.people
    readings: Dict[str, Optional[str]] = {}

    def read(text: str) -> Optional[str]:
        if text not in readings:
            readings[text] = address(text)
        return readings[text]

    def person(who: str) -> str:
        """A person's address, alias or plus-address (`alice+tag@`) as the person; anything else as it is."""
        if people is None:
            return who
        if who in people:
            return people[who]
        plus, at = who.find("+"), who.rfind("@")
        return people.get(who[:plus] + who[at:], who) if 0 <= plus < at else who

    everyone = sorted(set(people.values())) if people else []
    through: Dict[str, Tuple[List[str], Set[str]]] = {}

    def members(group: str) -> Tuple[List[str], Set[str]]:
        """Who a group read reaches: its members, then the members of the groups read among them, each once (groups can hold each other), and everyone here for one with GAM's CUSTOMER member."""
        if group in through:
            return through[group]
        found: List[str] = []
        seen = {group}
        queue = [group]
        while queue:
            following = queue.pop(0)
            listed = [person(r) for r in (read(m) for m in context.groups.get(following, ())) if r] + (everyone if following in context.everyone else [])
            for member in listed:
                if member not in seen:
                    seen.add(member)
                    found.append(member)
                    if member in context.groups:
                        queue.append(member)
        through[group] = (found, set(found))
        return through[group]

    # Pass 1: each Gmail event's email and type once; when the attacker's first email went; and whose Gmail log the pull has after it.
    ided: List[Tuple[str, Optional[int], Event]] = []
    first = _DISTANT_FUTURE
    sent_here: Set[str] = set()
    receivers: List[Tuple[str, Optional[datetime]]] = []
    for event in events:
        if event.log != "gmail":
            continue
        raw = event.parameters.get("message_info.rfc2822_message_id")
        if not raw:
            continue
        message = bare_message_id(raw)
        kind = _mail_type(event)
        ided.append((message, kind, event))
        if kind in (2, 6):
            receivers.append((event.who, event.time))
        if message in attackers and event.time:
            first = min(first, event.time)
        if kind == 1 and event.ip not in attacker:
            sent_here.add(message)
    receipts: Set[str] = set()
    latest: Dict[str, datetime] = {}
    for who, at in receivers:
        found = read(who)
        if found is None:
            continue
        mailbox = person(found)
        when = at or first
        if when >= first:
            receipts.add(mailbox)
        latest[mailbox] = max(latest.get(mailbox, _DISTANT_PAST), when)
    # Pass 2: the attacker's emails and the picks, each once; then only the emails here after the first of them that went to someone those reached.
    named = attackers | picks
    mails = _mails((e for m, _, e in ided if m in named), person, read)
    reached_all: Set[str] = set()
    for message in attackers:
        for who in (mails[message].order if message in mails else ()):
            reached_all.add(who)
            if who in context.groups:
                reached_all.update(members(who)[0])
    candidates: Set[str] = set()
    for message, kind, event in ided:
        if kind == 1 and (event.time or first) >= first and message not in named and message not in candidates and event.ip not in attacker:
            if is_account(event.who) and any(person(a) in reached_all for a in destinations(event, read)[0]):
                candidates.add(message)
    for message, kind, event in ided:       # only one an account here sent can be a warning: mail from outside a person received is never summed
        if kind in (2, 6) and (event.time or first) >= first and message not in named and message not in candidates and message in sent_here:
            found = read(event.who)
            if found and person(found) in reached_all:
                candidates.add(message)
    for message, mail in _mails((e for m, _, e in ided if m in candidates), person, read).items():
        mails.setdefault(message, mail)
    insides: Dict[str, bool] = {}
    mailboxes: Dict[str, bool] = {}

    def is_inside(who: str) -> bool:
        if who not in insides:
            insides[who] = who in receipts or (people is not None and who in people) or who.rsplit("@", 1)[-1] in context.domains
        return insides[who]

    def is_mailbox(who: str) -> bool:
        if who not in mailboxes:
            mailboxes[who] = is_inside(who) and (people is None or who in people or who in receipts)
        return mailboxes[who]

    warnings: Dict[str, Candidate] = {}
    by_address: Dict[str, List[str]] = {}
    for message, mail in mails.items():
        if message in attackers:
            continue
        send = mail.ours(attacker)
        if send is None:
            continue
        warnings[message] = Candidate(message, send.who, mail.subject, send.time, 0)
        for who in mail.addressed:
            by_address.setdefault(who, []).append(message)
    reached_by: Dict[str, Set[str]] = {}

    def reached(message: str) -> Set[str]:
        """Who the log says a picked warning reached: a mailbox here whose last word of it is a receipt; outside, or no one's mailbox, a send naming it with nothing failing."""
        if message not in reached_by:
            mail = mails.get(message)
            reached_by[message] = {a for a in (mail.order if mail else ()) if (mail.received_last(a) if is_mailbox(a) else
                                                                                (not mail.whole_failed and a in mail.sent_ok and a not in mail.failed))} if mail else set()
        return reached_by[message]

    def covered(found: Set[str]) -> Callable[[str], bool]:
        """Whom a warning's reach covers: those it reached, and an outside address or a group not read there through a group read it reached by its send. A person here only by their own receipt."""
        groups = [g for g in found if g in context.groups]
        return lambda who: who in found or (not is_mailbox(who) and any(who in members(g)[1] for g in groups))

    def earlier(a: Candidate, b: Candidate) -> bool:
        return (a.at or _DISTANT_PAST, a.message_id) < (b.at or _DISTANT_PAST, b.message_id)

    out: List[Reach] = []
    for pasted in ids:
        message = bare_message_id(pasted)
        mail = mails.get(message) or _Mail()
        times = [e.time for e in (*mail.sends, *mail.begun) if e.time]
        after = min(times) if times else _DISTANT_PAST
        picked: List[Candidate] = []
        for chosen in sorted(context.warnings.get(message, ())):
            found_mail = mails.get(chosen)
            send = found_mail.ours(attacker) if found_mail else None
            if chosen in attackers:
                problem: Optional[str] = "it's one the attacker sent"
            elif found_mail and not found_mail.sends and found_mail.begun:
                problem = "the logs read show its send begun, never sent"
            elif not found_mail or not found_mail.sends:
                problem = "it isn't in the logs read: pull its sender's Gmail log"
            elif send is None:
                problem = "Google marked its send failed, or it was sent from the attacker's address"
            elif (send.time or _DISTANT_PAST) <= after:
                problem = "it was sent before the email"
            else:
                problem = None
            shown = send or (found_mail.sends[0] if found_mail and found_mail.sends else None)
            picked.append(Candidate(chosen, shown.who if shown else "", found_mail.subject if found_mail else "", shown.time if shown else None, 0, frozenset(), problem))
        counting = [p.message_id for p in picked if p.problem is None]
        warned = covered({a for c in counting for a in reached(c)})

        def is_expanded(who: str) -> bool:      # a group here whose members were read counts by them; a group it bounced from reached no one
            failure = mail.failed.get(who)  # noqa: B023 - called only in this iteration
            return who in context.groups and is_inside(who) and not is_mailbox(who) and not (failure and failure[0])

        addresses = list(mail.order)
        via: Dict[str, str] = {}
        listed = set(mail.order)
        for group in mail.order:
            if is_expanded(group):
                for member in members(group)[0]:
                    if member not in listed:
                        listed.add(member)
                        addresses.append(member)
                        via[member] = group

        def contact(who: str) -> Optional[Contact]:
            """The first email here that reached them after this one, and how many: by their own address, or for one that isn't a mailbox here, through the group it was listed by."""
            emails = list(by_address.get(who, ()))
            if not is_mailbox(who) and who in via:      # noqa: B023 - called only in this iteration
                emails += by_address.get(via[who], ())  # noqa: B023
            best: Optional[Candidate] = None
            seen_ids: Set[str] = set()
            count = 0
            for other in emails:
                if other in seen_ids:
                    continue
                seen_ids.add(other)
                email = warnings.get(other)
                if email is None or email.at is None or email.at <= after:  # noqa: B023
                    continue
                count += 1
                if best is None or earlier(email, best):
                    best = email
            return Contact(best.message_id, best.sender, best.subject, best.at, count) if best and best.at else None

        recipients: List[Recipient] = []
        for who in addresses:
            expanded = is_expanded(who)
            failure = mail.failed.get(who)
            if who in mail.received:
                outcome, at = "received", mail.received[who]
            elif failure:
                outcome, at = ("bounced" if failure[0] else "blocked"), failure[1]
            elif is_inside(who) and not is_mailbox(who):
                outcome, at = "notAMailbox", None if after == _DISTANT_PAST else after
            else:
                outcome, at = "sent", None if after == _DISTANT_PAST else after
            recipients.append(Recipient(who, is_inside(who), is_mailbox(who), outcome, at, None if not counting or expanded else warned(who), via.get(who), expanded,
                                        None if expanded else contact(who)))
        reach = {r.address for r in recipients if not r.expanded and not (r.outcome == "bounced" and not r.mailbox)}
        covers: Dict[str, int] = {}
        picked_ids = {p.message_id for p in picked}
        for who in reach:
            for other in by_address.get(who, ()):
                if other not in picked_ids:
                    covers[other] = covers.get(other, 0) + 1
        # The most people first, then the earlier, at most CANDIDATES_SHOWN: whole counts taken from the top until there are enough, and only those sorted.
        by_count: Dict[int, List[str]] = {}
        for other, count in covers.items():
            by_count.setdefault(count, []).append(other)
        offered: List[Candidate] = []
        for count in sorted(by_count, reverse=True):
            if len(offered) >= CANDIDATES_SHOWN:
                break
            room = CANDIDATES_SHOWN - len(offered)
            best: List[Candidate] = sorted((w for w in (warnings.get(o) for o in by_count[count]) if w and (w.at or _DISTANT_PAST) > after),
                                           key=lambda w: (w.at or _DISTANT_PAST, w.message_id))[:room]
            offered += [replace(w, covers=count) for w in best]
        # People here still to warn whose log the pull doesn't show from this email on. None while the directory isn't read. One a read holds whole past the email is their mailbox being quiet.
        unread = [] if people is None else [r.address for r in recipients if r.mailbox and r.warned is not True and (r.address not in latest or latest[r.address] < after)
                                            and not any(w.holds(r.address, after) for w in context.gmail_whole)]
        warnings_out = []
        for pick in picked:
            shown_to = frozenset(a for a in reach if covered(reached(pick.message_id))(a)) if pick.problem is None else frozenset()
            warnings_out.append(replace(pick, covers=len(shown_to), warned=shown_to))
        out.append(Reach(message, mail.subject, tuple(unique(e.who for e in mail.sends if is_account(e.who))), tuple(recipients), tuple(offered[:CANDIDATES_SHOWN]), tuple(warnings_out),
                         mail.unreadable, mail.unnamed, tuple(unique(unread))))
    return tuple(out)


@dataclass(frozen=True)
class GotIt:
    """One person the attacker's emails got to, across every email."""

    address: str
    inside: bool
    mailbox: bool
    received: bool      # a receipt in its mailbox here; else sent, and nothing in the logs read says it failed
    at: Optional[datetime]
    subjects: Tuple[str, ...]
    emails: int
    to_warn: bool
    warned: bool
    contacted: Optional[Contact]
    through: str = ""   # the group here it was listed through, when only a group's members named it


GOT_IT_COLUMNS = ("Email", "Where", "Got it", "First got", "Emails", "Subjects", "Warned", "To warn", "First contacted after", "Contacted by", "Contact subject", "Through group", "2-Step Verification")


def got_it(reaches: Sequence[Reach]) -> List[GotIt]:
    """Everyone the attacker's emails got to, each once, in the order they first did: a receipt here, or a send nothing in the logs read says failed. A bounce, a quarantine or a rejection
    didn't get through, and a group whose members are listed is them."""
    found: Dict[str, Dict[str, Any]] = {}
    for reach in reaches:
        subject = reach.subject or "(no subject)"
        for r in reach.recipients:
            if r.expanded or r.outcome not in ("received", "sent", "notAMailbox"):
                continue
            seen = found.get(r.address)
            if seen is None:
                found[r.address] = {"inside": r.inside, "mailbox": r.mailbox, "received": r.outcome == "received", "at": r.at, "subjects": [subject], "emails": 1, "to_warn": r.to_warn,
                                    "warned": r.warned is True, "contacted": r.contacted, "through": r.via}
                continue
            seen["received"] = seen["received"] or r.outcome == "received"
            if r.at:
                seen["at"] = min(seen["at"] or r.at, r.at)
            if subject not in seen["subjects"]:
                seen["subjects"].append(subject)
            seen["emails"] += 1
            seen["to_warn"] = seen["to_warn"] or r.to_warn
            seen["warned"] = seen["warned"] and r.warned is True
            if r.contacted and (seen["contacted"] is None or r.contacted.at < seen["contacted"].at):
                seen["contacted"] = r.contacted
            if r.via is None:       # named itself by any of them: not only through a group
                seen["through"] = None
    return [GotIt(a, f["inside"], f["mailbox"], f["received"], f["at"], tuple(f["subjects"]), f["emails"], f["to_warn"], f["warned"], f["contacted"], f["through"] or "") for a, f in found.items()]


def _way_in(statuses: Sequence[Status], events: Sequence[Event], context: Context) -> Optional[WayIn]:
    """Where it started, from the first account the attacker got into's Gmail receipts (type 2 or 6, not marked failed): a sender outside the tenant's domains, in the window. Two passes: the
    account's receipts, then everyone's receipts of those few emails."""
    got_in = [s for s in statuses if s.attacker_got_in and s.attacked_at]
    if not got_in:
        return None
    first_in = min(got_in, key=lambda s: s.attacked_at or _DISTANT_FUTURE)
    account, attacked_at = first_in.account.lower(), first_in.attacked_at or _DISTANT_FUTURE
    start = attacked_at - timedelta(hours=WAY_IN_HOURS)

    def person(text: str) -> Optional[str]:
        found = address(text)
        if found is None:
            return None
        return context.people.get(found, found) if context.people else found

    def receipt(event: Event) -> Optional[str]:
        raw = event.parameters.get("message_info.rfc2822_message_id")
        if event.log != "gmail" or _mail_type(event) not in (2, 6) or (event.parameters.get("event_info.success") or "").lower() == "false" or not raw:
            return None
        return bare_message_id(raw)

    def sender(event: Event) -> Optional[str]:
        return address(event.parameters.get("message_info.source.address") or "") or address(event.parameters.get("message_info.source.from_header_address") or "")

    found: Dict[str, Tuple[str, str, Optional[datetime]]] = {}
    unnamed: Set[str] = set()
    for event in events:
        message = receipt(event)
        if message is None or event.time is None or event.time < start or event.time > attacked_at or person(event.who) != account:
            continue
        by = sender(event)
        if by is None:
            unnamed.add(message)
            continue
        if by.rsplit("@", 1)[-1] in context.domains:
            continue
        seen = found.get(message)
        if seen and (seen[2] or _DISTANT_FUTURE) <= event.time:
            continue
        found[message] = (by, event.parameters.get("message_info.subject") or "", event.time)
    listed = sorted(found.items(), key=lambda kv: (kv[1][2] or _DISTANT_PAST, kv[0]), reverse=True)[:WAY_IN_SHOWN]
    picked = bare_message_id(context.phish) if context.phish else None
    wanted = {m for m, _ in listed} | ({picked} if picked else set())
    received: Dict[str, List[Tuple[str, Optional[datetime]]]] = {}
    about: Dict[str, Tuple[str, str]] = {}
    for event in events:
        message = receipt(event)
        if message is None or message not in wanted:
            continue
        mailbox = person(event.who)
        if mailbox is None:
            continue
        received.setdefault(message, []).append((mailbox, event.time))
        by = sender(event)
        if message not in about and by:
            about[message] = (by, event.parameters.get("message_info.subject") or "")

    def received_by(message: str) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(w for w, _ in sorted(received.get(message, ()), key=lambda p: p[1] or _DISTANT_FUTURE)))

    candidates = tuple(WayInEmail(m, f[0], f[1], f[2], received_by(m)) for m, f in listed)
    chosen = None
    if picked:
        chosen = next((c for c in candidates if c.message_id == picked), None)
        if chosen is None and picked in received:
            times = [t for _, t in received[picked] if t]
            chosen = WayInEmail(picked, about.get(picked, ("", ""))[0], about.get(picked, ("", ""))[1], min(times) if times else None, received_by(picked))
    return WayIn(account, attacked_at, candidates, len(unnamed - set(found)), chosen, None if chosen else picked)


# --- the rules --------------------------------------------------------------------------------------------------------------


def read(events: Sequence[Event], context: Optional[Context] = None) -> Reading:
    """The findings in ``events``."""
    context = context or Context()
    found: List[_Pending] = []
    # Each account's addresses it signed in from without Google's mark: a re-sign-in from one is the person's.
    ordinary = {key(e.who, e.ip) for e in events if e.log == "login" and e.event == "login_success" and e.ip and not is_marked(e)}
    passkeys_added: Dict[str, datetime] = {}        # each account's first passkey added in these logs, from any address
    for e in events:
        if e.log == "login" and e.event == "passkey_enrolled" and e.time:
            low = e.who.lower()
            passkeys_added[low] = min(e.time, passkeys_added.get(low, e.time))
    suspects: List[str] = []
    marked_by: Dict[str, List[str]] = {}
    for e in events:
        if not e.ip:
            continue
        marks = marks_attacker(e, ordinary, context.theirs, passkeys_added)
        if not marks and e.ip != context.pulled_by:
            continue
        if e.ip not in suspects:
            suspects.append(e.ip)
        if marks and is_marked(e) and is_account(e.who):    # only an account's own sign-in Google marked can be the person's
            marked_by.setdefault(e.ip, []).append(e.who)
    if context.pulled_by and context.pulled_by not in suspects:
        suspects.append(context.pulled_by)

    # Each suspect address and the accounts it touched; one many accounts signed in from (not the operator's) is set aside as likely shared: securing everyone behind an office's address
    # would lock the office out.
    acting: Set[str] = set()
    suspected = set(suspects)
    from_suspect: Dict[str, List[Event]] = {}
    for e in events:
        if e.ip in suspected:
            from_suspect.setdefault(e.ip, []).append(e)
    for ip in suspects:
        here = from_suspect.get(ip, [])
        accounts = unique(e.who for e in here if is_account(e.who))
        signed_in = unique(e.who for e in here if e.log == "login" and e.event == "login_success" and is_account(e.who))
        if ip != context.pulled_by and len(signed_in) > SHARED:
            found.append(_Pending("sharedAddress", f"{len(signed_in)} accounts signed in from {ip}, where Google saw a suspicious sign-in: it may be shared (an office or a VPN). Check it before "
                                  "securing anyone.", signed_in, [ip], len(here), [], todo=[("checkShared", f"Check whether {ip} is your office or a VPN before securing anyone behind it.", [])]))
            continue
        acting.add(ip)
        why = f"You pulled the logs by {ip}" if ip == context.pulled_by else f"Google called a sign-in from {ip} suspicious"
        found.append(_Pending("suspectAddress", why + ("." if not accounts else f": it touched {list_words(accounts)}."), accounts, [ip], len(here), [],
                              markable=[] if ip == context.pulled_by else unique(marked_by.get(ip, []))))

    # A sign-in (to Google, or to another app through it), an app grant or an account change from a suspect address: the attacker had that account.
    entered = [e for e in events if e.ip in acting and is_account(e.who) and ((e.log in ("login", "saml") and e.event == "login_success") or (e.log == "token" and e.event == "authorize")
                                                                                or e.log == "useraccounts")]
    for account, theirs in grouped(entered):
        what = unique({"login": "signed in to Google", "saml": "reached another app through Google (SAML)", "token": "granted an app access"}.get(e.log, "changed account settings") for e in theirs)
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("signedInFromSuspect", f"{account} {list_words(what)} from {list_words(ips)}.", [account], ips, len(theirs), [secure_draft(account)]))
    about = _Accounts(events)

    # A device registered to an account in the minutes around its sign-in from a suspect address: the attacker's own computer joined to the account as it signed in. The Devices log has no
    # address, so only the account and the time tie the two. The sign-in nearest the registration, from any address, decides: a person's own new computer beside their own sign-in isn't the
    # attacker's, however near a suspect one; an even tie says nothing.
    sign_ins: Dict[str, List[Event]] = {}
    for e in events:
        if e.log in ("login", "saml") and e.event == "login_success" and e.time and is_account(e.who):
            sign_ins.setdefault(e.who.lower(), []).append(e)
    joined: List[Tuple[Event, Event, float]] = []
    for device in events:
        if device.log != "mobile" or device.event != "DEVICE_REGISTER_UNREGISTER_EVENT" or device.parameters.get("ACCOUNT_STATE") == "UNREGISTERED" or not is_account(device.who) or not device.time:
            continue
        near = [(s, (device.time - (s.time or device.time)).total_seconds()) for s in sign_ins.get(device.who.lower(), ())]
        near = [(s, gap) for s, gap in near if _DEVICE_BEFORE <= gap <= _DEVICE_AFTER]
        if not near:
            continue
        nearest, gap = min(near, key=lambda p: abs(p[1]))
        if nearest.ip not in acting or any(abs(g) == abs(gap) and s.ip not in acting for s, g in near):
            continue
        joined.append((device, nearest, abs(gap)))
    joined_devices = [d for d, _, _ in joined]
    for account, theirs in grouped(joined_devices):
        tied = [j for j in joined if j[0].who.lower() == account.lower()]
        ips = unique(j[1].ip for j in tied)
        gap = max(j[2] for j in tied)
        when = "within a minute of" if gap <= 60 else f"within {math.ceil(gap / 60)} minutes of"
        devices = list_words(unique(device_words(d) for d in theirs))
        left: List[Event] = []
        for device in theirs:   # still there unless the latest word on the device says it left. Registered or approved again is there again, from any address; gone never counts from a suspect's
            ids = {v for v in (device.parameters.get("DEVICE_ID"), device.parameters.get("RESOURCE_ID")) if v}
            if not ids or not device.time:
                left.append(device)
                continue
            says: List[Tuple[Event, bool]] = []
            for event in about[account]:
                if _past_time(event) <= device.time:
                    continue
                word = device_word(event, ids, account)
                if word is not None and (word or event.ip not in acting):
                    says.append((event, word))
            if not says or max(says, key=lambda w: _past_time(w[0]))[1]:
                left.append(device)
        found.append(_Pending("deviceAtSuspectSignIn", f"{devices[:1].upper()}{devices[1:]} joined {account}'s account {when} its sign-in from {list_words(ips)}: likely the attacker's own. Check Account "
                              "lists it, to block or take the account off.", [account], ips, len(theirs), [secure_draft(account)],
                              todo=[("deleteDevice", f"Block or take {account} off {device_words(d)} that joined at the attacker's sign-in: Do It Here, or the Admin console's Devices.",
                                     [Draft("undo", account=account, pick="computer", resource_id=d.parameters.get("RESOURCE_ID") or "", registered_at=d.time)]) for d in left]))

    # A failed sign-in from a suspect address: someone had that account's password, likely from the fake page.
    inside = {e.who.lower() for e in entered}
    for account, theirs in grouped(e for e in events if e.ip in acting and is_account(e.who) and e.log == "login" and e.event == "login_failure"):
        if account.lower() not in inside:
            ips = unique(e.ip for e in theirs)
            found.append(_Pending("failedFromSuspect", f"Someone tried {account}'s password from {list_words(ips)}: they may have typed it into the fake page.", [account], ips, len(theirs), [secure_draft(account)]))

    # Every account the attacker touched, or whose password Chrome saw typed on a phishing site. Each one's status and timeline, and its lock if it's still to do. Not a Gmail row's account: whose
    # mailbox that is isn't certain from the row alone.
    touched = sorted((e for e in events if e.ip in acting and is_account(e.who) and e.log in ("login", "saml", "token", "useraccounts")), key=_sort_time)
    phished_at: Dict[str, datetime] = {}
    for e in events:
        if e.log == "chrome" and e.event == "PASSWORD_REUSE" and e.parameters.get("EVENT_REASON") == "PASSWORD_REUSED_PHISHING_URL" and e.time:
            trigger = e.parameters.get("TRIGGER_USER") or ""
            account = trigger if is_account(trigger) else e.who
            if is_account(account):
                phished_at[account.lower()] = min(e.time, phished_at.get(account.lower(), e.time))
    accounts = unique([*(e.who for e in touched), *sorted(phished_at)])
    own = [about[a] for a in accounts]

    def all_statuses(held: Mapping[str, Tuple[datetime, datetime]]) -> List[Status]:
        return [_status(a, o, acting, context, joined_devices, phished_at.get(a.lower()), held) for a, o in zip(accounts, own, strict=True)]

    # An account the attacker got into, from then until its own lock: what it did as an admin meanwhile is no lock. Read once to know those, then again without them.
    held: Dict[str, Tuple[datetime, datetime]] = {}
    for status in all_statuses({}):
        if status.attacker_got_in and status.attacked_at:
            held[status.account.lower()] = (status.attacked_at, max(status.attacked_at, status.locked_at or _DISTANT_FUTURE))
    statuses = all_statuses(held)
    status_tasks: List[Tuple[str, str, Sequence[str], Sequence[Draft], Optional[Shown]]] = []
    for status, mine in zip(statuses, own, strict=True):
        account = status.account
        theirs_shown = Shown((account,), tuple(unique(e.ip for e in mine if e.who.lower() == account.lower() and e.ip in suspected)))
        if status.state == "stillIn":
            status_tasks.append(("secureAgain", f"Secure {account} again: the attacker signed in from {status.back_in_from} after the lock. Look for a passkey or another way back.", [account], [secure_draft(account)], theirs_shown))
        elif status.state == "partlyLocked":
            status_tasks.append(("finishLock", f"Sign {account} out of every session: the password was reset, but no sign-out was seen." if status.reset
                                 else f"Reset {account}'s password: they were signed out, but the password the attacker may have wasn't changed.", [account], [secure_draft(account)], theirs_shown))
        elif status.state == "notLocked":
            tail = (f", and no lock could be seen ({status.unseen})." if status.unseen else
                    ", and no lock is seen after it; one came earlier, so pull from before the attack began to tell." if status.earlier_lock_at else ", and no lock has been seen since.")
            status_tasks.append(("secure", f"Secure {account}: " + ("the attacker got in" if status.attacker_got_in else "the attacker's address tried their password") + tail, [account], [secure_draft(account)], theirs_shown))
        if status.new_addresses:
            status_tasks.append(("confirmAddress", f"Ask {account} whether {list_words(status.new_addresses)} {'is' if len(status.new_addresses) == 1 else 'are'} theirs: a sign-in after the lock, from an address these logs didn't show "
                                 "before the attack.", [account], [], Shown((account,), status.new_addresses)))
        # The account's contacts printed between the attacker's first event and the lock: who the phish goes to next. The Contacts log names no address, so the time ties it to the attack.
        printed = [e for e in mine if e.log == "contacts" and e.event == "print_contacts" and e.who.lower() == account.lower() and e.time and status.attacked_at and e.time >= status.attacked_at
                   and (status.state == "stillIn" or status.locked_at is None or e.time <= status.locked_at)]
        if printed:
            counts = [int(e.parameters["CONTACTS_COUNT"]) for e in printed if (e.parameters.get("CONTACTS_COUNT") or "").isdigit()]
            found.append(_Pending("contactsTaken", f"{account}'s contacts" + (f" ({max(counts):,})" if counts else "") + " were printed " + ("" if len(printed) == 1 else f"{len(printed)} times ")
                                  + "while the attacker had the account: the people on that list may get the phish next. Warn them not to act on it.", [account], [], len(printed), [],
                                  todo=[("warnContacts", f"Warn the people in {account}'s contacts not to act on email from {account} sent while the attacker had it.", [])]))

    # What a lock leaves in place, done from a suspect address: a way back in or a way to take the account back. A reset and a sign-out undo none of them.
    for account, theirs in grouped(e for e in events if e.ip in acting and is_account(e.who) and e.event in LEFTOVERS and e.log in ("login", "useraccounts")):
        things = unique(LEFTOVERS[e.event] + (f" ({e.parameters['affected_email_address']})" if e.event == "blocked_sender" and e.parameters.get("affected_email_address") else "") for e in theirs)
        recovery = any(e.event.startswith("recovery_") for e in theirs)
        blocked = any(e.event == "blocked_sender" for e in theirs)
        others = any(not e.event.startswith("recovery_") and e.event != "blocked_sender" for e in theirs)

        def still_set(edit: str, removal: str, who: str = account, mine_now: Sequence[Event] = theirs) -> bool:
            """Each recovery detail the attacker changed stays to clear unless an admin, not from a suspect address, removed that same detail after the change."""
            times = [e.time for e in mine_now if e.event == edit and e.time]
            if not times:
                return False
            changed = max(times)
            return not any(e.log == "admin" and e.event == removal and (e.parameters.get("USER_EMAIL") or "").lower() == who.lower() and e.ip not in acting and _past_time(e) > changed for e in about[who])

        cleared = not still_set("recovery_email_edit", "REMOVE_RECOVERY_EMAIL") and not still_set("recovery_phone_edit", "REMOVE_RECOVERY_PHONE")
        todo: List[Tuple[str, str, Sequence[Draft]]] = []
        if others:
            added = unique(LEFTOVERS[e.event] for e in theirs if not e.event.startswith("recovery_") and e.event != "blocked_sender")
            todo.append(("removeAdded", f"Remove what the attacker added to {account}: {list_words(added)}, on the Admin console's page for them (Security), or with them in their Google Account.", []))
        if any(e.event == "recovery_secret_qa_edit" for e in theirs):
            todo.append(("recoveryQuestion", f"Have {account} set a new recovery question in their Google Account: the attacker changed it, and no admin step resets it.", []))
        if any(e.event in ("recovery_email_edit", "recovery_phone_edit") for e in theirs) and not cleared:
            todo.append(("clearRecovery", f"Clear {account}'s recovery email and phone: Check Account's “Clear recovery email & phone” step.", [Draft("undo", account=account, pick="clearRecovery")]))
        if blocked:
            blocked_senders = unique(e.parameters["affected_email_address"] for e in theirs if e.event == "blocked_sender" and e.parameters.get("affected_email_address"))
            todo.append(("unblockSender", f"Have {account} unblock {list_words(blocked_senders) if blocked_senders else 'the blocked sender'} in Gmail: Settings, Filters and Blocked Addresses.", []))
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("leftInPlace", f"From {list_words(ips)}, {account}'s account got {list_words(things)}: a lock doesn't undo {'it' if len(things) == 1 else 'them'}."
                              + (" Remove what isn't theirs on the Admin console's page for them (Security), or with them in their Google Account." if others else "")
                              + (" Check Account shows the recovery details, and its “Clear recovery email & phone” step removes them." if recovery else "")
                              + (" A blocked sender's mail goes to Spam, and blocking the bounce address hides a mass send: unblock it in their Gmail settings (Filters and Blocked Addresses)." if blocked else ""),
                              [account], ips, len(theirs), [secure_draft(account)] if recovery else [], todo=todo))

    # Another app reached through Google from a suspect address: that app keeps its own session, which Google's sign-out doesn't end.
    for account, theirs in grouped(e for e in events if e.log == "saml" and e.event == "login_success" and e.ip in acting and is_account(e.who)):
        apps = unique(e.parameters["application_name"] for e in theirs if e.parameters.get("application_name"))
        named = list_words(apps) if apps else "another app"
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("otherAppSession", f"{account} signed in to {named} through Google from {list_words(ips)}: Google's sign-out doesn't end {'those apps' + chr(39) + ' sessions' if len(apps) > 1 else 'that app' + chr(39) + 's session'}. "
                              "Sign them out there too.", [account], ips, len(theirs), [], todo=[("signOutApp", f"Sign {account} out of {named} too: Google's sign-out doesn't end that session.", [])]))

    # The account's data exported with Google Takeout from a suspect address: it may be in the attacker's hands.
    for account, theirs in grouped(e for e in events if e.log == "takeout" and e.ip in acting and is_account(e.who) and e.event in ("STARTED_USER_TAKEOUT", "DOWNLOADED_USER_TAKEOUT", "SCHEDULED_USER_TAKEOUT")):
        products = unique(e.parameters["PRODUCTS_REQUESTED"] for e in theirs if e.parameters.get("PRODUCTS_REQUESTED"))
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("exportedFromSuspect", f"{account}'s data was exported with Google Takeout from {list_words(ips)}" + (f" ({list_words(products)})" if products else "") + ": treat it as in the attacker's hands.",
                              [account], ips, len(theirs), [secure_draft(account)], todo=[("treatAsTaken", f"Treat {account}'s exported data as the attacker's, and decide who must be told.", [])]))

    # An app acting for an account from a suspect address: the attacker's app, or theirs through the person's grant.
    for account, theirs in grouped(e for e in events if e.ip in acting and e.app and is_account(e.who)):
        apps, logs, ips = unique(e.app for e in theirs), unique(log_name(e.log) for e in theirs), unique(e.ip for e in theirs)
        found.append(_Pending("appFromSuspect", f"{list_words(apps)} acted for {account} from {list_words(ips)} ({list_words(logs)}).", [account], ips, len(theirs), [secure_draft(account)],
                              todo=[("look", f"See what {list_words(apps)} did for {account} from the attacker's address.", [])], shows=Shown((account,), tuple(ips), apps=tuple(apps))))

    # Anything else a suspect address did as an account: Gemini asked to search a mailbox, Drive, Contacts, Calendar… Not the logs other rules read, nor access evaluation, whose token checks come with every sign-in.
    covered_logs = {"login", "token", "saml", "useraccounts", "gmail", "chat", "takeout", "chrome", "accessevaluation"}
    for account, theirs in grouped(e for e in events if e.ip in acting and not e.app and is_account(e.who) and e.log not in covered_logs):
        logs, ips = unique(log_name(e.log) for e in theirs), unique(e.ip for e in theirs)
        found.append(_Pending("actedFromSuspect", f"{account} used {list_words(logs)} from {list_words(ips)}: see what was searched, opened or changed.", [account], ips, len(theirs), [secure_draft(account)],
                              todo=[("look", f"See what was searched, opened or changed in {account}'s {list_words(logs)} from the attacker's address.", [])], shows=Shown((account,), tuple(ips), logs=tuple(unique(e.log for e in theirs)))))

    # AI agents acting for people: said, by agent, and called out when the account is one the attacker had.
    agents: Dict[str, List[Event]] = {}
    for e in events:
        if e.agent and is_account(e.who):
            agents.setdefault(e.agent, []).append(e)
    for agent, theirs in agents.items():
        acted_for = unique(e.who for e in theirs)
        attacked = [a for a in acted_for if a.lower() in inside]
        found.append(_Pending("agentActed", f"{agent[:1].upper()}{agent[1:]} acted for {list_words(acted_for)} ({len(theirs)} event{'' if len(theirs) == 1 else 's'}: {list_words(unique(log_name(e.log) for e in theirs))})"
                              + (f", including {list_words(attacked)}, whom the attacker had: check what it did." if attacked else "."), acted_for, unique(e.ip for e in theirs if e.ip), len(theirs), [],
                              todo=[("look", f"See what {agent} did for {list_words(attacked)}.", [])] if attacked else [], shows=Shown(tuple(attacked), agent=agent)))

    # Chat messages from a suspect address: phishing can go out in Chat too.
    for account, theirs in grouped(e for e in events if e.log == "chat" and e.event == "message_posted" and e.ip in acting and is_account(e.who)):
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("chatFromSuspect", f"{account} posted {len(theirs)} Chat message{'' if len(theirs) == 1 else 's'} from {list_words(ips)}: ask who got them not to open their links.", [account], ips, len(theirs), [],
                              todo=[("warnChat", f"Ask who got {account}'s Chat messages not to open their links.", [])]))

    # Chrome's own warnings, by the account whose password it was (the trigger's), else the profile's.
    def chrome(name: str, keep: Callable[[Event], bool] = lambda _: True) -> List[Tuple[str, List[Event]]]:
        out = []
        for e in events:
            if e.log == "chrome" and e.event == name and keep(e):
                trigger = e.parameters.get("TRIGGER_USER") or ""
                out.append(replace(e, who=trigger) if is_account(trigger) else e)
        return grouped(e for e in out if is_account(e.who))

    def sites(theirs: Sequence[Event]) -> str:
        urls = unique(e.parameters["URL"] for e in theirs if e.parameters.get("URL"))
        return list_words(urls) if urls else "a site"

    for account, theirs in chrome("PASSWORD_REUSE", lambda e: e.parameters.get("EVENT_REASON") == "PASSWORD_REUSED_PHISHING_URL"):
        found.append(_Pending("passwordOnPhishingSite", f"{account} typed their password on {sites(theirs)}, a phishing site (Chrome).", [account], [], len(theirs), [secure_draft(account)]))
    for account, theirs in chrome("PASSWORD_REUSE", lambda e: e.parameters.get("EVENT_REASON") != "PASSWORD_REUSED_PHISHING_URL"):
        found.append(_Pending("passwordOnOtherSite", f"{account} used their password on {sites(theirs)} (Chrome): if that site is breached, so is this account.", [account], [], len(theirs), [],
                              todo=[("checkPassword", f"Have {account} change that password wherever else they use it.", [])]))
    for account, theirs in chrome("PASSWORD_BREACH"):
        found.append(_Pending("breachedPassword", f"Chrome found a password of {account}'s in a data breach, on {sites(theirs)}.", [account], [], len(theirs), [], todo=[("checkPassword", f"Have {account} change the password Chrome found in a breach.", [])]))
    for account, theirs in chrome("UNSAFE_SITE_VISIT", lambda e: e.parameters.get("EVENT_RESULT") == "BYPASSED"):
        found.append(_Pending("pastPhishingWarning", f"{account} went past Chrome's warning to {sites(theirs)}: check whether they typed a password there.", [account], [], len(theirs), [],
                              todo=[("checkPassword", f"Ask {account} whether they typed a password at {sites(theirs)}.", [])]))

    # Mail sent from a suspect address: the message to pull from everyone. Only a send: what the attacker read, drafted or trashed there is the person's own mail. One Phishing has trashed from every mailbox, or
    # that its Find showed nobody here has, is done. The latest of the two decides: a run after a check found copies again. A run that left any is still to do, saying what it left.
    messages: Dict[str, List[Event]] = {}
    for e in events:
        if e.log == "gmail" and e.ip in acting and is_sent(e):
            raw = e.parameters.get("message_info.rfc2822_message_id")
            if raw:
                messages.setdefault(raw, []).append(e)
    runs: Dict[str, Pulled] = {}
    for run in context.pulled:
        at = bare_message_id(run.message_id)
        if at not in runs or runs[at].at < run.at:
            runs[at] = run
    checks: Dict[str, NobodyHas] = {}
    for check in context.nobody_has:
        at = bare_message_id(check.message_id)
        if at not in checks or checks[at].at < check.at:
            checks[at] = check
    # A delivery or a resend the log shows after the last look: what that look said is out of date.
    newest_mail: Dict[str, datetime] = {}
    for e in events:
        raw = e.parameters.get("message_info.rfc2822_message_id") if e.log == "gmail" else None
        if raw and e.time and _mail_type(e) in (1, 2, 6, 35):
            bare = bare_message_id(raw)
            if bare not in newest_mail or newest_mail[bare] < e.time:
                newest_mail[bare] = e.time
    pulled_done: Set[str] = set()
    done_tasks: List[Tuple[str, str, Sequence[str], Done]] = []
    for message, theirs in messages.items():
        subject = next((e.parameters["message_info.subject"] for e in theirs if e.parameters.get("message_info.subject")), "")
        named = f"“{subject or '(no subject)'}” ({message})"
        sent_by = unique(e.who for e in theirs if is_account(e.who))
        last_run, last_check = runs.get(bare_message_id(message)), checks.get(bare_message_id(message))
        newest = newest_mail.get(bare_message_id(message))
        looked = max((a for a in (last_run.at if last_run else None, last_check.at if last_check else None) if a), default=None)
        stale = looked is not None and newest is not None and newest > looked
        check_is_latest = last_check is not None and (last_run is None or last_run.at <= last_check.at)
        todo = []
        if stale:
            todo = [("pullEmail", f"Pull {named} from every mailbox: it went out from the attacker's address. The log shows it after Phishing last looked, so find it again.", [pull_draft(message, subject)])]
        elif last_check and check_is_latest and not last_check.gone:
            todo = [("pullEmail", f"Pull {named} from every mailbox: it went out from the attacker's address. Phishing's Find last showed a mailbox with it, or couldn't search them all.", [pull_draft(message, subject)])]
        elif last_check and check_is_latest:
            pulled_done.add(message)
            done_tasks.append(("pullEmail", f"Nobody here has {named}: Phishing's Find searched every mailbox and its Spam.", sent_by, Done("nobodyHasIt", last_check.at, last_check.by)))
        elif last_run and last_run.is_done:
            pulled_done.add(message)
            done_tasks.append(("pullEmail", f"Pulled {named}: trashed from " + ("the one mailbox" if last_run.mailboxes == 1 else f"the {last_run.mailboxes} mailboxes") + " that had it, every mailbox searched first.", sent_by,
                               Done("trashed", last_run.at, last_run.by)))
        else:
            todo = [("pullEmail", f"Pull {named} from every mailbox: it went out from the attacker's address." + (f" {last_run.left}" if last_run else ""), [pull_draft(message, subject)])]
        ips = unique(e.ip for e in theirs)
        found.append(_Pending("mailFromSuspect", f"{named} was sent from {list_words(ips)}.", sent_by, ips, len(theirs), [pull_draft(message, subject)], todo=todo))

    # Who each reached, and whether the warnings picked reached them all: done only on the log's own word for each of them, with the directory read and nothing unreadable; what the logs read can't show is to pull again.
    reaches = _reaches(list(messages), events, acting, context)
    warn_tasks: List[Tuple[str, str, Sequence[str], Sequence[Draft], Optional[Shown], Sequence[str], str]] = []
    unread: List[str] = []
    senders: List[str] = []
    listed: Set[str] = set()
    for reach in reaches:
        named = f"“{reach.subject or '(no subject)'}” ({reach.message_id})"
        shows = Shown(reach.senders, logs=("gmail",)) if reach.senders else None
        for sender in reach.senders:
            if f"sender {sender}" not in listed:
                listed.add(f"sender {sender}")
                senders.append(sender)
        for person_unread in reach.unread:
            if f"unread {person_unread}" not in listed:
                listed.add(f"unread {person_unread}")
                unread.append(person_unread)
        for warning in reach.warnings:
            if warning.problem:
                warn_tasks.append(("warnRecipients", f"The warning picked for {named}, {warning.message_id}, doesn't count: {warning.problem}.", reach.senders, [], shows, [], ""))
        if reach.unnamed > 0:
            warn_tasks.append(("warnRecipients", f"The log shows {'a send' if reach.unnamed == 1 else f'{reach.unnamed} sends'} of {named} naming no recipient: who it went to isn't in the logs read. Check it in the Admin console's Gmail log events, then warn them.",
                               reach.senders, [], shows, [], ""))
        if reach.unreadable > 0:
            warn_tasks.append(("warnRecipients", f"{reach.unreadable} of the recipients the log names for {named} don't read as an address: check them in the Admin console's Gmail log events, then warn them.", reach.senders, [], None, [], ""))
        if not reach.reached:
            continue
        # Done once everyone it got to is shown warned, by the log's own word for each, with the directory read and nothing unreadable. A bounce, a quarantine or a rejection didn't get through.
        warned = {r.address for r in reach.reached if r.warned is True}
        by = [w for w in reach.warnings if w.problem is None and w.warned & warned]
        got = got_it([reach])
        timed = [w for w in by if w.at]
        if all(not g.to_warn for g in got) and reach.unreadable == 0 and reach.unnamed == 0 and context.people is not None and timed:
            latest = max(timed, key=lambda w: w.at or _DISTANT_PAST)
            groups = sum(1 for g in got if g.inside and not g.mailbox)      # counted on the warning's send, as outside is: who got one through a group is in its members' own logs
            done_tasks.append(("warnRecipients", f"Warned everyone {named} got to: {len(got)} {'person' if len(got) == 1 else 'people'}"
                               + ("" if groups == 0 else f" ({'one' if groups == 1 else groups} of them a group or an address no account has, counted on the warning's send)")
                               + f", by {list_words([w.sender + chr(39) + 's “' + w.subject + '”' for w in by])}.", reach.senders, Done("warned", latest.at or _DISTANT_PAST, latest.sender)))
    if reaches and context.people is None:
        warn_tasks.append(("readDirectory", "Read the directory (open Users) to finish Who it reached: until then a group can't be told from a person, nor whose Gmail log to pull.", senders, [], Shown(tuple(senders), logs=("gmail",)) if senders else None, [], ""))
    to_warn = [g for g in got_it(reaches) if g.to_warn]
    if to_warn:
        outside = sum(1 for g in to_warn if not g.inside)
        warn_tasks.append(("warnRecipients", f"Warn the {len(to_warn)} {'person' if len(to_warn) == 1 else 'people'} the attacker's {'email' if len(reaches) == 1 else 'emails'} got to that no warning picked is shown reaching"
                           + (f", {outside} of them outside" if outside else "") + ": Everyone it got to lists each once, to copy or export. Pick the warning sent to them under each email's Who it reached, or send one.",
                           senders, [], Shown(tuple(senders), logs=("gmail",)) if senders else None, [], fingerprint(",".join(sorted(g.address for g in to_warn)))))
    if unread:
        shown = unread[:20]
        warn_tasks.append(("pullAgain", f"Pull the Gmail log again for {list_words(shown)}" + (f" (the first 20 of {len(unread)})" if len(unread) > len(shown) else "")
                           + ": whether a warning reached them is in their own Gmail log, which the pull doesn't hold past the attacker's email.", shown, [], None, ["gmail"], ""))

    ranked = [p for _, p in sorted(enumerate(found), key=lambda pair: (FINDING_KINDS.index(pair[1].kind), pair[0]))]
    # A finding about an account already locked drafts no lock again, nor one about an email already pulled: what's left is in the to-do list.
    locked = {s.account.lower() for s in statuses if s.state == "locked"}

    def still_to_draft(draft: Draft) -> bool:
        if draft.kind == "secureAccount":
            return draft.account.lower() not in locked
        return not (draft.kind == "pullEmail" and draft.message_id in pulled_done)

    # Logs read for every account that stop short of the attack: pull them again for just these accounts.
    coverage: List[Tuple[str, str, Sequence[str], Sequence[Draft], Optional[Shown]]] = []
    pulls_again: List[str] = []
    attack_times = [s.attacked_at for s in statuses if s.attacked_at]
    if attack_times:
        first_attack = min(attack_times)
        accounts = [s.account for s in statuses]
        # A read for just some accounts reaches further back for them alone: the log stays short for the rest, and the next pull is for those.
        short = sorted(log for log, oldest in context.capped.items() if oldest > first_attack and any(a.lower() not in context.covered.get(log, ()) for a in accounts))
        everyone = [a for a in accounts if any(a.lower() not in context.covered.get(log, ()) for log in short)]
        if short:
            coverage.append(("pullAgain", f"Pull the {list_words([log_name(s) for s in short])} {'log' if len(short) == 1 else 'logs'} again for just " + (list_words(everyone) if len(everyone) <= 20 else "the accounts above, 20 at a time")
                             + ": read for every account, " + ("it stops at its newest events and doesn't" if len(short) == 1 else "they stop at their newest events and don't") + " reach back to the attack.", everyone, [], None))
            pulls_again = short
    # Narrow first: each attacker address read for every account, Google filtering on it before it reads, so the read stays small at any size. Never a shared one, nor one read so already.
    sweeps: List[Tuple[str, str, Sequence[str], Sequence[Draft], Optional[Shown]]] = []
    sweep_for: Dict[str, str] = {}
    for ip in suspects:
        if ip in acting and ip not in context.swept:
            words = f"Read every account's logs from {ip}, over the window shown: whom else it signed in to, sent from or changed. Google filters on the address before it reads, so this stays quick however large the organization."
            sweeps.append(("sweep", words, [], [], None))
            sweep_for[words] = ip
    todos: List[Tuple[str, str, Sequence[str], Sequence[Draft], Optional[Shown], Sequence[str], str]] = [
        *((*t, [], "") for t in status_tasks), *((*t, pulls_again, "") for t in coverage), *((*t, [], "") for t in sweeps)]
    for item in ranked:     # each item's basis beyond its words: for one a finding points to, how many events it stands on and from where
        basis = fingerprint(f"{item.events}|" + ",".join(item.addresses))
        for kind, words, drafts in item.todo:
            todos.append((kind, words, item.accounts, drafts, item.shows or Shown(tuple(item.accounts), tuple(item.addresses)), [], basis))
    todos += warn_tasks
    said: Set[str] = set()
    distinct = []
    for todo_item in todos:     # most urgent first, each once
        at = todo_item[1] + "|" + "".join(d.title for d in todo_item[3])
        if at not in said:
            said.add(at)
            distinct.append(todo_item)
    ordered = [t for _, t in sorted(enumerate(distinct), key=lambda pair: (TASK_KINDS.index(pair[1][0]), pair[0]))]

    return Reading(
        findings=tuple(Finding(f"F{i + 1}", p.kind, p.words, tuple(p.accounts), tuple(p.addresses), p.events, tuple(d for d in p.drafts if still_to_draft(d)), tuple(p.markable)) for i, p in enumerate(ranked)),
        statuses=tuple(statuses),
        to_do=tuple(Task(f"T{i + 1}", t[0], t[1], tuple(t[2]), tuple(t[3]), t[4] if t[4] and t[4].accounts else None, tuple(t[5]), None, sweep_for.get(t[1]),
                         tuple(t[4].addresses) if t[0] == "confirmAddress" and t[4] else (), t[6]) for i, t in enumerate(ordered)),
        done=tuple(Task(f"D{i + 1}", t[0], t[1], tuple(t[2]), done=t[3]) for i, t in enumerate(done_tasks)),
        events=len(events), reaches=reaches, way_in=_way_in(statuses, events, context))
