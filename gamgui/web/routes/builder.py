"""Command Builder + Sequencer (/builder).

Browse/search the categorized GAM command catalog; for the curated *buildable* commands, fill typed
slots (drag a user/group in), preview the exact `gam …` (argv assembled only via `GAMCommands` —
never shell), run it through the guard, and optionally chain commands into a sequence run as one
audited BatchJob. Browse-only commands are inert (read/copy syntax only).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard as guard_mod
from ...core.catalog import load_catalog
from ...core.catalog.catalog import AREA_ORDER
from ...core.catalog.models import SlotKind
from ...core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from ...core.gam.commands import GAMCommands
from ...core.gam.errors import GAMError
from ...core.gam.parser import parse_records
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/builder")

MAX_SEQUENCE_STEPS = 25


def _st(request: Request):
    return request.app.state.gamgui


def _catalog(request: Request):
    st = _st(request)
    if st.catalog is None:
        st.catalog = load_catalog()
    return st.catalog


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _err(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html", {"ok": False, "message": message})


# --- slot assembly ---------------------------------------------------------------------

async def _assemble(request: Request, cmd):
    """Read slot values from the form, validate, and build the argv via the curated builder.

    Returns (slots: dict, argv: list, target: str, error: str|None)."""
    form = await request.form()
    slots, target = {}, ""
    for slot in cmd.slots:
        val = (form.get(slot.key) or "").strip()
        if slot.required and not val:
            return {}, [], "", f"{slot.label} is required."
        slots[slot.key] = val
        if not target and slot.kind in (SlotKind.TARGET_USER, SlotKind.USER, SlotKind.GROUP) and val:
            target = val
    try:
        argv = cmd.build(slots)
    except Exception as exc:  # noqa: BLE001 — a builder/validation error (e.g. bad role)
        return {}, [], "", str(exc)
    return slots, list(argv), (target or "(command)"), None


def _preview_of(cmd, argv, target) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=target,
                         summary=cmd.name, risk=cmd.risk, argv=argv)


def _gam_str(argv) -> str:
    return "gam " + " ".join(argv)


def _render_read(request: Request, out: str, gam: str) -> HTMLResponse:
    """Render a read command's output as a table when it looks tabular (CSV/JSON), else verbatim.

    Generic read commands span `print` (CSV/JSON → table) and `info`/`show` (human text → table
    would be garbage), so pick the renderer from the shape rather than forcing every read into a
    grid."""
    text = (out or "").strip()
    first = text.splitlines()[0] if text else ""
    looks_tabular = text[:1] in "[{" or "," in first
    records = parse_records(out) if looks_tabular else []
    if records:
        return TEMPLATES.TemplateResponse(request, "_records_table.html", {"records": records, "gam": gam})
    return TEMPLATES.TemplateResponse(request, "_read_output.html", {"output": out, "gam": gam})


# --- pages -----------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "builder.html", {"connected": False})
    cat = _catalog(request)
    counts = cat.area_counts()
    areas = [(a, counts[a]) for a in AREA_ORDER if counts.get(a)]
    # User/group lists aren't fetched here — the slot pickers query /builder/pick on demand so the
    # page loads instantly and the picker scales to large domains (only the cached top matches render).
    return TEMPLATES.TemplateResponse(request, "builder.html", {
        "connected": True, "areas": areas, "sequence": st.builder_sequence, "row_actions": ROW_ACTIONS,
    })


PICK_LIMIT = 25   # most matches a slot picker shows at once — keeps large domains snappy


@router.get("/pick", response_class=HTMLResponse)
async def pick(request: Request, kind: str = "users", q: str = "") -> HTMLResponse:
    """Type-ahead for a User/Group slot: search the cached directory, return at most PICK_LIMIT hits.

    Scales to large domains — the match runs against the in-memory user cache (shared with the Users
    list) and only the top matches are ever rendered, never the whole directory."""
    st = _st(request)
    ql = q.strip().lower()
    pairs = []
    try:
        if kind == "groups" and st.connector is not None:
            pairs = [(g.email, getattr(g, "name", "")) for g in await st.connector.list_groups()]
        else:
            pairs = [(u.primary_email, getattr(u, "full_name", "")) for u in await st.users()]
    except Exception:  # noqa: BLE001 — a directory hiccup just yields no suggestions
        pairs = []
    if ql:
        pairs = [(e, n) for e, n in pairs if ql in (e or "").lower() or ql in (n or "").lower()]
    return TEMPLATES.TemplateResponse(request, "_picker_options.html",
                                      {"items": pairs[:PICK_LIMIT], "more": len(pairs) > PICK_LIMIT})


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
    items = cat.search(q) if q else (cat.in_area(area) if area else cat.all_sorted())
    if buildable in ("1", "true", "on"):
        items = [c for c in items if c.buildable]
    return _paginated(request, items, q=q, page=page)


# Quick actions offered when you click a person in a result table — each is a curated buildable
# command keyed on the "email" slot, so the clicked address pre-fills the form.
ROW_ACTIONS = [
    {"cid": "build.print_delegates", "label": "Delegates", "risk": "read"},
    {"cid": "build.print_forwarding", "label": "Forwarding", "risk": "read"},
    {"cid": "build.set_vacation", "label": "Set vacation", "risk": "change"},
    {"cid": "build.set_signature", "label": "Set signature", "risk": "change"},
    {"cid": "build.set_organization", "label": "Title / dept", "risk": "change"},
    {"cid": "build.reset_password", "label": "Reset password", "risk": "change"},
    {"cid": "build.suspend_user", "label": "Suspend", "risk": "destructive"},
    {"cid": "build.delete_user", "label": "Delete account", "risk": "destructive"},
]


@router.get("/command/{cid}", response_class=HTMLResponse)
async def command_form(request: Request, cid: str) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None:
        return _err(request, "Unknown command.")
    # Pre-fill any slot whose key is passed as a query param (e.g. ?email=alice@x.com from a row click).
    prefill = {k: v for k, v in request.query_params.items() if k != "cid"}
    return TEMPLATES.TemplateResponse(request, "_builder_form.html", {"cmd": cmd, "prefill": prefill})


# --- single-command preview + run ------------------------------------------------------

@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, cid: str = Form(...)) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be built — copy its syntax and run it in GAM directly.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    decision = guard_mod.evaluate([_preview_of(cmd, argv, target)])
    return TEMPLATES.TemplateResponse(request, "_builder_preview.html", {
        "cmd": cmd, "gam": _gam_str(argv), "decision": decision, "target": target, "slots": slots,
    })


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, cid: str = Form(...)) -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be run from here.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    preview = _preview_of(cmd, argv, target)
    if cmd.risk == RiskLevel.READ_ONLY:
        form = await request.form()
        export = bool(form.get("td_export"))
        if export:  # send the result to a Google Sheet instead of the in-app table
            owner = (form.get("td_user") or "").strip()
            argv = argv + GAMCommands.todrive_args(owner, (form.get("td_title") or "").strip())
        try:
            out = await conn.runner.run_authenticated(conn.domain, argv)
        except Exception as exc:  # noqa: BLE001
            return _err(request, _friendly(exc))
        if export:
            return TEMPLATES.TemplateResponse(request, "_export_result.html",
                                              {"gam": _gam_str(argv), "output": out,
                                               "owner": owner or "the admin account"})
        return _render_read(request, out, _gam_str(argv))
    # A mutation that needs confirmation must come back through the preview (the "Confirm & run"
    # button sends confirmed=1) — a bare POST never silently runs a destructive command.
    decision = guard_mod.evaluate([preview])
    form = await request.form()
    if decision.requires_confirmation and not form.get("confirmed"):
        return TEMPLATES.TemplateResponse(request, "_builder_preview.html", {
            "cmd": cmd, "gam": _gam_str(argv), "decision": decision, "target": target, "slots": slots,
        })
    result = (await conn.apply([preview]))[0]
    return TEMPLATES.TemplateResponse(request, "_action_result.html",
                                      {"ok": result.ok, "message": (cmd.name + " — " + ("done" if result.ok else result.detail))})


# --- sequence --------------------------------------------------------------------------

@router.post("/sequence/add", response_class=HTMLResponse)
async def seq_add(request: Request, cid: str = Form(...)) -> HTMLResponse:
    st = _st(request)
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be added.")
    if len(st.builder_sequence) >= MAX_SEQUENCE_STEPS:
        return _err(request, f"Sequence is capped at {MAX_SEQUENCE_STEPS} steps.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    st.builder_sequence.append({
        "cid": cid, "label": cmd.name, "target": target, "argv": argv,
        "risk": int(cmd.risk), "gam": _gam_str(argv),
    })
    return TEMPLATES.TemplateResponse(request, "_sequence.html", {"sequence": st.builder_sequence})


@router.post("/sequence/remove", response_class=HTMLResponse)
async def seq_remove(request: Request, index: int = Form(...)) -> HTMLResponse:
    st = _st(request)
    if 0 <= index < len(st.builder_sequence):
        st.builder_sequence.pop(index)
    return TEMPLATES.TemplateResponse(request, "_sequence.html", {"sequence": st.builder_sequence})


@router.post("/sequence/move", response_class=HTMLResponse)
async def seq_move(request: Request, index: int = Form(...), to: int = Form(...)) -> HTMLResponse:
    st = _st(request)
    seq = st.builder_sequence
    if 0 <= index < len(seq) and 0 <= to < len(seq):
        seq.insert(to, seq.pop(index))
    return TEMPLATES.TemplateResponse(request, "_sequence.html", {"sequence": seq})


@router.post("/sequence/clear", response_class=HTMLResponse)
async def seq_clear(request: Request) -> HTMLResponse:
    _st(request).builder_sequence.clear()
    return TEMPLATES.TemplateResponse(request, "_sequence.html", {"sequence": []})


def _seq_previews(seq) -> list:
    return [ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=s["target"],
                          summary=s["label"], risk=RiskLevel(s["risk"]), argv=s["argv"]) for s in seq]


@router.post("/sequence/preview", response_class=HTMLResponse)
async def seq_preview(request: Request) -> HTMLResponse:
    st = _st(request)
    if not st.builder_sequence:
        return _err(request, "The sequence is empty.")
    decision = guard_mod.evaluate(_seq_previews(st.builder_sequence))
    return TEMPLATES.TemplateResponse(request, "_sequence_preview.html",
                                      {"sequence": st.builder_sequence, "decision": decision})


async def _run_sequence(job, conn, previews) -> None:
    try:
        for p in previews:
            job.current = p.summary
            try:
                res = (await conn.apply([p]))[0]
                ok, detail = res.ok, res.detail
            except Exception as exc:  # noqa: BLE001 — report every step, never abort the run
                ok, detail = False, str(exc)
            line = f"{p.summary} — {p.target}" + (f": {detail}" if (not ok and detail) else "")
            job.log.append(("✓ " if ok else "✗ ") + line)
            (job.__setattr__("applied", job.applied + 1) if ok else job.failed.append(f"{p.summary} ({p.target})"))
            job.done += 1
    finally:
        job.current = ""
        job.finished = True


@router.post("/sequence/run", response_class=HTMLResponse)
async def seq_run(request: Request, confirm: str = Form(""), confirmed: str = Form("")) -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    seq = list(st.builder_sequence)
    if not seq:
        return _err(request, "The sequence is empty.")
    previews = _seq_previews(seq)
    decision = guard_mod.evaluate(previews)
    # Enforce the full guard server-side (mirrors /run): a bulk-destructive sequence needs typed
    # "confirm"; any other confirmation-requiring sequence needs the Confirm & run click.
    def _needs_confirm(msg: str = "") -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_sequence_preview.html",
                                          {"sequence": seq, "decision": decision, "error": msg})
    if decision.requires_typed_confirmation:
        if confirm.strip().lower() != "confirm":
            return _needs_confirm("Type confirm to run this destructive bulk sequence.")
    elif decision.requires_confirmation and not confirmed:
        return _needs_confirm()
    job = start_job(st.jobs, len(previews))
    job.task = asyncio.create_task(_run_sequence(job, conn, previews))
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": job})


@router.get("/sequence/status", response_class=HTMLResponse)
async def seq_status(request: Request, job: str = "") -> HTMLResponse:
    j = _st(request).jobs.get(job)
    if j is None:
        return _err(request, "That run is no longer available.")
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": j})
