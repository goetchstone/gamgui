"""Calendars: find a calendar (a room/resource or a user's), see who has access, search its events,
and delete a stray event.

Reads are bounded (one calendar; event search requires a query or date window + a result cap).
Deletion is destructive on real calendars, so it's preview -> confirm -> audited, targeting one
specific event id (a recurring master removes the whole series).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard
from ...core.bulk import stop_reason, stop_requested
from ...core.connectors.base import RiskLevel
from ...core.gam.commands import GAMCommands
from ...core.gam.errors import GAMError, GAMErrorKind
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, connector, error_partial, friendly, write_failed

router = APIRouter(prefix="/calendars")

EVENT_CAP = 200
_CALENDAR_LIST_TEMPLATE = "_calendar_list.html"
_CALENDAR_INDEX_JOB_TEMPLATE = "_calendar_index_job.html"
_SUBSCRIBE_JOB_TEMPLATE = "_calendar_subscribe_job.html"
# Strict allowlist: only true secondary calendars can be deleted. Primary calendars are a user's
# email; holiday/system use @group.v.calendar.google.com or a `#…@` id; rooms use
# @resource.calendar.google.com; imports use @import.calendar.google.com — none end in this suffix.
SECONDARY_SUFFIX = "@group.calendar.google.com"
# A group share past the bulk threshold confirms first; its confirm step holds the resolved members
# under a single-use token (web/previews.py) and posts the live share form back.
_SHARE_FLOW = "calendar_share_group"
_CONFIRM_SAMPLE = 10   # members named in the confirm step (the count is always the full one)


def _is_secondary(cal: str) -> bool:
    c = (cal or "").strip().lower()
    return bool(c) and "#" not in c and c.endswith(SECONDARY_SUFFIX)


def _is_user_scope(scope: str) -> bool:
    """A bare user email (not a group/domain/default scope) — the only scope we can auto-subscribe."""
    s = (scope or "").strip().lower()
    if s in ("default", "domain") or s.startswith(("group:", "domain:")):
        return False
    return "@" in s


async def _subscribers_for(conn, target: str, known_users: set) -> "tuple[str, list]":
    """Who to auto-subscribe for an ACL scope: ``("user"|"group"|"none", [emails])``.

    Granting an ACL is not what makes a calendar show up — subscribing it onto the person's calendar
    list is. That is the whole complaint this answers ("I shared it and they still can't see it"), so
    a group grant has to fan out to the group's members.

    A *bare* address is ambiguous: GAM accepts a group's address as a calendar ACL scope exactly like
    a person's, and nothing in the string says which it is. Resolve it against the directory we
    already have cached first — a known account is a person, full stop. Only then ask whether it is a
    group, because "the member lookup errored" is a weak signal to hang the decision on: it is also
    what a transient failure looks like, and mistaking a person for an empty group would silently skip
    the subscribe that is the point of the feature.
    """
    scope = (target or "").strip()
    low = scope.lower()
    if low in ("default", "domain") or low.startswith("domain:"):
        return "none", []                       # can't enumerate a whole domain; no fan-out
    if not low.startswith("group:") and low in known_users:
        return "user", [scope]                  # a known account: never treat it as a group
    group = scope[len("group:"):].strip() if low.startswith("group:") else scope
    try:
        members = await conn.list_group_members(group)
    except GAMError as exc:
        # Only GAM's "not a group" lets an address be a person: "Group: x, Does not exist" (exit 56) for
        # the domain's own, "Group: x, Service not applicable/Does not exist" (exit 73) for an outside
        # address, both NOT_FOUND. A rate limit or a refusal says nothing about what the address is, and
        # reading it as a person once gave a group's address a person's rule: the caller refuses instead.
        if exc.kind != GAMErrorKind.NOT_FOUND:
            raise
        members = None
    if members is not None:                     # the lookup answered, so it is a group, members or none
        return "group", [m.email for m in members if getattr(m, "email", "") and "@" in m.email]
    # Not a group. An explicit group: scope still goes as typed, and GAM answers for it.
    return ("group", []) if low.startswith("group:") else ("user", [scope] if _is_user_scope(scope) else [])


async def _run_subscribe(job, conn, cal: str, emails: list) -> None:
    """Background: put ``cal`` on each member's calendar list so it actually appears for them. Stops
    at a failure every later member would share (``stop_reason``), or at Stop."""
    try:
        for email in emails:
            if stop_requested(job):
                break
            job.current = email
            kind, why, detail = None, "", ""
            try:
                res = await conn.subscribe_calendar_for(email, cal)
                ok = bool(getattr(res, "ok", False))
                if not ok:
                    kind, why, detail = getattr(res, "kind", None), getattr(res, "remediation", ""), getattr(res, "detail", "")
            except Exception as exc:            # noqa: BLE001 — one member must not stop the rest
                ok, kind, why, detail = False, getattr(exc, "kind", None), friendly(exc), str(exc)
            job.record(email, ok, why, detail)
            stop = stop_reason(kind, why, job.total - job.done)
            if stop:
                job.error = stop
                break
    except Exception as exc:                    # noqa: BLE001 — whole-batch failure (auth expired)
        job.error = friendly(exc)
    finally:
        job.finish()


async def _active_emails(request: Request, strict: bool = False) -> set:
    """Lowercased emails of current, non-suspended users (from the shared cache). A failed read is an
    empty set, or with ``strict`` raises, for a caller that would otherwise name someone as suspended."""
    try:
        return {u.primary_email.lower() for u in await request.app.state.gamgui.users()
                if u.primary_email and not getattr(u, "suspended", False)}
    except Exception:
        if strict:
            raise
        return set()


async def _resolve_delete_owner(request: Request, conn, cal: str):
    """(owner_to_impersonate, refusal_message). owner is '' when the calendar can't be deleted here.

    Re-reads Google fresh and re-validates the id — never trusts the client. A delete acts as the
    calendar's data owner (Calendars ``dataOwner``), never another owner in its ACL: a co-owner may not
    be allowed to delete it. A data owner who is suspended or gone is refused with guidance, not swapped
    for a co-owner (GAM can't act as a suspended account)."""
    if not _is_secondary(cal):
        return "", ("Only secondary calendars can be deleted here. Primary, holiday and room "
                    "calendars can't be removed this way.")
    owner = await conn.calendar_data_owner(cal)
    if not owner or "@" not in owner:
        return "", ("Google didn't name this calendar's data owner, so there's no account to delete it "
                    "as. Transfer ownership to an active user first.")
    try:
        active = await _active_emails(request, strict=True)
    except Exception as exc:  # noqa: BLE001 — never read a failed directory as "suspended"
        return "", (f"Couldn't read the directory to check that the data owner, {owner}, is active. "
                    f"Try again. {friendly(exc)}")
    if owner.lower() not in active:
        return "", (f"The calendar's data owner, {owner}, is suspended or no longer exists. Transfer "
                    "ownership to an active user, then delete.")
    return owner, ""


def _humanize_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 172800:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} d ago"


def _index_ready(request: Request) -> bool:
    """The index has rows AND they belong to the currently-connected domain.

    The index is one file; if the admin switched tenants, the stored domain won't match the active
    one — treat that as "needs rebuild" so we never serve (or delete against) another tenant's data.
    """
    st = request.app.state.gamgui
    idx = st.calendar_index
    if idx is None:
        return False
    status = idx.status()
    return status.count > 0 and status.domain == st.audit_domain


def _index_ctx(request: Request) -> dict:
    """Status of the persistent calendar index for the page/status partials."""
    idx = request.app.state.gamgui.calendar_index
    ready = _index_ready(request)
    if idx is None or not ready:
        return {"count": 0, "age": "", "ready": False}
    status = idx.status()
    age = _humanize_age(time.time() - status.updated_at) if status.updated_at else ""
    return {"count": status.count, "age": age, "ready": True}


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    connected = connector(request) is not None
    ctx: dict = {"connected": connected}
    if connected:
        ctx["index"] = _index_ctx(request)
    return TEMPLATES.TemplateResponse(request, "calendars.html", ctx)


@router.get("/resources", response_class=HTMLResponse)
async def resources(request: Request, q: str = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        rs = await conn.list_resources(q.strip())
    except Exception as exc:
        return error_partial(request, friendly(exc))
    items = [
        {"cal_id": r.email, "label": r.name or r.email, "meta": r.resource_type or "resource", "owner": ""}
        for r in rs if r.email
    ]
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items})


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    """Instant name search, served entirely from the local index (no live domain scan)."""
    if connector(request) is None:
        return error_partial(request, NOT_CONNECTED)
    idx = request.app.state.gamgui.calendar_index
    if idx is None or not _index_ready(request):  # missing, empty, or built for another domain
        return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {
            "items": [],
            "notes": ["No calendar index yet — build it once (the button above) to search every "
                      "shared calendar by name. After that, searches are instant."],
        })
    items = []
    for c in idx.search(q.strip()):
        owner = c.owner if c.kind != "room" else ""
        if c.kind == "room":
            meta = "room"
        else:
            meta = "" if owner else "shared"
            if c.subscribers:
                sub_text = f"{c.subscribers} subscriber{'' if c.subscribers == 1 else 's'}"
                meta = f"{meta} · {sub_text}" if meta else sub_text
        items.append({"cal_id": c.id, "label": c.summary or c.id, "meta": meta, "owner": owner})
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items, "notes": []})


async def _build_index(job, conn, idx, domain: str) -> None:
    """Background: scan the whole domain once and atomically replace the index."""
    try:
        job.current = "Scanning every user's calendars…"
        cals = await conn.scan_all_calendars()
        await asyncio.to_thread(idx.replace_all, domain, cals)
        job.applied = len(cals)
        job.log.append(f"Indexed {len(cals)} calendars.")
    except Exception as exc:  # noqa: BLE001 — surface any failure in the status partial
        job.error = str(exc)
    finally:
        job.finish()


@router.post("/index/rebuild", response_class=HTMLResponse)
async def index_rebuild(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    if st.calendar_index is None:
        return error_partial(request, "Calendar index is unavailable.")
    existing = st.jobs.get(st.cal_index_job_id)
    if existing is not None and not existing.finished:  # don't start a second multi-minute scan
        return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": existing})
    job = start_job(st.jobs, 0, kind="index", title="Calendar index rebuild")
    st.cal_index_job_id = job.id
    job.task = asyncio.create_task(_build_index(job, st.connector, st.calendar_index, st.audit_domain))
    return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": job})


@router.get("/index/status", response_class=HTMLResponse)
async def index_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job) if job else None
    if j is not None:
        # Job partial keeps polling while running, then shows a final result (no more polling).
        return TEMPLATES.TemplateResponse(request, _CALENDAR_INDEX_JOB_TEMPLATE, {"job": j})
    # Unknown/pruned job -> the resting status strip (count + age).
    return TEMPLATES.TemplateResponse(request, "_calendar_index_status.html", {"index": _index_ctx(request)})


@router.get("/user", response_class=HTMLResponse)
async def user_calendars(request: Request, email: str = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    email = email.strip()
    if not email:
        return error_partial(request, "Enter a user's email.")
    try:
        cals = await conn.list_user_calendars(email)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    items = [
        {"cal_id": c.id, "label": c.summary or c.id,
         "meta": (("primary · " if c.primary else "") + (c.access_role or "")), "owner": ""}
        for c in cals if c.id
    ]
    return TEMPLATES.TemplateResponse(request, _CALENDAR_LIST_TEMPLATE, {"items": items})


async def _detail_ctx(request: Request, conn, cal: str, label: str) -> dict:
    """Build the shared context for `_calendar_detail.html` — the GET detail view and the share/
    unshare mutations all re-render from this so the ACL list + delete zone stay identical."""
    acls = await conn.list_calendar_acls_for(cal)
    is_secondary = _is_secondary(cal)
    owner, delete_note = "", ""
    if is_secondary:   # the same answer the delete preview and the delete itself give
        try:
            owner, refusal = await _resolve_delete_owner(request, conn, cal)
        except Exception as exc:  # noqa: BLE001 — the page still shows; only the delete zone waits
            owner, refusal = "", f"Couldn't check who can delete this calendar. {friendly(exc)}"
        if not owner:
            delete_note = f"This calendar can't be deleted here. {refusal}"
    return {
        "cal": cal, "label": label.strip(), "acls": acls, "acl_count": len(acls),
        "is_secondary": is_secondary, "owner": owner, "deletable": bool(owner),
        "delete_note": delete_note, "share_notice": "",
    }


@router.get("/detail", response_class=HTMLResponse)
async def detail(request: Request, cal: str, label: str = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        ctx = await _detail_ctx(request, conn, cal, label)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", ctx)


def _fanout(emails: list) -> list:
    """The guard's view of a group share: one additive subscribe per member (the ACL grant is one more)."""
    return guard.changes(emails, RiskLevel.LOW, "Add the calendar to their list")


def _share_key(cal: str, target: str, role: str) -> tuple:
    return (cal.strip(), target.strip().lower(), role)


@router.post("/share", response_class=HTMLResponse)
async def share(request: Request, cal: Annotated[str, Form()], target: Annotated[str, Form()],
                role: Annotated[str, Form()] = "reader", label: Annotated[str, Form()] = "") -> HTMLResponse:
    """Grant access, then put the calendar on the person's (or each group member's) list. A group of
    DEFAULT_BULK_THRESHOLD or more members is resolved first and answered with a confirm step naming
    the count (``/share/group`` runs it), not written: the fan-out is one write per member."""
    tenant = request.app.state.gamgui.tenant_key()   # before the group read (web/previews.py)
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    cal, target = cal.strip(), target.strip()
    if not target:
        return error_partial(request, "Enter a person or group to share with.")
    try:   # the builder's <CalendarACLRole> check, before any confirm step; this runs nothing
        GAMCommands.add_calendar_acl_cal(cal, target, role=role)
    except ValueError as exc:
        return error_partial(request, f"Couldn't share calendar: {exc}.")
    try:
        kind, emails = await _subscribers_for(conn, target, await _active_emails(request))
    except Exception as exc:  # noqa: BLE001 — any lookup failure but "not a group": nothing is written
        return error_partial(request, f"Couldn't tell whether {target} is a group, so nothing was shared. "
                             f"{friendly(exc)}", getattr(exc, "stderr", ""))
    decision = guard.evaluate(_fanout(emails)) if kind == "group" else None
    if decision is not None and decision.requires_confirmation:
        st = request.app.state.gamgui
        token = st.previews.hold(_SHARE_FLOW, _share_key(cal, target, role), (cal, target, role, emails),
                                 tenant=tenant)
        try:
            ctx = await _detail_ctx(request, conn, cal, label)
        except Exception as exc:
            return error_partial(request, friendly(exc))
        ctx.update(share_target=target, share_role=role, share_confirm={
            "target": target, "count": len(emails), "sample": emails[:_CONFIRM_SAMPLE],
            "warnings": decision.warnings, "token": token})
        return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", ctx)
    return await _grant_and_subscribe(request, conn, cal, target, role, label, kind, emails)


@router.post("/share/group", response_class=HTMLResponse)
async def share_group(request: Request, cal: Annotated[str, Form()] = "", target: Annotated[str, Form()] = "",
                      role: Annotated[str, Form()] = "reader", label: Annotated[str, Form()] = "") -> HTMLResponse:
    """The confirm step of a group share past the bulk threshold: runs the members its preview resolved
    (held under the token), only when the live share form still names that calendar, group and role,
    and only with ``confirmed=1`` — checked here, not just drawn by the template."""
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    held, refusal = st.previews.take(_SHARE_FLOW, str(form.get(TOKEN_FIELD) or ""),
                                     _share_key(cal, target, role), again="click Share again")
    if refusal:
        return error_partial(request, refusal)
    cal, target, role, emails = held
    refusal = guard.enforce(_fanout(emails), form, confirm_step=True)
    if refusal:
        return error_partial(request, refusal)
    return await _grant_and_subscribe(request, conn, cal, target, role, label, "group", emails)


async def _grant_and_subscribe(request: Request, conn, cal: str, target: str, role: str, label: str,
                               kind: str, emails: list) -> HTMLResponse:
    """Add the ACL, then subscribe: one person inline, a group's members as a polled background job.

    A bare address that resolved to a group is sent as ``group:<address>``: GAM reads a bare address as
    a person's rule (``user:<address>``), which gives no member access, so every subscribe would fail."""
    scope = target if kind != "group" or target.lower().startswith("group:") else f"group:{target}"
    try:
        result = await conn.add_calendar_acl_for(cal, scope, role=role)
    except ValueError as exc:  # the builder refuses a role outside the grammar's <CalendarACLRole>
        return error_partial(request, f"Couldn't share calendar: {exc}.")
    if not result.ok:
        return write_failed(request, f"Couldn't share the calendar with {target}.", result)
    notice, job = "", None
    if kind == "user" and emails:
        sub = await conn.subscribe_calendar_for(emails[0], cal)
        if sub.ok:
            notice = f"Shared with {target} — it will now appear in their Google Calendar."
        else:
            notice = (f"Shared with {target}, but couldn't auto-add it to their calendar list — "
                      "they can add it manually in Google Calendar.")
    elif kind == "group" and emails:
        # Fan out in the background: one gam call per member, so a large group would otherwise hold
        # the request open for minutes. Progress is polled, same as the index rebuild.
        st = request.app.state.gamgui
        n = len(emails)
        job = start_job(st.jobs, n, kind="subscribe",
                        title=f"Calendar to {target}'s {n} member{'s' if n != 1 else ''}")
        job.task = asyncio.create_task(_run_subscribe(job, conn, cal, emails))
        notice = (f"Shared with {target} — adding it to {len(emails)} "
                  f"member{'s' if len(emails) != 1 else ''}' calendars now.")
    elif kind == "group":
        notice = f"Shared with {target}, but that group has no members to add it for."
    else:
        notice = (f"Shared with {target}. That scope can't be auto-subscribed, so people may need to "
                  "add the calendar themselves.")
    try:
        ctx = await _detail_ctx(request, conn, cal, label)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    ctx["share_notice"] = notice
    ctx["subscribe_job"] = job
    return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", ctx)


@router.get("/share/status", response_class=HTMLResponse)
async def share_status(request: Request, job: str = "") -> HTMLResponse:
    """Poll target for the group fan-out panel (unknown/pruned job renders as finished, not an error)."""
    st = request.app.state.gamgui
    return TEMPLATES.TemplateResponse(
        request, _SUBSCRIBE_JOB_TEMPLATE, {"subscribe_job": st.jobs.get(job) if job else None})


@router.post("/unshare", response_class=HTMLResponse)
async def unshare(request: Request, cal: Annotated[str, Form()], scope: Annotated[str, Form()],
                  label: Annotated[str, Form()] = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    cal, scope = cal.strip(), scope.strip()
    result = await conn.remove_calendar_acl_for(cal, scope)
    if not result.ok:
        return write_failed(request, f"Couldn't remove {scope}'s access.", result)
    try:
        ctx = await _detail_ctx(request, conn, cal, label)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    ctx["share_notice"] = f"Removed access for {scope}."
    return TEMPLATES.TemplateResponse(request, "_calendar_detail.html", ctx)


def _delete_view(request: Request, *, cal: str, label: str, owner: str, acl_count: int = 0,
                 deleted: bool = False, error: str = "") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_calendar_delete.html", {
        "cal": cal, "label": label, "owner": owner, "acl_count": acl_count,
        "deleted": deleted, "error": error,
    })


@router.post("/delete/preview", response_class=HTMLResponse)
async def delete_preview(request: Request, cal: Annotated[str, Form()],
                         label: Annotated[str, Form()] = "",
                         acl_count: Annotated[int, Form()] = 0) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    cal = cal.strip()
    try:
        owner, refusal = await _resolve_delete_owner(request, conn, cal)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    if not owner:
        return error_partial(request, refusal)
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, acl_count=acl_count)


@router.post("/delete", response_class=HTMLResponse)
async def delete_cal(request: Request, cal: Annotated[str, Form()],
                     confirm: Annotated[str, Form()] = "",
                     label: Annotated[str, Form()] = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    cal = cal.strip()
    # Re-resolve owner + re-validate the id server-side — never act on a client-supplied identity.
    try:
        owner, refusal = await _resolve_delete_owner(request, conn, cal)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    if not owner:
        return error_partial(request, refusal)
    if confirm.strip() != "DELETE":  # exact-case gate
        return _delete_view(request, cal=cal, label=label.strip(), owner=owner,
                            error="Type DELETE (in capitals) to confirm.")
    result = await conn.delete_calendar(owner, cal)
    if not result.ok:
        return write_failed(request, "Couldn't delete the calendar.", result)
    idx = request.app.state.gamgui.calendar_index
    if idx is not None:
        # Off the event loop — a sync SQLite write could block briefly if a rebuild is mid-write.
        await asyncio.to_thread(idx.remove, cal)  # drop it from search immediately (no full rebuild)
    return _delete_view(request, cal=cal, label=label.strip(), owner=owner, deleted=True)


def _local_midnight(day: str) -> str:
    """A date-picker day as the start of that day where the operator is, e.g. 2026-11-13T00:00:00-05:00.

    GAM reads a bare yyyy-mm-dd as midnight in its own timezone (UTC unless configured), so "on or after
    the 13th" in New York listed the evening of the 12th. The offset is that day's own (DST included).
    Anything else goes on as typed, for GAM to read or refuse."""
    try:
        d = date.fromisoformat(day) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None  # not 2026-W46-5
    except ValueError:
        d = None
    if d is None:
        return day
    try:
        return datetime(d.year, d.month, d.day).astimezone().isoformat()
    except (OverflowError, ValueError):   # 0001-01-01 or 9999-12-31 shifted past a year GAM can read
        return day


@router.get("/events", response_class=HTMLResponse)
async def events(request: Request, cal: str, q: str = "", after: str = "", before: str = "") -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    q, after, before = q.strip(), _local_midnight(after.strip()), _local_midnight(before.strip())
    if not (q or after or before):  # never an unbounded all-events scan
        return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": [], "need_filter": True})
    try:
        evs = await conn.search_events(cal, query=q, after=after, before=before, cap=EVENT_CAP)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_event_results.html", {"cal": cal, "events": evs, "capped": len(evs) >= EVENT_CAP})


@router.post("/event/preview", response_class=HTMLResponse)
async def event_preview(request: Request, cal: Annotated[str, Form()], event_id: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:
        ev = await conn.get_event(cal, event_id)
    except Exception as exc:
        return error_partial(request, friendly(exc))
    if ev is None:
        return error_partial(request, "That event no longer exists.")
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": ev, "deleted": False})


@router.post("/event/delete", response_class=HTMLResponse)
async def event_delete(request: Request, cal: Annotated[str, Form()], event_id: Annotated[str, Form()]) -> HTMLResponse:
    conn = connector(request)
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    refusal = guard.enforce(guard.changes([event_id], RiskLevel.DESTRUCTIVE, "Delete event"), await request.form())
    if refusal:
        return error_partial(request, refusal)
    result = await conn.delete_event(cal, event_id)
    if not result.ok:
        return write_failed(request, "Couldn't delete the event.", result)
    return TEMPLATES.TemplateResponse(request, "_event_delete.html", {"cal": cal, "event": None, "deleted": True})
