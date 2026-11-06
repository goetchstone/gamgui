"""One pull of the incident logs: each log read whole, oldest first, bounded, and never on the event loop.

Most logs come back short of the cap on one read of the window. One at the cap is read a day at a time from the window's start, each day read
back from its end until it's whole, an event read twice kept once. Past the bounds, a read that fails, or a day still at its cap, it stops at that
day's start, so what's unread is the newest: the attack is the oldest part, and the newest is the easiest to read again. A failure is said,
never a part passed as the whole. Ported from SwiftGamGui's ``IncidentLogs.swift``.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .logs import CAP, GMAIL_MAX_HOURS, NAMES, READS_PER_DAY, READS_PER_LOG, Row, addresses as addresses_in, capped as is_capped, newest_first, parse_time, report_time, window_words
from .reads import in_words

AT_ONCE = 3     # at most this many reads at once
MOST_ACCOUNTS = 20

Read = Callable[[Sequence[str], str, str], Awaitable[List[Row]]]


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

    @classmethod
    def read(cls, application: str, rows: List[Row], hours: int, capped: bool = False, through: Optional[datetime] = None) -> "Log":
        return cls(application, rows, "", hours, capped, through, sum(r.flagged for r in rows), len({r.who for r in rows}))

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
        if self.through:
            return f"Read completely through {report_time(self.through)}: later events weren't read."
        return f"Older events past the cap ({CAP} an account a read) weren't read."


@dataclass
class Pull:
    query: Query
    logs: List[Log] = field(default_factory=list)   # in the query's order
    ended: bool = False
    run: str = field(default_factory=lambda: secrets.token_hex(8))
    at: Optional[datetime] = None       # the window's end
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
            log = await asyncio.to_thread(Log.read, application, rows, hours, through is not None, through)
    except Exception as exc:  # noqa: BLE001 - said beside the rest, whatever it was
        log = Log(application, None, in_words(exc), hours)
    state.state, state.failed = "done", log.rows is None
    state.events = len(log.rows or [])
    return log


async def pull_logs(conn: Any, query: Query, pull: Pull, progress: Dict[str, Progress], cap: int = CAP, now: Optional[datetime] = None,
                    stopped: Callable[[], bool] = lambda: False, finished: Callable[[], None] = lambda: None) -> None:
    """Every log of ``query`` into ``pull``, at most ``AT_ONCE`` at a time, each as it finishes, in the query's order."""
    at = now or datetime.now(timezone.utc)
    pull.at = at
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


def about(pull: Pull, exported: str) -> str:
    """What was pulled: the CSVs alone don't say which window, accounts or address. A log that wasn't read is said, never an empty CSV that would pass as no events."""
    query = pull.query
    span = (f"From {report_time(query.start)} to {report_time(query.end or datetime.now(timezone.utc))}." if query.start
            else f"Over the {window_words(query.hours)} up to {report_time(pull.at)}." if pull.at else f"Over the last {window_words(query.hours)}.")
    lines = [f"Incident logs exported {exported}.", span, f"Accounts: {', '.join(query.users) if query.users else 'every account'}.",
             f"From IP address: {query.ip or 'any'}.", ""]
    for log in pull.logs:
        if not log.nothing_kept:
            stopped = "" if not log.capped else (f"; read completely through {report_time(log.through)}, later events weren't read" if log.through
                                                 else "; older events past the cap weren't read")
            shorter = f", the last {window_words(log.hours)} (the most it reads)" if log.hours < query.hours else ""
            unfiltered = "; its registrations, which name no address, not filtered by it" if log.application == "mobile" and query.ip else ""
            lines.append(f"{log.file_name}: the {log.name} log, {len(log.rows or [])} events{shorter}{stopped}{unfiltered}.")
    for log in pull.logs:
        if log.nothing_kept:
            lines.append(f"Not exported, not read: the {log.name} log: {log.problem or log.how_far}")
    return "\n".join(lines) + "\n"
