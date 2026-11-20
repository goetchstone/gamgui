"""Signature backups (roadmap 4.1): each person's Gmail signature as it was, kept before a signature run overwrites it.

Gmail keeps no copy of a replaced signature, so a run reads each person's stored signature byte for byte
(``GAMConnector.signature_exact``) and appends it here before it writes the new one. A person whose signature couldn't be
read, or whose copy couldn't be kept, is not overwritten. Put Back sets a kept signature again, through the chokepoint.

One file per run, ``<run>.jsonl`` in a ``0700`` folder of the app's data folder, each ``0600``: a header line (the run's id,
when, and what it applied to), then one line per person (``email``, ``previous``, ``set``), appended and flushed before
that person's write, so a run cut off midway still holds every signature it replaced. Appending keeps a domain-wide run
linear. Only the last ``KEEP_RUNS`` runs are kept: a new run removes the oldest beyond that.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .paths import app_data_dir

KEEP_RUNS = 5
_SUFFIX = ".jsonl"


class BackupProblem(Exception):
    """A kept run that can't be read whole: never restored in part, never read as empty."""


@dataclass(frozen=True)
class Entry:
    email: str
    previous: str     # the stored signature before the run, byte for byte; "" for none
    set: str          # what the run wrote


@dataclass
class Run:
    id: str
    when: float
    what: str                         # the scope in words ("everyone in sales@example.com")
    entries: List[Entry] = field(default_factory=list)


class SignatureBackups:
    def __init__(self, folder: Optional[Path] = None) -> None:
        self.folder = Path(folder) if folder is not None else app_data_dir() / "signature-backups"

    # --- a run being written -------------------------------------------------------------------------------------------
    def start(self, what: str) -> "RunWriter":
        """A new run's file, made ``0600`` in a ``0700`` folder; the oldest runs beyond KEEP_RUNS - 1 go first. OSError when it
        can't be made: the caller writes nothing without somewhere to keep the copies."""
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        for old in self._files()[KEEP_RUNS - 1:]:
            try:
                old.unlink()
            except FileNotFoundError:
                pass
        run_id = f"{int(time.time() * 1000):013d}-{secrets.token_hex(4)}"
        path = self.folder / f"{run_id}{_SUFFIX}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        writer = RunWriter(run_id, path, fd)
        writer._line({"run": run_id, "when": time.time(), "what": what})
        return writer

    # --- the kept runs -------------------------------------------------------------------------------------------------
    def runs(self) -> List[Run]:
        """The kept runs, newest first, each with its entries. A file that can't be read whole is listed by its id with
        no entries and ``what`` saying so, never dropped: its people's old signatures may be in it."""
        found = []
        for path in self._files():
            try:
                found.append(self.load(path.stem))
            except BackupProblem as exc:
                found.append(Run(id=path.stem, when=0.0, what=f"unreadable: {exc}"))
        return found

    def load(self, run_id: str) -> Run:
        path = self.folder / f"{run_id}{_SUFFIX}"
        if path.parent != self.folder or not run_id or "/" in run_id or run_id.startswith("."):
            raise BackupProblem("not a kept run")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        except OSError as exc:
            raise BackupProblem("it couldn't be read") from exc
        try:
            head = json.loads(lines[0])
            run = Run(id=str(head["run"]), when=float(head["when"]), what=str(head["what"]))
            for line in lines[1:]:
                if line:
                    d = json.loads(line)
                    run.entries.append(Entry(str(d["email"]), str(d["previous"]), str(d["set"])))
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise BackupProblem("it isn't whole") from exc
        if run.id != run_id:
            raise BackupProblem("it isn't whole")
        return run

    def _files(self) -> List[Path]:
        try:
            return sorted((p for p in self.folder.iterdir() if p.suffix == _SUFFIX and not p.is_symlink()), reverse=True)
        except FileNotFoundError:
            return []


class RunWriter:
    """One run's file, open for appending. ``keep`` before each write; ``close`` when the run ends."""

    def __init__(self, run_id: str, path: Path, fd: int) -> None:
        self.id, self.path, self._fd = run_id, path, fd

    def keep(self, email: str, previous: str, new: str) -> None:
        """``email``'s signature as it was, on disk before its write. OSError when it can't be: then nothing is written."""
        self._line({"email": email, "previous": previous, "set": new})

    def _line(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(self._fd, data[written:])
        os.fsync(self._fd)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
