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
from typing import Any, Dict, Iterable, Iterator, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ...core.audit import default_audit_path, iter_records, read_records
from ..csvutil import csv_safe as _csv_safe
from ..server import TEMPLATES

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


def _flag(value: str) -> bool:
    """Read a checkbox-style query flag leniently.

    Pager links carry the current filter state along, and "off" is naturally spelled as an empty
    ``failed=``; declaring the parameter as an int made FastAPI 422 that, which broke Prev/Next and
    Clear filters whenever the failures-only filter was off. Anything unrecognised reads as off.
    """
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _matches(record: Dict[str, Any], q: str) -> bool:
    q = q.lower()
    haystack = [
        str(record.get("action") or ""),
        str(record.get("target") or ""),
        str(record.get("extra", {}).get("error") or "") if isinstance(record.get("extra"), dict) else "",
        " ".join(str(a) for a in (record.get("argv") or [])),
    ]
    return any(q in h.lower() for h in haystack)


def _keep(record: Dict[str, Any], q: str, failed: bool) -> bool:
    """Per-record predicate, so the list and streaming readers filter identically."""
    if failed and record.get("ok") is not False:
        return False
    return not q or _matches(record, q)


def _filter_records(records: Iterable[Dict[str, Any]], q: str, failed: bool) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    return [r for r in records if _keep(r, q, failed)]


def _rows_context(records: List[Dict[str, Any]], q: str = "", failed: bool = False, page: int = 1) -> dict:
    # Filter over the WHOLE log, then paginate the result — capping the read first would let a
    # single bulk apply push older matches out of the window without the UI ever saying so.
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
async def audit_rows(request: Request, q: str = "", failed: str = "", page: int = 1) -> HTMLResponse:
    records = read_records(_audit_path(request))
    ctx = _rows_context(records, q, _flag(failed), page)
    return TEMPLATES.TemplateResponse(request, _AUDIT_ROWS, ctx)


def _export_csv(path: Path, q: str, failed: bool) -> Iterator[str]:
    """Stream the matching records as CSV, newest-first, over the entire log.

    The export is the artefact someone hands to an auditor, so it reads every generation of the
    log and never caps: a download that quietly omits last month's history is worse than no
    download at all. Streaming keeps that from materializing the whole log in memory.
    """
    q = (q or "").strip()
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush() -> str:
        chunk = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return chunk

    writer.writerow(["ts", "action", "target", "ok", "exit_code", "error", "argv"])
    yield flush()
    for r in iter_records(path):
        if not _keep(r, q, failed):
            continue
        extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        error = (extra or {}).get("error", "")
        argv = " ".join(str(a) for a in (r.get("argv") or []))
        writer.writerow([_csv_safe(c) for c in
                         (r.get("ts", ""), r.get("action", ""), r.get("target", ""),
                          r.get("ok"), r.get("exit_code"), error, argv)])
        yield flush()


@router.get("/export.csv")
async def audit_export(request: Request, q: str = "", failed: str = "") -> StreamingResponse:
    return StreamingResponse(
        _export_csv(_audit_path(request), q, _flag(failed)),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-export.csv"},
    )
