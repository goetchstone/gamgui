"""Audit Viewer routes (read-only).

Reads the local JSONL audit log (see ``core/audit.py``) — no gam calls at all. Every guarded
mutation elsewhere in the app appends a line to that log; this screen just surfaces it, including
the ok:false failures that otherwise sit silent in a file.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ...core.audit import default_audit_path, read_records
from ..server import TEMPLATES

# Cells starting with these can be interpreted as a formula if the CSV is opened in Excel/Sheets;
# prefix with a single quote to neutralise (CSV injection defence).
_CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _CSV_FORMULA_LEADS else s

router = APIRouter(prefix="/audit")

PAGE_SIZE = 25

_AUDIT_PAGE = "audit.html"
_AUDIT_ROWS = "_audit_rows.html"


def _audit_path(request: Request) -> Path:
    """The audit log path actually in use — the connected connector's own log if present.

    ``AppState`` doesn't keep a separate path field; the connector (when connected) owns the
    ``AuditLog`` instance that every mutation writes through, so its ``.path`` is the source of
    truth. Falls back to the default user-data-dir location when there's no connector (e.g. before
    setup), which mirrors what a freshly constructed ``AuditLog()`` would use anyway.
    """
    st = request.app.state.gamgui
    conn = getattr(st, "connector", None)
    audit = getattr(conn, "audit", None)
    path = getattr(audit, "path", None)
    return path if path is not None else default_audit_path()


def _matches(record: Dict[str, Any], q: str) -> bool:
    q = q.lower()
    haystack = [
        str(record.get("action") or ""),
        str(record.get("target") or ""),
        str(record.get("extra", {}).get("error") or "") if isinstance(record.get("extra"), dict) else "",
        " ".join(str(a) for a in (record.get("argv") or [])),
    ]
    return any(q in h.lower() for h in haystack)


def _filter_records(records: List[Dict[str, Any]], q: str, failed: bool) -> List[Dict[str, Any]]:
    out = records
    if failed:
        out = [r for r in out if r.get("ok") is False]
    q = (q or "").strip()
    if q:
        out = [r for r in out if _matches(r, q)]
    return out


def _rows_context(records: List[Dict[str, Any]], q: str = "", failed: bool = False, page: int = 1) -> dict:
    filtered = _filter_records(records, q, failed)
    total = len(filtered)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    return {
        "rows": filtered[start:start + PAGE_SIZE],
        "q": q, "failed": failed, "page": page, "pages": pages, "total": total,
    }


@router.get("", response_class=HTMLResponse)
async def audit_page(request: Request) -> HTMLResponse:
    records = read_records(_audit_path(request))
    total = len(records)
    failures = sum(1 for r in records if r.get("ok") is False)
    ctx = {"total": total, "failures": failures}
    ctx.update(_rows_context(records))
    return TEMPLATES.TemplateResponse(request, _AUDIT_PAGE, ctx)


@router.get("/rows", response_class=HTMLResponse)
async def audit_rows(request: Request, q: str = "", failed: int = 0, page: int = 1) -> HTMLResponse:
    records = read_records(_audit_path(request))
    ctx = _rows_context(records, q, bool(failed), page)
    return TEMPLATES.TemplateResponse(request, _AUDIT_ROWS, ctx)


@router.get("/export.csv")
async def audit_export(request: Request, q: str = "", failed: int = 0) -> Response:
    records = read_records(_audit_path(request))
    filtered = _filter_records(records, q, bool(failed))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "action", "target", "ok", "exit_code", "error", "argv"])
    for r in filtered:
        extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        error = (extra or {}).get("error", "")
        argv = " ".join(str(a) for a in (r.get("argv") or []))
        writer.writerow([_csv_safe(c) for c in
                         (r.get("ts", ""), r.get("action", ""), r.get("target", ""),
                          r.get("ok"), r.get("exit_code"), error, argv)])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-export.csv"},
    )
