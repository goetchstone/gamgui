"""The Groups board (/groups): find a group, see its members and their roles, add someone by name or
email with a role, and remove a member after a confirm step (plan U7, A6); and change seven of its settings, each change
previewed (roadmap 4.4, at the end).

Every list here is capped server-side: the group finder and the people type-ahead return the top
matches from the cached directory, never the whole of it, and a group's members come a page at a time.

The add field is free text, and GAM reads the member as a <UserTypeEntity>: ``oauthuser`` is the
authorizing admin, a bare name becomes name@<domain> and a comma adds two people. So a group or member
that isn't one full address is refused in words before GAM (``looks_like_email``).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import bulk, group_settings, guard
from ...core.connectors.base import RiskLevel
from ...core.gam.errors import GAMErrorKind
from ...core.gam.commands import GROUP_ROLES, GROUP_SETTINGS, is_one_address, settings_skipped
from ...core.onboarding import looks_like_email
from ..jobs import retry_key, retry_of, start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import GAM_TROUBLE, NOT_CONNECTED, app_state, as_of, error_partial, friendly, unread_why, write_failed

PICK_LIMIT = 15      # group and people suggestions shown at once — keep typing to narrow
MEMBERS_PAGE = 50    # member rows per page; the list scrolls inside the fixed window
_ROLE_RANK = {"OWNER": 0, "MANAGER": 1, "MEMBER": 2}

_GROUPS_PAGE = "groups.html"
_LIST = "_board_members.html"

router = APIRouter(prefix="/groups")


def _pick(items, q: str, *fields) -> tuple[list, bool]:
    """The first PICK_LIMIT of ``items`` whose ``fields`` contain ``q`` (case-insensitive), and whether
    there were more."""
    ql = q.strip().lower()
    hits = [it for it in items if not ql or any(ql in (getattr(it, f, "") or "").lower() for f in fields)]
    return hits[:PICK_LIMIT], len(hits) > PICK_LIMIT


async def _group_choices(st, q: str = "", refresh: bool = False) -> dict:
    try:   # cached `gam print groups`, shared with onboarding's picker; the list says how old it is
        groups = await st.groups(force=refresh, stale_ok=True)
    except Exception as exc:  # noqa: BLE001 — any failure is shown in words, not a 500
        return {"groups": [], "more": False, "find_q": q, "find_error": "Couldn't list groups. " + friendly(exc)}
    shown, more = _pick(groups, q, "email", "name")
    return {"groups": shown, "more": more, "find_q": q, "find_error": "", **as_of(st.group_cache)}


async def _list_ctx(conn, group: str, q: str = "", page: int = 1, **extra) -> dict:
    """What ``_board_members.html`` shows: one page of ``group``'s members matching ``q``, owners and
    managers first. ``extra`` carries a write's result (message/error/details); ``focus=False`` keeps an
    operator's click from moving focus into the list (an add leaves it in the add form)."""
    ctx: dict = {"group": group, "q": q, "members": [], "total": 0, "page": 1, "pages": 1, "start": 0,
                 "message": "", "error": "", "details": "", "focus": True, **extra}
    try:
        members = await conn.list_group_members(group)
    except Exception as exc:  # noqa: BLE001 — a failed write's own message outranks the re-read's
        ctx["error"] = ctx["error"] or friendly(exc)
        ctx["unread"] = True
        return ctx
    ql = q.strip().lower()
    rows = sorted((m for m in members if not ql or ql in m.email.lower() or ql in m.role.lower()),
                  key=lambda m: (_ROLE_RANK.get(m.role, 3), m.email.lower()))
    pages = max(1, -(-len(rows) // MEMBERS_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * MEMBERS_PAGE
    ctx.update(members=rows[start:start + MEMBERS_PAGE], total=len(rows), all_count=len(members),
               page=page, pages=pages, start=start)
    return ctx


async def _panel_ctx(st, group: str) -> dict:
    names = {g.email.lower(): g.name for g in await _cached_groups(st)}
    return {"group": group, "group_name": names.get(group.lower(), ""), "roles": GROUP_ROLES,
            **await _list_ctx(st.connector, group)}


async def _cached_groups(st) -> list:
    try:
        return await st.groups(stale_ok=True)
    except Exception:  # noqa: BLE001 — only the heading's display name comes from here
        return []


def _list(request: Request, ctx: dict) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _LIST, ctx)


@router.get("", response_class=HTMLResponse)
async def board(request: Request, group: str = "") -> HTMLResponse:
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE, {"connected": False})
    group = group.strip()
    panel = await _panel_ctx(st, group) if group else {"group": ""}
    return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE,
                                      {"connected": True, "selected": group, **await _group_choices(st), **panel})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", selected: str = "", refresh: int = 0) -> HTMLResponse:
    """The group finder: the top matches by address or name from the cached group list."""
    st = app_state(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "_group_results.html",
                                          {"groups": [], "more": False, "find_q": q, "find_error": NOT_CONNECTED})
    ctx = await _group_choices(st, q, bool(refresh))
    return TEMPLATES.TemplateResponse(request, "_group_results.html", {**ctx, "selected": selected})


@router.get("/people", response_class=HTMLResponse)
async def people(request: Request, q: str = "") -> HTMLResponse:
    """The add-member type-ahead: the top matches by address or name from the cached directory."""
    st = app_state(request)
    try:
        users = await st.users(stale_ok=True)
    except Exception:  # noqa: BLE001 — a directory hiccup just yields no suggestions; typing still works
        users = []
    shown, more = _pick(users, q, "primary_email", "full_name")
    return TEMPLATES.TemplateResponse(request, "_group_people.html",
                                      {"people": shown, "more": more, **as_of(st.user_cache)})


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, group: str = "") -> HTMLResponse:
    """A picked group's panel: its name, the add-member form, and the first page of members."""
    st = app_state(request)
    group = group.strip()
    if st.connector is None or not group:
        return TEMPLATES.TemplateResponse(request, "_group_panel.html",
                                          {"group": "", "error": NOT_CONNECTED if st.connector is None else ""})
    return TEMPLATES.TemplateResponse(request, "_group_panel.html", await _panel_ctx(st, group))


@router.get("/members/list", response_class=HTMLResponse)
async def member_list(request: Request, group: str = "", q: str = "", page: int = 1) -> HTMLResponse:
    conn, group = app_state(request).connector, group.strip()
    if conn is None or not group:
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a group first."})
    return _list(request, await _list_ctx(conn, group, q, page))


@router.post("/members", response_class=HTMLResponse)
async def add_member(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "member",
    q: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add one person (or group) with a role — a single-target LOW write, so no confirm step."""
    conn = app_state(request).connector
    group, email, role = group.strip(), email.strip(), (role or "member").strip().lower()
    if conn is None or not looks_like_email(group):
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a group first."})
    if not email:
        return _list(request, await _list_ctx(conn, group, q, error="Enter the email address of the person to add."))
    if not looks_like_email(email):
        return _list(request, await _list_ctx(conn, group, q, error=(
            f"“{email}” isn't one email address. Pick someone from the suggestions, or type their full address.")))
    try:
        result = await conn.add_group_member(group, email, role=role)
    except ValueError:   # GAMCommands refused the role before anything ran
        return _list(request, await _list_ctx(conn, group, q, error="Pick a role: member, manager or owner."))
    if not result.ok:
        return _list(request, await _list_ctx(conn, group, q, details=result.detail,
                                              error=f"Couldn't add {email} to {group}. {result.remediation}".strip()))
    return _list(request, await _list_ctx(conn, group, q, message=f"Added {email} to {group} as {role}.", focus=False))


@router.post("/members/remove/preview", response_class=HTMLResponse)
async def remove_preview(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    page: Annotated[int, Form()] = 1,
) -> HTMLResponse:
    """The confirm step for a removal: it names who leaves which group, and writes nothing."""
    if not looks_like_email(group) or not looks_like_email(email):
        return error_partial(request, "Pick a member to remove.")
    return TEMPLATES.TemplateResponse(request, "_group_remove_confirm.html", {
        "group": group.strip(), "email": email.strip(), "role": role.strip().lower(), "q": q, "page": page})


@router.post("/members/remove", response_class=HTMLResponse)
async def remove_member(
    request: Request,
    group: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    page: Annotated[int, Form()] = 1,
) -> HTMLResponse:
    """Remove one member — only from its confirm step (guard.enforce, confirm_step)."""
    conn = app_state(request).connector
    group, email = group.strip(), email.strip()
    if conn is None or not looks_like_email(group) or not looks_like_email(email):
        return _list(request, {"group": group, "members": [], "unread": True,
                               "error": NOT_CONNECTED if conn is None else "Pick a member to remove."})
    refusal = guard.enforce(guard.changes([email], RiskLevel.LOW, f"Remove from {group}"), await request.form(),
                            confirm_step=True)
    if refusal:
        return _list(request, await _list_ctx(conn, group, q, page, error=refusal))
    result = await conn.remove_group_member(group, email)
    if not result.ok:
        return _list(request, await _list_ctx(conn, group, q, page, details=result.detail,
                                              error=f"Couldn't remove {email} from {group}. {result.remediation}".strip()))
    return _list(request, await _list_ctx(conn, group, q, page, message=f"Removed {email} from {group}."))


# --- remove several members (roadmap 3.5) ------------------------------------------------------------------------------
# Tick members on the list, then "Remove selected…": the preview reads the group again and holds the ticked people who are
# still in it, with their roles, under a single-use token (web/previews.py). Remove takes exactly those, through the
# chokepoint one at a time, as a stoppable job with "Retry the N that failed" (the bulk department's mechanism, 2.1).
_REMOVE_FLOW = "group_remove_many"
_REMOVE_PREVIEW = "_group_remove_many.html"
_REMOVE_RUN = "_group_remove_run.html"


def _picked(form) -> list:
    """The ticked members: the confirm step's ``picks`` (one comma-joined value) when it is posted, else the list's boxes
    (``pick``, one each). Never both: the confirm step's own record of the selection is what its preview was keyed on."""
    values = str(form.get("picks")).split(",") if form.get("picks") is not None else form.getlist("pick")
    return sorted({str(v).strip().lower() for v in values if looks_like_email(str(v).strip())})


def _remove_key(group: str, picked: list) -> tuple:
    return (group.strip().lower(), tuple(picked))


async def _held_members(conn, group: str, wanted: set):
    """``group``'s members read now, those in ``wanted``: ``(still, gone)`` — each still a member with their role, and the
    ticked addresses the group no longer holds. A failed read raises: it is never "nobody to remove"."""
    members = await conn.list_group_members(group)
    by_email = {m.email.lower(): m for m in members if m.email}
    still = [by_email[e] for e in sorted(wanted) if e in by_email]
    return still, sorted(wanted - set(by_email))


def _remove_preview_page(request: Request, group: str, still, gone, token: str, retry: str = "", picked=()) -> HTMLResponse:
    decision = guard.evaluate(guard.changes([m.email for m in still], RiskLevel.LOW, f"Remove from {group}"),
                              typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    return TEMPLATES.TemplateResponse(request, _REMOVE_PREVIEW, {
        "group": group, "still": still, "gone": gone, "token": token, "retry": retry, "decision": decision, "picked": picked,
        "owners": [m.email for m in still if m.role == "OWNER"]})


@router.post("/members/remove-many/preview", response_class=HTMLResponse)
async def remove_many_preview(request: Request, group: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    tenant = st.tenant_key()   # before the read: what the preview is bound to
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    group = group.strip()
    picked = _picked(await request.form())
    if not looks_like_email(group) or not picked:
        return error_partial(request, "Tick the members to remove first.")
    try:
        still, gone = await _held_members(conn, group, set(picked))
    except Exception as exc:  # noqa: BLE001 — a failed read offers nothing
        return error_partial(request, f"Couldn't read {group}'s members again, so nothing is offered. " + friendly(exc))
    token = (st.previews.hold(_REMOVE_FLOW, _remove_key(group, picked), (group, [m.email for m in still]), tenant=tenant)
             if still else "")
    return _remove_preview_page(request, group, still, gone, token, picked=picked)


@router.post("/members/remove-many", response_class=HTMLResponse)
async def remove_many(request: Request, group: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    retried = str(form.get("retry") or "")   # a retry's preview held its people under the failed job
    key = retry_key(retried) if retried else _remove_key(group, _picked(form))
    held, refusal = st.previews.take(_REMOVE_FLOW, str(form.get(TOKEN_FIELD) or ""), key,
                                     again="click Retry again" if retried else "click Remove selected… again", what="selection")
    if refusal:
        return error_partial(request, refusal)
    group, emails = held
    refusal = guard.enforce(guard.changes(emails, RiskLevel.LOW, f"Remove from {group}"), form, confirm_step=True,
                            typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return error_partial(request, refusal)
    n = len(emails)
    title = f"Remove {n} from {group}, retried" if retried else f"Remove {n} from {group}"
    job = start_job(st.jobs, n, kind="group_remove", title=title)
    job.retry = group   # "Retry the N that failed" removes the failed ones from the same group
    job.task = asyncio.create_task(bulk.remove_members(job, conn, group, emails))
    return TEMPLATES.TemplateResponse(request, _REMOVE_RUN, {"job": job, "group": group})


@router.post("/members/remove-many/retry", response_class=HTMLResponse)
async def remove_many_retry(request: Request, job: Annotated[str, Form()] = "") -> HTMLResponse:
    """The confirm step again for exactly a finished run's failed members, still in the group (plan U5). Writes nothing."""
    st = app_state(request)
    tenant = st.tenant_key()
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    done, refusal = retry_of(st.jobs, job, "group_remove")
    if done is None:
        return error_partial(request, refusal or "That run is no longer available.")
    group = str(done.retry)
    try:
        still, gone = await _held_members(st.connector, group, {e.lower() for e in done.failed_items})
    except Exception as exc:  # noqa: BLE001 — a failed read offers nothing
        return error_partial(request, f"Couldn't read {group}'s members again, so nothing is offered. " + friendly(exc))
    token = (st.previews.hold(_REMOVE_FLOW, retry_key(done.id), (group, [m.email for m in still]), tenant=tenant)
             if still else "")
    return _remove_preview_page(request, group, still, gone, token, retry=done.id)


@router.get("/members/remove-many/status", response_class=HTMLResponse)
async def remove_many_status(request: Request, job: str = "") -> HTMLResponse:
    found = app_state(request).jobs.get(job)
    if found is None or found.kind != "group_remove":
        return error_partial(request, "That run is no longer here.")
    return TEMPLATES.TemplateResponse(request, _REMOVE_RUN, {"job": found, "group": str(found.retry)})


# --- a group's settings (roadmap 4.4) ----------------------------------------------------------------------------------
# "Show settings" reads seven of the group's settings (core/group_settings.py). Preview reads them again and holds exactly
# the ones the form changes, old and new, under a single-use token; the confirm step posts only that token. Apply reads them
# a third time and refuses when one held has changed since (so an older preview can't undo a newer change), then writes only
# the held settings. Google refuses some combinations itself, and its words reach the screen.
_SETTINGS_FLOW = "group_settings"
_SETTINGS_KEY = ("group_settings",)   # the confirm step posts only its token: nothing in it names what runs
_SETTINGS_CARD = "_group_settings.html"
_SETTINGS_CONFIRM = "_group_settings_confirm.html"
_SETTINGS_TRY_AGAIN = f"{GAM_TROUBLE} Please try again."


async def _settings_now(conn, group: str):
    """``(settings, why not, address)``: the offered settings read now and the group's primary address as GAM read it (an
    alias resolves to it), or None and why they can't be told (invariant 10)."""
    skipped = "GAM doesn't read or change the settings of an abuse@ or postmaster@ group: use the Admin console."
    if settings_skipped(group):
        return None, skipped, group
    try:
        record = await conn.group_settings(group)
    except Exception as exc:  # noqa: BLE001 — unread: nothing offered, never a default
        return None, unread_why(exc, _SETTINGS_TRY_AGAIN), group
    addr = str(record.get("email") or "").strip()
    if settings_skipped(addr):
        return None, skipped, addr
    found = group_settings.read(record)
    if found is None or not is_one_address(addr):
        return None, "GAM's answer left some of them out, so none are offered.", group
    return found, "", addr


@router.get("/settings", response_class=HTMLResponse)
async def settings_card(request: Request, group: str = "") -> HTMLResponse:
    conn, group = app_state(request).connector, group.strip()
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    if not is_one_address(group):
        return error_partial(request, "Pick a group first.")
    settings, why, _ = await _settings_now(conn, group)
    return TEMPLATES.TemplateResponse(request, _SETTINGS_CARD, {
        "group": group, "settings": settings or [], "error": f"Couldn't read {group}'s settings. {why}" if why else ""})


@router.post("/settings/preview", response_class=HTMLResponse)
async def settings_preview(request: Request, group: Annotated[str, Form()] = "") -> HTMLResponse:
    st = app_state(request)
    tenant = st.tenant_key()   # before the read: what the preview is bound to
    conn, group = st.connector, group.strip()
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    if not is_one_address(group):
        return error_partial(request, "Pick a group first.")
    settings, why, group = await _settings_now(conn, group)   # from here, the group as GAM read it
    if settings is None:
        return error_partial(request, f"Couldn't read {group}'s settings again, so nothing is changed. {why}")
    form = await request.form()
    try:
        held = group_settings.changes(settings, {k: str(form.get(k) or "") for k in GROUP_SETTINGS})
    except ValueError:
        return error_partial(request, "Pick each setting from its list.")
    if not held:
        return error_partial(request, f"Nothing to change: each setting is already as {group} has it.")
    token = st.previews.hold(_SETTINGS_FLOW, _SETTINGS_KEY, (group, held), tenant=tenant)
    return TEMPLATES.TemplateResponse(request, _SETTINGS_CONFIRM, {"group": group, "held": held, "token": token})


@router.post("/settings/apply", response_class=HTMLResponse)
async def settings_apply(request: Request) -> HTMLResponse:
    st = app_state(request)
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    taken, refusal = st.previews.take(_SETTINGS_FLOW, str(form.get(TOKEN_FIELD) or ""), _SETTINGS_KEY,
                                      again="click Preview changes again")
    if refusal:
        return error_partial(request, refusal)
    group, held = taken
    n = len(held)
    refusal = guard.enforce(guard.changes([group], RiskLevel.LOW, f"Change {n} setting{'s' if n != 1 else ''}"), form,
                            confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    settings, why, _ = await _settings_now(conn, group)   # read again at the click
    if settings is None:
        return error_partial(request, f"Couldn't read {group}'s settings again, so nothing is changed. {why}")
    if since := group_settings.moved(held, settings):
        return error_partial(request, f"{', '.join(since)} changed since the preview, so nothing is changed. "
                                      "Show the settings again and preview what you want.")
    result = await conn.update_group_settings(group, {c.key: c.new for c in held})
    if not result.ok:
        said = group_settings.google_said(result.detail) if result.kind is not GAMErrorKind.NOT_FOUND else ""
        if said:
            return error_partial(request, f"Google refused the change to {group}'s settings: “{said}”. Nothing was changed.",
                                 result.detail)
        return write_failed(request, f"Couldn't change {group}'s settings.", result)
    return TEMPLATES.TemplateResponse(request, "_group_settings_done.html", {
        "group": group, "message": f"Changed {n} setting{'s' if n != 1 else ''} of {group}."})
