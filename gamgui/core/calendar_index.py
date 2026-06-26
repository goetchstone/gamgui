"""A local, persistent index of the domain's calendars (SQLite).

Google Workspace has no API to list calendars domain-wide, so finding a *secondary* calendar by
name means scanning every user's calendar list — O(users), which is minutes on a large tenant and
doesn't survive an app restart. This index makes that scan happen ONCE (in the background) and
stores the result locally, so name search is instant at any company size and persists across
restarts.

It holds only DERIVED, rebuildable data — calendar id / name / owner / kind / subscriber count —
and **no secrets**. Delete the file and it rebuilds from Google on the next "Rebuild index".
Credentials still live only in the Keychain; this changes nothing about the security model. (It is
a deliberate, scoped exception to the project's otherwise no-local-state stance, justified because
the data is a cache, not a source of truth.)
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .paths import app_data_dir


def default_index_path() -> Path:
    return app_data_dir() / "calendar_index.db"


@dataclass
class IndexedCalendar:
    id: str
    summary: str
    owner: str
    kind: str          # "room" | "secondary"
    subscribers: int   # how many user calendar lists reference it (rough "in use" signal)


@dataclass
class IndexStatus:
    count: int
    updated_at: Optional[float]   # epoch seconds of the last successful rebuild, or None
    domain: str


class CalendarIndex:
    """Thin SQLite wrapper. A fresh connection per call (cheap, avoids thread-affinity issues)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        self._restrict_perms()

    def _restrict_perms(self) -> None:
        """Calendar names + owner emails are domain-sensitive; keep them off other local accounts.

        Mirrors core/secrets/ephemeral.py: dir 0700, files 0600 (incl. the -wal/-shm siblings)."""
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        for p in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                if p.exists():
                    os.chmod(p, 0o600)
            except OSError:
                pass

    def _conn(self) -> sqlite3.Connection:
        # WAL lets a search read while a background rebuild writes; busy_timeout rides out brief locks.
        c = sqlite3.connect(self.path, timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _init(self) -> None:
        with closing(self._conn()) as c, c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS calendars "
                "(id TEXT PRIMARY KEY, summary TEXT, owner TEXT, kind TEXT, subscribers INTEGER)"
            )
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    # --- writes ------------------------------------------------------------------------
    def replace_all(self, domain: str, cals: List[IndexedCalendar]) -> int:
        """Atomically swap the whole index for a fresh scan; records domain + timestamp."""
        with closing(self._conn()) as c, c:  # `c` as a context manager = one transaction (atomic swap)
            c.execute("DELETE FROM calendars")
            c.executemany(
                "INSERT OR REPLACE INTO calendars (id, summary, owner, kind, subscribers) VALUES (?,?,?,?,?)",
                [(x.id, x.summary, x.owner, x.kind, int(x.subscribers)) for x in cals],
            )
            c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('domain', ?)", (domain,))
            c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('updated_at', ?)", (str(time.time()),))
        self._restrict_perms()  # WAL sidecars may have just been (re)created
        return len(cals)

    def remove(self, calendar_id: str) -> None:
        """Drop one calendar (e.g. right after we delete it) so it leaves search immediately."""
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))

    # --- reads -------------------------------------------------------------------------
    def search(self, query: str, limit: int = 200) -> List[IndexedCalendar]:
        q = (query or "").strip()
        with closing(self._conn()) as c:
            if q:
                # Escape LIKE wildcards so a literal % / _ in the query isn't treated as a wildcard.
                esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like = f"%{esc}%"
                rows = c.execute(
                    "SELECT * FROM calendars WHERE summary LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' "
                    "ORDER BY (kind='room') DESC, summary LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM calendars ORDER BY (kind='room') DESC, summary LIMIT ?", (limit,)
                ).fetchall()
        return [IndexedCalendar(r["id"], r["summary"] or "", r["owner"] or "", r["kind"] or "", r["subscribers"] or 0)
                for r in rows]

    def status(self) -> IndexStatus:
        with closing(self._conn()) as c:
            count = c.execute("SELECT COUNT(*) FROM calendars").fetchone()[0]
            up = c.execute("SELECT v FROM meta WHERE k = 'updated_at'").fetchone()
            dom = c.execute("SELECT v FROM meta WHERE k = 'domain'").fetchone()
        updated = None
        if up:
            try:
                updated = float(up[0])
            except (TypeError, ValueError):
                updated = None
        return IndexStatus(count=count, updated_at=updated, domain=(dom[0] if dom else ""))

    def is_empty(self) -> bool:
        return self.status().count == 0
