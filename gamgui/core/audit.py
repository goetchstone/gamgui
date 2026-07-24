"""Append-only local audit log (JSONL).

Every mutation (and optionally reads) is recorded with a redacted copy of the gam argument vector
so there is a durable, reviewable record of what the tool did. Secrets are never written — values
following sensitive keys (e.g. ``password``) are masked.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import app_data_dir
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Sequence

# gam argument keys whose following value must be masked in the log.
_SENSITIVE_KEYS = {"password", "signature", "recoveryemail", "recoveryphone", "alternateemail"}
_MASK = "***redacted***"

# Roll the log past this size. A single bulk apply appends thousands of records and readers walk
# the file, so it can't grow forever — but rolling is the only moment history can be lost, so the
# threshold is set well above what normal use produces (~16 MiB is on the order of 100k records).
MAX_LOG_BYTES = 16 * 1024 * 1024
# Generations kept behind the live file (audit.jsonl.1 … .10). Every reader spans all of them, so
# this is the retention bound of the whole audit trail: roughly 10 × MAX_LOG_BYTES (~176 MiB, on
# the order of a million records) before the oldest generation is dropped by a roll. A bound is
# unavoidable for a local file; losing a generation per roll is not.
RETENTION_GENERATIONS = 10
_READ_CHUNK = 64 * 1024


def redact_argv(argv: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Return a copy of ``argv`` with values after sensitive keys masked."""
    if argv is None:
        return None
    out: List[str] = []
    mask_next = False
    for tok in argv:
        if mask_next:
            out.append(_MASK)
            mask_next = False
            continue
        out.append(tok)
        if tok.lower() in _SENSITIVE_KEYS:
            mask_next = True
    return out


def default_audit_path() -> Path:
    base = app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.jsonl"


def rolled_path(path: Path, generation: int = 1) -> Path:
    """A rolled generation of ``path`` — ``.1`` is the most recent one, ``.N`` the oldest kept."""
    return Path(f"{path}.{generation}")


def generation_paths(path: Path) -> List[Path]:
    """Every surviving generation of ``path``, newest-first: the live file, then ``.1`` … ``.N``.

    Readers that must see the whole trail (the CSV export above all) walk this list in order, so
    the newest-first ordering of the records themselves survives the generation boundary.
    """
    p = Path(path)
    candidates = [p] + [rolled_path(p, gen) for gen in range(1, RETENTION_GENERATIONS + 1)]
    return [c for c in candidates if c.exists()]


class AuditLog:
    def __init__(self, path: Optional[Path] = None, *, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = Path(path) if path else default_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        *,
        connector: str = "google_workspace",
        target: Optional[str] = None,
        argv: Optional[Sequence[str]] = None,
        exit_code: Optional[int] = None,
        ok: Optional[bool] = None,
        actor: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "connector": connector,
            "action": action,
            "target": target,
            "argv": redact_argv(argv),
            "exit_code": exit_code,
            "ok": ok,
            "actor": actor,
        }
        if extra:
            entry["extra"] = extra
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self._roll_if_large()
            # 0600 — the log can reveal who was changed, even without secrets.
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        return entry

    def _roll_if_large(self) -> None:
        """Move the log aside once it passes ``max_bytes``; called with ``_lock`` held.

        Generations shift oldest-first (``.9`` → ``.10``, … ``.1`` → ``.2``) so a roll never
        overwrites history still inside ``RETENTION_GENERATIONS``; only the oldest kept generation
        is displaced. A rename never splits a record, and every writer re-opens the path per
        record, so nothing can be lost mid-roll. Readers span every generation (see
        ``iter_records``). The chmod is belt-and-braces: ``os.replace`` carries the 0600 mode over,
        but a log created by hand or by an older build might not have had it.
        """
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        try:
            for gen in range(RETENTION_GENERATIONS - 1, 0, -1):
                src = rolled_path(self.path, gen)
                if src.exists():
                    os.replace(src, rolled_path(self.path, gen + 1))
            rolled = rolled_path(self.path, 1)
            os.replace(self.path, rolled)
            os.chmod(rolled, 0o600)
        except OSError:
            return  # keep appending to the current file rather than dropping the record

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        """The newest ``limit`` records, oldest-first (reading order)."""
        out = list(iter_records(self.path, limit=limit))
        out.reverse()
        return out


def _decode(line: str) -> Optional[Dict[str, Any]]:
    """A record, or None for a blank/malformed line (skipped rather than raised)."""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _iter_lines_reverse(fh: BinaryIO) -> Iterator[str]:
    """Yield an open file's lines last-first, walking backwards from EOF in chunks.

    Readers almost always want only the newest few records, and the audit search box re-reads on
    every keystroke; seeking from the end keeps that off the O(whole file) path.
    """
    fh.seek(0, os.SEEK_END)
    pos = fh.tell()
    head = b""  # partial line carried back from the chunk that follows this one
    while pos > 0:
        step = min(_READ_CHUNK, pos)
        pos -= step
        fh.seek(pos)
        parts = (fh.read(step) + head).split(b"\n")
        head = parts.pop(0)
        for part in reversed(parts):
            yield part.decode("utf-8", "replace")
    if head:
        yield head.decode("utf-8", "replace")


def _open_generations(path: Path) -> List[BinaryIO]:
    """Open every surviving generation newest-first, in one pass, before any record is read.

    A roll that lands mid-read renames every generation one step older. Resolving paths lazily
    would then make the reader open the file it already drained (now ``.1``) a second time and skip
    the generation before it, so a long export could emit duplicates and drop history. Open handles
    pin the inodes they were resolved to, and the (dev, inode) set discards a generation a
    concurrent roll already moved under us.
    """
    handles: List[BinaryIO] = []
    inodes = set()
    try:
        for candidate in generation_paths(path):
            try:
                fh = open(candidate, "rb")
            except OSError:
                continue
            try:
                st = os.fstat(fh.fileno())
            except OSError:
                fh.close()
                continue
            key = (st.st_dev, st.st_ino)
            if key in inodes:
                fh.close()
                continue
            inodes.add(key)
            handles.append(fh)
    except BaseException:
        for fh in handles:
            fh.close()
        raise
    return handles


def iter_records(path: Optional[Path] = None, *, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Stream audit records newest-first, spanning every rolled generation.

    Streaming (rather than materializing) matters for the CSV export, which must cover the whole
    log: an audit export that silently stops at the newest N is not an audit record. Tolerant of a
    missing file and of malformed/blank lines. ``limit`` stops early once N records are yielded.
    """
    p = Path(path) if path else default_audit_path()
    handles = _open_generations(p)
    seen = 0
    try:
        for fh in handles:
            try:
                for line in _iter_lines_reverse(fh):
                    record = _decode(line)
                    if record is None:
                        continue
                    yield record
                    seen += 1
                    if limit is not None and seen >= limit:
                        return
            except OSError:
                continue
    finally:
        for fh in handles:
            fh.close()


def read_records(path: Optional[Path] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read the JSONL audit log for display purposes only (no writes, no gam calls).

    Most-recent-written-first, spanning every rolled generation. ``limit`` (default: none) caps how
    many records are read — callers that filter must NOT cap, or the filter would only ever see
    the newest slice and quietly hide older matches.
    """
    return list(iter_records(path, limit=limit))
