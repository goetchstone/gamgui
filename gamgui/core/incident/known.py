"""What's kept on this Mac beside the incidents, true of any of them: the sign-ins marked as the person's own ("That was them"), and the emails Phishing's Find showed nobody here has.

Reads and this Mac only. A read is never audited, so what Find showed is kept here; a mark is the operator's word about their own domain. Each is one `0600` file beside the incidents, read and
written by descriptor, whole or not at all. A file holding anything else is moved aside and said, never saved over. Runbook: docs/domains/incident.md.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar

from ..gam.commands import MessageId, is_one_address
from .store import Problem, _private_dir, _read_at, _when, _write_atomic, default_root, is_time

logger = logging.getLogger(__name__)

FORMAT = 1
MARKS_FILE = "known-sign-ins.json"
MARKS_CAP = 500
NOTE_LENGTH = 500
CHECKS_FILE = "nobody-has.json"
CHECKS_CAP = 2_000
_FILE_CAP = 8 << 20
_DOMAIN_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")

T = TypeVar("T")


def _domain(text: str) -> Optional[str]:
    domain = text.strip().lower()
    return domain if "." in domain and len(domain) <= 253 and _DOMAIN_LETTERS.issuperset(domain) else None


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _load(root: Path, name: str, parse: Callable[[Any], Optional[List[T]]]) -> "tuple[List[T], str]":
    """What the file holds and the name it was moved aside to, if it held something else. Raises Problem when it can't be looked at: nothing is then made over what's there."""
    try:
        folder = _private_dir(root)
    except OSError as exc:
        raise Problem(f"Its folder couldn't be read: {exc.strerror}.") from exc
    try:
        try:
            raw = _read_at(folder, name, _FILE_CAP)
        except FileNotFoundError:
            return [], ""
        except OSError as exc:
            raise Problem(f"Its file couldn't be read: {exc.strerror}.") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
            json.dumps(data, ensure_ascii=False).encode("utf-8")
            kept = parse(data)
        except (UnicodeError, ValueError, RecursionError):
            kept = None
        if kept is not None:
            return kept, ""
        aside = f"{name}.unreadable-{secrets.token_hex(4)}"
        try:
            os.rename(name, aside, src_dir_fd=folder, dst_dir_fd=folder)
        except OSError as exc:
            raise Problem(f"Its file holds something else, and couldn't be moved aside: {exc.strerror}.") from exc
        return [], aside
    finally:
        os.close(folder)


def _save(root: Path, name: str, data: Dict[str, Any]) -> None:
    try:
        folder = _private_dir(root)
        try:
            _write_atomic(folder, name, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        finally:
            os.close(folder)
    except (OSError, UnicodeError) as exc:
        raise Problem(f"It couldn't be saved: {getattr(exc, 'strerror', None) or exc}.") from exc


@dataclass(frozen=True)
class Mark:
    """A sign-in the operator says was the person's own: that account from that address makes no suspect. Never hidden: listed with its note, and undone from there."""

    domain: str
    account: str
    address: str
    note: str
    at: datetime

    @property
    def id(self) -> str:
        return "|".join((self.domain, self.account, self.address))


def _mark(domain: str, account: str, address: str, note: str, at: datetime) -> Optional[Mark]:
    domain_ok = _domain(domain)
    account, address = account.strip().lower(), address.strip().lower()
    if domain_ok is None or not is_one_address(account) or not _is_ip(address) or not 1 <= len(note) <= NOTE_LENGTH or not is_time(at):
        return None
    return Mark(domain_ok, account, address, note, at)


class KnownSignIns:
    """The marks on this Mac, newest first."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_root()
        self.marks: List[Mark]
        self.marks, self.moved_aside = _load(self.root, MARKS_FILE, self._parse)

    @staticmethod
    def _parse(data: Any) -> Optional[List[Mark]]:
        if not isinstance(data, dict) or data.get("version") != FORMAT or not isinstance(data.get("marks"), list) or len(data["marks"]) > MARKS_CAP:
            return None
        marks: List[Mark] = []
        for entry in data["marks"]:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("domain", "account", "address", "note")):
                return None
            at = _when(entry.get("at"))
            mark = _mark(entry["domain"], entry["account"], entry["address"], entry["note"], at) if at else None
            if mark is None or mark.id != "|".join((entry["domain"], entry["account"], entry["address"])):
                return None
            marks.append(mark)
        return marks

    def for_domain(self, domain: str) -> List[Mark]:
        return [m for m in self.marks if m.domain == domain.lower()]

    def _write(self, marks: List[Mark]) -> None:
        _save(self.root, MARKS_FILE, {"version": FORMAT, "marks": [{"domain": m.domain, "account": m.account, "address": m.address, "note": m.note, "at": m.at.isoformat()} for m in marks]})
        self.marks = marks

    def mark(self, domain: str, account: str, address: str, note: str, at: Optional[datetime] = None) -> Mark:
        note = note.strip()
        mark = _mark(domain, account, address, note, at or datetime.now(timezone.utc))
        if mark is None:
            raise Problem("Say how you know it was them." if not note else f"The note must be {NOTE_LENGTH} characters or fewer." if len(note) > NOTE_LENGTH
                          else f"“{account}” from “{address}” isn't one account and one IP address.")
        changed = [m for m in self.marks if m.id != mark.id]
        if len(changed) >= MARKS_CAP:
            raise Problem(f"{MARKS_CAP} sign-ins are marked already: undo some before marking more.")
        self._write([mark, *changed])
        return mark

    def unmark(self, mark_id: str) -> None:
        changed = [m for m in self.marks if m.id != mark_id]
        if len(changed) != len(self.marks):
            self._write(changed)


@dataclass(frozen=True)
class Check:
    """What Phishing's Find last showed of this email. Gone: no mailbox here has it, every mailbox and its Spam searched. Not gone: a mailbox has it, or Find couldn't tell."""

    domain: str
    message_id: str
    at: datetime
    by: str = ""
    gone: bool = True


def _check(domain: str, message_id: str, at: datetime, by: str, gone: bool = True) -> Optional[Check]:
    domain_ok = _domain(domain)
    by = by.strip().lower()
    try:
        bare = MessageId(message_id).id
    except ValueError:
        return None
    if domain_ok is None or (by and not is_one_address(by)) or not is_time(at):
        return None
    return Check(domain_ok, bare, at, by, gone)


class NobodyHasChecks:
    """What Find last showed of each email, newest first. Past the cap the oldest goes: it only ever makes an email to-do again."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_root()
        self.checks: List[Check]
        self.checks, self.moved_aside = _load(self.root, CHECKS_FILE, self._parse)

    @staticmethod
    def _parse(data: Any) -> Optional[List[Check]]:
        if not isinstance(data, dict) or data.get("version") != FORMAT or not isinstance(data.get("checks"), list) or len(data["checks"]) > CHECKS_CAP:
            return None
        checks: List[Check] = []
        for entry in data["checks"]:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("domain", "message_id", "by")) or not isinstance(entry.get("gone"), bool):
                return None
            at = _when(entry.get("at"))
            check = _check(entry["domain"], entry["message_id"], at, entry["by"], entry["gone"]) if at else None
            if check is None or check.message_id != entry["message_id"]:
                return None
            checks.append(check)
        return checks

    def for_domain(self, domain: str) -> List[Check]:
        return [c for c in self.checks if c.domain == domain.lower()]

    def record(self, domain: str, message_ids: Sequence[str], by: str, at: Optional[datetime] = None, not_gone: Sequence[str] = ()) -> None:
        """``message_ids`` shown gone, ``not_gone`` shown in a mailbox or left unsure: a later Find takes back an earlier one."""
        moment = at or datetime.now(timezone.utc)
        new = [c for c in (*(_check(domain, m, moment, by) for m in message_ids), *(_check(domain, m, moment, by, False) for m in not_gone)) if c]
        if not new:
            return
        replaced = {(c.domain, c.message_id) for c in new}
        changed = [*new, *(c for c in self.checks if (c.domain, c.message_id) not in replaced)][:CHECKS_CAP]
        _save(self.root, CHECKS_FILE, {"version": FORMAT, "checks": [{"domain": c.domain, "message_id": c.message_id, "at": c.at.isoformat(), "by": c.by, "gone": c.gone} for c in changed]})
        self.checks = changed


__all__ = ["CHECKS_CAP", "Check", "KnownSignIns", "MARKS_CAP", "Mark", "NobodyHasChecks", "Problem"]
