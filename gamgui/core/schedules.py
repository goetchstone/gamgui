"""Schedules (plan F7a): an audit run that says when it's due, run on the operator's click.

Nothing here runs anything. A cadence only decides when a run says it is due, on Home and on its own page; the run is
the operator's Run button, the same audit run as ever (``web/routes/audit_runs.py``), whose reads are recorded as usual.
There is no background runner, no write, no sensitive read and no Sheet export.

- **The cadence** is ``daily``, ``weekly`` or ``monthly``, per domain and per run (``ScheduleStore``).
- **Due.** A run *counts* when every step was read, at most with some accounts GAM named as unread (``answered``): a run
  stopped, or with a step that failed, doesn't. Never counted since the cadence was set: due now. Otherwise it is due from
  the local day one day, seven days or one calendar month after the day it last counted (the 31st into a shorter month is
  that month's last day), and stays due until a run counts again.
- **Kept runs.** A scheduled run that counts keeps its tables (the findings the page shows, never the steps' raw reads)
  on this Mac (``KeptRuns``): the last ``keep`` of them per run and domain, chosen by the operator (``KEEP_DEFAULT``, at
  most ``KEEP_CAP``). A table over ``KEEP_ROWS_CAP`` rows isn't kept. It is tenant data at rest, so it lives in a
  ``0700`` folder of the app's data folder, each file ``0600``, and is deleted when the run is no longer scheduled, on
  Delete, or when Setup removes the domain.
- **Changed since.** Each table of a finished scheduled run is compared with the same table in the newest earlier kept
  run that read it whole (and, for Inactive users, with the same window). A row is its cells: one with any cell changed is
  gone and new. A table unknown or partly unknown, now or then, is never compared (invariant 10): no change is said only
  of two whole tables.

Store rules (failure-log 2026-11-18, 2026-11-13): no file is the empty store; a schedules file the disk failed to read
is unknown, said, and never saved over; one that isn't a store is moved aside (``set_aside``) before an empty one starts.
A kept run that can't be read is listed as such, never read as none, and is never the run a table is compared with.
"""

from __future__ import annotations

import calendar
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .paths import app_data_dir

logger = logging.getLogger(__name__)

CADENCES = ("daily", "weekly", "monthly")
CADENCE_WORDS = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
KEEP_DEFAULT = 5          # runs kept when the operator doesn't say
KEEP_CAP = 12             # at most: a year of monthly runs
KEEP_ROWS_CAP = 50_000    # rows a kept table may hold; a larger one isn't kept, and can't be compared with later
FILE_VERSION = 1          # the schedules file's
KEPT_VERSION = 2          # a kept run's: 2 holds each table's columns with its rows, never in its first line (1 did)
TZ: Optional[tzinfo] = None   # the Mac's own time zone (tests set one)

_LOCK = threading.RLock()     # one load-change-save of the schedules file at a time in this process
# One keep, prune or delete of kept runs at a time in this process, each with its own look at the schedule: a run that
# finishes as it is unscheduled (or its domain removed) is never kept after its runs were deleted. Never held by a page.
_KEEP_LOCK = threading.RLock()
_MAX_TIME = 1e11              # seconds since 1970 a stored time may be (the year 5138): past it, the file isn't a store
_MAX_DAYS = 10_000            # a kept run's window, in days
_RUN_KEY = re.compile(r"[a-z][a-z_]{0,39}")
_KEPT_NAME = re.compile(r"^\d{13}-[0-9a-f]{8}\.jsonl$")
_TEMP_PREFIX = ".kept-"
_HEADER_CAP = 1_000_000   # bytes the first line of a kept run may take
_STALE_TEMP = 3600        # a half-written kept run older than this is a crash's leftover


class StoreProblem(Exception):
    """A schedule or a kept run couldn't be read or kept. ``str()`` says why, in words."""


def is_run_key(key: str) -> bool:
    """A key a schedule or a kept run may be filed under: never a path, never a newline."""
    return isinstance(key, str) and bool(_RUN_KEY.fullmatch(key))


def default_root() -> Path:
    return app_data_dir() / "schedules"


def _domain(domain: str) -> str:
    return (domain or "").strip().lower()


# --- the cadence and "due" ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Schedule:
    run: str
    cadence: str
    keep: int
    since: float          # when the cadence was set (wall clock)
    done: float = 0.0     # when a run that counts last finished; 0: none since it was set


def local_day(when: float) -> date:
    return datetime.fromtimestamp(when, TZ).date()


def next_day(last: date, cadence: str) -> date:
    """The local day a run is due again after it counted on ``last``."""
    if cadence == "daily":
        return last + timedelta(days=1)
    if cadence == "weekly":
        return last + timedelta(days=7)
    if cadence == "monthly":
        year, month = (last.year + 1, 1) if last.month == 12 else (last.year, last.month + 1)
        return date(year, month, min(last.day, calendar.monthrange(year, month)[1]))
    raise ValueError(f"not a cadence: {cadence!r}")


def due_day(schedule: Schedule) -> Optional[date]:
    """The local day ``schedule`` is next due; None when no run has counted since it was set, so it is due now."""
    return next_day(local_day(schedule.done), schedule.cadence) if schedule.done else None


def is_due(schedule: Schedule, now: float) -> bool:
    nxt = due_day(schedule)
    return nxt is None or local_day(now) >= nxt


def day_words(d: date) -> str:
    """"Mon 25 Nov 2026": the day, as every schedule line says it."""
    return f"{d:%a} {d.day} {d:%b %Y}"


def when_words(when: float) -> str:
    """"Mon 25 Nov 2026, 09:14": a kept run, by when it finished (local time)."""
    moment = datetime.fromtimestamp(when, TZ)
    return f"{day_words(moment.date())}, {moment:%H:%M}"


# --- the schedules file ---------------------------------------------------------------------------------------------
def _a_time(value: object) -> bool:
    """A stored time: a finite number of seconds from 1970 that a date can be made of."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and 0 <= value < _MAX_TIME)


def _schedule_from(run: str, raw: object) -> Schedule:
    if not isinstance(raw, dict) or not _RUN_KEY.fullmatch(run):
        raise ValueError("not a schedule")
    cadence, keep, since, done = raw.get("cadence"), raw.get("keep"), raw.get("since"), raw.get("done", 0)
    if cadence not in CADENCES or isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= KEEP_CAP:
        raise ValueError("not a schedule")
    times = [float(t) for t in (since, done) if isinstance(t, (int, float)) and _a_time(t)]
    if len(times) != 2:
        raise ValueError("not a schedule")
    return Schedule(run, str(cadence), int(keep), times[0], times[1])


_FILE_CAP = 1 << 20     # bytes a schedules file may hold: this app's is a few KB, so anything larger isn't one
_UNREAD = ("The schedules file couldn't be read just now, so what is due is unknown. Nothing was changed. Try again.")
_NOT_A_FILE = "{} isn't a file (it may be a link), so what is due is unknown and nothing is saved over it."


def _read_all(fd: int, cap: int) -> bytes:
    """What an open file holds, or b"" when it grew past ``cap`` while read (the size checked first was only what it said
    then)."""
    chunks: List[bytes] = []
    total = 0
    while chunk := os.read(fd, 1 << 16):
        total += len(chunk)
        if total > cap:
            return b""
        chunks.append(chunk)
    return b"".join(chunks)


class ScheduleStore:
    """Every domain's schedules, in one ``schedules.json`` (``0600``) in a ``0700`` folder of the app's data folder.
    Read again on every look and every change, so a change made from another window is never saved over."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.path = self.root / "schedules.json"
        self.set_aside: Optional[Path] = None     # where a file that wasn't a store went: said on the screen

    def _folder_fd(self, make: bool) -> Optional[int]:
        """The store's folder as a held descriptor, never through a link (O_NOFOLLOW with O_DIRECTORY), ours, `0700` when
        made. None when it doesn't exist and ``make`` is off. StoreProblem, in words, for a link, a file or another user's."""
        if make:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.root, 0o700)
            except FileExistsError:
                pass
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            if make:
                raise
            return None
        except OSError as exc:      # a link: on macOS O_NOFOLLOW with O_DIRECTORY says "not a directory"
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StoreProblem(f"{self.root} isn't a folder (it may be a link), so what is due is unknown and "
                                   "nothing is saved there.") from exc
            raise StoreProblem(_UNREAD) from exc
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid():
                raise StoreProblem(f"{self.root} belongs to another user, so what is due is unknown and nothing is saved "
                                   "there.")
            if make and stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _load(self) -> Dict[str, Dict[str, Schedule]]:
        folder = self._folder_fd(make=False)
        if folder is None:
            return {}
        try:
            try:   # not following a link, and not hanging on a FIFO: the regular-file check can only follow the open
                fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
            except FileNotFoundError:
                return {}
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise StoreProblem(_NOT_A_FILE.format(self.path)) from exc
                raise StoreProblem(_UNREAD) from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise StoreProblem(_NOT_A_FILE.format(self.path))
                raw = b"" if info.st_size > _FILE_CAP else _read_all(fd, _FILE_CAP)
            except OSError as exc:
                raise StoreProblem(_UNREAD) from exc
            finally:
                os.close(fd)
            try:
                if not raw:
                    raise ValueError("empty, or larger than a schedules file")
                data = json.loads(raw.decode("utf-8"))
                if (not isinstance(data, dict) or data.get("version") != FILE_VERSION
                        or not isinstance(data.get("domains"), dict)):
                    raise ValueError("not a schedules file")
                found: Dict[str, Dict[str, Schedule]] = {}
                for domain, runs in data["domains"].items():
                    if not isinstance(domain, str) or not domain or domain != _domain(domain) or not isinstance(runs, dict):
                        raise ValueError("not a schedules file")
                    found[domain] = {run: _schedule_from(run, s) for run, s in runs.items()}
                return found
            except (ValueError, TypeError, AttributeError, UnicodeDecodeError, OverflowError, RecursionError):
                return self._move_aside(folder)
        finally:
            os.close(folder)

    def _move_aside(self, folder: int) -> Dict[str, Dict[str, Schedule]]:
        """Keep a file that isn't a store under a new name in the held ``folder``, and start empty: a save then writes a
        new file instead of over it. One that can't be moved stays, unknown, and nothing is saved over it."""
        aside = f"{self.path.name}.unreadable-{int(time.time())}-{secrets.token_hex(3)}"
        try:
            os.replace(self.path.name, aside, src_dir_fd=folder, dst_dir_fd=folder)
        except OSError as exc:
            raise StoreProblem("The schedules file isn't one GamGUI can read, and it couldn't be moved aside, so what is "
                               "due is unknown and nothing is saved over it.") from exc
        self.set_aside = self.root / aside
        return {}

    def _save(self, every: Dict[str, Dict[str, Schedule]]) -> None:
        data = {"version": FILE_VERSION, "domains": {
            domain: {s.run: {"cadence": s.cadence, "keep": s.keep, "since": s.since, "done": s.done} for s in runs.values()}
            for domain, runs in sorted(every.items()) if runs}}
        body = json.dumps(data, indent=2).encode("utf-8")
        folder = self._folder_fd(make=True)
        assert folder is not None
        try:
            temp = f".schedules-{secrets.token_hex(6)}"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder)
            try:
                written = 0
                while written < len(body):
                    written += os.write(fd, body[written:])
                os.fsync(fd)      # whole on disk before it replaces the file: a crash never leaves it empty
                os.close(fd)
                fd = -1
                os.replace(temp, self.path.name, src_dir_fd=folder, dst_dir_fd=folder)
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp, dir_fd=folder)
                except OSError:
                    pass
                raise
        finally:
            os.close(folder)

    def schedules(self, domain: str) -> Dict[str, Schedule]:
        """``domain``'s schedules by run key. StoreProblem when they can't be told."""
        with _LOCK:
            return dict(self._load().get(_domain(domain), {}))

    def put(self, domain: str, run: str, cadence: str, keep: int, now: float) -> Optional[Schedule]:
        """Schedule ``run`` on ``domain``, or (``cadence`` "") stop scheduling it. A run already scheduled keeps when it
        last counted. StoreProblem, or OSError, when it couldn't be saved."""
        if not _RUN_KEY.fullmatch(run) or not _domain(domain):
            raise ValueError("not a run")
        if cadence and (cadence not in CADENCES or not 1 <= keep <= KEEP_CAP or not _a_time(now)):
            raise ValueError("not a schedule")
        with _LOCK:
            every = self._load()
            runs = every.setdefault(_domain(domain), {})
            old = runs.pop(run, None)
            new = Schedule(run, cadence, keep, old.since if old else now, old.done if old else 0.0) if cadence else None
            if new is not None:
                runs[run] = new
            if new is not None or old is not None:
                self._save(every)
            return new

    def mark_done(self, domain: str, run: str, when: float) -> None:
        """A run of ``run`` that counts finished at ``when``: it is due again a cadence on. Nothing when it isn't
        scheduled (any more)."""
        if not _a_time(when):
            raise ValueError("not a time")
        with _LOCK:
            every = self._load()
            old = every.get(_domain(domain), {}).get(run)
            if old is None:
                return
            # This run's time, even if an earlier one is later: that one came from a clock set ahead, and kept, it would hold
            # the schedule off for as long as the clock was wrong.
            every[_domain(domain)][run] = Schedule(old.run, old.cadence, old.keep, old.since, when)
            self._save(every)

    def forget_domain(self, domain: str) -> int:
        """Every schedule of ``domain`` (Setup removed it); how many there were."""
        with _LOCK:
            every = self._load()
            gone = every.pop(_domain(domain), None)
            if gone:
                self._save(every)
            return len(gone or {})


def _private_folder(folder: Path) -> None:
    """``folder`` made if need be, ``0700``; StoreProblem when it is a link, not a folder or another user's."""
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        folder.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreProblem(f"{folder} isn't a folder (it may be a link), so nothing is kept there.")
    if info.st_uid != os.getuid():
        raise StoreProblem(f"{folder} belongs to another user, so nothing is kept there.")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(folder, 0o700)


# --- kept runs ------------------------------------------------------------------------------------------------------
@dataclass
class KeptTable:
    key: str
    title: str
    columns: Tuple[str, ...]
    known: bool
    partial: bool
    count: int                                 # rows it had
    kept: bool                                 # False: over KEEP_ROWS_CAP, so its rows weren't kept
    rows: Optional[List[Tuple[str, ...]]] = None   # loaded only by ``KeptRuns.load``


@dataclass
class KeptRun:
    id: str
    when: float = 0.0          # when it finished (wall clock)
    days: int = 0              # Inactive users: its window
    tables: List[KeptTable] = field(default_factory=list)
    problem: str = ""          # why it couldn't be read, in words: then nothing else here is known

    def table(self, key: str) -> Optional[KeptTable]:
        return next((t for t in self.tables if t.key == key), None)


def _cells(row: Iterable[object]) -> Tuple[str, ...]:
    return tuple("" if c is None else str(c) for c in row)


class KeptRuns:
    """The kept runs: ``<root>/<domain's hash>/<run key>/<id>.jsonl``, folders ``0700``, files ``0600``. A file is a header
    line (the run, when, its window, each table's title, columns and count) and one line per kept table's rows; listing
    reads the header alone. Written under a temporary name and renamed into place, so only a whole run is ever listed.
    Named by a sequence (`<13 digits>-<8 hex>`), so newest is the last kept, whatever the clock said."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else default_root() / "kept"

    def _top(self, domain: str) -> Path:
        if not _domain(domain):
            raise ValueError("not a domain")
        return self.root / hashlib.sha256(_domain(domain).encode()).hexdigest()[:24]

    def _folder(self, domain: str, run: str, make: bool) -> Optional[Path]:
        """The run's folder on ``domain``. Every folder from the store's own down is a real folder, never a link: a link
        anywhere in it would lead a read, a keep or a delete outside the store. None when one doesn't exist yet and
        ``make`` is off."""
        if not _RUN_KEY.fullmatch(run):
            raise ValueError("not a run")
        folder = self._top(domain) / run
        chain = (self.root.parent, self.root, folder.parent, folder)
        if make:
            for level in chain:
                _private_folder(level)
            return folder
        return folder if all(_existing_folder(level) for level in chain) else None

    def _files(self, folder: Optional[Path]) -> List[Path]:
        if folder is None:
            return []
        try:
            return sorted((p for p in folder.iterdir() if _KEPT_NAME.match(p.name) and not p.is_symlink()), reverse=True)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreProblem("The kept runs' folder couldn't be read just now. Try again.") from exc

    # --- keep ---------------------------------------------------------------------------------------------------------
    def keep(self, domain: str, run: str, when: float, days: int, findings: Sequence) -> KeptRun:
        """Keep a run's findings (``core.audit_runs.Finding``) whole. StoreProblem or OSError when it couldn't be."""
        folder = self._folder(domain, run, make=True)
        assert folder is not None
        # Named by the order kept, not by the clock: a run kept while the Mac's clock was wrong would otherwise sort
        # ahead of every later one, be compared with forever and outlive them all in a prune.
        last = max((int(p.name[:13]) for p in self._files(folder)), default=0)
        kept_id = f"{last + 1:013d}-{secrets.token_hex(4)}"
        tables = [KeptTable(f.key, f.title, tuple(f.columns), bool(f.known), bool(f.partial), len(f.rows),
                            bool(f.known) and len(f.rows) <= KEEP_ROWS_CAP) for f in findings]
        # The first line, which listing reads alone, holds no columns: a read GAM flattens has as many as its widest record
        # needs (13 a member for every member of the largest shared drive), which would make it too long to read back.
        header = {"version": KEPT_VERSION, "id": kept_id, "domain": _domain(domain), "run": run, "when": when,
                  "days": int(days),
                  "tables": [{"key": t.key, "title": t.title, "known": t.known, "partial": t.partial, "count": t.count,
                              "kept": t.kept} for t in tables]}
        lines = [json.dumps(header, ensure_ascii=False)]
        lines += [json.dumps({"table": f.key, "columns": list(t.columns), "rows": [list(_cells(r)) for r in f.rows]},
                             ensure_ascii=False)
                  for f, t in zip(findings, tables, strict=True) if t.kept]
        temp = folder / f"{_TEMP_PREFIX}{kept_id}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            data = ("\n".join(lines) + "\n").encode("utf-8")
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.rename(temp, folder / f"{kept_id}.jsonl")
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return KeptRun(kept_id, when, int(days), tables)

    def prune(self, domain: str, run: str, keep: int) -> int:
        """Delete all but the newest ``keep`` kept runs (and a crash's half-written leftovers); how many went."""
        folder = self._folder(domain, run, make=False)
        gone = 0
        for path in self._files(folder)[max(keep, 0):]:
            gone += _unlink(path)
        if folder is not None:
            for leftover in folder.glob(f"{_TEMP_PREFIX}*"):
                try:
                    if time.time() - os.lstat(leftover).st_mtime > _STALE_TEMP:
                        _unlink(leftover)
                except OSError:
                    pass
        return gone

    def delete_all(self, domain: str, run: str) -> int:
        """Every kept run of ``run`` on ``domain``, and its folder; how many runs went."""
        folder = self._folder(domain, run, make=False)
        if folder is None:
            return 0
        gone = 0
        for path in folder.iterdir():
            if _KEPT_NAME.match(path.name):
                gone += _unlink(path)
            elif path.name.startswith(_TEMP_PREFIX):
                _unlink(path)
        try:
            folder.rmdir()
        except OSError:
            pass
        return gone

    def run_keys(self, domain: str) -> List[str]:
        """The runs ``domain`` has a kept-runs folder for, whatever they hold. StoreProblem when that can't be told."""
        top = self._top(domain)
        if not all(_existing_folder(level) for level in (self.root.parent, self.root, top)):
            return []
        try:
            return sorted(p.name for p in top.iterdir() if _RUN_KEY.fullmatch(p.name) and not p.is_symlink())
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreProblem("The kept runs' folder couldn't be read just now. Try again.") from exc

    def forget_domain(self, domain: str) -> int:
        """Every kept run of ``domain``, of every run it has a folder for (Setup removed it); how many went."""
        top = self._top(domain)
        runs = self.run_keys(domain)
        if not runs and not _existing_folder(top):
            return 0
        gone = sum(self.delete_all(domain, run) for run in runs)
        try:
            top.rmdir()
        except OSError:
            pass
        return gone

    # --- read ---------------------------------------------------------------------------------------------------------
    def runs(self, domain: str, run: str) -> List[KeptRun]:
        """The kept runs, newest first, their headers only. One that can't be read is listed with ``problem``."""
        return [self._read(path, domain, run, whole=False) for path in self._files(self._folder(domain, run, make=False))]

    def load(self, domain: str, run: str, kept_id: str) -> KeptRun:
        """The kept run ``kept_id``, rows included. Picks among the files the folder lists, never a path built from
        ``kept_id``."""
        path = next((p for p in self._files(self._folder(domain, run, make=False)) if p.stem == kept_id), None)
        if path is None:
            return KeptRun(kept_id, problem="It is no longer kept.")
        return self._read(path, domain, run, whole=True)

    def _read(self, path: Path, domain: str, run: str, whole: bool) -> KeptRun:
        kept_id = path.stem
        try:   # not following a link, and not hanging on a FIFO: the regular-file check can only follow the open
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return KeptRun(kept_id, problem="It isn't a file.")
                first = f.readline(_HEADER_CAP)
                rest = f.read() if whole else b""
        except OSError:
            return KeptRun(kept_id, problem="It couldn't be read just now.")
        try:
            head = json.loads(first.decode("utf-8"))
            if (not isinstance(head, dict) or head.get("version") not in (1, KEPT_VERSION) or head.get("id") != kept_id
                    or head.get("domain") != _domain(domain) or head.get("run") != run):
                raise ValueError("not this run")
            when, days = head["when"], head["days"]
            if not _a_time(when) or isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= _MAX_DAYS:
                raise ValueError("not this run")
            found = KeptRun(kept_id, float(when), days)
            for t in head["tables"]:   # version 1 kept the columns here; 2 keeps them with the rows
                columns = tuple(str(c) for c in t["columns"]) if head["version"] == 1 else ()
                found.tables.append(KeptTable(str(t["key"]), str(t["title"]), columns,
                                              t["known"] is True, t["partial"] is True, int(t["count"]), t["kept"] is True))
            if whole:
                rows: Dict[str, Tuple[Optional[Tuple[str, ...]], List[Tuple[str, ...]]]] = {}
                for line in rest.decode("utf-8").split("\n"):
                    if line:
                        body = json.loads(line)
                        cols = tuple(str(c) for c in body["columns"]) if "columns" in body else None
                        rows[str(body["table"])] = (cols, [_cells(r) for r in body["rows"] if isinstance(r, list)])
                for t in found.tables:
                    if t.kept:
                        if t.key not in rows or len(rows[t.key][1]) != t.count:
                            raise ValueError("a table isn't whole")
                        cols, t.rows = rows[t.key]
                        if cols is not None:
                            t.columns = cols
                        elif head["version"] != 1:
                            raise ValueError("a table's columns aren't kept")
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError, OverflowError, RecursionError):
            return KeptRun(kept_id, problem="It isn't whole.")
        return found


def _existing_folder(path: Path) -> bool:
    """True for a real folder, False when nothing is there; StoreProblem for anything else (a link, a file)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise StoreProblem(f"{path} isn't a folder (it may be a link), so the kept runs in it are left alone.")
    return True


def _unlink(path: Path) -> int:
    try:
        os.unlink(path)
        return 1
    except FileNotFoundError:
        return 0


# --- changed since --------------------------------------------------------------------------------------------------
@dataclass
class Change:
    """What one table holds that the same table in an earlier kept run didn't (``new``), and the other way (``gone``).
    ``why`` says, in words, why it wasn't compared: then ``new`` and ``gone`` say nothing."""

    key: str
    since: float = 0.0
    new: List[Tuple[str, ...]] = field(default_factory=list)
    gone: List[Tuple[str, ...]] = field(default_factory=list)
    why: str = ""
    columns: Tuple[str, ...] = ()   # the columns ``new`` and ``gone`` are in, when not the table's own (see ``_aligned``)
    left_out: Tuple[str, ...] = ()  # columns that move on their own, left out of the comparison (Finding.compare_without)

    @property
    def compared(self) -> bool:
        return not self.why


NOT_KNOWN_NOW = "Unknown this time, so it isn't compared."
PART_UNKNOWN_NOW = "Part of this table is unknown this time, so what's new or gone can't be told."
NOTHING_BEFORE = "No earlier run that read this table whole is kept, so there's nothing to compare it with yet."
KEPT_RUN_UNREAD = ("A run kept before couldn't be read, so this table isn't compared. Delete the kept runs if it stays "
                   "that way.")
NOT_ANSWERED = "A step of this run has no answer, so its tables aren't compared: one may hold \"unknown\" where it had a value."


def _aligned(now_cols: Sequence[str], now_rows: List[Tuple[str, ...]], then_cols: Sequence[str],
             then_rows: List[Tuple[str, ...]]) -> Tuple[Tuple[str, ...], List[Tuple[str, ...]], List[Tuple[str, ...]]]:
    """Both tables in one set of columns: now's, then any only the earlier one had, a cell a table lacks being "". A read
    whose JSON GAM flattens gets as many columns as its widest record needs (`permissions.5.role` once a drive has a sixth
    member), so its columns can differ from run to run while the rows they share don't."""
    now_cols, then_cols = tuple(now_cols), tuple(then_cols)
    if now_cols == then_cols:
        return now_cols, now_rows, then_rows
    union = now_cols + tuple(c for c in then_cols if c not in now_cols)

    def project(cols: Tuple[str, ...], rows: List[Tuple[str, ...]]) -> List[Tuple[str, ...]]:
        return [tuple(dict(zip(cols, r, strict=False)).get(c, "") for c in union) for r in rows]
    return union, project(now_cols, now_rows), project(then_cols, then_rows)


def _difference(rows: Sequence[Tuple[str, ...]], minus: Sequence[Tuple[str, ...]]) -> List[Tuple[str, ...]]:
    """``rows`` not in ``minus``, as a multiset, in ``rows``' order."""
    left = Counter(minus)
    out = []
    for row in rows:
        if left[row]:
            left[row] -= 1
        else:
            out.append(row)
    return out


@dataclass(frozen=True)
class Due:
    due: bool
    headline: str     # "License usage is due", or "Weekly, next due Mon 25 Nov 2026"
    detail: str = ""  # why it's due, a sentence; "" when it isn't

    @property
    def line(self) -> str:
        return f"{self.headline}. {self.detail}".strip()


def due_words(title: str, schedule: Schedule, now: float) -> Due:
    """Whether ``schedule`` is due at ``now``, in the words Home and the run's own page say it."""
    nxt = due_day(schedule)
    cadence = CADENCE_WORDS[schedule.cadence]
    if schedule.done > now:     # counted at a time still to come: the Mac's clock was ahead then, or is behind now
        return Due(True, f"{title} is due", f"{cadence}; its last run is dated after now, so when it's next due can't be "
                                            "told. Check the Mac's clock.")
    if nxt is None:
        return Due(True, f"{title} is due", f"{cadence}; no run of it has read every step since it was scheduled.")
    if local_day(now) >= nxt:
        return Due(True, f"{title} is due",
                   f"{cadence}; its last run that read every step was on {day_words(local_day(schedule.done))}.")
    return Due(False, f"{cadence}, next due {day_words(nxt)}")


@dataclass
class AfterRun:
    changes: Dict[str, Change]
    note: str = ""          # what happened to the keep, in words; "" when the run isn't scheduled
    problem: bool = False   # the note says something went wrong


KEPT_UNREAD = "The runs kept before couldn't be read just now, so this table isn't compared."


def after_run(store: ScheduleStore, kept: KeptRuns, domain: str, run: str, findings: Sequence, days: int,
              answered: bool, when: float) -> AfterRun:
    """A finished run, on the domain it read: when ``run`` is scheduled there, each table against the last kept run that
    read it, then this run kept (when ``answered``), the oldest beyond its count deleted, and the schedule told it counted.
    Nothing is kept or counted for a run that isn't scheduled, or whose schedule can't be read."""
    try:
        schedule = store.schedules(domain).get(run)
    except StoreProblem as exc:
        return AfterRun({}, f"{exc} So this run isn't kept and isn't compared.", True)
    if schedule is None:
        return AfterRun({})

    def load(kept_id: str) -> KeptRun:
        try:
            return kept.load(domain, run, kept_id)
        except StoreProblem as exc:
            return KeptRun(kept_id, problem=str(exc))

    if not answered:   # a table built on a step with no answer can hold "unknown" cells with no partial flag: never compared
        return AfterRun({f.key: Change(f.key, why=NOT_ANSWERED) for f in findings},
                        "Not kept, and it doesn't count as its scheduled run: a step has no answer.", True)
    try:
        changes = changes_since(findings, days, kept.runs(domain, run), load)
    except StoreProblem:
        changes = {f.key: Change(f.key, why=KEPT_UNREAD if f.known and not f.partial else
                                 (NOT_KNOWN_NOW if not f.known else PART_UNKNOWN_NOW)) for f in findings}
    with _KEEP_LOCK:
        try:   # looked at again: it may have been unscheduled, or its domain removed, while this run read
            schedule = store.schedules(domain).get(run)
        except StoreProblem as exc:
            return AfterRun(changes, f"{exc} So this run isn't kept.", True)
        if schedule is None:
            return AfterRun(changes, "Not kept: it stopped being scheduled while it ran.")
        after = AfterRun(changes, f"Kept on this Mac, as one of the last {schedule.keep} "
                                  f"run{'s' if schedule.keep != 1 else ''} it keeps.")
        try:
            kept.keep(domain, run, when, days, findings)
            kept.prune(domain, run, schedule.keep)
        except StoreProblem as exc:
            after.note, after.problem = f"It couldn't be kept on this Mac: {exc}", True
        except OSError as exc:
            logger.warning("keeping a scheduled run failed", exc_info=exc)
            after.note = ("It couldn't be kept on this Mac, so the next run can't say what changed since this one. "
                          "Check the disk has room.")
            after.problem = True
        try:
            store.mark_done(domain, run, when)
        except (OSError, StoreProblem) as exc:
            logger.warning("marking a scheduled run done failed", exc_info=exc)
            after.note += " It counts as its scheduled run, but that couldn't be saved, so it may still say it's due."
            after.problem = True
    return after


def set_schedule(store: ScheduleStore, kept: KeptRuns, domain: str, run: str, cadence: str, keep: int,
                 now: float) -> Tuple[Optional[Schedule], Optional[int]]:
    """Save ``run``'s schedule on ``domain`` (``cadence`` "": stop scheduling it), then make what it keeps match: its newest
    ``keep``, or none. The saved schedule, and how many kept runs were deleted (None: they couldn't all be). StoreProblem,
    OSError or ValueError when the schedule itself couldn't be saved; then nothing was deleted."""
    with _KEEP_LOCK:
        saved = store.put(domain, run, cadence, keep, now)
        try:
            return saved, (kept.prune(domain, run, saved.keep) if saved else kept.delete_all(domain, run))
        except (StoreProblem, OSError) as exc:
            logger.warning("deleting kept runs failed", exc_info=exc)
            return saved, None


def delete_kept(kept: KeptRuns, domain: str, run: str) -> int:
    """Every kept run of ``run`` on ``domain``; how many went. Its schedule stays."""
    with _KEEP_LOCK:
        return kept.delete_all(domain, run)


def forget_domain(store: ScheduleStore, kept: KeptRuns, domain: str) -> Tuple[int, int]:
    """Every schedule of ``domain`` and every run they kept (Setup removed it): how many of each went. The schedules go
    first, so a run finishing meanwhile finds itself unscheduled and keeps nothing."""
    with _KEEP_LOCK:
        return store.forget_domain(domain), kept.forget_domain(domain)


def changes_since(findings: Sequence, days: int, earlier: Sequence[KeptRun],
                  load: Callable[[str], KeptRun]) -> Dict[str, Change]:
    """Each known, whole table in ``findings`` against the newest of ``earlier`` (newest first, headers) that kept the same
    table whole, read with the same window ``days``. ``load`` reads a kept run's rows; one that can't be read is passed over."""
    loaded: Dict[str, KeptRun] = {}
    out: Dict[str, Change] = {}
    for f in findings:
        if not f.known:
            out[f.key] = Change(f.key, why=NOT_KNOWN_NOW)
            continue
        if f.partial:
            out[f.key] = Change(f.key, why=PART_UNKNOWN_NOW)
            continue
        now_rows = [_cells(r) for r in f.rows]
        change = Change(f.key, why=NOTHING_BEFORE)
        for head in earlier:
            if head.problem:     # what it held can't be told: it may be the run to compare with, so nothing older is
                change = Change(f.key, why=KEPT_RUN_UNREAD)
                break
            t = head.table(f.key)
            if head.days != days or t is None or not t.known or t.partial or not t.kept:
                continue
            if head.id not in loaded:
                loaded[head.id] = load(head.id)
            whole = loaded[head.id].table(f.key)
            if loaded[head.id].problem or whole is None or whole.rows is None:
                change = Change(f.key, why=f"The run kept on {when_words(head.when)} couldn't be read whole, so this "
                                           "table isn't compared.")
                break
            cols, now_cmp, then_cmp = _aligned(f.columns, now_rows, whole.columns, whole.rows)
            skip = set(getattr(f, "compare_without", ()) or ())
            left_out = tuple(c for c in cols if c in skip)
            if left_out:   # a column that moves on its own (a device's last sync) would make every row look changed
                keep = [i for i, c in enumerate(cols) if c not in skip]
                cols = tuple(cols[i] for i in keep)
                now_cmp = [tuple(r[i] for i in keep) for r in now_cmp]
                then_cmp = [tuple(r[i] for i in keep) for r in then_cmp]
            change = Change(f.key, head.when, _difference(now_cmp, then_cmp), _difference(then_cmp, now_cmp),
                            columns=cols if cols != tuple(f.columns) else (), left_out=left_out)
            break
        out[f.key] = change
    return out
