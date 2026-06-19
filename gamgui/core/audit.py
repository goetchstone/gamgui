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
from typing import Any, Dict, List, Optional, Sequence

# gam argument keys whose following value must be masked in the log.
_SENSITIVE_KEYS = {"password", "signature"}
_MASK = "***redacted***"


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
    base = Path.home() / "Library" / "Application Support" / "GamGUI"
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.jsonl"


class AuditLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
            # 0600 — the log can reveal who was changed, even without secrets.
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        return entry

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
