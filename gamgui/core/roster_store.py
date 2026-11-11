"""The roster snapshot kept on this Mac (plan 2026-11-11, C2.5).

A snapshot is student data: names and addresses of every teacher and student in a district, and who is in which class.
So it is kept the way this app keeps anything sensitive on disk (SECURITY.md):

- **One per domain**, a SQLite file in ``<app data>/roster/``: the folder `0700` and opened without following links, the
  file created `0600` under a temporary name and renamed into place, so a half-written snapshot never replaces a whole one.
- **For a stated number of days** (``KEEP_DAYS``). Past it, the next look deletes it. **Purge** deletes it at once.
- **Never the Directory.** The user cache stays in memory; this holds only what the SIS exported.

Nothing here talks to Google. The snapshot is what the planner (C3) compares with Classroom.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .paths import app_data_dir
from .roster import Class, Course, Enrollment, Issue, Person, School, Snapshot, Term

KEEP_DAYS = 14
_LOCK = threading.RLock()     # one save, read or purge at a time: two saves of one domain once swept each other's temp
_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schools (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE terms (id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, start TEXT NOT NULL, "end" TEXT NOT NULL);
CREATE TABLE courses (id TEXT PRIMARY KEY, title TEXT NOT NULL, code TEXT NOT NULL, school_id TEXT NOT NULL);
CREATE TABLE classes (id TEXT PRIMARY KEY, title TEXT NOT NULL, course_id TEXT NOT NULL, school_id TEXT NOT NULL,
                      term_ids TEXT NOT NULL, code TEXT NOT NULL, location TEXT NOT NULL);
CREATE TABLE people (id TEXT PRIMARY KEY, role TEXT NOT NULL, email TEXT NOT NULL, given TEXT NOT NULL, family TEXT NOT NULL);
CREATE TABLE enrollments (class_id TEXT NOT NULL, person_id TEXT NOT NULL, role TEXT NOT NULL, "primary" INTEGER NOT NULL);
CREATE TABLE issues (code TEXT NOT NULL, file TEXT NOT NULL, row INTEGER NOT NULL, source_id TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE issue_counts (code TEXT PRIMARY KEY, count INTEGER NOT NULL);
"""


class StoreError(Exception):
    """The snapshot couldn't be kept or read. ``str()`` says why, in words."""


def default_root() -> Path:
    return app_data_dir() / "roster"


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RosterStore:
    """The roster snapshots kept in ``root``, one per domain."""

    def __init__(self, root: Optional[Path] = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.root = Path(root) if root else default_root()
        self.now = now

    # --- the folder and the file --------------------------------------------------------------------------------------
    def _folder(self, make: bool) -> Optional[Path]:
        """The store's folder, `0700`, refused if it's a link or not a folder. None when it doesn't exist and ``make`` is off."""
        try:
            info = os.lstat(self.root)
        except FileNotFoundError:
            if not make:
                return None
            self.root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.root, 0o700)
            except FileExistsError:
                pass
            info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode):
            raise StoreError(f"{self.root} isn't a folder (it may be a link), so no roster is kept there.")
        if info.st_uid != os.getuid():
            raise StoreError(f"{self.root} belongs to another user, so no roster is kept there.")
        os.chmod(self.root, 0o700)       # lstat said a folder, not a link
        return self.root

    def path(self, domain: str) -> Path:
        """The domain's snapshot file: named by a hash, so a domain's spelling never becomes a path."""
        key = hashlib.sha256(domain.strip().lower().encode()).hexdigest()[:24]
        return self.root / f"{key}.sqlite"

    # --- keep, read, purge --------------------------------------------------------------------------------------------
    def save(self, domain: str, snap: Snapshot, label: str, sha256: str) -> Dict[str, object]:
        """Keep ``snap`` as the domain's roster, replacing any earlier one. Returns its metadata. One save or purge at a
        time (``_LOCK``): two saves of one domain once swept each other's temp file."""
        with _LOCK:
            return self._save(domain, snap, label, sha256)

    def _save(self, domain: str, snap: Snapshot, label: object, sha256: object) -> Dict[str, object]:
        folder = self._folder(make=True)
        assert folder is not None
        final = self.path(domain)
        temp = final.with_name(f".saving-{os.getpid()}-{final.name}")
        try:
            self._sweep(final)               # a save a quit cut off left its whole snapshot here: never kept
        except OSError as exc:
            raise StoreError(f"Couldn't keep the roster on this Mac ({exc}).") from exc
        now = self.now()
        meta = {"source": snap.source, "label": str(label or "")[:200], "sha256": str(sha256 or ""),
                "imported_at": _utc(now), "expires_at": _utc(now + timedelta(days=KEEP_DAYS)), "counts": snap.counts,
                "others": snap.others, "domain": domain.strip().lower()}
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            os.close(fd)
            con = sqlite3.connect(temp)
            try:
                con.executescript(_SCHEMA)
                con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
                con.executemany("INSERT INTO schools VALUES (?, ?)", [(s.id, s.name) for s in snap.schools.values()])
                con.executemany("INSERT INTO terms VALUES (?, ?, ?, ?, ?)",
                                [(t.id, t.title, t.kind, t.start, t.end) for t in snap.terms.values()])
                con.executemany("INSERT INTO courses VALUES (?, ?, ?, ?)",
                                [(c.id, c.title, c.code, c.school_id) for c in snap.courses.values()])
                con.executemany("INSERT INTO classes VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [(c.id, c.title, c.course_id, c.school_id, json.dumps(list(c.term_ids)), c.code, c.location)
                                 for c in snap.classes.values()])
                con.executemany("INSERT INTO people VALUES (?, ?, ?, ?, ?)",
                                [(p.id, p.role, p.email, p.given, p.family) for p in snap.people.values()])
                con.executemany("INSERT INTO enrollments VALUES (?, ?, ?, ?)",
                                [(e.class_id, e.person_id, e.role, int(e.primary)) for e in snap.enrollments])
                con.executemany("INSERT INTO issues VALUES (?, ?, ?, ?, ?)",
                                [(i.code, i.file, i.row, i.source_id, i.detail) for i in snap.issue_sample])
                con.executemany("INSERT INTO issue_counts VALUES (?, ?)", list(snap.issue_counts.items()))
                con.commit()
            finally:
                con.close()
            os.chmod(temp, 0o600)
            os.replace(temp, final)
        except Exception as exc:  # noqa: BLE001 — OSError, sqlite3.Error, an unencodable string: the temp file never stays
            for leftover in (temp, Path(f"{temp}-journal")):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
            raise StoreError(f"Couldn't keep the roster on this Mac ({exc}).") from exc
        return meta

    def _sweep(self, final: Path) -> None:
        """Delete every half-written save of ``final``, whichever process wrote it, and its journals. A link is removed as a
        link, never followed; a folder by that name is left (it can't hold a save, and refusing on it once blocked a purge)."""
        prefix, suffix = ".saving-", f"-{final.name}"
        for name in os.listdir(self.root):
            stem = name.removesuffix("-journal").removesuffix("-wal").removesuffix("-shm")
            if stem.startswith(prefix) and stem.endswith(suffix):
                path = self.root / name
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    continue
                os.unlink(path)

    def _open(self, domain: str) -> Optional[sqlite3.Connection]:
        if self._folder(make=False) is None:
            return None
        path = self.path(domain)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise StoreError("The kept roster isn't a plain file, so it isn't read.")
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise StoreError(f"Couldn't read the kept roster ({exc}).") from exc

    def meta(self, domain: str) -> Optional[Dict[str, object]]:
        """The kept roster's metadata, or None when there's none. One past its days is deleted here, and reads as none. One
        that can't be read is deleted too, and said: its days can't be told, and student data never stays unbounded."""
        with _LOCK:
            return self._meta(domain)

    def _meta(self, domain: str) -> Optional[Dict[str, object]]:
        con = self._open(domain)
        if con is None:
            return None
        try:
            meta = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM meta")}
        except (sqlite3.Error, ValueError) as exc:
            con.close()
            self._purge(domain)
            raise StoreError(f"The kept roster couldn't be read ({exc}), so it was deleted. Upload the export again.") from exc
        finally:
            con.close()
        if self._expired(meta):
            self._purge(domain)
            return None
        return meta

    def _expired(self, meta: Dict[str, object]) -> bool:
        """Past its days, or its dates can't be read, or it says it was imported in the future: then it's deleted. Fails
        closed: the snapshot is student data, and a date it can't trust never keeps it. A stored expiry is never trusted
        past ``imported_at + KEEP_DAYS``."""
        try:
            imported = datetime.strptime(str(meta["imported_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            expires = datetime.strptime(str(meta["expires_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            return True
        now = self.now()
        return now >= min(expires, imported + timedelta(days=KEEP_DAYS)) or imported > now + timedelta(days=1)

    def load(self, domain: str) -> Optional[Snapshot]:
        """The kept roster, whole, or None when there's none (or it has expired)."""
        with _LOCK:
            return self._load(domain)

    def _load(self, domain: str) -> Optional[Snapshot]:
        meta = self._meta(domain)
        if meta is None:
            return None
        con = self._open(domain)
        if con is None:
            return None
        try:
            others = meta.get("others", 0)
            snap = Snapshot(source=str(meta.get("source", "")), others=others if isinstance(others, int) else 0)
            snap.schools = {r[0]: School(*r) for r in con.execute("SELECT id, name FROM schools")}
            snap.terms = {r[0]: Term(*r) for r in con.execute('SELECT id, title, kind, start, "end" FROM terms')}
            snap.courses = {r[0]: Course(*r) for r in con.execute("SELECT id, title, code, school_id FROM courses")}
            snap.classes = {r[0]: Class(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5], r[6]) for r in
                            con.execute("SELECT id, title, course_id, school_id, term_ids, code, location FROM classes")}
            snap.people = {r[0]: Person(*r) for r in con.execute("SELECT id, role, email, given, family FROM people")}
            snap.enrollments = [Enrollment(r[0], r[1], r[2], bool(r[3])) for r in
                                con.execute('SELECT class_id, person_id, role, "primary" FROM enrollments')]
            snap.issue_sample = [Issue(*r) for r in con.execute("SELECT code, file, row, source_id, detail FROM issues")]
            snap.issue_counts = dict(con.execute("SELECT code, count FROM issue_counts"))
            return snap
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StoreError(f"Couldn't read the kept roster ({exc}).") from exc
        finally:
            con.close()

    def issues(self, domain: str) -> Optional[Tuple[Dict[str, int], List[Issue]]]:
        """The kept roster's issue counts and kept rows, without the rest of it: what the import page shows on every view
        (loading every person and enrolment to show them cost seconds on a district's roster). None when there's none."""
        with _LOCK:
            if self._meta(domain) is None:
                return None
            con = self._open(domain)
            if con is None:
                return None
            try:
                counts = dict(con.execute("SELECT code, count FROM issue_counts"))
                rows = [Issue(*r) for r in con.execute("SELECT code, file, row, source_id, detail FROM issues")]
                return counts, rows
            except (sqlite3.Error, TypeError) as exc:
                raise StoreError(f"Couldn't read the kept roster ({exc}).") from exc
            finally:
                con.close()

    def purge(self, domain: str) -> bool:
        """Delete the domain's kept roster, and any half-written save of it. True when there was one."""
        with _LOCK:
            return self._purge(domain)

    def _purge(self, domain: str) -> bool:
        if self._folder(make=False) is None:
            return False
        path = self.path(domain)
        found = True
        try:
            os.unlink(path)                  # the snapshot first: nothing found after it may keep it
        except FileNotFoundError:
            found = False
        except OSError as exc:
            raise StoreError(f"Couldn't delete the kept roster ({exc}).") from exc
        for extra in ("-journal", "-wal", "-shm"):
            try:
                os.unlink(f"{path}{extra}")
            except OSError:
                pass
        try:
            self._sweep(path)                # a half-written save holds the same student data
        except OSError as exc:
            raise StoreError(f"Deleted the kept roster, but not a half-written copy of it ({exc}).") from exc
        return found
