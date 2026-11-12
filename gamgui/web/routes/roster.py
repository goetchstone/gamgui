"""Plan a Classroom import: upload a SIS roster export and see what's in it (plan 2026-11-11 C2.2, C2.4).

Read only toward Google: nothing here calls GAM. The upload is read by `core/roster.py` (hostile until read) and kept by
`core/roster_store.py` (student data: `0600`, 14 days, Purge). A package that can't be read is refused under the picker
with its fix, and nothing is kept. Row problems are grouped by kind for the SIS team, with an Issues CSV.

Every POST here changes only this Mac (upload, purge, the column map's steps): `LOCAL_ONLY` in
`tests/test_write_routes_guarded.py`.

A single CSV that isn't a known export (plan C5.3) isn't read at once: it's held in memory (`roster_pending` on the app's
state, never on disk) while the operator maps its columns and sees three rows mapped, then read like any export. Maps are
saved by name (`core/roster_maps.py`: column names and role words only, never a row).
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import zipfile
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import UploadFile

from ...core.csv_safe import csv_safe
from ...core.roster import (ISSUE_WORDS, MEMBERS_CAP, UPLOAD_CAP, Issue, NeedsMap, PackageError, Snapshot, header_of,
                            read_upload)
from ...core.roster_mapped import FIELDS, ColumnMap, guess, preview, read_mapped
from ...core.roster_maps import MapStore
from ...core.roster_handover import HandoverError, kept_changes
from ...core.roster_handover import build as handover_files
from ...core.roster_plan import KINDS, plan
from ...core.roster_store import KEEP_DAYS, RosterStore, StoreError
from ..jobs import start_job
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, app_state
from .classroom import _why as explain

ISSUES_SHOWN = 20         # rows shown per kind of issue; the Issues CSV has the rest that were kept
_PAGE = "classroom_import.html"
_KEPT = "_roster_kept.html"
_PLAN = "_roster_plan.html"
_CHANGES = "_roster_changes.html"
_MAP = "_roster_map.html"
PREVIEW_ROWS = 3          # rows shown mapped before anything is read (plan C5.3)

router = APIRouter(prefix="/classroom/import")


def map_store(st) -> MapStore:
    """The saved column maps, made on first use (tests hand one in on the app state's ``roster_maps``)."""
    if st.roster_maps is None:
        st.roster_maps = MapStore()
    return st.roster_maps


def roster_store(st) -> RosterStore:
    """The roster store, made on first use (tests hand one in on ``AppState.roster_store``)."""
    if st.roster_store is None:
        st.roster_store = RosterStore()
    return st.roster_store


def _issues(found: Optional[Tuple[Dict[str, int], List[Issue]]], shown: Optional[int] = ISSUES_SHOWN) -> List[Dict[str, Any]]:
    """Each kind of issue: its words, its whole count and its rows (the first ``shown``, or all kept), most frequent first."""
    if found is None:
        return []
    counts, kept = found
    out = []
    for code, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        cause, fix = ISSUE_WORDS.get(code, (code, ""))
        rows = [i for i in kept if i.code == code]
        out.append({"code": code, "count": count, "cause": cause, "fix": fix, "rows": rows if shown is None else rows[:shown]})
    return out


def _kept(st) -> Dict[str, Any]:
    """What the page shows of the kept roster (its metadata and issues, never the people), or why it can't."""
    ctx: Dict[str, Any] = {"meta": None, "issues": [], "store_error": "", "keep_days": KEEP_DAYS, "shown": ISSUES_SHOWN}
    try:
        meta = roster_store(st).meta(st.audit_domain)
        found = roster_store(st).issues(st.audit_domain) if meta is not None else None
    except StoreError as exc:
        ctx["store_error"] = str(exc)
        return ctx
    ctx.update(meta=meta, issues=_issues(found))
    return ctx


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": False})
    return TEMPLATES.TemplateResponse(request, _PAGE, {"connected": True, **await asyncio.to_thread(_kept, st)})


async def _read_picked(request: Request) -> Tuple[List[Tuple[str, bytes]], str]:
    """The picked files as (name, bytes), bounded together by ``UPLOAD_CAP``, or why not. Starlette has already spooled
    them to a private temp file; each is read at most one byte past what's left of the cap, then closed."""
    form = await request.form(max_files=MEMBERS_CAP, max_fields=8)
    uploads: List[Tuple[str, bytes]] = []
    left = UPLOAD_CAP
    try:
        for value in form.getlist("files"):
            if not isinstance(value, UploadFile):
                continue
            body = await value.read(left + 1)
            if len(body) > left:
                return [], f"That's more than {UPLOAD_CAP >> 20} MB, more than a roster export should be."
            left -= len(body)
            if body or value.filename:
                uploads.append((value.filename or "", body))
    finally:
        await form.close()
    return uploads, ""


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request) -> HTMLResponse:
    """Read the picked export and keep it, replacing the last one. A package that can't be read is refused with its fix,
    under the picker, and nothing is kept. Audited (what, how many; never the rows)."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    st.roster_pending = None         # a new pick, read or refused, replaces a spreadsheet waiting for its map
    domain = st.audit_domain         # what was picked belongs to this domain, whatever the app is on when the read ends
    try:
        uploads, refusal = await _read_picked(request)
    except Exception:  # noqa: BLE001 — a malformed or oversized multipart body: said in words, never a 500
        uploads, refusal = [], "The upload couldn't be read. Choose the files again."
    if refusal:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": refusal})
    try:
        snap = await asyncio.to_thread(read_upload, uploads)
    except NeedsMap as needs:
        try:
            header = await asyncio.to_thread(header_of, needs.name, needs.body)
        except PackageError as exc:
            return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": str(exc)})
        if st.audit_domain != domain:
            return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": _MOVED})
        st.roster_pending = {"domain": domain, "name": needs.name, "body": needs.body, "header": header}
        return await _map_panel(request, st, None, first=True)
    except PackageError as exc:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": str(exc)})
    label = uploads[0][0] if len(uploads) == 1 else f"{len(uploads)} CSV files"
    return await _keep(request, st, snap, label, [body for _, body in uploads], domain)


_MOVED = "GamGUI switched to another domain while the file was read, so nothing was kept. Pick it again on its domain."


async def _keep(request: Request, st, snap: Snapshot, label: str, bodies: List[bytes], domain: str,
                extra: Optional[Dict[str, Any]] = None) -> HTMLResponse:
    """Keep a read snapshot (replacing the last), audit it (what, how many; never the rows) and show it. Only on the
    domain it was picked on: a tenant switch during the read discards it, never keeps one district's roster under
    another's name."""
    if st.audit_domain != domain or st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": _MOVED})
    hasher = hashlib.sha256()        # piece by piece: joining the bytes first was another copy of the whole upload
    for body in bodies:
        hasher.update(body)
    digest = hasher.hexdigest()
    try:
        meta = await asyncio.to_thread(roster_store(st).save, domain, snap, label, digest)
    except StoreError as exc:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": str(exc)})
    st.connector.audit.record("roster_import", connector="local", target=domain, ok=True,
                              extra={"source": snap.source, "label": label, "sha256": digest, "counts": meta["counts"],
                                     "issues": snap.issue_counts, **(extra or {})})
    return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "just_read": True})


# --- a spreadsheet mapped by its columns (C5.3) ------------------------------------------------------------------------
def _pending(st) -> Optional[dict]:
    """The spreadsheet waiting for its map, if it was picked on this domain."""
    held = st.roster_pending
    return held if held and held.get("domain") == st.audit_domain else None


def _map_from(form) -> ColumnMap:
    """The map as the form posts it: a column per field ("" = not used) and the role words, comma-separated."""
    def words(key: str, default: str) -> List[str]:
        raw = str(form.get(key, default) or "")[:400]
        return [w.strip().lower() for w in raw.split(",") if w.strip()][:20]
    return ColumnMap.from_json({**{key: str(form.get(f"col_{key}", "") or "")[:200] for key, _, _ in FIELDS},
                                "teacher_words": words("teacher_words", "teacher"),
                                "student_words": words("student_words", "student")})


async def _map_panel(request: Request, st, cmap: Optional[ColumnMap], *, first: bool = False, error: str = "",
                     using: str = "") -> HTMLResponse:
    """The mapping step: the spreadsheet's columns, the map (a saved one that fits, or a guess, on first showing), and
    ``PREVIEW_ROWS`` rows read with it. Nothing is kept until "Read with this map"."""
    held = _pending(st)
    if held is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st),
                                                           "upload_error": "Pick the spreadsheet again."})
    header: List[str] = held["header"]
    store = map_store(st)
    saved = await asyncio.to_thread(store.names)
    if cmap is None:
        fits = [n for n in saved if (m := store.get(n)) is not None and not m.problem(header)]
        found = store.get(fits[0]) if first and fits else None
        cmap, using = (found, fits[0]) if found is not None else (guess(header), "")
    problem = cmap.problem(header)
    rows: List[Dict[str, str]] = []
    if not problem:
        try:
            rows = await asyncio.to_thread(preview, held["name"], held["body"], cmap, PREVIEW_ROWS)
        except PackageError as exc:
            problem = str(exc)
    return TEMPLATES.TemplateResponse(request, _MAP, {
        "name": held["name"], "header": header, "cmap": cmap, "fields": FIELDS, "rows": rows, "problem": problem,
        "error": error, "saved": saved, "using": using, "set_aside": store.set_aside})


@router.post("/map/preview", response_class=HTMLResponse)
async def map_preview(request: Request) -> HTMLResponse:
    """Show the map again with what was changed, or with a saved map picked (``use``)."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    form = await request.form(max_files=0, max_fields=32)
    # Every control posts the whole form, so `use` is posted with each change: it loads a saved map only when its own
    # select changed (htmx names the control that fired), or a column changed after picking one would be undone.
    use = str(form.get("use", "") or "") if request.headers.get("HX-Trigger-Name") == "use" else ""
    chosen = map_store(st).get(use) if use else None
    if chosen is not None:
        return await _map_panel(request, st, chosen, using=use)
    return await _map_panel(request, st, _map_from(form))


@router.post("/map/read", response_class=HTMLResponse)
async def map_read(request: Request) -> HTMLResponse:
    """Read the held spreadsheet with the posted map and keep it, like any export; save the map first when it's named."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    form = await request.form(max_files=0, max_fields=32)
    cmap = _map_from(form)
    held = _pending(st)
    if held is None or cmap.problem(held["header"]):
        return await _map_panel(request, st, cmap)
    save_as = str(form.get("save_as", "") or "").strip()
    if save_as:
        try:
            await asyncio.to_thread(map_store(st).save, save_as, cmap)
        except ValueError as exc:
            return await _map_panel(request, st, cmap, error=str(exc))
    try:
        snap = await asyncio.to_thread(read_mapped, held["name"], held["body"], cmap)
    except PackageError as exc:
        return await _map_panel(request, st, cmap, error=str(exc))
    if st.roster_pending is not held:         # dropped, replaced or switched away from while it was read: keep nothing
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": _MOVED})
    st.roster_pending = None
    # Audited with which fields were mapped, never the column names: a name is the spreadsheet's text.
    mapped = sorted(key for key, _, _ in FIELDS if cmap.column(key))
    return await _keep(request, st, snap, held["name"], [held["body"]], held["domain"], {"mapped_fields": mapped})


@router.post("/map/delete", response_class=HTMLResponse)
async def map_delete(request: Request) -> HTMLResponse:
    """Delete a saved map (never the spreadsheet), and show the map step again as it was."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    form = await request.form(max_files=0, max_fields=32)
    name = str(form.get("delete", "") or "")
    if name:
        await asyncio.to_thread(map_store(st).delete, name)
    return await _map_panel(request, st, _map_from(form))


@router.post("/map/cancel", response_class=HTMLResponse)
async def map_cancel(request: Request) -> HTMLResponse:
    """Drop the held spreadsheet without reading it."""
    st = app_state(request)
    st.roster_pending = None
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    return TEMPLATES.TemplateResponse(request, _KEPT, await asyncio.to_thread(_kept, st))


@router.post("/purge", response_class=HTMLResponse)
async def purge(request: Request) -> HTMLResponse:
    """Delete the kept roster now. Audited."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _KEPT, {"upload_error": NOT_CONNECTED})
    st.roster_pending = None
    try:
        gone = await asyncio.to_thread(roster_store(st).purge, st.audit_domain)
    except StoreError as exc:
        return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "upload_error": str(exc)})
    st.connector.audit.record("roster_purge", connector="local", target=st.audit_domain, ok=True, extra={"deleted": gone})
    return TEMPLATES.TemplateResponse(request, _KEPT, {**await asyncio.to_thread(_kept, st), "purged": gone})


@router.get("/issues.csv")
async def issues_csv(request: Request) -> Response:
    """The kept roster's issues for the SIS team: each kind's cause and fix, and each row kept (up to the reader's sample
    per kind; the counts on the screen are whole). Formula-safe (`csv_safe`)."""
    st = app_state(request)
    if st.connector is None:
        return Response("Not connected.\n", status_code=400, media_type="text/plain")
    try:
        found = await asyncio.to_thread(roster_store(st).issues, st.audit_domain)
    except StoreError:
        return Response("The kept roster couldn't be read.\n", status_code=500, media_type="text/plain")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["kind", "cause", "fix", "file", "line", "ID", "detail", "kind_total"])
    for kind in _issues(found, shown=None):
        for i in kind["rows"]:
            writer.writerow([csv_safe(v) for v in (kind["code"], kind["cause"], kind["fix"], i.file, i.row, i.source_id,
                                                   i.detail, kind["count"])])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="roster-issues.csv"'})


# --- compare with Classroom (C3) ------------------------------------------------------------------------------------
CHANGES_PAGE = 50         # changes per page of the change list


def _plan_for(st) -> Optional[Dict[str, Any]]:
    """The last compare, if it's this domain's and of the roster kept now (a new upload or a purge drops it)."""
    held = st.roster_compare
    if not held or held.get("domain") != st.audit_domain:
        return None
    try:
        meta = roster_store(st).meta(st.audit_domain)
    except StoreError:
        return None
    return held if meta is not None and meta.get("sha256") == held.get("sha256") else None


async def _compare(job, st, domain: str) -> None:
    """Background: read Classroom (two GAM runs), plan against the kept roster, and hold the change list. Read only."""
    try:
        job.current = "Reading every class in Classroom, with its people…"
        store = roster_store(st)
        meta = await asyncio.to_thread(store.meta, domain)
        snap = await asyncio.to_thread(store.load, domain)
        if meta is None or snap is None:
            job.error = "No roster is kept to compare. Read the export again."
            return
        classes, unread = await st.connector.classroom_state()
        directory: Optional[set] = None
        try:
            directory = {u.primary_email.lower() for u in await st.users()}   # fresh: it decides what is added
        except Exception:  # noqa: BLE001 — without the directory, addresses aren't checked (the plan says so)
            directory = None
        out = await asyncio.to_thread(plan, snap, classes, directory, len(unread))
        if directory is None:
            out.notes.append("The directory couldn't be read, so addresses weren't checked against Workspace accounts.")
        st.roster_compare = {"domain": domain, "sha256": meta.get("sha256"), "when": clock_text(), "plan": out}
        job.applied = len(out.changes)
    except Exception as exc:  # noqa: BLE001 — said in fixed words: an exception's own text never reaches a page
        job.error = explain(exc)
    finally:
        job.finish()


def clock_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _changes_ctx(held: Dict[str, Any], kind: str = "", q: str = "", page: int = 1) -> Dict[str, Any]:
    out = held["plan"]
    rows = [c for c in out.changes if (not kind or c.kind == kind)]
    ql = q.strip().lower()
    if ql:
        rows = [c for c in rows if ql in c.title.lower() or ql in c.person or ql in c.alias.lower()]
    pages = max(1, -(-len(rows) // CHANGES_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * CHANGES_PAGE
    return {"plan": out, "when": held["when"], "kinds": KINDS, "words": dict(KINDS), "kind": kind, "q": q.strip(),
            "rows": rows[start:start + CHANGES_PAGE], "total": len(rows), "page": page, "pages": pages, "start": start}


@router.post("/compare", response_class=HTMLResponse)
async def compare(request: Request) -> HTMLResponse:
    """Compare the kept roster with Classroom, as a job (a district's read takes minutes). One at a time. Reads only."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _PLAN, {"error": NOT_CONNECTED})
    running = st.jobs.get(st.roster_compare_job)
    if running is not None and not running.finished:
        return TEMPLATES.TemplateResponse(request, _PLAN, {"job": running})
    job = start_job(st.jobs, 0, kind="roster_compare", title="Compare the roster with Classroom")
    st.roster_compare_job = job.id
    job.task = asyncio.create_task(_compare(job, st, st.audit_domain))
    return TEMPLATES.TemplateResponse(request, _PLAN, {"job": job})


@router.get("/compare/status", response_class=HTMLResponse)
async def compare_status(request: Request, job: str = "") -> HTMLResponse:
    """The compare's panel: a sentence while it reads, then the change list (or why not)."""
    st = app_state(request)
    j = st.jobs.get(job) if job else None
    if j is not None and not j.finished:
        return TEMPLATES.TemplateResponse(request, _PLAN, {"job": j})
    if j is not None and j.error:
        return TEMPLATES.TemplateResponse(request, _PLAN, {"job": j, "error": j.error})
    held = _plan_for(st)
    if held is None:
        return TEMPLATES.TemplateResponse(request, _PLAN, {})
    return TEMPLATES.TemplateResponse(request, _PLAN, {"job": j, **_changes_ctx(held)})


@router.get("/changes", response_class=HTMLResponse)
async def changes(request: Request, kind: str = "", q: str = "", page: str = "1") -> HTMLResponse:
    """One page of the change list, by kind and search (server-side)."""
    st = app_state(request)
    held = _plan_for(st) if st.connector is not None else None
    if held is None:
        return TEMPLATES.TemplateResponse(request, _CHANGES, {"rows": [], "total": 0, "gone": True})
    kind = kind if kind in dict(KINDS) else ""
    return TEMPLATES.TemplateResponse(request, _CHANGES,
                                      _changes_ctx(held, kind, q, int(page) if page.isdigit() else 1))


# --- hand the change list over (C4) ---------------------------------------------------------------------------------
def _zip_name(domain: str, when: str) -> str:
    stamp = "".join(ch for ch in when if ch.isdigit())[:12]
    safe = "".join(ch for ch in domain if ch.isascii() and (ch.isalnum() or ch in ".-")) or "domain"
    return f"classroom-changes-{safe}-{stamp}.zip"


@router.get("/handover.zip")
async def handover(request: Request, include: Annotated[Optional[List[str]], Query()] = None) -> Response:
    """The change list as files someone runs with GAM outside GamGUI (plan C4): README.txt, review.csv and a numbered CSV
    per kind. ``include`` names flagged kinds the operator chose to include; the rest of a flagged kind stays out. An
    incomplete list hands nothing over. Audited: what was handed over, never the rows."""
    st = app_state(request)
    held = _plan_for(st) if st.connector is not None else None
    if held is None:
        return Response("There's no change list to hand over. Compare with Classroom first.\n", status_code=400,
                        media_type="text/plain")
    chosen = [k for k in (include or []) if k in held["plan"].flags]
    try:
        files = handover_files(held["plan"], st.audit_domain, held["when"], chosen)
    except HandoverError as exc:
        return Response(str(exc) + "\n", status_code=400, media_type="text/plain")
    stamp = datetime.strptime(held["when"], "%Y-%m-%d %H:%M UTC").timetuple()[:6]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=stamp)         # the compare's time: one compare, one set of bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[name])
    kept = kept_changes(held["plan"], chosen)
    counts: Dict[str, int] = {}
    for change in kept:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    st.connector.audit.record("roster_handover", connector="local", target=st.audit_domain, ok=True,
                              extra={"compared": held["when"], "counts": counts, "included_flagged": chosen})
    return Response(out.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_zip_name(st.audit_domain, held["when"])}"'})
