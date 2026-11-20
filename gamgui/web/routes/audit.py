"""Audit Viewer routes (read-only).

Reads the local JSONL audit log (see ``core/audit.py``) — no gam calls at all. Every guarded
mutation elsewhere in the app, and every catalog read the operator runs, appends a line to that log;
this screen just surfaces it, including the ok:false failures that otherwise sit silent in a file.
It filters by domain (``extra.domain``); a record written before records named one is "unknown",
never filed under some domain.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ...core.audit import DOMAIN_KEY, default_audit_path, iter_records, read_records
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


# The domain filter's value for records that name no domain: those written before records did. No
# Workspace domain can be spelled this way (a domain has a dot), so it never matches a real one.
UNKNOWN_DOMAIN = "unknown"


def _extra(record: Dict[str, Any]) -> Dict[str, Any]:
    extra = record.get("extra")
    return extra if isinstance(extra, dict) else {}


def record_domain(record: Dict[str, Any]) -> str:
    """The domain a record names, lowercased, or "" when it names none (written before records did)."""
    return str(_extra(record).get(DOMAIN_KEY) or "").strip().lower()


def _matches(record: Dict[str, Any], q: str) -> bool:
    q = q.lower()
    extra = _extra(record)
    haystack = [
        str(record.get("action") or ""),
        str(record.get("target") or ""),
        str(extra.get("error") or ""),
        str(extra.get("detail") or ""),
        record_domain(record),
        " ".join(str(a) for a in (record.get("argv") or [])),
    ]
    return any(q in h.lower() for h in haystack)


def _keep(record: Dict[str, Any], q: str, failed: bool, domain: str = "") -> bool:
    """Per-record predicate, so the list and streaming readers filter identically. ``domain`` is a
    domain, ``UNKNOWN_DOMAIN`` (records that name none) or "" (every record)."""
    if failed and record.get("ok") is not False:
        return False
    if domain and (record_domain(record) or UNKNOWN_DOMAIN) != domain:
        return False
    return not q or _matches(record, q)


def _domain_param(value: str) -> str:
    return (value or "").strip().lower()


def _filter_records(records: Iterable[Dict[str, Any]], q: str, failed: bool, domain: str = "") -> List[Dict[str, Any]]:
    q = (q or "").strip()
    return [r for r in records if _keep(r, q, failed, domain)]


def _rows_context(records: List[Dict[str, Any]], q: str = "", failed: bool = False, page: int = 1,
                  domain: str = "") -> dict:
    # Filter over the WHOLE log, then paginate the result — capping the read first would let a
    # single bulk apply push older matches out of the window without the UI ever saying so.
    filtered = _filter_records(records, q, failed, domain)
    total = len(filtered)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    return {
        "rows": filtered[start:start + PAGE_SIZE],
        "q": q, "failed": failed, "domain": domain, "page": page, "pages": pages, "total": total,
        "unknown_domain": UNKNOWN_DOMAIN,
    }


def _domain_choices(records: Iterable[Dict[str, Any]]) -> List[str]:
    """The domains the log's records name, sorted, then ``UNKNOWN_DOMAIN`` if any record names none."""
    named, unknown = set(), False
    for r in records:
        d = record_domain(r)
        if d:
            named.add(d)
        else:
            unknown = True
    return sorted(named) + ([UNKNOWN_DOMAIN] if unknown else [])


@router.get("", response_class=HTMLResponse)
async def audit_page(request: Request, failed: str = "", domain: str = "") -> HTMLResponse:
    # ``failed=1`` opens it filtered to failures (Home's "All failures" link, plan U4).
    records = read_records(_audit_path(request))
    total = len(records)
    failures = sum(1 for r in records if r.get("ok") is False)
    ctx = {"total": total, "failures": failures, "domains": _domain_choices(records)}
    ctx.update(_rows_context(records, failed=_flag(failed), domain=_domain_param(domain)))
    return TEMPLATES.TemplateResponse(request, _AUDIT_PAGE, ctx)


@router.get("/rows", response_class=HTMLResponse)
async def audit_rows(request: Request, q: str = "", failed: str = "", page: int = 1, domain: str = "") -> HTMLResponse:
    records = read_records(_audit_path(request))
    ctx = _rows_context(records, q, _flag(failed), page, _domain_param(domain))
    return TEMPLATES.TemplateResponse(request, _AUDIT_ROWS, ctx)


def _export_csv(path: Path, q: str, failed: bool, domain: str = "") -> Iterator[str]:
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

    # New columns go last so the earlier ones keep their places: "detail" (what a change with no argv
    # did), then "domain" (UNKNOWN_DOMAIN for a record that names none), "origin" and "rows" (a read's:
    # where it was started and its row count, blank when its output wasn't a table).
    writer.writerow(["ts", "action", "target", "ok", "exit_code", "error", "argv", "detail",
                     "domain", "origin", "rows"])
    yield flush()
    for r in iter_records(path):
        if not _keep(r, q, failed, domain):
            continue
        extra = _extra(r)
        error = extra.get("error", "")
        detail = extra.get("detail", "")
        argv = " ".join(str(a) for a in (r.get("argv") or []))
        writer.writerow([_csv_safe(c) for c in
                         (r.get("ts", ""), r.get("action", ""), r.get("target", ""),
                          r.get("ok"), r.get("exit_code"), error, argv, detail,
                          record_domain(r) or UNKNOWN_DOMAIN, extra.get("origin", ""), extra.get("rows", ""))])
        yield flush()


@router.get("/export.csv")
async def audit_export(request: Request, q: str = "", failed: str = "", domain: str = "") -> StreamingResponse:
    return StreamingResponse(
        _export_csv(_audit_path(request), q, _flag(failed), _domain_param(domain)),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-export.csv"},
    )
