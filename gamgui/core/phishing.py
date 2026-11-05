"""Take an email out of every mailbox that has it, by its Message-ID: reading who has a copy.

Find is GAM's own dry run of ``trash messages`` over every active user, which is a read, plus a read of the copies Gmail filed to
Spam, which the dry run can't see. Trash then runs one guarded step per mailbox, so its preview names each one and Stop works
between them. Each email's result comes from GAM's lines, never its exit code, since an ``all users`` run's code is the last
user's: a list that didn't come back whole is refused rather than shown as whole. Pure: the reads are on ``GAMConnector``.
Ported from SwiftGamGui's ``MessageCopies``, ``SpamCopies`` and ``MessageRecipients``; its runbook is ``phishing.md``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .gam.commands import MessageId, id_list
from .gam.errors import ACCOUNT_WIDE_KINDS, GAMError, is_progress_line
from .onboarding import looks_like_email

MOST_AT_ONCE = 100           # each email is two domain-wide reads
SEARCHED_AT_ONCE = 2
# The codes GAM's per-user printers set here, and 0 when none set one. Any other is the command failing.
PER_USER_EXIT_CODES = frozenset({0, 50, 51, 60, 73})
_GMAIL_OFF = "Gmail Service/App not enabled"
_COUNTED = re.compile(r" \((\d{1,9})/(\d{1,9})\)$")
_USER_LINE = re.compile(r"^User: ([^\s,]+), ")
_USERS_LISTED = re.compile(r"Got (\d{1,9}) Users?\b.*")
_NOT_TRASHED = re.compile(r"Messages?: (\d{1,9}), Not Trashed: (.*)", re.S)
_OVER_CAP = re.compile(r"Count \d{1,9} exceeds maximum to Trash \d{1,9}")
_TRASH_FAILED = re.compile(r"(?:User:, |User )?Trash Failed: (.+)", re.S)
_GETTING = re.compile(r"Getting all Messages that match query \(.*\) for ([^\s,]+)( \(\d{1,9}/\d{1,9}\))?")
_ASK_FOR_ID = "Paste the email's Message-ID, from its headers (in Gmail, More › Show original)."
# GAM overwrites its page counts on one line (writeGotMessage, `show_gettings_got_nl` off by default), so a domain of more than
# one page of users is "\rGot 500 Users...\rGot 742 Users...\r\n": a line ends at a CR too, and the last count is the count.
_LINE_BREAK = re.compile(r"[\r\n]+")


class Incomplete(Exception):
    """The answer didn't come back whole: some users weren't reported."""


def parse_message_ids(pasted: str) -> List[MessageId]:
    """One Message-ID, or several, one a line or between commas (no Message-ID holds one). ValueError, in words."""
    lines: List[str] = []
    for piece in re.split(r"[\n\r,]+", pasted or ""):
        line = piece.strip()
        if not line:
            continue
        if lines and lines[-1].lower() == "message-id:":
            lines[-1] += " " + line
        else:
            lines.append(line)
    if len(lines) <= 1:
        return [MessageId.parse(lines[0] if lines else pasted or "")]
    seen: Set[str] = set()
    found: List[MessageId] = []
    for i, line in enumerate(lines, 1):
        try:
            message = MessageId.parse(line)
        except ValueError as exc:
            raise ValueError(f"Line {i}: {exc}") from exc
        if message.id not in seen:
            seen.add(message.id)
            found.append(message)
        if len(found) > MOST_AT_ONCE:
            raise ValueError(f"At most {MOST_AT_ONCE} Message-IDs at once: find the rest after these.")
    return found


def _command_failed(rc: int, other: Sequence[str], argv: Sequence[str]) -> Optional[GAMError]:
    """An account-wide failure among the lines that aren't one user's. One user's invalid_grant is theirs."""
    if not other:
        return None
    failure = GAMError.from_run(rc, "\n".join(other), list(argv))
    return failure if failure.kinds & ACCOUNT_WIDE_KINDS else None


@dataclass(frozen=True)
class Mailbox:
    email: str
    copies: int = 0
    gmail_off: bool = False
    failed: str = ""        # GAM couldn't search it: its words

    @property
    def searched(self) -> bool:
        return not self.gmail_off and not self.failed

    @property
    def why_unsearched(self) -> str:
        return "Gmail is off" if self.gmail_off else self.failed


def _mailbox_line(line: str) -> Optional[Tuple[Mailbox, Optional[Tuple[int, int]]]]:
    """One user's line and GAM's (i/n), or None for a line that isn't one user's. A user's line in words not known here is
    that mailbox failed with them: not read, so never read as clear."""
    text, count = line, None
    m = _COUNTED.search(text)
    if m:
        count = (int(m.group(1)), int(m.group(2)))
        text = text[:m.start()]
    head = _USER_LINE.match(text)
    if not head:
        return None
    email, rest = head.group(1), text[head.end():]
    known = _NOT_TRASHED.fullmatch(rest)
    if known:
        found, why = int(known.group(1)), known.group(2)
        if why == "No Messages matched" and found == 0:
            return Mailbox(email), count
        if found > 0 and (why == 'Use the "doit" argument to perform action' or _OVER_CAP.fullmatch(why)):
            return Mailbox(email, copies=found), count
        return Mailbox(email, failed=rest), count
    if rest == _GMAIL_OFF:
        return Mailbox(email, gmail_off=True), count
    failed = _TRASH_FAILED.fullmatch(rest)
    return Mailbox(email, failed=failed.group(1) if failed else rest), count


@dataclass(frozen=True)
class MessageCopies:
    """Every active user GAM searched, in its order, from the dry run's lines."""

    mailboxes: Tuple[Mailbox, ...]
    other_lines: Tuple[str, ...] = ()

    @property
    def with_copies(self) -> List[Mailbox]:
        return [m for m in self.mailboxes if m.copies > 0]

    @property
    def unsearched(self) -> List[Mailbox]:
        return [m for m in self.mailboxes if not m.searched]

    @property
    def looks_like_missing_scope(self) -> bool:
        """Every mailbox says Gmail is off: more likely the Gmail scope isn't delegated than Gmail off for everyone."""
        return bool(self.mailboxes) and all(m.gmail_off for m in self.mailboxes)


def read_copies(rc: int, stderr: str, argv: Sequence[str]) -> MessageCopies:
    """A finished dry run. Raises GAMError when the command itself failed, and Incomplete unless every user it listed was
    reported, once: a domain-wide search that reports nobody is never read as "no copies"."""
    if rc not in PER_USER_EXIT_CODES:
        raise GAMError.from_run(rc, stderr, list(argv))
    mailboxes: List[Mailbox] = []
    other: List[str] = []
    numbers: List[int] = []
    total: Optional[int] = None
    listed: Optional[int] = None
    for raw in _LINE_BREAK.split(stderr or ""):
        line = raw.strip()
        if not line:
            continue
        if is_progress_line(line):
            m = _USERS_LISTED.fullmatch(line)
            if m:
                listed = int(m.group(1))
            continue
        read = _mailbox_line(line)
        if read is None:
            other.append(line)
            continue
        mailbox, count = read
        mailboxes.append(mailbox)
        if count:
            numbers.append(count[0])
            if total is not None and total != count[1]:
                raise Incomplete("GAM's user counts disagree, so the list can't be trusted as whole.")
            total = count[1]
    failure = _command_failed(rc, other, argv)
    if failure:
        raise failure
    if not mailboxes:
        raise Incomplete("GAM reported no mailbox at all, so none can be said to be clear.")
    if listed is not None and listed != len(mailboxes):
        raise Incomplete(f"GAM reported {len(mailboxes)} of the {listed} users it listed.")
    if total is not None and (len(numbers) != len(mailboxes) or sorted(numbers) != list(range(1, max(total, 1) + 1))):
        raise Incomplete(f"GAM reported {len(mailboxes)} of the {total} users it searched.")
    if len({m.email.lower() for m in mailboxes}) != len(mailboxes):
        raise Incomplete("GAM reported a user twice, so the list can't be trusted as whole.")
    return MessageCopies(tuple(mailboxes), tuple(other))


@dataclass(frozen=True)
class SpamMailbox:
    email: str
    ids: Tuple[str, ...]

    @property
    def copies(self) -> int:
        return len(self.ids)


@dataclass(frozen=True)
class SpamCopies:
    mailboxes: Tuple[SpamMailbox, ...] = ()
    unsearched: Tuple[Tuple[str, str], ...] = ()    # (address, why)
    searched: Tuple[str, ...] = ()                  # every mailbox searched, a copy in Spam or not
    other_lines: Tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return sum(m.copies for m in self.mailboxes)


def read_spam_copies(rc: int, stderr: str, records: Iterable[Mapping[str, Any]], argv: Sequence[str], message: MessageId) -> SpamCopies:
    """A finished `print messages`. A user is searched when its "Getting all" line came and no failure line did, and every user
    GAM listed must be one or the other. A copy is kept only when its labels can be told apart and say Spam and not Trash, and
    its Message-ID is exactly this one: anything else leaves that mailbox unsearched, never read as clear."""
    if rc not in PER_USER_EXIT_CODES:
        raise GAMError.from_run(rc, stderr, list(argv))
    order: List[str] = []
    names: Dict[str, str] = {}
    searched: Set[str] = set()
    failed: Dict[str, str] = {}
    index_of: Dict[str, int] = {}
    other: List[str] = []
    total: Optional[int] = None
    listed: Optional[int] = None

    def note(email: str, shown: str) -> None:
        if email not in names:
            order.append(email)
            names[email] = shown

    def count(email: str, m: Optional[re.Match]) -> None:
        nonlocal total
        if not m:
            return
        index, of = int(m.group(1)), int(m.group(2))
        if total is not None and total != of:
            raise Incomplete("GAM's user counts disagree, so the list can't be trusted as whole.")
        total = of
        if email in index_of and index_of[email] != index:
            raise Incomplete(f"GAM reported {email} twice, so the list can't be trusted as whole.")
        index_of[email] = index

    for raw in _LINE_BREAK.split(stderr or ""):
        line = raw.strip()
        if not line:
            continue
        getting = _GETTING.fullmatch(line)
        if getting:
            email = getting.group(1).lower()
            if email in searched:
                raise Incomplete(f"GAM searched {email} twice, so the list can't be trusted as whole.")
            note(email, getting.group(1))
            searched.add(email)
            count(email, _COUNTED.search(line))
            continue
        if is_progress_line(line):
            m = _USERS_LISTED.fullmatch(line)
            if m:
                listed = int(m.group(1))
            continue
        head = _USER_LINE.match(line)
        if not head:
            other.append(line)
            continue
        email = head.group(1).lower()
        why = line[head.end():]
        numbered = _COUNTED.search(why)
        if numbered:
            why = why[:numbered.start()]
        note(email, head.group(1))
        failed.setdefault(email, "Gmail is off" if why == _GMAIL_OFF else why)
        if not why.startswith("Message: "):      # one message's numbers the messages, not the users
            count(email, numbered)
    failure = _command_failed(rc, other, argv)
    if failure:
        raise failure
    if not order:
        raise Incomplete("GAM reported no mailbox at all, so none can be said to be clear.")
    if listed is not None and listed != len(order):
        raise Incomplete(f"GAM reported {len(order)} of the {listed} users it listed.")
    if total is not None and (len(index_of) != len(order) or len(index_of) != total or set(index_of.values()) != set(range(1, max(total, 1) + 1))):
        raise Incomplete(f"GAM reported {len(order)} of the {total} users it searched.")

    ids: Dict[str, List[str]] = {}
    wanted = f"<{message.id}>"
    for record in records:
        email, gmail_id = str(record.get("User") or "").lower(), str(record.get("id") or "")
        if email not in searched and email not in failed:
            raise Incomplete(f"GAM printed a copy for {record.get('User')}, a user it didn't report searching.")
        if email in failed:
            continue
        words = str(record.get("Labels") or "").split()
        apart = str(record.get("LabelsCount") or "") == str(len(words))
        if apart and ("TRASH" in words or "SENT" in words):
            continue
        if not apart or "SPAM" not in words or str(record.get("Message-ID") or "").strip() != wanted or id_list([gmail_id]) is None:
            failed[email] = f"Gmail matched a copy ({gmail_id or 'no ID'}) that isn't plainly this email in Spam: look at it by hand."
            continue
        if gmail_id not in ids.setdefault(email, []):
            ids[email].append(gmail_id)
    return SpamCopies(
        mailboxes=tuple(SpamMailbox(names[e], tuple(ids[e])) for e in order if e not in failed and ids.get(e)),
        unsearched=tuple((names[e], failed[e]) for e in order if e in failed),
        searched=tuple(names[e] for e in order if e not in failed),
        other_lines=tuple(other),
    )


@dataclass(frozen=True)
class EmailSearch:
    """One email's Find: its dry run, and beside it its Spam read, whose failure never blocks the rest."""

    message: MessageId
    copies: MessageCopies
    spam: Optional[SpamCopies] = None
    spam_problem: str = ""
    sample: Optional[Tuple[str, "MessageRecipients"]] = None    # (mailbox, its copy): what the ID matched, to check by eye
    sample_problem: str = ""

    @property
    def has_copies(self) -> bool:
        return bool(self.copies.with_copies) or bool(self.spam and self.spam.mailboxes)

    @property
    def first_with_copy(self) -> Optional[str]:
        """The mailbox to read the sample from: in Spam only when that's the only place."""
        if self.copies.with_copies:
            return self.copies.with_copies[0].email
        return self.spam.mailboxes[0].email if self.spam and self.spam.mailboxes else None

    @property
    def gam_also_said(self) -> Tuple[str, ...]:
        """Lines neither read knows, each once. Never guessed at, and never read as nothing."""
        return tuple(dict.fromkeys((*self.copies.other_lines, *(self.spam.other_lines if self.spam else ()))))

    @property
    def searched_everywhere(self) -> bool:
        return not self.copies.unsearched and self.spam is not None and not self.spam.unsearched and not self.gam_also_said

    @property
    def step_count(self) -> int:
        return len(self.copies.with_copies) + (len(self.spam.mailboxes) if self.spam else 0)


@dataclass
class Found:
    """A Find: the emails some mailbox has, those none does, those it's unsure of, and those it couldn't search."""

    searches: List[EmailSearch] = field(default_factory=list)
    problems: List[Tuple[MessageId, str]] = field(default_factory=list)
    run: str = field(default_factory=lambda: secrets.token_hex(8))   # what a preview is bound to: this Find, never another

    @property
    def with_copies(self) -> List[EmailSearch]:
        return [s for s in self.searches if s.has_copies]

    @property
    def nobody_has(self) -> List[EmailSearch]:
        return [s for s in self.searches if not s.has_copies and s.searched_everywhere]

    @property
    def unsure(self) -> List[EmailSearch]:
        """Found nowhere while a mailbox or its Spam went unsearched: unsure, never clear."""
        return [s for s in self.searches if not s.has_copies and not s.searched_everywhere]

    @property
    def step_count(self) -> int:
        return sum(s.step_count for s in self.searches)


@dataclass(frozen=True)
class TrashStep:
    message: MessageId
    email: str
    copies: int
    ids: Tuple[str, ...] = ()     # in Spam, by Gmail's ID; empty for the mailbox's copies by query
    searched_everywhere: bool = True

    @property
    def in_spam(self) -> bool:
        return bool(self.ids)


def trash_steps(found: Found) -> List[TrashStep]:
    """One step per mailbox Find showed copies in, and one more for each with copies in Spam."""
    steps: List[TrashStep] = []
    for search in found.with_copies:
        everywhere = search.searched_everywhere
        steps += [TrashStep(search.message, m.email, m.copies, (), everywhere) for m in search.copies.with_copies]
        if search.spam:
            steps += [TrashStep(search.message, m.email, m.copies, m.ids, everywhere) for m in search.spam.mailboxes]
    return steps


def step_summary(step: TrashStep, several: bool) -> str:
    copies = "the copy" if step.copies == 1 else f"the {step.copies} copies"
    if several:
        copies += f" of <{step.message.id}>"
    return f"Move {copies} in {step.email}'s Spam to Trash." if step.in_spam else f"Move {copies} in {step.email}'s mailbox to its Trash."


COPIES_COLUMNS = ["mailbox", "found", "copies", "in_spam", "outcome"]
ALL_COPIES_COLUMNS = ["message_id", *COPIES_COLUMNS]
RECIPIENTS_COLUMNS = ["address", "where", "header"]


def copies_rows(search: EmailSearch, outcomes: Optional[Mapping[str, List[str]]] = None) -> List[List[str]]:
    """Who has a copy, as the screen lists them: a count in Spam only for a mailbox the Spam read reported searching."""
    outcomes = outcomes or {}
    spam: Dict[str, str] = {}
    if search.spam:
        spam.update({e.lower(): "0" for e in search.spam.searched})
        spam.update({m.email.lower(): str(m.copies) for m in search.spam.mailboxes})
        spam.update({e.lower(): f"not searched: {why}" for e, why in search.spam.unsearched})

    def row(email: str, found: str, copies: str) -> List[str]:
        key = email.lower()
        return [email, found, copies, spam.get(key, "not searched"), "; ".join(outcomes.get(key, []))]

    rows = []
    for m in search.copies.mailboxes:
        if m.copies:
            rows.append(row(m.email, "copies", str(m.copies)))
        elif m.searched:
            rows.append(row(m.email, "none", "0"))
        else:
            rows.append(row(m.email, f"not searched: {m.why_unsearched}", ""))
    listed = {m.email.lower() for m in search.copies.mailboxes}
    if search.spam:     # a mailbox only the Spam read reported: never dropped
        rows += [row(e, "", "") for e in (*[m.email for m in search.spam.mailboxes], *[e for e, _ in search.spam.unsearched])
                 if e.lower() not in listed]
    return rows


def all_copies_rows(found: Found, outcomes: Optional[Mapping[str, Mapping[str, List[str]]]] = None) -> List[List[str]]:
    outcomes = outcomes or {}
    rows = [[s.message.id, *r] for s in found.searches for r in copies_rows(s, outcomes.get(s.message.id))]
    return rows + [[m.id, "", f"not searched: {why}", "", "", ""] for m, why in found.problems]


# --- who it was sent to --------------------------------------------------------------------------------------------------


def addresses_in(header: str) -> Tuple[List[str], List[str]]:
    """The addresses in one To, Cc or Bcc header (RFC 5322 `address-list`), as decoded: each `<addr-spec>`, or a bare one. A
    display name, quoted or not, a comment and a group's name are never taken for the address. A piece naming anything but
    exactly one address is returned as it is, unreadable, never cut down to one: the sender wrote the headers."""
    addresses: List[str] = []
    unreadable: List[str] = []
    raw: List[str] = []
    plain: List[str] = []
    angle: List[str] = []
    angles: List[str] = []
    in_angle = quoted = escaped = False
    comment = 0

    def finish() -> None:
        nonlocal raw, plain, angle, angles, in_angle, quoted, escaped, comment
        piece = "".join(raw).strip()
        if "@" in piece:     # else a group's end, an empty piece, a name split by a comma
            bare = [w.rsplit(":", 1)[-1] for w in "".join(plain).split()]
            named = [a.strip() for a in angles] + [w for w in bare if "@" in w]
            if len({n.lower() for n in named}) == 1 and looks_like_email(named[0]):
                addresses.append(named[0])
            else:
                unreadable.append(piece)
        raw, plain, angle, angles = [], [], [], []
        in_angle = quoted = escaped = False
        comment = 0

    for c in header or "":
        if c in ",;" and not quoted and not comment and not in_angle:
            finish()
            continue
        raw.append(c)
        if escaped:
            escaped = False
            if in_angle:
                angle.append(c)
        elif quoted:
            if c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
        elif comment:
            if c == "\\":
                escaped = True
            elif c == "(":
                comment += 1
            elif c == ")":
                comment -= 1
        elif in_angle:
            if c == ">":
                in_angle = False
                angles.append("".join(angle))
            else:
                angle.append(c)
        elif c == '"':
            quoted = True
        elif c == "(":
            comment = 1
        elif c == "<":
            in_angle = True
            angle = []
        else:
            plain.append(c)
    if in_angle:     # an angle address left open at the end is still one the piece names
        angles.append("".join(angle))
    finish()
    return addresses, unreadable


_HEADER_REPEAT = re.compile(r"(to|cc|bcc)( \d+)?", re.I)


@dataclass(frozen=True)
class MessageRecipients:
    """Who an email was sent to, from the sender's own copy. Only that copy can hold a Bcc header, and only when the message was
    written in Gmail, so this may not be everyone: the screen always says so, and names Google's own record."""

    recipients: Tuple[Tuple[str, str], ...]     # (address, header), each once, To then Cc then Bcc
    unreadable: Tuple[Tuple[str, str], ...]
    subject: str
    sender: str
    headings: Tuple[Tuple[str, str], ...]       # each distinct (From, Subject): more than one means the ID matched different emails
    date: str
    labels: Tuple[str, ...]
    has_bcc: bool

    @property
    def sent_by_them(self) -> bool:
        return "SENT" in self.labels


def read_recipients(records: Sequence[Mapping[str, Any]]) -> Optional[MessageRecipients]:
    """The sender's copy, or None when there's none: deleted and purged, or never in that mailbox."""
    if not records:
        return None
    recipients: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    unreadable: List[Tuple[str, str]] = []
    has_bcc = False
    for kind, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        for record in records:
            for column in sorted(record):
                m = _HEADER_REPEAT.fullmatch(str(column))
                value = record[column]
                if not m or m.group(1).lower() != kind or not isinstance(value, str):
                    continue
                if kind == "bcc":
                    has_bcc = True
                found, odd = addresses_in(value)
                for address in found:
                    if address.lower() not in seen:
                        seen.add(address.lower())
                        recipients.append((address, header))
                unreadable += [(piece, header) for piece in odd]
    labels: List[str] = []
    headings: List[Tuple[str, str]] = []
    for record in records:
        labels += [w for w in str(record.get("Labels") or "").split() if w not in labels]
        heading = (str(record.get("From") or ""), str(record.get("Subject") or ""))
        if heading not in headings:
            headings.append(heading)
    first = records[0]
    return MessageRecipients(tuple(recipients), tuple(unreadable), str(first.get("Subject") or ""), str(first.get("From") or ""),
                             tuple(headings), str(first.get("Date") or ""), tuple(labels), has_bcc)


def is_internal(address: str, domains: Iterable[str]) -> bool:
    domain = address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""
    return domain in {d.lower() for d in domains}


def recipients_rows(recipients: MessageRecipients, domains: Iterable[str]) -> List[List[str]]:
    """One row per address: outside the tenant's domains first, then inside."""
    known = list(domains)
    outside = [[a, "outside", h] for a, h in recipients.recipients if not is_internal(a, known)]
    inside = [[a, "inside", h] for a, h in recipients.recipients if is_internal(a, known)]
    return outside + inside + [[piece, "check by hand", h] for piece, h in recipients.unreadable]
