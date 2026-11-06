"""Phishing's latest run on each email, from this app's audit log: what shows an email pulled as done. A reader of the log, never a writer. A run recorded before these keys, or that can't say its run,
is passed over: never shown done, so never hidden. Runbook: docs/domains/incident.md."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..audit import iter_records
from .logs import parse_time
from .reader import Pulled
from .report import Change

ACTIONS = frozenset({"trash_message", "trash_spam_messages"})
SCAN_LIMIT = 100_000


def about(message_id: str, run: str, domain: str, steps: int, searched_everywhere: bool, by: str, place: str) -> Dict[str, str]:
    """What a run's write says it was, kept in the audit record: ``run`` tells this run from the last."""
    return {"message_id": message_id, "run": run, "domain": domain, "steps": str(steps), "searched_everywhere": "true" if searched_everywhere else "false", "by": by, "place": place}


def latest_runs(path: Optional[Path], domain: str, limit: int = SCAN_LIMIT) -> List[Pulled]:
    """Each email's newest run on this domain, reading the log newest first, at most ``limit`` records. Off the event loop."""
    latest: Dict[str, str] = {}
    seen: Dict[str, Dict[str, Any]] = {}
    for record in iter_records(path, limit=limit):
        extra = record.get("extra")
        if record.get("action") not in ACTIONS or not isinstance(extra, dict):
            continue
        message_id, run, steps, at = extra.get("message_id"), extra.get("run"), str(extra.get("steps") or ""), parse_time(str(record.get("ts") or ""))
        if str(extra.get("domain") or "").lower() != domain.lower() or not (message_id and run and at and steps.isascii() and steps.isdigit() and int(steps) > 0):
            continue
        if latest.setdefault(message_id, run) != run:   # the newest seen is its latest: an earlier run's records are passed over
            continue
        one = seen.setdefault(message_id, {"at": at, "by": str(extra.get("by") or ""), "steps": int(steps), "searched": extra.get("searched_everywhere") == "true",
                                           "trashed": 0, "failed": 0, "mailboxes": set()})
        one["trashed" if record.get("ok") is True else "failed"] += 1
        one["mailboxes"].add(str(record.get("target") or "").lower())
    return [Pulled(message_id, one["at"], one["by"], one["steps"], one["trashed"], one["failed"], len(one["mailboxes"]), one["searched"]) for message_id, one in seen.items()]


NOT_WRITES = frozenset({"sensitive_read", "sensitive_csv_export"})      # audited, and changing nothing


def changes_since(path: Optional[Path], since: datetime, words: Callable[[str], str], limit: int = SCAN_LIMIT) -> Tuple[List[Change], Optional[datetime]]:
    """What this app's audit log shows it ran, newest first, stopping at ``since``, and how far back it read when ``limit`` stopped it first (None when it got there): an older step isn't "none". Phishing's trash
    steps are said once an email, as pulled, so they aren't here. Off the event loop."""
    changes: List[Change] = []
    read, oldest = 0, None
    for record in iter_records(path, limit=limit):
        read += 1
        at = parse_time(str(record.get("ts") or ""))
        if at is None:
            continue
        if at < since:
            return changes, None
        oldest = at
        action = str(record.get("action") or "")
        if action and action not in ACTIONS and action not in NOT_WRITES and record.get("target"):
            ok = record.get("ok")
            changes.append(Change(at, words(action), str(record.get("target")), str(record.get("actor") or ""), ok if isinstance(ok, bool) else None))
    return changes, oldest if read >= limit else None
