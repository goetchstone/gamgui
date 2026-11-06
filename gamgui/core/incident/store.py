"""Incidents kept on this Mac, so one survives the app restarting or being rebuilt and can be picked up after the fact.

Each is a folder in ``root`` named by its ID, holding ``incident.json`` (what only a person can say: a title, when it was opened and by whom, how it was reported, notes, the
warnings picked, the review) and ``evidence/``: one folder per saved read of the logs, exactly as this app exports them, a CSV a log and ``about.txt``. Everything else is worked out
from the evidence when shown, never saved. It names people and addresses, so it stays here: ``0600`` in ``0700`` folders, every file opened relative to a held directory without
following a link, a regular file, and bounded. Ported from SwiftGamGui's ``Stores/IncidentStore.swift``. This is file and path handling under the user's data dir, so it gets the care
invariant 5 asks for: nothing here takes a path from a form, and a name from an export is only ever matched against what this app writes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import stat
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..gam.commands import GAMCommands
from ..paths import app_data_dir
from .logs import EARLIEST_YEAR, LATEST_YEAR, TABLE_HEADER

logger = logging.getLogger(__name__)

FILE_NAME = "incident.json"
FORMAT = 1
CAP = 200
NOTES_CAP = 500
EXPORTS_CAP = 100
FILE_CAP = 256 * 1024 * 1024
ABOUT_CAP = 1 << 20        # an export's about.txt: a few KB as this app writes it
EXPORT_CAP = 1024 * 1024 * 1024     # an export's files together
RECORD_CAP = 16 * 1024 * 1024       # incident.json: notes and review text, never logs
TITLE_LENGTH = 200
NAME_LENGTH = 100
NOTE_LENGTH = 2000
REVIEW_LENGTH = 8000
_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{6}")
_ABOUT = "about.txt"
_LOG_FILE = re.compile(r"([a-z]+)-log(-partial)?\.csv")
_HEADER = ",".join(TABLE_HEADER).encode()
_EVIDENCE = "evidence"
_BUFFER = 1 << 20


class Problem(Exception):
    """Why that couldn't be done, in words."""


def default_root() -> Path:
    return app_data_dir() / "incidents"


def is_id(text: str) -> bool:
    return bool(_ID.fullmatch(text or ""))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_time(when: datetime) -> bool:
    """A time every screen can say in any zone."""
    return EARLIEST_YEAR <= when.year <= LATEST_YEAR


def _when(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not is_time(parsed):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_message_id(text: Any) -> bool:
    """A Message-ID is printable ASCII, at most a header line long."""
    return isinstance(text, str) and 1 <= len(text) <= 998 and all(0x21 <= ord(c) <= 0x7E for c in text)


@dataclass(frozen=True)
class Note:
    at: datetime
    text: str


@dataclass(frozen=True)
class Warning:
    """A warning picked for an email the attacker sent: both bare Message-IDs, as the Gmail log names them."""

    email: str
    warning: str


@dataclass(frozen=True)
class Tick:
    """A to-do item marked done by hand: kept by its kind, its words and its basis, so one the logs later say more about comes back to do."""

    kind: str
    words: str
    basis: str
    how: str
    at: datetime
    by: str = ""


@dataclass(frozen=True)
class Mark:
    """"That was them": a sign-in the operator says was the person's own, shown in the report, never hidden, and undoable."""

    account: str
    address: str
    note: str = ""


@dataclass(frozen=True)
class Review:
    """The post-incident review, as the operator writes it."""

    kind: str = ""
    severity: str = ""
    meeting: Optional[datetime] = None
    attendees: str = ""
    cause: str = ""
    went_well: str = ""
    improve: str = ""
    plan: str = ""
    summary: str = ""

    @property
    def is_empty(self) -> bool:
        return replace(self, summary="") == Review()


@dataclass(frozen=True)
class Record:
    id: str
    domain: str     # the tenant it happened on: it opens only while connected to it
    title: str
    opened: datetime
    opened_by: str = ""
    reported: str = ""
    notes: Tuple[Note, ...] = ()
    warnings: Tuple[Warning, ...] = ()
    review: Review = field(default_factory=Review)
    phish: Optional[str] = None     # the email the operator picked as where it started
    ticks: Tuple[Tick, ...] = ()
    marks: Tuple[Mark, ...] = ()


@dataclass(frozen=True)
class Export:
    """One saved read of the logs: its name, its `about.txt`, and each log's CSV, as text."""

    name: str
    about: str
    logs: Tuple[Tuple[str, str], ...]   # (application, csv)
    files: Tuple[Tuple[str, bytes], ...] = ()


def valid(record: Record) -> Record:
    """``record`` as this store keeps it, trimmed, or why not."""
    record = replace(record, title=record.title.strip(), opened_by=record.opened_by.strip(), reported=record.reported.strip())
    if not is_id(record.id):
        raise Problem("That isn't an incident's ID.")
    if "." not in record.domain or len(record.domain) > 253:
        raise Problem("Connect a domain on Setup first.")
    if not all(is_time(t) for t in (record.opened, *(n.at for n in record.notes), *(t.at for t in record.ticks))):
        raise Problem("That date isn't one an incident can keep.")
    if not 1 <= len(record.title) <= TITLE_LENGTH:
        raise Problem(f"Give the incident a title of {TITLE_LENGTH} characters or fewer.")
    if len(record.opened_by) > NAME_LENGTH:
        raise Problem(f"The name must be {NAME_LENGTH} characters or fewer.")
    if len(record.reported) > NOTE_LENGTH:
        raise Problem(f"How it was reported must be {NOTE_LENGTH} characters or fewer.")
    if len(record.notes) > NOTES_CAP or not all(1 <= len(n.text) <= NOTE_LENGTH for n in record.notes):
        raise Problem(f"A note must be 1 to {NOTE_LENGTH} characters.")
    if len(record.warnings) > NOTES_CAP:
        raise Problem(f"At most {NOTES_CAP} warnings picked are kept with an incident.")
    if not all(_is_message_id(w.email) and _is_message_id(w.warning) for w in record.warnings):
        raise Problem("A warning picked isn't an email's Message-ID.")
    if record.phish is not None and not _is_message_id(record.phish):
        raise Problem("The email picked isn't an email's Message-ID.")
    r = record.review
    if len(r.kind) > NAME_LENGTH or len(r.severity) > NAME_LENGTH or any(len(t) > REVIEW_LENGTH for t in (r.attendees, r.cause, r.went_well, r.improve, r.plan, r.summary)):
        raise Problem(f"The type and severity must be {NAME_LENGTH} characters or fewer, and each answer of the review {REVIEW_LENGTH}.")
    if len(record.ticks) > NOTES_CAP:
        raise Problem(f"At most {NOTES_CAP} items marked done are kept with an incident.")
    if not all(1 <= len(t.how) <= NOTE_LENGTH for t in record.ticks):
        raise Problem(f"Say how it was done in 1 to {NOTE_LENGTH} characters.")
    if not all(1 <= len(t.kind) <= 40 and 1 <= len(t.words) <= REVIEW_LENGTH and len(t.basis) <= 40 and len(t.by) <= NAME_LENGTH for t in record.ticks):
        raise Problem(f"That item can't be kept as done: its words are longer than {REVIEW_LENGTH} characters.")
    if len(record.marks) > NOTES_CAP or not all(1 <= len(m.account) <= 320 and 1 <= len(m.address) <= 64 and len(m.note) <= NOTE_LENGTH for m in record.marks):
        raise Problem("That can't be kept as theirs.")
    return record


def _as_json(record: Record) -> Dict[str, Any]:
    r = record.review
    return {"version": FORMAT, "id": record.id, "domain": record.domain, "title": record.title, "opened": _iso(record.opened), "openedBy": record.opened_by, "reported": record.reported,
            "notes": [{"at": _iso(n.at), "text": n.text} for n in record.notes], "warnings": [{"email": w.email, "warning": w.warning} for w in record.warnings],
            "review": {"kind": r.kind, "severity": r.severity, "attendees": r.attendees, "cause": r.cause, "wentWell": r.went_well, "improve": r.improve, "plan": r.plan, "summary": r.summary,
                       "meeting": _iso(r.meeting) if r.meeting else None},
            "phish": record.phish, "ticks": [{"kind": t.kind, "words": t.words, "basis": t.basis, "how": t.how, "at": _iso(t.at), "by": t.by} for t in record.ticks],
            "marks": [{"account": m.account, "address": m.address, "note": m.note} for m in record.marks]}


def _from_json(data: Any, record_id: str) -> Optional[Record]:
    """The record in this format's file, or None for anything else."""
    if not isinstance(data, dict) or data.get("version") != FORMAT or data.get("id") != record_id:
        return None
    opened = _when(data.get("opened"))
    domain, title, opened_by, reported = (data.get(k) for k in ("domain", "title", "openedBy", "reported"))
    if opened is None or not all(isinstance(t, str) for t in (domain, title, opened_by, reported)):
        return None
    try:
        notes = tuple(Note(_must_be_time(n["at"]), str(n["text"])) for n in data.get("notes") or [])
        warnings = tuple(Warning(str(w["email"]), str(w["warning"])) for w in data.get("warnings") or [])
        ticks = tuple(Tick(str(t["kind"]), str(t["words"]), str(t["basis"]), str(t["how"]), _must_be_time(t["at"]), str(t["by"])) for t in data.get("ticks") or [])
        marks = tuple(Mark(str(m["account"]), str(m["address"]), str(m.get("note") or "")) for m in data.get("marks") or [])
        kept = data.get("review") or {}
        meeting = _when(kept.get("meeting")) if kept.get("meeting") else None
        if kept.get("meeting") and meeting is None:
            return None
        review = Review(str(kept.get("kind") or ""), str(kept.get("severity") or ""), meeting, str(kept.get("attendees") or ""), str(kept.get("cause") or ""), str(kept.get("wentWell") or ""),
                        str(kept.get("improve") or ""), str(kept.get("plan") or ""), str(kept.get("summary") or ""))
        phish = data.get("phish")
        return valid(Record(record_id, str(domain), str(title), opened, str(opened_by), str(reported), notes, warnings, review, phish if isinstance(phish, str) else None, ticks, marks))
    except (KeyError, TypeError, AttributeError, ValueError, Problem):
        return None


def _must_be_time(text: Any) -> datetime:
    when = _when(text)
    if when is None:
        raise ValueError("not a time")
    return when


# --- files, by descriptor --------------------------------------------------------------------------------------------------


def _private_dir(path: Path) -> int:
    """``path``, made if new and `0700`, as an open descriptor: never through a link, and held, so a swap after this reaches nothing. The caller closes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o700)
    except OSError:
        os.close(fd)
        raise
    return fd


def _sub_dir(parent: int, name: str, make: bool = True) -> int:
    """The folder ``name`` in ``parent``, `0700`, never through a link. The caller closes it."""
    if make:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    if make:
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            os.close(fd)
            raise
    return fd


def _read_at(folder: int, name: str, cap: int = FILE_CAP) -> bytes:
    """A regular file in the held ``folder``, at most ``cap`` bytes, not following a link, and not hanging on a FIFO."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Problem(f"{name} isn't a regular file.")
        if info.st_size > cap:
            raise Problem(f"{name} is larger than {cap >> 20} MB.")
        chunks: List[bytes] = []
        total = 0
        while chunk := os.read(fd, _BUFFER):
            total += len(chunk)
            if total > cap:     # it grew: the size above was only what it said then
                raise Problem(f"{name} is larger than {cap >> 20} MB.")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_new(folder: int, name: str, data: bytes) -> None:
    """A new file in the held ``folder``, `0600`, never over anything and never through a link."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(folder: int, name: str, data: bytes) -> None:
    """The whole of ``name`` in the held ``folder``, or nothing changed."""
    temp = f".{name}.{secrets.token_hex(6)}"
    try:
        _write_new(folder, temp, data)
        os.rename(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=folder)
        except OSError:
            pass
        raise
    _sync_folder(folder)


def _sync_folder(folder: int) -> None:
    """A rename made durable, as far as it can be. It has happened by now: saying the write failed would be false, and memory would part from the disk."""
    try:
        os.fsync(folder)
    except OSError as exc:
        logger.warning("couldn't sync a folder after a rename: %s", exc.strerror)


def check_export(name: str, files: Mapping[str, bytes]) -> Export:
    """A saved read of the logs, checked: `about.txt` and at least one log's CSV, for a log this app reads, headed as this app heads it. Other files are left out."""
    logs: List[Tuple[str, str]] = []
    kept: List[Tuple[str, bytes]] = []
    about: Optional[str] = None
    for file_name in sorted(files):
        data = files[file_name]
        found = _LOG_FILE.fullmatch(file_name)
        application = found.group(1) if found and found.group(1) in GAMCommands.REPORT_APPLICATIONS else None
        if file_name != _ABOUT and application is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Problem(f"{file_name} isn't text.") from exc
        if application:
            if not data.startswith(_HEADER):
                raise Problem(f"{file_name} isn't one of this app's exports of a log.")
            logs.append((application, text))
        else:
            if not text.startswith("Incident logs exported "):
                raise Problem("about.txt isn't one of this app's exports.")
            if len(data) > ABOUT_CAP:
                raise Problem(f"about.txt is larger than {ABOUT_CAP >> 10} KB.")
            about = text
        kept.append((file_name, data))
    if about is None or not logs:
        raise Problem("That isn't an export of the incident logs: it needs about.txt and a log's CSV, as Export all writes them.")
    return Export(name, about, tuple(logs), tuple(kept))


def read_zip(name: str, data: bytes) -> Export:
    """An export picked by the operator, as Export all saves it. Nothing in it is ever a path here: a member is only matched by its whole name against what this app writes, and read to its
    declared size, bounded, so neither a `../` nor a zip bomb reaches anything."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - a hostile zip reaches ValueError, UnicodeDecodeError and NotImplementedError here too
        raise Problem("That isn't an export of the incident logs: it isn't a zip file.") from exc
    files: Dict[str, bytes] = {}
    total = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir() or (info.filename != _ABOUT and not _LOG_FILE.fullmatch(info.filename)):
                continue
            if info.file_size > FILE_CAP:
                raise Problem(f"{info.filename} is larger than {FILE_CAP >> 20} MB.")
            total += info.file_size
            if total > EXPORT_CAP:
                raise Problem(f"The export is larger than {EXPORT_CAP >> 30} GB together.")
            try:
                with archive.open(info) as member:
                    body = member.read(FILE_CAP + 1)    # what it says it holds is only what it says
            except Exception as exc:  # noqa: BLE001 - and zlib.error here
                raise Problem(f"{info.filename} wasn't read from the zip file.") from exc
            if len(body) > FILE_CAP:
                raise Problem(f"{info.filename} is larger than {FILE_CAP >> 20} MB.")
            files[info.filename] = body
    return check_export(name, files)


_SAVING = ".saving-"             # a saved read still being written: not evidence yet
OPEN_FILE = "open-incident"      # the open incident's ID, never its contents: it opens again after a restart


class IncidentStore:
    """The incidents in ``root``, newest opened first. Opening refuses when the folder can't be read: a store that opened anyway could make a second incident over one it didn't see."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_root()
        self.records: List[Record] = []
        self.unreadable: List[str] = []     # incidents whose file couldn't be read: moved aside, and named here for the screen to say so
        self._load()

    def _load(self) -> None:
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise Problem(f"Couldn't read the incidents: {exc.strerror}.") from exc
        try:
            for name in sorted(os.listdir(root)):
                if not is_id(name):
                    continue
                try:
                    folder = _sub_dir(root, name, make=False)
                except OSError:     # a stray file, or a link: nothing of ours
                    continue
                try:
                    record = self._read_record(folder, name)
                    if record:
                        self.records.append(record)
                finally:
                    os.close(folder)
        finally:
            os.close(root)
        self.records.sort(key=lambda r: (r.opened, r.id), reverse=True)
        self.records = self.records[:CAP]

    def _read_record(self, folder: int, record_id: str) -> Optional[Record]:
        try:
            raw = _read_at(folder, FILE_NAME, RECORD_CAP)
        except FileNotFoundError:      # moved aside before: still named, or the incident and its evidence vanish without a word
            if any(n.startswith(f"{FILE_NAME}.unreadable-") for n in os.listdir(folder)):
                self.unreadable.append(record_id)
            return None
        except (OSError, Problem):
            self.unreadable.append(record_id)
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            json.dumps(data, ensure_ascii=False).encode("utf-8")    # a lone surrogate reads, then fails every screen
            record = _from_json(data, record_id)
        except (UnicodeError, ValueError, RecursionError):
            record = None
        if record is None:
            self.unreadable.append(record_id)
            try:    # moved aside, never saved over
                os.rename(FILE_NAME, f"{FILE_NAME}.unreadable-{secrets.token_hex(4)}", src_dir_fd=folder, dst_dir_fd=folder)
            except OSError as exc:
                logger.error("couldn't move an unreadable incident aside: %s", exc.strerror)
        return record

    def remembered_open(self) -> str:
        """The ID of the incident last open, when it's still one kept here."""
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return ""
        try:
            record_id = _read_at(root, OPEN_FILE, 64).decode("ascii", "replace").strip()
        except (OSError, Problem):
            return ""
        finally:
            os.close(root)
        return record_id if self.get(record_id) else ""

    def remember_open(self, record_id: str) -> None:
        """A convenience, so a failure to keep it stops nothing."""
        try:
            root = _private_dir(self.root)
            try:
                _write_atomic(root, OPEN_FILE, record_id.encode("ascii"))
            finally:
                os.close(root)
        except OSError as exc:
            logger.error("couldn't remember the open incident: %s", exc.strerror)

    def for_domain(self, domain: str) -> List[Record]:
        return [r for r in self.records if r.domain == domain.lower()]

    def get(self, record_id: str) -> Optional[Record]:
        return next((r for r in self.records if r.id == record_id), None)

    def create(self, domain: str, title: str, opened: datetime, opened_by: str = "", reported: str = "") -> Record:
        if len(self.records) >= CAP:
            raise Problem(f"{CAP} incidents are kept already.")
        stamp = opened.astimezone().strftime("%Y-%m-%d")
        taken = {r.id for r in self.records}
        while True:     # never an ID kept already: saving it would write over that incident's record
            record_id = f"{stamp}-{secrets.token_hex(3)}"
            if record_id not in taken and not (self.root / record_id).exists():
                break
        record = valid(Record(record_id, domain.strip().lower(), title, opened, opened_by, reported))
        self._write(record)
        self.records.append(record)
        self.records.sort(key=lambda r: (r.opened, r.id), reverse=True)
        return record

    def save(self, record: Record) -> Record:
        if self.get(record.id) is None:
            raise Problem("That incident isn't kept here.")
        record = valid(record)
        self._write(record)
        self.records = [record if r.id == record.id else r for r in self.records]
        return record

    def add_note(self, record_id: str, text: str, at: datetime) -> Record:
        """A note at ``at``, which can be earlier than now ("called the bank at 9")."""
        record = self.get(record_id)
        if record is None:
            raise Problem("That incident isn't kept here.")
        if len(record.notes) >= NOTES_CAP:
            raise Problem(f"{NOTES_CAP} notes are kept already.")
        return self.save(replace(record, notes=tuple(sorted((*record.notes, Note(at, text.strip())), key=lambda n: n.at))))

    def _write(self, record: Record) -> None:
        data = (json.dumps(_as_json(record), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            root = _private_dir(self.root)
            try:
                folder = _sub_dir(root, record.id)
                try:
                    _write_atomic(folder, FILE_NAME, data)
                finally:
                    os.close(folder)
            finally:
                os.close(root)
        except OSError as exc:
            raise Problem(f"Couldn't save the incident: {exc.strerror}.") from exc

    # --- evidence ------------------------------------------------------------------------------------------------------------

    def _evidence(self, record_id: str, make: bool = False) -> Optional[int]:
        """The incident's evidence folder as a descriptor, or None when it has none. The caller closes it."""
        if not is_id(record_id):
            raise Problem("That isn't an incident's ID.")
        try:
            root = _private_dir(self.root) if make else os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                folder = _sub_dir(root, record_id, make=make)
                try:
                    return _sub_dir(folder, _EVIDENCE, make=make)
                finally:
                    os.close(folder)
            finally:
                os.close(root)
        except FileNotFoundError:
            return None

    def keep(self, export: Export, record_id: str) -> str:
        """``export`` as evidence of the incident: a new folder in its evidence, named as the export's, " (2)" and on when that's taken, each file written `0600`, never through a link. Whole or not at
        all, a crash included: it's written in a folder readers skip, and only then given its name. Returns the name."""
        try:
            evidence = self._evidence(record_id, make=True)
            assert evidence is not None
            try:
                names = os.listdir(evidence)
                for stale in names:
                    if stale.startswith(_SAVING):       # what a crash left: never evidence
                        self._remove(evidence, stale)
                if len([n for n in names if not n.startswith(".")]) >= EXPORTS_CAP:
                    raise Problem(f"{EXPORTS_CAP} reads of the logs are kept for this incident already.")
                base = "".join(c for c in export.name if c not in "/:\\\n\r\0")[:100].strip() or "Incident logs"
                base = "Incident logs" if base.startswith(".") else base
                saving = f"{_SAVING}{secrets.token_hex(6)}"
                os.mkdir(saving, 0o700, dir_fd=evidence)
                try:
                    folder = _sub_dir(evidence, saving)
                    try:
                        for file_name, data in export.files:
                            _write_new(folder, file_name, data)
                        os.fsync(folder)
                    finally:
                        os.close(folder)
                    for n in range(1, 100):
                        name = base if n == 1 else f"{base} ({n})"
                        try:
                            os.stat(name, dir_fd=evidence, follow_symlinks=False)
                        except FileNotFoundError:       # rename would replace an empty folder or fail on a full one: taken is taken, whatever it holds
                            os.rename(saving, name, src_dir_fd=evidence, dst_dir_fd=evidence)
                            _sync_folder(evidence)
                            return name
                    raise Problem("Couldn't save the evidence: too many reads of that name.")
                except BaseException:
                    self._remove(evidence, saving)
                    raise
            finally:
                os.close(evidence)
        except OSError as exc:
            raise Problem(f"Couldn't save the evidence: {exc.strerror}.") from exc

    def names(self, record_id: str) -> List[str]:
        """The names of the incident's saved reads, read or not, without reading one."""
        evidence = self._evidence(record_id)
        if evidence is None:
            return []
        try:
            return sorted(n for n in os.listdir(evidence) if not n.startswith("."))
        finally:
            os.close(evidence)

    @staticmethod
    def _remove(evidence: int, name: str) -> None:
        """A half-written read of the logs, gone, by descriptors: it's evidence or it isn't."""
        try:
            folder = _sub_dir(evidence, name, make=False)
            try:
                for file_name in os.listdir(folder):
                    os.unlink(file_name, dir_fd=folder)
            finally:
                os.close(folder)
            os.rmdir(name, dir_fd=evidence)
        except OSError as exc:
            logger.error("couldn't remove a half-saved read of the logs: %s", exc.strerror)

    def _read_saved(self, evidence: int, name: str) -> Export:
        folder = _sub_dir(evidence, name, make=False)
        try:
            files: Dict[str, bytes] = {}
            total = 0
            for file_name in sorted(os.listdir(folder)):
                if file_name == _ABOUT or _LOG_FILE.fullmatch(file_name):
                    files[file_name] = _read_at(folder, file_name)
                    total += len(files[file_name])
                    if total > EXPORT_CAP:
                        raise Problem(f"{name} is larger than {EXPORT_CAP >> 30} GB together.")
            return check_export(name, files)
        finally:
            os.close(folder)

    def exports(self, record_id: str) -> Tuple[List[Export], List[str]]:
        """Every read of the logs kept for the incident, oldest first (by name: an export's name is when it was made), and the names of any that couldn't be read."""
        evidence = self._evidence(record_id)
        if evidence is None:
            return [], []
        exports: List[Export] = []
        unreadable: List[str] = []
        try:
            for name in sorted(n for n in os.listdir(evidence) if not n.startswith("."))[:EXPORTS_CAP]:
                try:
                    exports.append(self._read_saved(evidence, name))
                except (OSError, Problem):
                    unreadable.append(name)
        finally:
            os.close(evidence)
        return exports, unreadable

    def evidence_files(self, record_id: str) -> Iterator[Tuple[str, bytes, str]]:
        """Every saved file, as (its path under the evidence, its bytes, its SHA-256): what shows the evidence unchanged. A stray file is no saved read; anything else that won't open (a link, no
        access) is evidence left out, so it raises, naming it."""
        evidence = self._evidence(record_id)
        if evidence is None:
            return
        try:
            for name in sorted(n for n in os.listdir(evidence) if not n.startswith("."))[:EXPORTS_CAP]:
                try:
                    folder = _sub_dir(evidence, name, make=False)
                except NotADirectoryError:
                    continue
                except OSError as exc:
                    raise Problem(f"Couldn't read the evidence {name}: {exc.strerror}.") from exc
                try:
                    for file_name in sorted(n for n in os.listdir(folder) if not n.startswith(".")):
                        try:
                            data = _read_at(folder, file_name)
                        except OSError as exc:
                            raise Problem(f"Couldn't read the evidence {name}/{file_name}: {exc.strerror}.") from exc
                        yield f"{name}/{file_name}", data, hashlib.sha256(data).hexdigest()
                finally:
                    os.close(folder)
        finally:
            os.close(evidence)


def export_of(about: str, logs: Sequence[Tuple[str, str]], name: str) -> Export:
    """A pull as evidence: its `about.txt` and each log's CSV, checked as an export read back is. ``logs`` is (file name, csv)."""
    try:
        files = {file_name: text.encode("utf-8") for file_name, text in logs}
        files[_ABOUT] = about.encode("utf-8")
    except UnicodeError as exc:
        raise Problem("A log holds text that can't be written to a file.") from exc
    return check_export(name, files)


__all__ = ["CAP", "EXPORTS_CAP", "Export", "IncidentStore", "Mark", "Note", "Problem", "Record", "Review", "Tick", "Warning", "check_export", "export_of", "is_id", "read_zip", "valid"]
