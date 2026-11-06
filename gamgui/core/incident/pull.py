"""One pull of the incident logs: each log read whole, oldest first, bounded, and never on the event loop.

Most logs come back short of the cap on one read of the window. One at the cap is read a day at a time from the window's start, each day read
back from its end until it's whole, an event read twice kept once. Past the bounds, a read that fails, or a day still at its cap, it stops at that
day's start, so what's unread is the newest: the attack is the oldest part, and the newest is the easiest to read again. A failure is said,
never a part passed as the whole. Ported from SwiftGamGui's ``IncidentLogs.swift``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import secrets
import time
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ..csv_safe import csv_safe
from .reader import Whole
from .logs import (
    CAP,
    EXPORT_ROWS,
    GMAIL_MAX_HOURS,
    NAMES,
    READS_PER_DAY,
    READS_PER_LOG,
    Row,
    capped as is_capped,
    newest_first,
    parse_time,
    report_time,
    rows_from_export,
    table,
    window_words,
)
from .logs import addresses as addresses_in
from .store import Export, export_of
from .reads import in_words

AT_ONCE = 3     # at most this many reads at once
MOST_ACCOUNTS = 20

Read = Callable[[Sequence[str], str, str], Awaitable[List[Row]]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Query:
    applications: Tuple[str, ...]
    users: Tuple[str, ...]      # empty for every account
    hours: int
    ip: str = ""
    start: Optional[datetime] = None    # a window from a time to a time instead of the last `hours`: Read the Rest
    end: Optional[datetime] = None


@dataclass(frozen=True)
class Estimate:
    """How far a log read whole will get within its bounds, from its first read of the whole window: that read's events over their span are the
    log's events an hour, and that read's time each read's. Oldest first, so what's left is the newest."""

    per_hour: int
    window_hours: int
    hours: int
    too_busy: bool      # too busy to read even its first day whole: it stops at once
    reads: int
    seconds: int

    @property
    def whole(self) -> bool:
        return not self.too_busy and self.hours >= self.window_hours

    @classmethod
    def of(cls, count: int, span: float, took: float, window_hours: int, cap: int, per_day: int, budget: int) -> "Estimate":
        per_hour = round(count / max(span / 3600, 1 / 60))
        too_busy = per_hour * 24 > cap * per_day
        hours = 0 if too_busy else min(window_hours, cap * budget // max(per_hour, 1))
        reads = 1 if too_busy else min(budget, 1 + math.ceil(per_hour * hours / cap))
        return cls(per_hour, window_hours, hours, too_busy, reads, math.ceil(reads * max(took, 0.1)))

    @property
    def words(self) -> str:
        if self.too_busy:
            return f"About {self.per_hour} events an hour: too busy to read one day whole. Filter by the attacker's address, or name the accounts."
        if self.whole:
            return f"About {self.per_hour} events an hour: it will read all of it, in about {self.reads} reads."
        return f"About {self.per_hour} events an hour: it will read the oldest {window_words(self.hours)} of {window_words(self.window_hours)}. Read the Rest goes on from there."


@dataclass
class Progress:
    """One log while a pull reads it, told after every read, so it moves while a busy log takes a dozen reads a day. Bounded by the reads."""

    state: str = "waiting"      # waiting, reading, done
    failed: bool = False
    events: int = 0
    reads: int = 0
    through: Optional[datetime] = None
    estimate: Optional[Estimate] = None


@dataclass
class Log:
    """One log's read: its rows, or what went wrong in words."""

    application: str
    rows: Optional[List[Row]] = None
    problem: str = ""
    hours: int = 0
    capped: bool = False        # not all of the window was read: see `through`
    through: Optional[datetime] = None      # with it, read completely up to that time and the later events weren't; without, the older weren't
    flagged: int = 0
    accounts: int = 0
    covered: FrozenSet[str] = frozenset()   # stopped short, and these accounts read whole past where it stopped, by a later read of just them
    whole: Tuple[Whole, ...] = ()       # whose events it holds every one of, and between when: what tells a quiet mailbox from one not read

    @classmethod
    def read(cls, application: str, rows: List[Row], hours: int, capped: bool = False, through: Optional[datetime] = None, covered: Iterable[str] = (), scope: Optional[Sequence[str]] = None,
             whole: Sequence[Whole] = ()) -> "Log":
        """``scope``: the accounts a read with no address filter was for (empty: every account), to work out ``whole`` from its rows, never from a window it claims; None for a read that can't say. A read
        stopped at its cap holds only up to where it read through. ``whole`` given instead: a log put together from reads, theirs."""
        times = [r.date for r in rows if r.date]
        held: Tuple[Whole, ...]
        if scope is not None and times and (not capped or through is not None):
            newest = max(times)
            to = min(newest, through) if through else newest
            held = (Whole(frozenset(a.lower() for a in scope) if scope else None, min(times), to),) if min(times) < to else ()
        else:
            held = tuple(whole)
        return cls(application, rows, "", hours, capped, through, sum(r.flagged for r in rows), len({r.who for r in rows}), frozenset(covered), held)

    @property
    def name(self) -> str:
        return NAMES.get(self.application, self.application)

    @property
    def file_name(self) -> str:
        """A log cut short says so in its name: a file gets passed around without its note."""
        return f"{self.application}-log{'-partial' if self.capped else ''}.csv"

    @property
    def nothing_kept(self) -> bool:
        """Never read, or stopped before anything of it stood: exporting it would pass as no events."""
        return self.rows is None or (self.capped and not self.rows)

    @property
    def how_far(self) -> str:
        if not self.capped:
            return ""
        also = f" Past that, read only for {', '.join(sorted(self.covered))}." if self.covered else ""
        if self.through:
            return f"Read completely through {report_time(self.through)}: later events weren't read.{also}"
        return f"Older events past the cap ({CAP} an account a read) weren't read.{also}"


@dataclass
class Pull:
    query: Query
    logs: List[Log] = field(default_factory=list)   # in the query's order
    ended: bool = False
    run: str = field(default_factory=lambda: secrets.token_hex(8))
    at: Optional[datetime] = None       # the window's end
    from_evidence: bool = False         # made from an incident's saved reads, not read from Google: never saved to it again
    addresses: List[str] = field(default_factory=list)      # the sign-in log's IP addresses, the flagged ones first: what to filter every log by next

    def log(self, application: str) -> Optional[Log]:
        return next((log for log in self.logs if log.application == application), None)

    def work_out_addresses(self) -> None:
        log = self.log("login")
        self.addresses = addresses_in(log.rows) if log and log.rows else []


async def paged(users: Sequence[str], window: Tuple[str, str], cap: int, pages: int, read: Read) -> Tuple[List[Row], bool]:
    """A log read past its cap, still bounded. The window is fixed once, as times. A read at the cap is read again up to where it stopped: its oldest
    event's second and one more (Google may leave the end's own second out), the events already read dropped, until a read comes back short or the
    reads run out; for accounts, each one at the cap goes on by itself. Capped when a last read was still at the cap, a later read failed, or some
    address reached the cap that isn't one of the accounts asked (an alias: no telling which to go on with). A first read that fails raises."""
    start = window[0]
    rows = list(await read(users, window[0], window[1]))
    seen = {r.identity for r in rows}
    reads, stopped = 1, False
    going: List[Tuple[Sequence[str], List[Row]]]
    if len(users) <= 1:     # every row of one read is that read's, whatever address it carries
        going = [(users, rows)] if len(rows) >= cap else []
    else:
        asked = {u.lower() for u in users}
        stray = sum(1 for r in rows if r.who.lower() not in asked)     # an old name, or none at all: it could be any of theirs, so it counts toward each
        by_account = [([u], [r for r in rows if r.who.lower() == u.lower()]) for u in users]
        going = [(who, page) for who, page in by_account if page and len(page) + stray >= cap]
        stopped = stray >= cap
    begin = parse_time(start)
    while going:
        who, page = going.pop(0)
        dates = [r.date for r in page if r.date]
        if reads >= pages or not dates:
            stopped = True
            continue
        oldest = min(dates)
        if begin and oldest <= begin:   # read back to the window's start: nothing earlier is in it, and an end before the start is GAM's usage error
            continue
        reads += 1
        try:
            more = await read(who, start, report_time(oldest + timedelta(seconds=1)))
        except Exception:  # noqa: BLE001 - what came back is kept and said as capped, never a failure of the whole
            stopped = True
            continue
        for row in more:
            if row.identity not in seen:
                seen.add(row.identity)
                rows.append(row)
        if len(more) < cap:
            continue
        older = [r.date for r in more if r.date]
        if older and min(older) < oldest:       # at the cap again: on from its own oldest, unless a whole read fell in that one second
            going.append((who, more))
        else:
            stopped = True
    return await asyncio.to_thread(newest_first, rows), stopped


async def complete(users: Sequence[str], start: datetime, end: datetime, cap: int, read: Read, *, per_day: int = READS_PER_DAY, budget: int = READS_PER_LOG,
                   through: Callable[[datetime], None] = lambda _: None, reading: Callable[[int], None] = lambda _: None,
                   estimated: Callable[[Estimate], None] = lambda _: None) -> Tuple[List[Row], Optional[datetime]]:
    """Every event of the window, read oldest first. Returns the rows, newest first, and when it was read completely through (None: all of it). A first
    read that fails raises: that is the log's failure."""
    began = time.monotonic()
    first = await read(users, report_time(start), report_time(end))
    took = time.monotonic() - began
    if not is_capped(first, users if len(users) > 1 else (), cap):
        reading(len(first))
        return list(first), None
    dates = [r.date for r in first if r.date]
    if dates:
        estimated(Estimate.of(len(first), (max(dates) - min(dates)).total_seconds(), took, math.ceil((end - start).total_seconds() / 3600), cap, per_day, budget))
    reading(0)
    rows: List[Row] = []
    seen: set = set()
    reads, used, day = 1, 0, start
    today: set = set()

    async def counted(who: Sequence[str], since: str, until: str) -> List[Row]:
        nonlocal used
        used += 1
        got = await read(who, since, until)
        today.update(r.identity for r in got if r.identity not in seen)
        reading(len(seen) + len(today))
        return got

    while day < end:
        following = min(day + timedelta(days=1), end)
        left = min(per_day, budget - reads)
        if left <= 0:
            return await asyncio.to_thread(newest_first, rows), day
        used = 0
        today.clear()
        # Each day's end a second past the next day's start: whether Google keeps an end's own second isn't said.
        until = report_time(end if following == end else following + timedelta(seconds=1))
        try:
            these, stopped = await paged(users, (report_time(day), until), cap, left, counted)
        except Exception:  # noqa: BLE001 - it stops there: the older days are whole, and the newer unread is said
            return await asyncio.to_thread(newest_first, rows), day
        reads += used
        for row in these:
            if row.identity not in seen:
                seen.add(row.identity)
                rows.append(row)
        if stopped:
            return await asyncio.to_thread(newest_first, rows), day
        day = following
        through(day)
    return await asyncio.to_thread(newest_first, rows), None


STOPPED = "Stopped before it was read."


async def _chrome(conn: Any, query: Query, hours: int, cap: int, note: Callable[[int, int], None], stopped_now: Callable[[], bool]) -> Log:
    """Chrome's log for some accounts: each read both ways, by the profile's account and by GAM's own device match, which finds a Chromebook's sign-ins."""
    rows: List[Row] = []
    stopped = False
    for user in query.users:
        if stopped_now():       # never the accounts read so far passed as the whole
            return Log("chrome", None, STOPPED, hours)
        reads = await asyncio.gather(conn.activity("chrome", [user], hours, ip=query.ip, cap=cap, chrome="profile"),
                                     conn.activity("chrome", [user], hours, ip=query.ip, cap=cap, chrome="device"), return_exceptions=True)
        seen: set = set()
        for got in reads:
            if isinstance(got, BaseException):
                if isinstance(got, asyncio.CancelledError):
                    raise got
                return Log("chrome", None, f"{user}: {in_words(got)}", hours)
            stopped = stopped or len(got) >= cap
            rows += [r for r in got if r.identity not in seen]
            seen.update(r.identity for r in got)
        note(len(rows), 2)
    return await asyncio.to_thread(Log.read, "chrome", newest_first(rows), hours, stopped)


async def _admin(conn: Any, query: Query, hours: int, cap: int, note: Callable[[int, int], None], stopped_now: Callable[[], bool]) -> Log:
    """The admin log for some accounts, both ways: what admins did to them (a reset, a sign-out: the lock the reader looks for) and what they did as admins."""
    if stopped_now():
        return Log("admin", None, STOPPED, hours)
    reads: List[Any] = list(await asyncio.gather(conn.activity("admin", list(query.users), hours, ip=query.ip, cap=cap, admin="affected"),
                                                 conn.activity("admin", list(query.users), hours, ip=query.ip, cap=cap, admin="actor"), return_exceptions=True))
    rows: List[Row] = []
    seen: set = set()
    for got, what in zip(reads, ("What admins did to these accounts", "What these accounts did as admins"), strict=True):
        if isinstance(got, BaseException):
            if isinstance(got, asyncio.CancelledError):
                raise got
            return Log("admin", None, f"{what}: {in_words(got)}", hours)
        rows += [r for r in got if r.identity not in seen]
        seen.update(r.identity for r in got)
    note(len(rows), 2)
    # What was done to an account is counted by the account it names, not by the admin who did it.
    to_each: Dict[str, int] = {}
    for row in reads[0]:
        target = row.value("USER_EMAIL").lower()
        to_each[target] = to_each.get(target, 0) + 1
    stopped = any(n >= cap for n in to_each.values()) or is_capped(reads[1], query.users, cap)
    return await asyncio.to_thread(Log.read, "admin", newest_first(rows), hours, stopped)


async def read_log(conn: Any, application: str, query: Query, now: datetime, cap: int = CAP, progress: Optional[Progress] = None,
                   stopped: Callable[[], bool] = lambda: False) -> Log:
    """One log. Never raises for a read that failed: that's the log's own ``problem``. Stop ends it before its next read: what's read stands, said as such."""
    state = progress or Progress()
    state.state = "reading"
    end = query.end or now
    start = query.start or end - timedelta(hours=query.hours)
    if application == "gmail":      # at most 30 days back from its end
        start = max(start, end - timedelta(hours=GMAIL_MAX_HOURS))
    span = math.ceil((end - start).total_seconds() / 3600)
    hours = min(max(span, 1), 4320)

    def note(events: int, reads: int = 1) -> None:
        state.events = events
        state.reads += reads

    try:
        if application == "chrome" and query.users:
            log = await _chrome(conn, query, hours, cap, note, stopped)
        elif application == "admin" and query.users:
            log = await _admin(conn, query, hours, cap, note, stopped)
        else:
            # The Devices log records no address, so a pull by one would drop it whole: its registrations instead, for the accounts asked, which the
            # reader ties to a suspect sign-in by time. One event name at most: GAM caps each name's read on its own.
            by_address = application == "mobile" and bool(query.ip)
            events = ["DEVICE_REGISTER_UNREGISTER_EVENT"] if by_address else []
            ip = "" if by_address else query.ip

            async def one(users: Sequence[str], since: str, until: str) -> List[Row]:
                if stopped():
                    raise ValueError(STOPPED)
                return await conn.activity(application, list(users), hours, start=since, end=until, ip=ip, events=events, cap=cap)

            def reached(when: datetime) -> None:
                state.through = when

            def estimated(estimate: Estimate) -> None:
                state.estimate = estimate

            rows, through = await complete(query.users, start, end, cap, one, through=reached, reading=note, estimated=estimated)
            log = await asyncio.to_thread(Log.read, application, rows, hours, through is not None, through, scope=query.users if not ip and not events else None)
    except Exception as exc:  # noqa: BLE001 - said beside the rest, whatever it was
        log = Log(application, None, in_words(exc), hours)
    state.state, state.failed = "done", log.rows is None
    state.events = len(log.rows or [])
    return log


async def pull_logs(conn: Any, query: Query, pull: Pull, progress: Dict[str, Progress], cap: int = CAP, now: Optional[datetime] = None,
                    stopped: Callable[[], bool] = lambda: False, finished: Callable[[], None] = lambda: None) -> None:
    """Every log of ``query`` into ``pull``, at most ``AT_ONCE`` at a time, each as it finishes, in the query's order."""
    at = now or datetime.now(timezone.utc)
    pull.at = query.end or at
    gate = asyncio.Semaphore(AT_ONCE)
    done: Dict[str, Log] = {}

    async def one(application: str) -> None:
        async with gate:
            done[application] = await read_log(conn, application, query, at, cap, progress[application], stopped)
            pull.logs = [done[a] for a in query.applications if a in done]
            finished()

    await asyncio.gather(*(one(a) for a in query.applications))
    await asyncio.to_thread(pull.work_out_addresses)
    pull.ended = True


_ABOUT_FILE = re.compile(r"([a-z]+)-log(?:-partial)?\.csv")
_ABOUT_LAST = re.compile(r", the last (\d{1,6}) (hours|days) \(the most it reads\)")
_ABOUT_THROUGH = re.compile(r"; read completely through (\S+), later events weren't read")


def _about_log(line: str) -> Optional[Tuple[str, str]]:
    """`<log>-log.csv: the <name> log, <count> events<what else>.` as (log, what else), found by hand: this line is a zip's words, and a pattern over it backtracked for seconds on a few KB."""
    file_name, colon, said = line.partition(": the ")
    found = _ABOUT_FILE.fullmatch(file_name)
    at = said.find(" log, ")
    if not (colon and found and at >= 0 and line.endswith(".")):
        return None
    count, events, rest = said[at + len(" log, "):-1].partition(" events")
    return (found.group(1), rest) if events and count.isdigit() else None


@dataclass(frozen=True)
class About:
    """What a saved read's `about.txt` says: its window, accounts and address, and each log's own window and how far it was read. What a CSV alone can't."""

    users: Tuple[str, ...] = ()
    every_account: bool = False
    ip: str = ""
    hours: int = 0
    windows: Mapping[str, int] = field(default_factory=dict)
    capped: FrozenSet[str] = frozenset()
    through: Mapping[str, datetime] = field(default_factory=dict)
    at: Optional[datetime] = None       # the window's end
    says_whose: bool = False    # it says both whose events and from which address: what a read holding a mailbox whole is judged by

    @classmethod
    def of(cls, text: str) -> "About":
        users: Tuple[str, ...] = ()
        every, ip, hours = False, "", 0
        at: Optional[datetime] = None
        said_accounts, said_address = False, False
        windows: Dict[str, int] = {}
        capped = set()
        through: Dict[str, datetime] = {}

        def span(count: str, unit: str) -> int:
            return int(count) * (24 if unit == "days" else 1)

        for line in text.split("\n"):
            if line.startswith("Accounts: ") and line.endswith("."):
                named = line[len("Accounts: "):-1]
                said_accounts = True
                every = named == "every account"
                users = () if every else tuple(n.strip() for n in named.split(","))
            elif line.startswith("From IP address: ") and line.endswith("."):
                named = line[len("From IP address: "):-1]
                said_address = True
                ip = "" if named == "any" else named
            elif (over := re.match(r"Over the (?:last )?(\d{1,6}) (hours|days)", line)):
                hours = span(over.group(1), over.group(2))
                if (up_to := re.search(r" up to (\S+)\.$", line)):
                    at = parse_time(up_to.group(1))
            elif (between := re.fullmatch(r"From (\S+) to (\S+)\.", line)):
                start, end = parse_time(between.group(1)), parse_time(between.group(2))
                hours = math.ceil((end - start).total_seconds() / 3600) if start and end else 0
                at = end
            elif (log := _about_log(line)):
                name, rest = log
                if (last := _ABOUT_LAST.search(rest)):
                    windows[name] = span(last.group(1), last.group(2))
                if "older events past the cap weren't read" in rest:
                    capped.add(name)
                if (read_to := _ABOUT_THROUGH.search(rest)) and (when := parse_time(read_to.group(1))):
                    capped.add(name)
                    through[name] = when
        return cls(users, every, ip, hours, windows, frozenset(capped), through, at, said_accounts and said_address)


def _application_order(application: str) -> int:
    order = list(NAMES)
    return order.index(application) if application in order else len(order)


def pull_of_export(export: Any) -> Optional[Pull]:
    """One saved read as the pull it saved, or None when it can't be read as one: a log isn't readable, it holds more than EXPORT_ROWS events, or anything else in it fails. Saved reads can come from
    a zip someone else made, and one must never stop the others being shown."""
    try:
        note = About.of(export.about)
        logs: List[Log] = []
        left = EXPORT_ROWS
        for application, text in export.logs:
            rows = rows_from_export(text, application, left)
            if rows is None:
                return None
            left -= len(rows)
            # Chrome's and the admin log's reads for accounts go newest first, each account to its own cap: they can't say. Nor can an about.txt that doesn't say whose and from where.
            per_account = application in ("chrome", "admin") and bool(note.users)
            scope = note.users if note.says_whose and not note.ip and not per_account else None
            logs.append(Log.read(application, rows, note.windows.get(application, note.hours), application in note.capped, note.through.get(application), scope=scope))
        logs.sort(key=lambda log: _application_order(log.application))
        held = Pull(Query(tuple(log.application for log in logs), note.users, note.hours, note.ip), logs, ended=True, at=note.at, from_evidence=True)
        held.work_out_addresses()
        return held
    except Exception:  # noqa: BLE001 - named as unreadable, whatever it was
        logger.exception("a saved read of the logs couldn't be read back")
        return None


def merged(pulls: Sequence[Pull]) -> Pull:
    """``pulls`` (at least one, oldest first) put together: each log's rows newest first, an event an earlier pull read once, within one pull every row kept, as two events can match in every column;
    a log stopped at its cap only while every read of it was, since a read for just the accounts that matter reaches further back; a log a pull couldn't read taken from those that could. The
    widest window, every account if any pull was, and the first address given. A narrower read never takes away what a wider one found."""
    if len(pulls) == 1:
        return pulls[0]
    logs: List[Log] = []
    for application in sorted({log.application for p in pulls for log in p.logs}, key=_application_order):
        every = [log for p in pulls if (log := p.log(application))]
        read = [log for log in every if log.rows is not None]
        if len(read) <= 1:      # read by one pull alone, or by none: the last one's words for why not
            logs.append(read[0] if read else every[-1])
            continue
        known: Set[str] = set()
        kept: List[Row] = []
        for log in read:
            rows = log.rows or []
            kept += [r for r in rows if r.identity not in known]
            known.update(r.identity for r in rows)
        # A log that stopped short stays so until a read holds what it left out: of every account, or of each account it was for. A read of just some accounts covers those alone (it's what clears Still
        # to do's "pull again" for them), and a read by an address holds only that address's, whatever it filtered out of the rest.
        short = [p for p in pulls if (log := p.log(application)) and log.rows is not None and log.capped]
        whole = [p for p in pulls if (log := p.log(application)) and log.rows is not None and not log.capped and not p.query.ip]
        covered = {a.lower() for p in whole for a in p.query.users}.union(*(log.covered for log in read))
        every_read = any(not p.query.users for p in whole)
        stopped = bool(short) and not every_read and not all(p.query.users and {a.lower() for a in p.query.users} <= covered for p in short)
        # Read through the latest of the widest reads: a later read of just some accounts says nothing of the rest.
        widest = [log.through for p in short if not p.query.users and (log := p.log(application)) and log.through]
        throughs = widest or [log.through for log in read if log.capped and log.through]
        logs.append(Log.read(application, newest_first(kept), max(log.hours for log in read), stopped, max(throughs) if stopped and throughs else None, covered if stopped else (), whole=[w for log in read for w in log.whole]))
    every_account = any(not p.query.users for p in pulls)
    users: List[str] = []
    if not every_account:
        seen: Set[str] = set()
        for p in pulls:
            for user in p.query.users:
                if user.lower() not in seen:
                    seen.add(user.lower())
                    users.append(user)
    together = Pull(Query(tuple(log.application for log in logs), tuple(users), max(p.query.hours for p in pulls), next((p.query.ip for p in pulls if p.query.ip), "")), logs,
                    ended=all(p.ended for p in pulls), at=max((p.at for p in pulls if p.at), default=None), from_evidence=all(p.from_evidence for p in pulls))
    together.work_out_addresses()
    return together


def evidence(exports: Sequence[Any]) -> Tuple[Optional[Pull], List[str]]:
    """An incident's saved reads put together as one pull, and the names of those that can't be read. Run off the event loop."""
    pulls: List[Pull] = []
    unreadable: List[str] = []
    for export in exports:
        held = pull_of_export(export)
        if held:
            pulls.append(held)
        else:
            unreadable.append(export.name)
    return (merged(pulls) if pulls else None), unreadable


@dataclass(frozen=True)
class Filter:
    """Which events a to-do item is about, for the logs to narrow to. Empty: any. An app's or agent's name is the logs' text, so this only ever compares it."""

    accounts: Tuple[str, ...] = ()
    addresses: Tuple[str, ...] = ()
    applications: Tuple[str, ...] = ()
    apps: Tuple[str, ...] = ()
    agent: str = ""

    def __bool__(self) -> bool:
        return bool(self.accounts or self.addresses or self.applications or self.apps or self.agent)

    def keeps(self, row: Row) -> bool:
        return ((not self.accounts or row.who.lower() in {a.lower() for a in self.accounts}) and (not self.addresses or row.ip in self.addresses)
                and (not self.applications or row.application in self.applications) and (not self.apps or row.app in self.apps) and (not self.agent or row.agent == self.agent))

    def applied(self, log: Log) -> Optional[Log]:
        """``log`` with only the rows kept, or None when this leaves the log out or keeps none of them. A log that wasn't read stays said, and so does one that stopped short: no events there isn't none at all."""
        if self.applications and log.application not in self.applications:
            return None
        if log.rows is None:
            return log
        kept = [r for r in log.rows if self.keeps(r)]
        return Log.read(log.application, kept, log.hours, log.capped, log.through, log.covered, whole=log.whole) if kept or log.capped else None

    @property
    def words(self) -> str:
        """"carol@example.com's events from 198.51.100.23, in Gmail, through Mail Sync Pro"."""
        names = [NAMES.get(a, a) for a in self.applications]
        return (f"{list_words(self.accounts)}'s events" if self.accounts else "Every account's events") + (f" from {list_words(self.addresses)}" if self.addresses else "") \
            + (f", in {list_words(names)}" if names else "") + (f", through {list_words(self.apps)}" if self.apps else "") + (f", by {self.agent}" if self.agent else "")

    @property
    def query(self) -> str:
        """As a page's address asks for it."""
        pairs = [("who", a) for a in self.accounts] + [("from", a) for a in self.addresses] + [("in", a) for a in self.applications] + [("app", a) for a in self.apps]
        return urlencode(pairs + ([("agent", self.agent)] if self.agent else []))

    @classmethod
    def of_query(cls, params: Any) -> "Filter":
        return cls(tuple(params.getlist("who")), tuple(params.getlist("from")), tuple(params.getlist("in")), tuple(params.getlist("app")), params.get("agent") or "")


def list_words(items: Sequence[str]) -> str:
    items = list(items)
    return " and ".join(items) if len(items) < 3 else f"{', '.join(items[:-1])} and {items[-1]}"


def and_unread(together: Pull, read: Pull) -> Tuple[Pull, List[str]]:
    """What ``read`` couldn't read is still said, once the evidence is what's shown: a log the evidence lacks shows as this read's failure, and a log it holds is said in words, its rows being older.
    ``together`` itself stays what reading the saved reads back gives."""
    unread = [log for log in read.logs if log.nothing_kept]
    missing = [log for log in unread if together.log(log.application) is None]
    words = [f"This pull didn't read the {log.name} log: {log.problem or log.how_far} What's shown of it is from the saved reads." for log in unread if log not in missing]
    return (replace(together, logs=[*together.logs, *missing]) if missing else together), words


def swept_address(about_text: str) -> Optional[str]:
    """The address a saved read of every account was made from, by its `about.txt`: what makes that address's sweep done. "Every account" is said, never taken from an Accounts line left out."""
    note = About.of(about_text)
    return note.ip if note.every_account and note.ip else None


def csv_of(log: Log) -> str:
    """A log as its export: safe to open in a spreadsheet."""
    buffer = io.StringIO()
    csv.writer(buffer).writerows([[csv_safe(c) for c in row] for row in table(log.rows or [])])
    return buffer.getvalue()


def from_another_tenant(held: Pull, domains: Set[str]) -> bool:
    """A saved read's events have an actor, and none at these domains: one tenant's logs never go into another's incident. For an honest mix-up: a hostile file can write a row at the right domain."""
    seen = False
    for log in held.logs:
        for row in log.rows or ():
            if "@" in row.who:
                seen = True
                if row.who.rsplit("@", 1)[-1].lower() in domains:
                    return False
    return seen


def as_export(pull: Pull, now: Optional[datetime] = None) -> Optional[Export]:
    """A pull as Export all writes it, named for when, ready to keep: only what was read from Google, never what's shown from the evidence. None when no log of it was read."""
    moment = (now or datetime.now(timezone.utc)).astimezone()
    logs = [(log.file_name, csv_of(log)) for log in pull.logs if not log.nothing_kept]
    if pull.from_evidence or not logs:
        return None
    return export_of(about(pull, moment.strftime("%Y-%m-%d %H:%M:%S %Z")), logs, f"Incident logs {moment.strftime('%Y-%m-%d %H%M%S')}")


def about(pull: Pull, exported: str) -> str:
    """What was pulled: the CSVs alone don't say which window, accounts or address. A log that wasn't read is said, never an empty CSV that would pass as no events."""
    query = pull.query
    span = (f"From {report_time(query.start)} to {report_time(query.end or datetime.now(timezone.utc))}." if query.start
            else f"Over the {window_words(query.hours)} up to {report_time(pull.at)}." if pull.at else f"Over the last {window_words(query.hours)}.")
    lines = [f"Incident logs exported {exported}.", span, f"Accounts: {', '.join(query.users) if query.users else 'every account'}.",
             f"From IP address: {query.ip or 'any'}.", ""]
    for log in pull.logs:
        if not log.nothing_kept:
            stopped = "" if not log.capped else ((f"; read completely through {report_time(log.through)}, later events weren't read" if log.through
                                                  else "; older events past the cap weren't read") + (f"; past that, read only for {', '.join(sorted(log.covered))}" if log.covered else ""))
            shorter = f", the last {window_words(log.hours)} (the most it reads)" if log.hours < query.hours else ""
            unfiltered = "; its registrations, which name no address, not filtered by it" if log.application == "mobile" and query.ip else ""
            lines.append(f"{log.file_name}: the {log.name} log, {len(log.rows or [])} events{shorter}{stopped}{unfiltered}.")
    for log in pull.logs:
        if log.nothing_kept:
            lines.append(f"Not exported, not read: the {log.name} log: {log.problem or log.how_far}")
    return "\n".join(lines) + "\n"
