"""Command Builder + Sequencer (/builder).

Browse/search the categorized GAM command catalog; for the curated *buildable* commands, fill typed
slots (drag a user/group in), preview the exact `gam …` (argv assembled only via `GAMCommands` —
never shell), run it through the guard, and optionally chain commands into a sequence run as one
audited BatchJob. Browse-only commands are inert (read/copy syntax only).
"""

from __future__ import annotations

import logging

import asyncio
import csv
import io
import re
import secrets
import shlex
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core import guard as guard_mod
from ...core import sheet_export
from ...core.catalog import load_catalog
from ...core.catalog.catalog import AREA_ORDER, COPY_FOLDER_REST, MOVE_FOLDER
from ...core.catalog.models import SlotKind
from ...core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from ...core.gam.commands import GAMCommands, carries_todrive
from ...core.gam.errors import GAMError
from ...core.setup import SetupService
from ...core.connectors.gam_connector import READ_ORIGIN_SEQUENCE, builder_done
from ...core.gam.parser import table_records
from ...core.bulk import stop_requested
from ...core.shared_drives import folder_move_of, manages_directly
from ..csvutil import csv_safe
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES, cell
from ._common import NOT_CONNECTED, app_state, as_of, error_partial, friendly, gam_detail, write_failed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/builder")

MAX_SEQUENCE_STEPS = 25

_SEQUENCE_PAGE = "_sequence.html"


def _catalog(request: Request):
    st = app_state(request)
    if st.catalog is None:
        st.catalog = load_catalog()
    return st.catalog


def _details(exc: Exception) -> str:
    """GAM's raw error text to show under the friendly message, so 'see details below' has details. Any other
    exception's text is Python's or the OS's: it goes to the log, never the page."""
    if isinstance(exc, GAMError):
        return (exc.stderr or "").strip() or exc.message
    logger.warning("a Builder run failed", exc_info=exc)
    return ""


# --- slot assembly ---------------------------------------------------------------------

async def _assemble(request: Request, cmd):
    """Read slot values from the form, validate, and build the argv via the curated builder.

    Returns (slots: dict, argv: list, target: str, error: str|None)."""
    form = await request.form()
    slots, target = {}, ""
    for slot in cmd.slots:
        val = str(form.get(slot.key) or "").strip()
        if slot.required and not val:
            return {}, [], "", f"{slot.label} is required."
        slots[slot.key] = val
        if not target and slot.kind in (SlotKind.TARGET_USER, SlotKind.USER, SlotKind.GROUP) and val:
            target = val
    try:
        argv = cmd.build(slots)
    except ValueError as exc:   # a builder's argument validation (e.g. a bad role): its words are ours
        return {}, [], "", str(exc)
    except Exception as exc:  # noqa: BLE001 — anything else is a fault, not the operator's input
        logger.warning("building a Builder command failed", exc_info=exc)
        return {}, [], "", "That command couldn't be built from these values, so nothing ran."
    return slots, list(argv), (target or "(command)"), None


def _preview_of(cmd, argv, target) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=target,
                         summary=cmd.name, risk=cmd.risk, argv=argv, meta={"cid": cmd.id})


async def _blocked(st, decision, commands) -> list:
    """What stops a preview: an account delete typed as an alias, or a change that would cut off the admin
    GamGUI signs in as (the run refuses that too; this says so before a confirm step is drawn)."""
    blocked = await _alias_deletes(st, decision)
    refusal = await st.connector.cut_off_refusal(commands, previewing=True) if st.connector is not None else ""
    return blocked + [refusal] if refusal else blocked


def _gam_str(argv) -> str:
    return "gam " + " ".join(argv)


async def _alias_deletes(st, decision) -> list:
    """An account delete typed as an alias would delete the account that owns it — refuse it at the
    preview. Each address is resolved by GAM itself (``info user``), so a secondary-domain alias or one
    added minutes ago is caught too. Fails closed: an address that can't be resolved can't be ruled out."""
    if not decision.typed_emails or st.connector is None:
        return []
    try:
        resolved = {a: await st.connector.primary_address(a) for a in decision.typed_emails}
    except Exception as exc:  # noqa: BLE001 - the message says why the delete can't be checked
        return [f"Couldn't confirm which account that address belongs to — {friendly(exc)}"]
    return guard_mod.alias_deletes(resolved)


async def _transfers(conn, decision) -> dict:
    """Unfinished data transfers from each account the change deletes — deleting one now loses what
    hasn't moved yet (the Users delete zone warns the same way) — and the accounts whose check
    couldn't be made, which are said, never read as having none (invariant 10)."""
    pending: list = []
    unknown: list = []
    for address in decision.typed_emails if conn is not None else []:
        found = await conn.incomplete_transfers_for(address)
        if found is None:
            unknown.append(address)
        else:
            pending += found
    return {"pending_transfers": pending, "transfers_unknown": unknown}


async def _folder_moves(st, previews) -> tuple:
    """A folder's move into a shared drive and its copy step (roadmap 4.8), read before they're held: what the folder holds,
    by who owns it, and whether the person is a Manager of the drive in their own name (GAM moves or copies into a drive only
    as one, verifyorganizer). A read that failed is said, never read as an empty folder or a Manager (invariant 10); only a
    read that answered "not a Manager" stops the preview. Returns (notes, refusals)."""
    conn = st.connector
    found = dict.fromkeys(m for m in (folder_move_of(p.argv) for p in previews
                                       if p.meta.get("cid") in (MOVE_FOLDER, COPY_FOLDER_REST)) if m)
    if conn is None or not found:
        return [], []
    domains = await st.tenant_domains()
    notes, refusals = [], []
    for email, folder, drive in found:
        (count, count_why), (members, manager_why) = await asyncio.gather(
            _answer(conn.folder_count(email, folder, domains)), _answer(conn.drive_members(drive)))
        manager = None if members is None else manages_directly(email, members)
        if manager is False:
            refusals.append(f"{email} isn't a Manager of that shared drive in their own name, and GAM moves or copies into a "
                            "drive only as one: make them a Manager on the Shared drives screen first.")
        notes.append({"email": email, "folder": folder, "drive": drive, "domains": domains, "count": count, "count_why": count_why,
                      "manager": manager, "manager_why": manager_why})
    return notes, refusals


async def _answer(read) -> tuple:
    """(what a read found, "") or (None, why it didn't answer, in words): unknown, never an answer (invariant 10)."""
    try:
        return await read, ""
    except Exception as exc:  # noqa: BLE001 — the preview says it couldn't tell, and why
        return None, friendly(exc)


def said_line(argv, output: str) -> str:
    """GAM's own line for a write that has one (roadmap 5.1, ``builder_done``): the new drive's ID, the role assignment's,
    the address renamed. "" for any other command, whose output this never shows."""
    if builder_done(argv) is None:
        return ""
    return next((line.strip() for line in (output or "").splitlines() if line.strip()), "")


# A mutation runs exactly what its preview showed: the preview holds the argv under a single-use token
# (web/previews.py) that its Run button posts back, and Run refuses a form that no longer matches —
# another command loaded, a slot retyped. Run once rebuilt the command from the live form, so a stale
# Run panel ran a command nobody had previewed (failure-log 2026-09-23). The sequence is held the
# same way, keyed by its steps. Reads are exempt: they change nothing, and run from the live form.
_FLOW = "builder"
_SEQ_FLOW = "builder_sequence"
_AGAIN = "here is a new preview; check it and run again"


def _form_key(cmd, slots: dict) -> tuple:
    return (cmd.id, tuple(sorted(slots.items())))


def _seq_key(seq) -> tuple:
    return tuple((s["cid"], s["target"], tuple(s["argv"]), s["risk"]) for s in seq)


async def _preview_page(request: Request, cmd, argv, target, slots, error: str = "") -> HTMLResponse:
    """The single-command preview: the exact `gam …`, the guard's decision, and its confirm step —
    for a mutation, holding what it shows under the token its Run button posts."""
    st = app_state(request)
    tenant = st.tenant_key()   # before the alias lookups read the directory (web/previews.py)
    decision = guard_mod.evaluate([_preview_of(cmd, argv, target)])
    folder_moves, refusals = await _folder_moves(st, [_preview_of(cmd, argv, target)])
    blocked = await _blocked(st, decision, [argv]) + refusals
    token = "" if cmd.risk == RiskLevel.READ_ONLY or blocked else st.previews.hold(
        _FLOW, _form_key(cmd, slots), (list(argv), target), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_builder_preview.html", {
        "cmd": cmd, "gam": _gam_str(argv), "decision": decision, "target": target, "slots": slots,
        **await _transfers(st.connector, decision), "folder_moves": folder_moves,
        "error": " ".join(blocked) or error, "blocked": bool(blocked), "token": token,
    })


async def _render_read(request: Request, out: str, cmd, argv, target: str, tenant: tuple) -> HTMLResponse:
    """Render a read command's output as a table when it looks tabular (CSV/JSON), else verbatim.

    Generic read commands span `print` (CSV/JSON → table) and `info`/`show` (human text → table
    would be garbage), so pick the renderer from the shape rather than forcing every read into a
    grid. ``tenant`` is the one the read started on: a switch that landed while it ran has already
    dropped the last result, so the old tenant's rows are neither kept for the CSV nor shown under
    the new one."""
    if app_state(request).tenant_key() != tenant:
        return error_partial(request, _SWITCHED)
    gam = _gam_str(argv)
    records = table_records(out) or []    # the same test the read's audit record counts rows by
    if records:
        # The whole result set stays here: the table's filter and pager work over every row of it
        # (plan U1 — a filter over only the rendered rows once hid row 101 on), and the CSV download
        # takes it too; for a sensitive read, so does what the download's audit record names (never
        # the rows). The id lets a stale table's request tell it has been replaced.
        # The read itself is kept too: Export to a Google Sheet… runs exactly this argv again (plan F4).
        last = app_state(request).builder_last_result = {
            "id": secrets.token_hex(6), "records": records, "gam": gam,
            "read": {"command": cmd.id, "argv": list(argv), "target": target},
            "sensitive": {"command": cmd.id, "argv": list(argv), "target": target} if cmd.sensitive else None}
        ctx = await _results_context(request, last, "", False, 1)
        ctx["sheet_ok"] = cmd.supports_export   # GAM's command takes todrive: Export to a Google Sheet… beside the CSV
        return TEMPLATES.TemplateResponse(request, "_records_table.html", ctx)
    return TEMPLATES.TemplateResponse(request, "_read_output.html", {"output": out, "gam": gam})


RESULT_ROWS = 10    # result rows a page — the table sits under the command's form in the build pane
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")
_NO_RESULT = "No results — run a read command first."
_REPLACED = "This result was replaced by a newer run — run the command again to see it."
_SWITCHED = "The active domain changed while this ran — run it again to read the domain that is active now."


def _columns_and_texts(last: dict) -> tuple:
    """Every column any row has, the first row's first (ragged JSON: the table and the CSV show the same
    columns), and each row's lowercased text to filter on — worked out once per result. The text is what
    the table shows (``cell``: a name as "Alice Anders") and, for a structured value, its raw form too,
    so a search finds what is on screen and still matches a field name."""
    if "texts" not in last:
        records = last["records"]
        last["cols"] = list(dict.fromkeys(k for r in records for k in r))
        last["texts"] = ["\n".join(_filter_text(r.get(k, "")) for k in last["cols"]).lower() for r in records]
    return last["cols"], last["texts"]


def _filter_text(value) -> str:
    shown = cell(value)
    return shown if isinstance(value, str) or str(value) == shown else f"{shown}\t{value}"


def _is_internal(address: str, domains: tuple) -> bool:
    """An address at one of the tenant's domains (primary, secondary, domain alias) or a subdomain of one."""
    host = address.rsplit("@", 1)[-1]
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_external(text: str, domains: tuple) -> bool:
    """The sharing-audit lens: a row that names "anyone" (a public or anyone-with-the-link share), or an
    address outside the tenant's domains."""
    if "anyone" in text:
        return True
    return bool(domains) and any(not _is_internal(e, domains) for e in _EMAIL.findall(text))


async def _matching(request: Request, last: dict, q: str, external: bool) -> list:
    """The result's rows that pass the filter and the External-only toggle — over every row, not a page."""
    q = q.strip().lower()
    domains = await app_state(request).tenant_domains() if external else ()
    _, texts = _columns_and_texts(last)
    return [r for r, t in zip(last["records"], texts, strict=True)
            if (not q or q in t) and (not external or _is_external(t, domains))]


async def _results_context(request: Request, last: dict, q: str, external: bool, page: int) -> dict:
    cols, _ = _columns_and_texts(last)
    rows = await _matching(request, last, q, external)
    pages = max(1, -(-len(rows) // RESULT_ROWS))
    page = min(max(1, page), pages)
    start = (page - 1) * RESULT_ROWS
    filtered = bool(q.strip()) or external
    query = {"rid": last.get("id", ""), "q": q.strip(), "external": "1" if external else ""}
    return {
        "rid": last.get("id", ""), "gam": last.get("gam", ""), "cols": cols, "q": q, "external": external,
        "rows": rows[start:start + RESULT_ROWS], "start": start, "matched": len(rows),
        "total": len(last["records"]), "page": page, "pages": pages, "filtered": filtered,
        "csv_url": "/builder/export.csv?" + urlencode({k: v for k, v in query.items() if v}),
    }


def _on(flag: str) -> bool:
    return flag in ("1", "true", "on")


def _last_result(request: Request, rid: str):
    """The retained result a table's request names, or why it can't be served."""
    last = app_state(request).builder_last_result
    if not last or not last.get("records"):
        return None, _NO_RESULT
    if rid and rid != last.get("id"):
        return None, _REPLACED
    return last, ""


@router.get("/results", response_class=HTMLResponse)
async def results(request: Request, rid: str = "", q: str = "", external: str = "",
                  page: int = 1) -> HTMLResponse:
    """A page of the last read's rows that match the filter — the table's filter box, External-only
    toggle and pager all ask here, so each searches the whole result, not the rows on screen."""
    last, why = _last_result(request, rid)
    if last is None:
        return error_partial(request, why)
    ctx = await _results_context(request, last, q, _on(external), page)
    # A pager button that just went disabled can't keep focus: the page line takes it, not <body>.
    trigger = request.headers.get("HX-Trigger")
    ctx["focus"] = (trigger == "rt-prev" and ctx["page"] == 1) or (trigger == "rt-next" and ctx["page"] == ctx["pages"])
    ctx["oob"] = True
    return TEMPLATES.TemplateResponse(request, "_records_page.html", ctx)


@router.get("/export.csv")
async def export_csv(request: Request, rid: str = "", q: str = "", external: str = "") -> Response:
    """Download the most recent read-command result set as CSV — every row, or, with the table's
    filter (``q``, ``external``), every row that matches it.

    The download of a sensitive read's result (backup codes, browser tokens) hands the secret out as a
    file, so it is audited like the Sheet export: ``sensitive_csv_export``, never the rows. With no
    connector to audit through, it is refused rather than served unrecorded."""
    st = app_state(request)
    last, why = _last_result(request, rid)
    conn = st.connector      # with the result, before any await: its record names the domain the rows came from (F6)
    if last is None:
        return Response(why, media_type="text/plain", status_code=404)
    records = await _matching(request, last, q, _on(external))
    sensitive = last.get("sensitive")
    if sensitive:
        if conn is None:
            return Response("Not connected — this result can't be exported without an audit record.",
                            media_type="text/plain", status_code=409)
        conn.audit_sensitive_csv(sensitive["command"], sensitive["argv"], sensitive["target"], rows=len(records))
    cols, _ = _columns_and_texts(last)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([csv_safe(k) for k in cols])  # header too — every emitted cell goes through the guard
    for r in records:
        writer.writerow([csv_safe(r.get(k, "")) for k in cols])
    name = "gam-results-filtered.csv" if q.strip() or _on(external) else "gam-results.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


# --- pages -----------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def page(request: Request, cid: str = "") -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "builder.html", {"connected": False})
    cat = _catalog(request)
    # `?cid=` opens that command's form, as Reports' named reports do (roadmap 3.1); an id the catalog doesn't hold opens none.
    opened = cat.by_id(cid) if cid else None
    counts = cat.area_counts()
    areas = [(a, counts[a]) for a in AREA_ORDER if counts.get(a)]
    # User/group lists aren't fetched here — the slot pickers query /builder/pick on demand so the
    # page loads instantly and the picker scales to large domains (only the cached top matches render).
    return TEMPLATES.TemplateResponse(request, "builder.html", {
        "connected": True, "areas": areas, "sequence": st.builder_sequence, "row_actions": ROW_ACTIONS,
        "open_cid": opened.id if opened is not None else "",
    })


PICK_LIMIT = 25   # most matches a slot picker shows at once — keeps large domains snappy


@router.get("/pick", response_class=HTMLResponse)
async def pick(request: Request, kind: str = "users", q: str = "") -> HTMLResponse:
    """Type-ahead for a User/Group slot: search the cached directory, return at most PICK_LIMIT hits.

    Scales to large domains — the match runs against the in-memory user cache (shared with the Users
    list) and only the top matches are ever rendered, never the whole directory."""
    st = app_state(request)
    ql = q.strip().lower()
    pairs = []
    age: dict = {}   # the list's "as of" label: served stale past the TTL while one refresh runs
    try:
        if kind == "groups" and st.connector is not None:
            pairs = [(g.email, getattr(g, "name", "")) for g in await st.groups(stale_ok=True)]
            age = as_of(st.group_cache)
        else:
            pairs = [(u.primary_email, getattr(u, "full_name", "")) for u in await st.users(stale_ok=True)]
            age = as_of(st.user_cache)
    except Exception:  # noqa: BLE001 — a directory hiccup just yields no suggestions
        pairs = []
    if ql:
        pairs = [(e, n) for e, n in pairs if ql in (e or "").lower() or ql in (n or "").lower()]
    return TEMPLATES.TemplateResponse(request, "_picker_options.html",
                                      {"items": pairs[:PICK_LIMIT], "more": len(pairs) > PICK_LIMIT,
                                       **age})


PAGE_SIZE = 6           # commands per page — sized so a page (2-line rows) fits a 13" window


def _paginated(request: Request, items, q="", page=1) -> HTMLResponse:
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE
    return TEMPLATES.TemplateResponse(request, "_catalog_list.html", {
        "items": items[start:start + PAGE_SIZE], "q": q, "page": page, "total": total, "pages": pages,
        "has_prev": page > 1, "has_next": page < pages,
    })


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_list(request: Request, area: str = "", q: str = "", buildable: str = "",
                       page: int = 1) -> HTMLResponse:
    """One flat, paginated list — search results, an area, or the buildable landing. No scroll box:
    each page is sized to fit, navigated with Prev/Next (the modern faceted-list pattern)."""
    cat = _catalog(request)
    q = q.strip()
    if q:
        items = cat.search(q)
    elif area:
        items = cat.in_area(area)
    else:
        items = cat.all_sorted()
    if _on(buildable):
        items = [c for c in items if c.buildable]
    return _paginated(request, items, q=q, page=page)


# Quick actions offered when you click an address in a result table. Each is a curated buildable
# command; `param` is the slot the clicked address pre-fills, `group` splits the menu (a result
# address may be a user or a group, so we offer both and let you pick the right one).
ROW_ACTIONS = [
    {"cid": "build.print_delegates", "label": "Delegates", "risk": "read", "param": "email", "group": "user"},
    {"cid": "build.print_forwarding", "label": "Forwarding", "risk": "read", "param": "email", "group": "user"},
    {"cid": "build.set_vacation", "label": "Set vacation", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.set_organization", "label": "Title / dept", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.reset_password", "label": "Reset password", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.suspend_user", "label": "Suspend", "risk": "destructive", "param": "email", "group": "user"},
    {"cid": "build.add_group_member", "label": "Add a member", "risk": "change", "param": "group", "group": "group"},
    {"cid": "build.remove_group_member", "label": "Remove a member", "risk": "change", "param": "group", "group": "group"},
]


@router.get("/command/{cid}", response_class=HTMLResponse)
async def command_form(request: Request, cid: str) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None:
        return error_partial(request, "Unknown command.")
    # Pre-fill any slot whose key is passed as a query param (e.g. ?email=alice@x.com from a row click).
    prefill = {k: v for k, v in request.query_params.items() if k != "cid"}
    return TEMPLATES.TemplateResponse(request, "_builder_form.html", {"cmd": cmd, "prefill": prefill})


# --- single-command preview + run ------------------------------------------------------

@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return error_partial(request, "That command can't be built — copy its syntax and run it in GAM directly.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return error_partial(request, error)
    return await _preview_page(request, cmd, argv, target, slots)


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    st = app_state(request)
    tenant, conn = st.tenant_key(), st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return error_partial(request, "That command can't be run from here.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return error_partial(request, error)
    if cmd.risk == RiskLevel.READ_ONLY:   # a read runs from the live form; its export to a Sheet is a write of its own (/sheet)
        try:
            out = await conn.catalog_read(cmd, argv, target)
        except Exception as exc:  # noqa: BLE001
            return error_partial(request, friendly(exc), _details(exc))
        return await _render_read(request, out, cmd, argv, target, tenant)
    # A mutation runs only from its preview: the held command, when the live form still matches it.
    # Otherwise the answer is a fresh preview of what the form holds now, to check before running.
    form = await request.form()
    token = str(form.get(TOKEN_FIELD) or "")
    held, refusal = st.previews.take(_FLOW, token, _form_key(cmd, slots), again=_AGAIN)
    if refusal:
        return await _preview_page(request, cmd, argv, target, slots,
                                   error=refusal if token else "Check what this will run, then run it.")
    preview = _preview_of(cmd, *held)
    # The preview's Run button sends confirmed=1 (and an account delete its typed address).
    refusal = guard_mod.enforce([preview], form, confirm_step=True)
    if refusal:
        return await _preview_page(request, cmd, argv, target, slots, error=refusal)
    result = (await conn.apply([preview]))[0]
    if not result.ok:   # what didn't happen, in words; GAM's own line one click away (roadmap 0.4)
        return write_failed(request, f"{cmd.name} failed.", result)
    # GAM's line for a write that has one (roadmap 5.1): the new drive's ID, the role assignment's, the address renamed.
    said = said_line(preview.argv or [], result.output)
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": True, "message": cmd.name + " — done",
                                                                      "said": said})


# --- export a read's table to a Google Sheet (plan F4) ------------------------------------

# A curated write: GAM runs the read again with `todrive` and makes a new Sheet in the connected admin's Drive. The preview
# holds the read's exact argv and the Sheet's name under a single-use token, keyed by the table it was made from (its id),
# and Export runs only that, once, with `confirmed=1` (guard.enforce), through conn.export_to_sheet -> _run_write.
_SHEET_FLOW = "builder_sheet"
_SHEET_AGAIN = "use Export to a Google Sheet… on the table again"


def _sheet_read(request: Request, rid: str):
    """The retained read a Sheet export names and its catalog command, or why it can't be exported."""
    last, why = _last_result(request, rid)
    if last is None:
        return None, None, why
    read = last.get("read") or {}
    cmd = _catalog(request).by_id(str(read.get("command") or ""))
    if cmd is None or not cmd.supports_export:
        return None, None, "This read can't go to a Google Sheet: GAM's command for it doesn't take todrive."
    if carries_todrive(read.get("argv") or ()):   # catalog_read refuses one; this holds no token for one either
        return None, None, "This read already says todrive, so it can't be exported again: build it without that word."
    return read, cmd, ""


def _sheet_preview_of(conn, cmd, argv) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=conn.sheet_owner() or "(admin Drive)",
                         summary=f"Export {cmd.name} to a Google Sheet", risk=RiskLevel.LOW, argv=argv, meta={"cid": cmd.id})


def _dwd_link(st, domain: str) -> str:
    """Setup's Domain-Wide Delegation link, every scope GamGUI uses filled in, or "" when it can't be made."""
    try:
        return str(SetupService(st.vault, st.runner).dwd_details(domain)["auth_url"])
    except Exception:  # noqa: BLE001 - only a link; the words say what to authorize
        logger.warning("couldn't make the Domain-Wide Delegation link", exc_info=True)
        return ""


async def _sheet_page(request: Request, rid: str, error: str = "") -> HTMLResponse:
    """What Export to a Google Sheet… runs: the read's exact `gam …` plus GAM's todrive options, whose Drive the Sheet
    goes in, that it holds a fresh read, and the scope check (a read). Held under a token its Export button posts back,
    unless the service account is known to lack a scope the export needs."""
    st = app_state(request)
    tenant, conn = st.tenant_key(), st.connector   # before the scope check reads (web/previews.py)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    read, cmd, why = _sheet_read(request, rid)
    if read is None:
        return error_partial(request, why)
    name = sheet_export.title(conn.domain, cmd.name)
    argv = list(read["argv"]) + GAMCommands.todrive_args(name)
    rows = len((st.builder_last_result or {}).get("records") or [])   # before the check: a switch during it drops them
    checked = await conn.sheet_export_scopes()
    missing = checked is not None and not all(checked.values())
    token = "" if missing else st.previews.hold(_SHEET_FLOW, rid, (cmd.id, list(read["argv"]), name), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_sheet_preview.html", {
        "cmd": cmd, "gam": "gam " + shlex.join(argv), "owner": conn.sheet_owner(), "name": name, "rid": rid,
        "rows": rows,
        "scopes": [(scope, what, None if checked is None else checked.get(scope)) for scope, what in sheet_export.SCOPES],
        "scope_words": sheet_export.scope_state(checked), "dwd_link": _dwd_link(st, conn.domain) if missing else "",
        "token": token, "error": error,
    })


@router.post("/sheet/preview", response_class=HTMLResponse)
async def sheet_preview(request: Request, rid: Annotated[str, Form()] = "") -> HTMLResponse:
    return await _sheet_page(request, rid)


@router.post("/sheet/export", response_class=HTMLResponse)
async def sheet_export_run(request: Request, rid: Annotated[str, Form()] = "") -> HTMLResponse:
    """Make the Sheet the preview showed, once: the held argv, never one rebuilt from the page.

    The connector is taken with the token, no await between them or from there to the export (plan F6): the token is
    bound to the domain its preview was made on, so this connector is that domain's, GAM runs the read there, and the
    record (``export_to_sheet``, ``sensitive_export``) names it even when a switch lands while the export runs."""
    st = app_state(request)
    form = await request.form()
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    token = str(form.get(TOKEN_FIELD) or "")
    held, refusal = st.previews.take(_SHEET_FLOW, token, rid, again=_SHEET_AGAIN, what="table")
    if refusal:
        return error_partial(request, refusal if token else "Check what the export will run first: " + _SHEET_AGAIN + ".")
    cid, read_argv, name = held
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.supports_export:
        return error_partial(request, "This read can't go to a Google Sheet: GAM's command for it doesn't take todrive.")
    refusal = guard_mod.enforce([_sheet_preview_of(conn, cmd, read_argv + GAMCommands.todrive_args(name))], form,
                                confirm_step=True)
    if refusal:
        return await _sheet_page(request, rid, error=refusal)
    try:
        res = await conn.export_to_sheet(cmd, read_argv, name)
    except ValueError:   # export_to_sheet's own refusal: not a read, or one carrying todrive. Nothing ran.
        logger.warning("a Sheet export was refused before GAM ran", exc_info=True)
        return error_partial(request, "This read can't be exported: it isn't a read, or it already carries todrive. "
                                      "Nothing ran.")
    shown = sheet_export.outcome(res.ok, res.noted.get("url", ""), res.noted.get("upload", ""), res.preview.target,
                                 "" if res.ok else res.detail)
    return TEMPLATES.TemplateResponse(request, "_export_result.html", {
        "outcome": shown, "name": name, "gam": "gam " + shlex.join(res.preview.argv), "detail": "" if res.ok else res.detail,
        # _run_write's "It worked, but the audit log couldn't record it": the Sheet is made, its record isn't, and Files
        # (which reads the log) won't list it, so the page must not say it was kept.
        "unrecorded": res.detail if res.ok else "",
    })


# --- sequence --------------------------------------------------------------------------

@router.post("/sequence/add", response_class=HTMLResponse)
async def seq_add(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    st = app_state(request)
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return error_partial(request, "That command can't be added.")
    if len(st.builder_sequence) >= MAX_SEQUENCE_STEPS:
        return error_partial(request, f"Sequence is capped at {MAX_SEQUENCE_STEPS} steps.")
    _, argv, target, error = await _assemble(request, cmd)
    if error:
        return error_partial(request, error)
    st.builder_sequence.append({
        "cid": cid, "label": cmd.name, "target": target, "argv": argv,
        "risk": int(cmd.risk), "gam": _gam_str(argv),
    })
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": st.builder_sequence})


@router.post("/sequence/remove", response_class=HTMLResponse)
async def seq_remove(request: Request, index: Annotated[int, Form()]) -> HTMLResponse:
    st = app_state(request)
    if 0 <= index < len(st.builder_sequence):
        st.builder_sequence.pop(index)
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": st.builder_sequence})


@router.post("/sequence/move", response_class=HTMLResponse)
async def seq_move(request: Request, index: Annotated[int, Form()], to: Annotated[int, Form()]) -> HTMLResponse:
    st = app_state(request)
    seq = st.builder_sequence
    if 0 <= index < len(seq) and 0 <= to < len(seq):
        seq.insert(to, seq.pop(index))
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": seq})


@router.post("/sequence/clear", response_class=HTMLResponse)
async def seq_clear(request: Request) -> HTMLResponse:
    app_state(request).builder_sequence.clear()
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": []})


def _seq_previews(seq) -> list:
    return [ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=s["target"],
                          summary=s["label"], risk=RiskLevel(s["risk"]), argv=s["argv"],
                          meta={"cid": s.get("cid", "")}) for s in seq]


async def _seq_preview_page(request: Request, seq, error: str = "") -> HTMLResponse:
    """The sequence's confirm step, holding the steps it shows under the token its Run form posts."""
    st = app_state(request)
    tenant = st.tenant_key()   # before the alias lookups read the directory (web/previews.py)
    previews = _seq_previews(seq)
    decision = guard_mod.evaluate(previews)
    folder_moves, refusals = await _folder_moves(st, previews)
    blocked = await _blocked(st, decision, [p.argv for p in previews]) + refusals
    token = "" if blocked else st.previews.hold(_SEQ_FLOW, _seq_key(seq), [dict(s) for s in seq], tenant=tenant)
    return TEMPLATES.TemplateResponse(request, "_sequence_preview.html", {
        "sequence": seq, "decision": decision, "error": " ".join(blocked) or error, "blocked": bool(blocked),
        "token": token, "folder_moves": folder_moves, **await _transfers(st.connector, decision)})


@router.post("/sequence/preview", response_class=HTMLResponse)
async def seq_preview(request: Request) -> HTMLResponse:
    st = app_state(request)
    if not st.builder_sequence:
        return error_partial(request, "The sequence is empty.")
    return await _seq_preview_page(request, list(st.builder_sequence))


async def _run_sequence(job, conn, previews, catalog=None) -> None:
    try:
        for p in previews:
            if stop_requested(job):   # Stop, between steps (plan U5)
                break
            job.current = p.summary
            cmd = catalog.by_id(p.meta.get("cid", "")) if catalog is not None else None
            try:
                if cmd is not None and cmd.risk == RiskLevel.READ_ONLY:
                    # A read step runs as the read it is, so it is recorded as one (`read`, or `sensitive_read`,
                    # never its output, origin `sequence`): through apply() it was filed as an `apply` like any write.
                    await conn.catalog_read(cmd, list(p.argv), p.target, origin=READ_ORIGIN_SEQUENCE)
                    ok, why, detail = True, "", ""
                else:
                    res = (await conn.apply([p]))[0]
                    ok, why, detail = res.ok, res.remediation, res.detail
                    if res.ok:   # GAM's line for a write that has one (a new drive's ID, a role assignment's), then any
                        detail = " ".join(filter(None, (said_line(p.argv or [], res.output), res.detail)))   # audit note
            except Exception as exc:  # noqa: BLE001 — report every step, never abort the run
                if not isinstance(exc, GAMError):
                    logger.warning("a sequence step failed", exc_info=exc)
                ok, why, detail = False, friendly(exc), gam_detail(exc)
            job.record(f"{p.summary} — {p.target}", ok, why, detail)
    finally:
        job.finish()


@router.post("/sequence/run", response_class=HTMLResponse)
async def seq_run(request: Request) -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    seq = list(st.builder_sequence)
    if not seq:
        return error_partial(request, "The sequence is empty.")
    # Only the steps the preview showed: a step added, removed or moved since is refused.
    form = await request.form()
    token = str(form.get(TOKEN_FIELD) or "")
    held, refusal = st.previews.take(_SEQ_FLOW, token, _seq_key(seq), again=_AGAIN, what="sequence")
    if refusal:
        return await _seq_preview_page(request, seq,
                                       error=refusal if token else "Check what this will run, then run it.")
    previews = _seq_previews(held)
    # Enforce the full guard server-side (mirrors /run): a bulk-destructive sequence needs typed
    # "confirm"; any other confirmation-requiring sequence needs the Confirm & run click; an account
    # delete, its address typed.
    refusal = guard_mod.enforce(previews, form)
    if refusal:
        return await _seq_preview_page(request, seq, error=refusal)
    n = len(previews)
    job = start_job(st.jobs, n, window=n,   # every step's row: at most MAX_SEQUENCE_STEPS
                    kind="sequence", title=f"Builder sequence — {n} step{'s' if n != 1 else ''}")
    job.task = asyncio.create_task(_run_sequence(job, conn, previews, _catalog(request)))
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": job})


@router.get("/sequence/status", response_class=HTMLResponse)
async def seq_status(request: Request, job: str = "") -> HTMLResponse:
    j = app_state(request).jobs.get(job)
    if j is None:
        return error_partial(request, "That run is no longer available.")
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": j})
