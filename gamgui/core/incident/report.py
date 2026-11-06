"""The incident report: each number with the list behind it, every step taken beside the record that shows it, and the review, as one document in text, Markdown and PDF. Pure: made from the reading, the audit
log's changes, the record, the evidence and the directory's 2-Step Verification, with no I/O. Only the summary, the notes, the review and what was marked done by hand are typed. Every piece of Markdown is
escaped: a subject an attacker wrote is its words, never a link, an image, HTML, a heading or a list. Ported from SwiftGamGui's IncidentReport. Runbook: docs/domains/incident.md."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Mapping, Optional, Sequence, Tuple, Union

from ..csv_safe import csv_safe
from . import reader
from .reader import GOT_IT_COLUMNS, REACH_COLUMNS, GotIt, Reading, Status, WayIn, got_it, received_words
from .store import Record, Review

When = Callable[[datetime], str]
Cell = Union[str, datetime, None]       # text, or a time: words in the report, ISO in a CSV

_STATES = {"locked": "locked", "partlyLocked": "partly locked", "notLocked": "not locked", "stillIn": "may still be in"}
NO_SUBJECT = "(no subject)"
_ESCAPED = frozenset("\\`*_[]<>()!|~&${}#+")
_LINE_BREAKS = "\n\r  \x0b\x0c\x85"


def cell_words(cell: Cell, when: When) -> str:
    return when(cell) if isinstance(cell, datetime) else (cell or "")


def cell_csv(cell: Cell) -> str:
    return cell.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(cell, datetime) else (cell or "")


@dataclass(frozen=True)
class Table:
    columns: Tuple[str, ...]
    rows: Tuple[Tuple[Cell, ...], ...] = ()

    def csv_text(self) -> str:
        """As a file, safe to open in a spreadsheet."""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(self.columns)
        writer.writerows([csv_safe(cell_csv(c)) for c in row] for row in self.rows)
        return buffer.getvalue()


@dataclass(frozen=True)
class Count:
    id: str
    label: str
    number: int
    about: str
    table: Table


@dataclass(frozen=True)
class Change:
    """One write this app's audit log shows."""

    at: datetime
    what: str
    target: str
    by: str
    ok: Optional[bool]


@dataclass(frozen=True)
class Action:
    at: datetime
    what: str
    account: str
    source: str


@dataclass(frozen=True)
class Evidence:
    name: str
    about: str = ""


@dataclass(frozen=True)
class Block:
    kind: str       # title, heading, line, item, table, fixed, gap
    text: str = ""
    columns: Tuple[str, ...] = ()
    rows: Tuple[Tuple[str, ...], ...] = ()
    lines: Tuple[str, ...] = ()


def review_is_empty(review: Review) -> bool:
    """A summary alone isn't a review."""
    return not (review.kind or review.severity or review.meeting or review.attendees or review.cause or review.went_well or review.improve or review.plan)


def two_step_words(address: str, directory: Optional[Mapping[str, Tuple[bool, bool]]]) -> str:
    """What the directory says of them: "on" only for an account found and enrolled. Blank when it isn't read, or doesn't hold them: never "on" for what isn't known. ``directory`` is each address's (enrolled, suspended)."""
    found = directory.get(address.lower()) if directory is not None else None
    if found is None:
        return ""
    enrolled, suspended = found
    return "on" if enrolled else "off (suspended)" if suspended else "off"


def got_it_table(got: Sequence[GotIt], directory: Optional[Mapping[str, Tuple[bool, bool]]]) -> Table:
    """2-Step Verification only for a person here, with the directory read."""
    def two_step(one: GotIt) -> str:
        return two_step_words(one.address, directory) if one.inside and one.mailbox else ""

    return Table(GOT_IT_COLUMNS, tuple((
        one.address, "inside" if one.inside else "outside", "received" if one.received else "sent, no bounce or failure in the logs read", one.at, str(one.emails), "; ".join(one.subjects),
        "yes" if one.warned else "", "yes" if one.to_warn else "", one.contacted.at if one.contacted else None, one.contacted.sender if one.contacted else "",
        one.contacted.subject if one.contacted else "", one.through, two_step(one)) for one in got))


def span(seconds: float) -> str:
    minutes = max(0, int(seconds / 60))
    days, hours, rest = minutes // 1440, minutes % 1440 // 60, minutes % 60
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in ((days, "day"), (hours, "hour"), (0 if days else rest, "minute")) if n]
    return " ".join(parts) or "under a minute"


@dataclass(frozen=True)
class Report:
    record: Record
    made: datetime
    made_by: str
    first_sign: Optional[datetime]
    contained: Optional[datetime]
    counts: Tuple[Count, ...]
    actions: Tuple[Action, ...]
    standing: Tuple[Tuple[str, str], ...]
    to_do: Tuple[str, ...]
    timeline: Table
    evidence: Tuple[Evidence, ...]
    emails: Tuple[str, ...]
    reached: Table
    way_in: Optional[WayIn]
    coverage: Tuple[str, ...] = field(default=())   # what the numbers rest on and what they can't show: what makes it not the whole story
    saved: bool = True      # the numbers come from reads saved with the incident
    timeline_cut: int = 0

    def count(self, count_id: str) -> Optional[Count]:
        return next((c for c in self.counts if c.id == count_id), None)

    def number(self, count_id: str) -> int:
        found = self.count(count_id)
        return found.number if found else 0

    def way_in_words(self, when: When) -> str:
        way_in = self.way_in
        if way_in is None:
            return "No account the attacker got into shows in the logs read."
        if way_in.picked:
            picked = way_in.picked
            return (f"The email that started it: “{picked.subject or NO_SUBJECT}” from {picked.sender or 'a sender the log doesn’t name'} (Message-ID {picked.message_id}). "
                    + received_words(picked, way_in, when))
        if way_in.picked_unread:
            return f"The email picked as where it started ({way_in.picked_unread}) isn't in the logs read: pull {way_in.account}'s Gmail log from before {when(way_in.attacked_at)}."
        if not way_in.candidates:
            return (f"Not found yet: the logs read show no email from outside reaching {way_in.account} in the 72 hours before the attacker's first sign at {when(way_in.attacked_at)}. "
                    "Pull its Gmail log from before then to see what came in.")
        many = len(way_in.candidates)
        return (f"Not picked yet: {many} email{'' if many == 1 else 's'} {way_in.account} received from outside in the 72 hours before the attacker's first sign at {when(way_in.attacked_at)} "
                "are listed on Investigate, under Where it started.")

    def agenda(self) -> List[str]:
        """The review meeting's, filled from the report's own numbers."""
        n = self.number
        opened = self.record.opened
        detect = (span((opened - self.first_sign).total_seconds()) if opened >= self.first_sign else "—") if self.first_sign else "—"
        contain = span((self.contained - self.first_sign).total_seconds()) if self.contained and self.first_sign else "not yet contained"
        picked = self.way_in.picked if self.way_in else None
        def many(count: int, word: str) -> str:
            return f"{count} {word}" + ("" if count == 1 else "es" if word.endswith("ss") else "s")

        return [f"1. What happened: the timeline and the attacker's emails ({many(n('accounts'), 'account')} touched, {many(n('emails'), 'email')} sent).",
                f"2. How it was found and how fast: time to detect {detect}, time to contain {contain}.",
                f"3. What was done, and what shows it: {many(n('actions'), 'step')} taken.",
                f"4. Who it got to and whether they were warned: {many(n('gotIt'), 'address')} ({n('gotItOutside')} outside), {n('toWarn')} still to warn, {n('contacted')} contacted from here after.",
                f"5. What's still open: {many(n('toDo'), 'item')}" + (f", and {n('reached2SV')} people it got to here with 2-Step Verification off" if n("reached2SV") else "") + ".",
                "6. How they got in, and why it worked" + (f": the logs show it began with “{picked.subject or NO_SUBJECT}” from {picked.sender}." if picked else "."),
                "7. What went well, and what to do better.",
                "8. The plan going forward: each change with who owns it and by when."]

    def blocks(self, when: When, zone: str = "", hashes: Sequence[Tuple[str, str]] = ()) -> List[Block]:
        """The one document, which text, Markdown and the PDF draw: the three always say the same. ``hashes`` is (file, SHA-256)."""
        out: List[Block] = []
        record, review = self.record, self.record.review

        def heading(text: str) -> None:
            out.append(Block("heading", text))

        def line(text: str) -> None:
            out.append(Block("line", text))

        def item(text: str) -> None:
            out.append(Block("item", text))

        def said(label: str, value: str) -> None:
            if value:
                line(f"{label}: {value}")

        def paragraphs(text: str) -> List[str]:
            return [p.strip() for p in text.splitlines() if p.strip()]

        out.append(Block("title", record.title))
        line(f"Domain: {record.domain}")
        line(f"Opened (identified): {when(record.opened)}" + (f" by {record.opened_by}" if record.opened_by else ""))
        said("How it was reported", record.reported)
        said("Type", review.kind)
        said("Severity", review.severity)
        line(f"Report made {when(self.made)}" + (f" by {self.made_by}" if self.made_by else "") + " with GamGUI, " + ("from the evidence listed at the end." if self.saved and self.evidence else "from a read of the logs that isn't saved with the incident."))
        if zone:
            line(f"Times are in {zone}.")
        line("Every count and list below is worked out by code from Google's logs and this app's audit log; only " + ("the summary, " if review.summary else "")
             + "the notes, the review and what was marked done by hand are typed.")
        if review.summary:
            heading("Summary")
            for paragraph in paragraphs(review.summary):
                line(paragraph)

        if self.coverage:
            heading("What this report can't show")
            for said_line in self.coverage:
                item(said_line)

        heading("At a glance")
        if self.first_sign:
            line(f"First sign of the attacker in the logs: {when(self.first_sign)}")
            line("Time to detect (first sign to opened): " + (span((record.opened - self.first_sign).total_seconds()) if record.opened >= self.first_sign else "opened before the first sign in these logs"))
        if self.contained and self.first_sign:
            line(f"Every account these logs show touched locked by: {when(self.contained)} (time to contain: {span((self.contained - self.first_sign).total_seconds())})")
        elif self.standing:
            line("Contained: not yet: the logs don't show every account touched locked.")
        for count in self.counts:
            if count.id != "actions":
                item(f"{count.label}: {count.number}")

        heading("Where it started")
        line(self.way_in_words(when))

        heading("What was done")
        if not self.actions:
            line("No step taken shows in the logs read, this app's audit log, or the notes.")
        for action in self.actions:
            item(f"{when(action.at)}: {action.what}" + (f" [{action.account}]" if action.account else "") + f" (shown by {action.source})")

        heading("The attacker's emails, and who they reached")
        if not self.emails:
            line("No email the attacker sent shows in the logs read.")
        for email in self.emails:
            item(email)
        if self.number("gotIt"):
            line("Everyone they got to, once each, with whether they were warned and contacted from here after, is in who-got-it.csv; every address each email was sent to, bounces too, is in who-it-reached.csv.")

        heading("Where each account stands")
        if not self.standing:
            line("No account the attacker touched shows in the logs read.")
        for account, words in self.standing:
            item(f"{account}: {words}")

        heading("Still to do")
        if not self.to_do:
            line("Nothing left that the logs read can show.")
        for task in self.to_do:
            item(task)

        heading("Timeline")
        if self.timeline_cut:
            line(f"The earliest events and everything done about them: {self.timeline_cut} more of the attacker's events are in the saved logs, not here.")
        if self.timeline.rows:
            out.append(Block("table", columns=self.timeline.columns, rows=tuple(tuple(cell_words(c, when) for c in row) for row in self.timeline.rows)))
        else:
            line("Nothing in the logs read.")

        heading("Notes")
        if not record.notes:
            line("None.")
        for note in record.notes:
            item(f"{when(note.at)}: {note.text}")

        heading("Post-incident review: lessons learned and the plan going forward")
        if review_is_empty(review):
            line("Not written yet. For the review meeting, go through:")
        else:
            if review.meeting:
                line(f"Review meeting: {when(review.meeting)}")
            said("Attended", review.attendees)
            said("How they got in", review.cause)
            said("What went well", review.went_well)
            said("What to do better", review.improve)
            if review.plan:
                line("The plan going forward:")
                for step in paragraphs(review.plan):
                    item(step)
            out.append(Block("gap"))
            line("The review meeting's agenda:")
        for step in self.agenda():
            line(step)

        heading("Evidence")
        line("Each read of the logs kept with the incident, exactly as exported from Google's reports, is in the evidence folder." if self.evidence else "No read of the logs is saved with this incident.")
        for each in self.evidence:
            item(each.name + (f": {each.about}" if each.about else ""))
        if hashes:
            out.append(Block("gap"))
            line("SHA-256 of each evidence file, to show it is unchanged:")
            out.append(Block("fixed", lines=tuple(f"{digest}  {name}" for name, digest in hashes)))
        return out


def make(reading: Reading, record: Record, changes: Sequence[Change], evidence: Sequence[Evidence], directory: Optional[Mapping[str, Tuple[bool, bool]]], made: datetime, made_by: str, when: When,
         coverage: Sequence[str] = (), saved: bool = True) -> Report:
    statuses = reading.statuses
    accounts = {s.account.lower() for s in statuses}
    signs = [s.attacked_at for s in statuses if s.attacked_at]
    first_sign = min(signs) if signs else None
    locks = [s.locked_at for s in statuses if s.locked_at]
    contained = max(locks) if statuses and locks and all(s.state == "locked" for s in statuses) else None
    got = got_it(reading.reaches)

    def got_table(keep: Callable[[GotIt], bool]) -> Table:
        return got_it_table([g for g in got if keep(g)], directory)

    def off(address: str, active_only: bool = False) -> bool:
        """Not enrolled: a person still to worry about, for the next accounts at risk, is one who's active."""
        found = directory.get(address.lower()) if directory is not None else None
        return found is not None and not found[0] and not (active_only and found[1])

    emails: List[str] = []
    email_rows: List[Tuple[Cell, ...]] = []
    for reach in reading.reaches:
        subject = reach.subject or NO_SUBJECT
        listed = got_it([reach])
        senders = f" from {', '.join(reach.senders)}" if reach.senders else ""
        warned = [w for w in reach.warnings if w.problem is None]
        times = [r.at for r in reach.recipients if r.at]
        outside, left, contacted = sum(not g.inside for g in listed), sum(g.to_warn for g in listed), sum(g.contacted is not None for g in listed)
        emails.append(f"“{subject}” ({reach.message_id}){senders}: got to {len(listed)} {'address' if len(listed) == 1 else 'addresses'} ({outside} outside); {left} still to warn, {contacted} contacted from here after it."
                      + (" Warned by " + "; ".join(f"“{w.subject or NO_SUBJECT}” from {w.sender}" + (f" at {when(w.at)}" if w.at else "") for w in warned) + "." if warned else " No warning picked counts yet.")
                      + (f" {reach.unnamed} send{' names' if reach.unnamed == 1 else 's name'} no recipient, so who that went to isn't in the logs read." if reach.unnamed else "")
                      + (f" {reach.unreadable} more the log names don't read as an address." if reach.unreadable else ""))
        email_rows.append((subject, reach.message_id, ", ".join(reach.senders), min(times) if times else None, str(len(listed)), str(outside), str(left)))

    # Every step taken: what the logs show done to each account, what this app ran, the emails pulled, each warning that counted, and the notes.
    actions: List[Action] = []
    for status in statuses:
        for moment in status.timeline:
            if moment.who in ("admin", "google"):
                actions.append(Action(moment.at, moment.words + (f" ({moment.count} times)" if moment.count > 1 else ""), status.account, "Google's Admin log" if moment.who == "admin" else "Google's sign-in log"))
    reached = {g.address.lower() for g in got if g.inside and g.mailbox}
    since = min(first_sign or record.opened, record.opened)
    for change in changes:
        if change.at >= since and change.target.lower() in accounts | reached:
            outcome = "" if change.ok else " (failed)" if change.ok is False else " (no result recorded)"
            actions.append(Action(change.at, change.what + outcome + (f" by {change.by}" if change.by else ""), change.target, "This app's audit log"))
    for task in reading.done:
        done = task.done
        if done is None or done.how == "warned":     # each warning that counted is said below, from the Gmail log
            continue
        by = f" (by {done.by})" if done.by else ""
        if done.how == "trashed":
            actions.append(Action(done.at, task.words + by, "", "This app's audit log (Phishing)"))
        elif done.how == "nobodyHasIt":
            actions.append(Action(done.at, task.words + by, "", "Phishing's Find, kept on this Mac"))
        else:
            actions.append(Action(done.at, f"Done by hand{by}: {task.words} How: {done.said}", task.accounts[0] if len(task.accounts) == 1 else "", "Marked done on the incident"))
    for reach in reading.reaches:
        for warning in reach.warnings:
            if warning.problem is None and warning.at:
                actions.append(Action(warning.at, f"Warning “{warning.subject or NO_SUBJECT}” sent by {warning.sender}: {warning.covers} of those “{reach.subject or NO_SUBJECT}” reached have it, by the log's word", warning.sender, "Google's Gmail log"))
    actions.extend(Action(note.at, note.text, "", "A note on the incident") for note in record.notes)
    actions.sort(key=lambda a: a.at)

    columns = ("account", "status", "the attacker got in", "first attacked", "locked", "2-Step Verification")

    def account_table(keep: Callable[[Status], bool]) -> Table:
        return Table(columns, tuple((s.account, _STATES.get(s.state, s.state), "yes" if s.attacker_got_in else "tried only", s.attacked_at, s.locked_at, two_step_words(s.account, directory)) for s in statuses if keep(s)))

    pulled = [t for t in reading.done if t.done and t.done.how in ("trashed", "nobodyHasIt")]
    by_hand = [t for t in reading.done if t.done and t.done.how == "byHand"]
    counts: List[Count] = [
        Count("accounts", "Accounts the attacker touched", len(statuses), "Every account the attacker signed in to, tried, or Google warned of, with where it stands.", account_table(lambda s: True)),
        Count("notLocked", "Not yet locked", sum(s.state != "locked" for s in statuses), "Accounts the logs don't show locked: not locked, partly locked, or signed in to after the lock.", account_table(lambda s: s.state != "locked")),
    ]
    if directory is not None:
        counts.append(Count("accounts2SV", "Touched with 2-Step Verification off", sum(off(s.account) for s in statuses),
                            "Accounts the attacker touched that the directory shows without 2-Step Verification, suspended or not.", account_table(lambda s: off(s.account))))
    counts += [
        Count("emails", "Emails the attacker sent", len(reading.reaches), "Each email the attacker sent: who sent it, when, and how many it got to.",
              Table(("subject", "Message-ID", "from", "sent", "got to", "outside", "still to warn"), tuple(email_rows))),
        Count("gotIt", "Got an email of the attacker's", len(got), "Each address an email of the attacker's got to, once, however many: a receipt here, or a send nothing says failed. A bounce, a quarantine or a rejection didn't get through, so isn't listed. Export it to send the warning.", got_table(lambda g: True)),
        Count("gotItOutside", "Got it, outside", sum(not g.inside for g in got), "The addresses outside an email of the attacker's got to: who to warn that this app can't see into.", got_table(lambda g: not g.inside)),
        Count("toWarn", "Still to warn", sum(g.to_warn for g in got), "Got one, and not shown warned of it by a warning picked that counts.", got_table(lambda g: g.to_warn)),
        Count("warned", "Warned", sum(g.warned for g in got), "Shown warned of every one that got to them, by the log's own word: their receipt of a warning picked (here), or its send (outside).", got_table(lambda g: g.warned)),
        Count("contacted", "Contacted from here after", sum(g.contacted is not None for g in got), "Sent an email from an account here after one of the attacker's got to them: the first, and how many.", got_table(lambda g: g.contacted is not None)),
        Count("notContacted", "Still to warn, no contact from here seen", sum(g.to_warn and g.contacted is None for g in got), "Still to warn, and no email from here after the attacker's in the logs read.", got_table(lambda g: g.to_warn and g.contacted is None)),
    ]
    picked = reading.way_in.picked if reading.way_in else None
    if picked:
        counts.append(Count("gotPhish", "Got the email that started it", len(picked.received_by), f"Everyone here the logs read show receiving “{picked.subject or NO_SUBJECT}” from {picked.sender}: they may have fallen for it too.",
                            Table(("Email", "Touched by the attacker"), tuple((a, "yes" if a.lower() in accounts else "") for a in picked.received_by))))
    if directory is not None:
        def here_off(g: GotIt) -> bool:
            return g.inside and g.mailbox and off(g.address, active_only=True)
        counts.append(Count("reached2SV", "Got it here with 2-Step Verification off", sum(here_off(g) for g in got), "People here an email of the attacker's got to who have 2-Step Verification off: the next accounts at risk.", got_table(here_off)))
    counts.append(Count("pulled", "Emails pulled, or that nobody here had", len(pulled), "Emails Phishing trashed from every mailbox that had them, or showed nobody here has.",
                        Table(("what", "when", "by"), tuple((t.words, t.done.at if t.done else None, t.done.by if t.done else "") for t in pulled))))
    if by_hand:
        counts.append(Count("byHand", "Marked done by hand", len(by_hand), "What no log can show done, marked done on the incident: how, when and by whom, as typed.",
                            Table(("what", "how", "when", "by"), tuple((t.words, t.done.said if t.done else "", t.done.at if t.done else None, t.done.by if t.done else "") for t in by_hand))))
    # A step that failed, or a note, is listed and isn't a step taken.
    noted = sum(a.source == "A note on the incident" for a in actions)
    failed = sum(a.source == "This app's audit log" and (" (failed)" in a.what or " (no result recorded)" in a.what) for a in actions)
    counts += [
        Count("actions", "Steps taken", len(actions) - noted - failed, f"Every step taken, oldest first, with the record that shows it. Listed too: {failed} that failed or showed no result, and {noted} note{'' if noted == 1 else 's'}.",
              Table(("when", "what", "account", "shown by"), tuple((a.at, a.what, a.account, a.source) for a in actions))),
        Count("toDo", "Still to do", len(reading.to_do), "What the logs show is left, most urgent first.", Table(("item", "to do"), tuple((t.id, t.words) for t in reading.to_do))),
    ]
    moments = sorted(((m.at, s.account, m.who, m.words) for s in statuses for m in s.timeline), key=lambda m: (m[0], m[1]))
    return Report(record=record, made=made, made_by=made_by, first_sign=first_sign, contained=contained, counts=tuple(counts), actions=tuple(actions),
                  standing=tuple((s.account, s.words(when)) for s in statuses), to_do=tuple(f"{t.id}: {t.words}" for t in reading.to_do),
                  timeline=Table(("when", "account", "who", "what"), tuple(moments)), evidence=tuple(evidence), emails=tuple(emails),
                  reached=Table(("email", "Message-ID", *REACH_COLUMNS), tuple((reach.subject or NO_SUBJECT, reach.message_id, *row) for reach in reading.reaches for row in reach.rows)), way_in=reading.way_in,
                  coverage=tuple(coverage), saved=saved, timeline_cut=sum(s.timeline_cut for s in statuses))


def _flat(text: str) -> str:
    """On one line: a subject or a note with a line break must never read as a heading of the report's own."""
    return "".join(" " if c in _LINE_BREAKS else c for c in text)


def text_of(blocks: Sequence[Block]) -> str:
    out: List[str] = []
    for block in blocks:
        if block.kind == "title":
            out.append(f"INCIDENT REPORT: {_flat(block.text)}")
        elif block.kind == "heading":
            out += ["", block.text.upper(), "=" * min(len(block.text), 72)]
        elif block.kind == "line":
            out.append(_flat(block.text))
        elif block.kind == "item":
            out.append(f"• {_flat(block.text)}")
        elif block.kind == "table":
            out += [_flat("  ·  ".join(row)) for row in block.rows]
        elif block.kind == "fixed":
            out += [_flat(fixed) for fixed in block.lines]
        else:
            out.append("")
    return "\n".join(out) + "\n"


def markdown_escape(text: str) -> str:
    """Every piece as its words: the inline marks anywhere, a list's dash or number and a heading's `=` at the start, leading spaces dropped. A URL stays as written (its text is its target)."""
    flat = "".join(" " if c in _LINE_BREAKS else c for c in text).lstrip(" \t")
    out: List[str] = []
    start, digits = True, False
    for c in flat:
        if c in _ESCAPED or (start and c in "-=") or (digits and c in ".)"):
            out.append("\\")
        out.append(c)
        digits = (start or digits) and c.isascii() and c.isdigit()
        start = False
    return "".join(out)


def markdown_of(blocks: Sequence[Block]) -> str:
    out = ""
    last: Optional[Block] = None

    def row(cells: Sequence[str]) -> str:
        return "| " + " | ".join(markdown_escape(c) for c in cells) + " |"

    for block in blocks:
        if block.kind == "gap":
            continue
        if block.kind == "title":
            chunk = f"# Incident report: {markdown_escape(block.text)}"
        elif block.kind == "heading":
            chunk = f"## {markdown_escape(block.text)}"
        elif block.kind == "line":
            chunk = markdown_escape(block.text)
        elif block.kind == "item":
            chunk = f"- {markdown_escape(block.text)}"
        elif block.kind == "table":
            chunk = "\n".join([row(block.columns), "|" + " --- |" * len(block.columns), *(row(r) for r in block.rows)])
        else:
            # A fence longer than any run of backticks in it, so nothing in it can close it.
            longest, run = 0, 0
            for character in "\n".join(block.lines):
                run = run + 1 if character == "`" else 0
                longest = max(longest, run)
            fence = "`" * max(3, longest + 1)
            chunk = "\n".join([fence, *block.lines, fence])
        if out:
            out += "\n" if last is not None and last.kind == "item" and block.kind == "item" else "\n\n"
        out += chunk
        last = block
    return out + "\n"


def evidence_about(about: str) -> str:
    """What a saved read was, from its `about.txt`: its window, accounts and address."""
    return " ".join(line for line in about.split("\n")[1:4] if line.strip())


__all__ = ["Action", "Block", "Change", "Count", "Evidence", "Report", "Table", "cell_csv", "cell_words", "evidence_about", "got_it_table", "make", "markdown_escape", "markdown_of", "reader", "review_is_empty", "span", "text_of", "two_step_words"]
