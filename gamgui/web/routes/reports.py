"""Reports / insights routes (read-only)."""

from __future__ import annotations

import asyncio
import csv
import io

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core import bulk, clock, guard, licenses
from ...core import reports as reports_mod
from ...core.audit_runs import INACTIVE_WINDOWS, named_run
from ...core.catalog.catalog import load_catalog
from ...core.connectors.base import RiskLevel
from ...core.gam.errors import GAMError, GAMErrorKind
from ...core.setup import admin_token_scopes
from ..csvutil import csv_safe
from ..jobs import start_job
from ..previews import TOKEN_FIELD
from ..server import TEMPLATES
from ._common import NOT_CONNECTED, as_of, error_partial, friendly, gam_detail, unread_why

router = APIRouter(prefix="/reports")

_REPORTS_PAGE = "reports.html"
_USAGE_REPORT_PARTIAL = "_usage_report.html"
_CONNECTED_APPS_PARTIAL = "_connected_apps.html"
_NAMES_SHOWN = 6   # who granted an app, before "and N more"
_READ_TIMED_OUT = ("GAM reads one account at a time and didn't finish in the hour it's allowed, so nothing was kept. "
                   "Trying again won't help on a domain this size.")


REPORT_SHOWN = 50   # a card lists this many; the rest are in its CSV (/reports/list.csv)


@router.get("", response_class=HTMLResponse)
async def reports_page(request: Request, refresh: int = 0) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _REPORTS_PAGE, {"connected": False, "reports": []})
    named = _named_reports(st)
    try:
        # The shared cache (CACHE_FIELDS covers REPORT_FIELDS); the header says how old it is.
        users = await st.users(force=bool(refresh), stale_ok=True)
    except Exception as exc:
        msg = friendly(exc, "Couldn't load users.")
        return TEMPLATES.TemplateResponse(
            request, _REPORTS_PAGE, {"connected": True, "reports": [], "error": msg, "total": 0, "named": named}
        )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORTS_PAGE,
        {"connected": True, "reports": reports_mod.build_reports(users), "total": len(users), "named": named,
         "reminders": reports_mod.REMINDERS,
         "fill_in": reports_mod.FILL_IN_REPORTS, "shown": REPORT_SHOWN,
         **as_of(st.user_cache)},
    )


def _named_reports(st) -> list:
    """Roadmap 3.1: each named read, the Builder command it opens (None when a GAM bump lost it), and whether the admin
    token holds its scope (None: a service-account read, or a token whose scopes can't be read)."""
    if st.catalog is None:
        st.catalog = load_catalog()
    granted = admin_token_scopes(st.vault, st.audit_domain) if st.vault is not None else None
    out = []
    for r in reports_mod.NAMED_REPORTS:
        cmd = reports_mod.named_report_command(st.catalog, r)
        out.append({"report": r, "cmd": cmd, "held": reports_mod.admin_scope_held(r, granted),
                    "runs_here": named_run(r, cmd) is not None})   # on Audit runs too, where it can be scheduled (F7a)
    return out


@router.get("/fill-in.csv")
async def fill_in_csv(request: Request, key: str = "") -> Response:
    """A completeness bucket (no title, department, phone or location) as a spreadsheet to fill in (roadmap 3.3): the
    columns bulk apply from CSV reads back (``FILL_IN_COLUMNS``), formula-safe, from the same cached directory the card
    counted. A list that couldn't be read is said in plain text, never an empty sheet (invariant 10)."""
    st = request.app.state.gamgui
    if key not in reports_mod.FILL_IN_REPORTS:
        return Response("Not a list that downloads.\n", status_code=400, media_type="text/plain")
    if st.connector is None:
        return Response(NOT_CONNECTED + "\n", status_code=400, media_type="text/plain")
    try:
        users = await st.users(stale_ok=True)
    except Exception as exc:  # noqa: BLE001 — said in words, as a plain-text answer
        return Response(friendly(exc, "Couldn't load users.") + "\n", status_code=502, media_type="text/plain")
    bucket = next(r for r in reports_mod.build_reports(users) if r.key == key)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(reports_mod.FILL_IN_COLUMNS)
    for row in reports_mod.fill_in_rows(bucket.users):
        writer.writerow([csv_safe(v) for v in row])
    # `key` is one of FILL_IN_REPORTS, so the name carries only filename-safe characters.
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{key.replace("_", "-")}.csv"'})


@router.get("/list.csv")
async def bucket_csv(request: Request, key: str = "") -> Response:
    """Everyone a card counts, as a spreadsheet: the card shows only its first REPORT_SHOWN (docs/scale.md: listing every
    member put 12.5 MB of HTML on the page at 20,000 people). From the same cached directory; formula-safe; a list that
    couldn't be read is said in plain text, never an empty sheet (invariant 10)."""
    st = request.app.state.gamgui
    if st.connector is None:
        return Response(NOT_CONNECTED + "\n", status_code=400, media_type="text/plain")
    try:
        users = await st.users(stale_ok=True)
    except Exception as exc:  # noqa: BLE001 — said in words, as a plain-text answer
        return Response(friendly(exc, "Couldn't load users.") + "\n", status_code=502, media_type="text/plain")
    bucket = next((r for r in reports_mod.build_reports(users) if r.key == key), None)
    if bucket is None:
        return Response("Not a list that downloads.\n", status_code=400, media_type="text/plain")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("email", "name", "org_unit", "suspended"))
    for u in sorted(bucket.users, key=lambda u: u.primary_email.lower()):
        writer.writerow([csv_safe(v) for v in (u.primary_email, u.full_name, u.org_unit_path, "yes" if u.suspended else "")])
    # `key` is a report's own key, so the name carries only filename-safe characters.
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{bucket.key.replace("_", "-")}.csv"'})


@router.get("/usage", response_class=HTMLResponse)
async def usage(request: Request) -> HTMLResponse:
    """Lazy-loaded storage/mail usage (a separate, slower Reports-API call)."""
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": NOT_CONNECTED})
    try:
        data = await conn.usage_report(reports_mod.USAGE_PARAMS)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": friendly(exc, "Couldn't load report data.")})
    rows = reports_mod.parse_usage(data["rows"])[:25]
    return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": rows, "date": data["date"]})


def _connected_apps_response(request: Request, job=None, announce: bool = False) -> HTMLResponse:
    """``announce`` only for a change the operator is waiting on: this panel arrives after the page loads, so
    app.js would speak a finished read's summary again on every visit to Reports."""
    st = request.app.state.gamgui
    ctx = {"connected": st.connector is not None, "job": job, "announce": announce, "names_shown": _NAMES_SHOWN}
    if st.connected_apps is not None:
        read, when = st.connected_apps
        ctx.update(read=read, read_age=clock.now() - when)
    return TEMPLATES.TemplateResponse(request, _CONNECTED_APPS_PARTIAL, ctx)


@router.get("/connected-apps", response_class=HTMLResponse)
async def connected_apps(request: Request, job: str = "") -> HTMLResponse:
    """The last read, a running one, or the button. Never reads by itself: one API call per account."""
    st = request.app.state.gamgui
    asked = st.jobs.get(job) if job else None
    if asked is not None and asked.kind != "connected_apps":
        asked = None
    # A finished read that isn't the latest says nothing about what's shown: its failure was another tenant's, or
    # was overtaken. It has its own line in the tray.
    if asked is not None and asked.finished and asked.id != st.connected_apps_job_id:
        asked = None
    return _connected_apps_response(request, asked or st.jobs.get(st.connected_apps_job_id), announce=asked is not None)


async def _read_connected_apps(job, st, conn) -> None:
    """Background: read every account's apps, kept only while ``conn`` is still the connected tenant's."""
    try:
        job.current = "Reading every account's apps…"
        read = await conn.connected_apps()
        if st.connector is conn:
            st.connected_apps = (read, clock.now())
            job.applied = read.app_count
        else:
            job.error = "Not kept: the connection changed during the read. Read again."
    except Exception as exc:  # noqa: BLE001 — surface any failure in the panel
        timed_out = isinstance(exc, GAMError) and exc.kind is GAMErrorKind.TIMEOUT
        job.error = _READ_TIMED_OUT if timed_out else friendly(exc)
    finally:
        job.finish()


@router.post("/connected-apps/read", response_class=HTMLResponse)
async def connected_apps_read(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _connected_apps_response(request)
    existing = st.jobs.get(st.connected_apps_job_id)
    if existing is not None and not existing.finished:  # don't start a second domain-wide read
        return _connected_apps_response(request, existing, announce=True)
    job = start_job(st.jobs, 0, kind="connected_apps", title="Connected apps read")
    st.connected_apps_job_id = job.id
    job.task = asyncio.create_task(_read_connected_apps(job, st, st.connector))
    return _connected_apps_response(request, job, announce=True)


# --- Inactive → suspend (roadmap 3.3, D3) --------------------------------------------------------------------------------
# The card's list comes from the cached directory, so the preview reads the directory again and holds the people that read
# found, under a single-use token (web/previews.py): Apply suspends exactly those, never a list rebuilt since. A new hire
# who hasn't signed in yet looks inactive, so each one made inside the window is flagged; the admin GamGUI signs in as is
# left out by name (the chokepoint refuses it whatever is sent).
_SUSPEND_FLOW = "suspend_inactive"
_SUSPEND_PARTIAL = "_inactive_suspend.html"
_SUSPEND_SHOWN = 200   # people the preview lists; the count is the whole of it (bounded page)
_ADMIN_UNCONFIRMED = ("GamGUI can't confirm which account it signs in as, so it can't leave that account out of a suspend. "
                      "Import the credentials again on Setup, then preview again.")


def _suspend_key(days) -> tuple:
    return ("inactive", str(days or "").strip())


def _window(value) -> int:
    """The preview's window: the Reports card's (none posted), or one an audit run offers (plan F3). 0: neither."""
    text = str(value or "").strip()
    if not text:
        return reports_mod.INACTIVE_DAYS
    return int(text) if text.isdigit() and int(text) in INACTIVE_WINDOWS else 0


@router.post("/inactive/preview", response_class=HTMLResponse)
async def inactive_preview(request: Request, days: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    window = _window(days)
    if not window:
        return error_partial(request, "Pick one of the windows the run offers, then preview again.")
    admin, admin_id, _why_not = await conn.connected_admin()
    if not admin:
        return error_partial(request, _ADMIN_UNCONFIRMED)
    try:
        users = await st.users(force=True)   # read now: the card counted a cached list
    except Exception as exc:  # noqa: BLE001 — a failed read is said, and nothing is offered
        return error_partial(request, "Couldn't read the directory again, so nothing is offered. " + friendly(exc))
    targets, left_out = reports_mod.inactive_to_suspend(users, admin, admin_id, inactive_days=window)
    emails = [t.email for t in targets]
    # Keyed on the window the preview used, posted back with Apply: a form edited since is refused, never re-read.
    token = st.previews.hold(_SUSPEND_FLOW, _suspend_key(window), emails, tenant=tenant) if emails else ""
    decision = guard.evaluate(conn.plan_suspend(emails), typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    return TEMPLATES.TemplateResponse(request, _SUSPEND_PARTIAL, {
        "targets": targets[:_SUSPEND_SHOWN], "count": len(targets), "new": sum(t.new for t in targets),
        "left_out": left_out, "days": window, "decision": decision, "token": token})


@router.post("/inactive/apply", response_class=HTMLResponse)
async def inactive_apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    emails, refusal = st.previews.take(_SUSPEND_FLOW, str(form.get(TOKEN_FIELD) or ""), _suspend_key(form.get("days")),
                                       again="click Suspend these… again")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(conn.plan_suspend(emails), form, confirm_step=True, typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return error_partial(request, refusal)
    n = len(emails)
    job = start_job(st.jobs, n, kind="suspend_inactive", title=f"Suspend {n} inactive account{'s' if n != 1 else ''}")
    job.task = asyncio.create_task(_run_suspend(job, st, conn, emails))
    return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"job": job})


async def _run_suspend(job, st, conn, emails) -> None:
    """The job's task: each suspend GAM accepted is patched into the cached directory as it lands. A failed write's outcome
    isn't known (a timeout may still have applied it), so any failure, or the loop dying, drops the whole list instead."""
    patches = st.batched_patch(suspended=True)
    try:
        await bulk.suspend_users(job, conn, emails, on_set=patches.add)
    finally:
        patches.flush()
        if job.failed_total or job.error:
            st.invalidate_users()


@router.get("/inactive/status", response_class=HTMLResponse)
async def inactive_status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != "suspend_inactive":
        return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"error": "That run is no longer here."})
    return TEMPLATES.TemplateResponse(request, "_inactive_suspend_run.html", {"job": found})


# --- Suspended → remove their licenses (roadmap 4.3, D3) ------------------------------------------------------------------
# The Suspended card and the License usage run both come from earlier reads, so the preview reads the seats and the
# directory again and holds each seat that read found on a suspended account, under a single-use token: Apply removes
# exactly those, as a stoppable job. Each person's data transfers are read just before their first seat goes, and one
# unfinished, failed or unread keeps their seats (licenses.many_refusal). A removal is DESTRUCTIVE in Google's words.
_SEATS_FLOW = "remove_suspended_licenses"
_SEATS_KIND = "suspended_licenses"
_SEATS_KEY = ("suspended",)
_SEATS_PARTIAL = "_suspended_licenses.html"
_SEATS_RUN = "_suspended_licenses_run.html"
_SEATS_SHOWN = 200   # seats the preview lists; the count is the whole of it (bounded page)


def _seat_check(held):
    """The guard's view of a removal run: one DESTRUCTIVE change a seat, and the address typed when they're one person's,
    as their page asks."""
    people = {email for email, _ in held}
    return (guard.changes([email for email, _ in held], RiskLevel.DESTRUCTIVE, "Remove a license"),
            next(iter(people)) if len(people) == 1 else None)


@router.post("/suspended-licenses/preview", response_class=HTMLResponse)
async def suspended_licenses_preview(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the reads: what the preview is bound to
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    try:   # read now, both: the card and the run counted earlier reads
        seats, users = await asyncio.gather(conn.license_seats(), st.users(force=True))
    except Exception as exc:  # noqa: BLE001 — either read failing offers nothing, never "none hold a seat"
        return error_partial(request, "Couldn't read the license seats or the directory again, so nothing is offered. "
                             + unread_why(exc), gam_detail(exc))
    found = licenses.on_suspended(seats, users)
    held = [(email, seat) for email, seat in found if licenses.nameable(seat)]
    token = st.previews.hold(_SEATS_FLOW, _SEATS_KEY, held, tenant=tenant) if held else ""
    changes, typed_address = _seat_check(held)
    decision = guard.evaluate(changes, typed_count_above=guard.COUNT_CONFIRM_ABOVE, typed_address=typed_address)
    return TEMPLATES.TemplateResponse(request, _SEATS_PARTIAL, {
        "held": held[:_SEATS_SHOWN], "count": len(held), "people": len({email for email, _ in held}),
        "unnameable": len(found) - len(held), "typed_address": typed_address, "decision": decision, "token": token,
        "might_lose": licenses.MIGHT_LOSE_DATA, "wait": licenses.REASSIGN_WAIT, "source": licenses.SOURCE,
        "only_read": licenses.ONLY_SEATS_READ})


@router.post("/suspended-licenses/apply", response_class=HTMLResponse)
async def suspended_licenses_apply(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    held, refusal = st.previews.take(_SEATS_FLOW, str(form.get(TOKEN_FIELD) or ""), _SEATS_KEY,
                                     again="click Remove their licenses… again")
    if refusal:
        return error_partial(request, refusal)
    changes, typed_address = _seat_check(held)
    refusal = guard.enforce(changes, form, confirm_step=True, typed_count_above=guard.COUNT_CONFIRM_ABOVE,
                            typed_address=typed_address)
    if refusal:
        return error_partial(request, refusal)
    n = len(held)
    job = start_job(st.jobs, n, kind=_SEATS_KIND, title=f"Remove {n} license{'s' if n != 1 else ''} from suspended accounts")
    job.task = asyncio.create_task(bulk.remove_licenses(job, conn, held))
    return TEMPLATES.TemplateResponse(request, _SEATS_RUN, {"job": job, "wait": licenses.REASSIGN_WAIT})


@router.get("/suspended-licenses/status", response_class=HTMLResponse)
async def suspended_licenses_status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != _SEATS_KIND:
        return TEMPLATES.TemplateResponse(request, _SEATS_RUN, {"error": "That run is no longer here."})
    return TEMPLATES.TemplateResponse(request, _SEATS_RUN, {"job": found, "wait": licenses.REASSIGN_WAIT})


# --- No 2SV / No recovery → a reminder email (roadmap 3.3, D3) ---------------------------------------------------------
# The card's form holds the subject and message; Preview reads the directory again and holds the people it found with that
# exact text, under a single-use token keyed on the bucket and the text. Send posts the live form: text edited since the
# preview is refused, never sent.
_REMIND_FLOW = "remind_bucket"
_REMIND_SHOWN = 200


def _remind_key(key: str, subject: str, message: str) -> tuple:
    return (key, subject.strip(), message.strip())


@router.post("/remind/preview", response_class=HTMLResponse)
async def remind_preview(request: Request, key: Annotated[str, Form()] = "", subject: Annotated[str, Form()] = "",
                         message: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    tenant = st.tenant_key()   # before the read: what the preview is bound to
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    if key not in reports_mod.REMINDERS:
        return error_partial(request, "Not a list that gets a reminder.")
    if not subject.strip() or not message.strip():
        return error_partial(request, "Write a subject and a message first.")
    try:
        users = await st.users(force=True)   # read now: the card counted a cached list
    except Exception as exc:  # noqa: BLE001 — a failed read is said, and nothing is offered
        return error_partial(request, "Couldn't read the directory again, so nothing is offered. " + friendly(exc))
    people = reports_mod.to_remind(users, key)
    emails = [u.primary_email for u in people]
    token = st.previews.hold(_REMIND_FLOW, _remind_key(key, subject, message), emails, tenant=tenant) if emails else ""
    decision = guard.evaluate(guard.changes(emails, RiskLevel.LOW, "Send a reminder"), typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    return TEMPLATES.TemplateResponse(request, "_remind_preview.html", {
        "key": key, "people": people[:_REMIND_SHOWN], "count": len(people), "token": token, "decision": decision,
        "subject": subject.strip()})


@router.post("/remind/apply", response_class=HTMLResponse)
async def remind_apply(request: Request, key: Annotated[str, Form()] = "", subject: Annotated[str, Form()] = "",
                       message: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    emails, refusal = st.previews.take(_REMIND_FLOW, str(form.get(TOKEN_FIELD) or ""), _remind_key(key, subject, message),
                                       again="click Preview again", what="message")
    if refusal:
        return error_partial(request, refusal)
    refusal = guard.enforce(guard.changes(emails, RiskLevel.LOW, "Send a reminder"), form, confirm_step=True,
                            typed_count_above=guard.COUNT_CONFIRM_ABOVE)
    if refusal:
        return error_partial(request, refusal)
    n = len(emails)
    job = start_job(st.jobs, n, kind="remind", title=f"Reminder “{subject.strip()}” to {n} {'person' if n == 1 else 'people'}")
    job.bucket = key   # which card's panel shows it, for the status poll and the jobs tray
    job.task = asyncio.create_task(bulk.send_reminders(job, conn, emails, subject.strip(), reports_mod.reminder_html(message)))
    return TEMPLATES.TemplateResponse(request, "_remind_run.html", {"job": job, "key": key})


@router.get("/remind/status", response_class=HTMLResponse)
async def remind_status(request: Request, job: str = "") -> HTMLResponse:
    found = request.app.state.gamgui.jobs.get(job)
    if found is None or found.kind != "remind":
        return TEMPLATES.TemplateResponse(request, "_remind_run.html", {"error": "That run is no longer here.", "key": ""})
    return TEMPLATES.TemplateResponse(request, "_remind_run.html", {"job": found, "key": found.bucket})


# --- mail review (roadmap 3.2): forwarding that is on, and mail delegates, across every active account ----------------
_MAIL_REVIEW_PARTIAL = "_mail_review.html"
MAIL_ROWS_SHOWN = 100   # rows of each list the panel draws; the count says how many more (bounded page, invariant 9)


def _mail_review_response(request: Request, job=None, announce: bool = False) -> HTMLResponse:
    st = request.app.state.gamgui
    ctx = {"connected": st.connector is not None, "job": job, "announce": announce, "shown": MAIL_ROWS_SHOWN}
    if st.mail_review is not None:
        review, when = st.mail_review
        ctx.update(review=review, review_age=clock.now() - when)
    return TEMPLATES.TemplateResponse(request, _MAIL_REVIEW_PARTIAL, ctx)


@router.get("/mail-review", response_class=HTMLResponse)
async def mail_review(request: Request, job: str = "") -> HTMLResponse:
    """The last review, a running one, or the button. Never reads by itself: one Gmail call per account, twice."""
    st = request.app.state.gamgui
    asked = st.jobs.get(job) if job else None
    if asked is not None and asked.kind != "mail_review":
        asked = None
    if asked is not None and asked.finished and asked.id != st.mail_review_job_id:
        asked = None
    return _mail_review_response(request, asked or st.jobs.get(st.mail_review_job_id), announce=asked is not None)


async def _read_mail_review(job, st, conn) -> None:
    """Background: read every account's forwarding and delegates, kept only while ``conn`` is still the connected tenant's."""
    try:
        job.current = "Reading the directory, then every active account's forwarding and delegates…"
        people = [u.primary_email for u in await st.users(force=True) if not u.suspended]   # the list each read is given
        review = await conn.mail_review(people)
        if st.connector is conn:
            st.mail_review = (review, clock.now())
            job.applied = len(review.forwarding) + len(review.delegations)
        else:
            job.error = "Not kept: the connection changed during the read. Read again."
    except Exception as exc:  # noqa: BLE001 — surface any failure in the panel, in fixed words
        timed_out = isinstance(exc, GAMError) and exc.kind is GAMErrorKind.TIMEOUT
        job.error = _READ_TIMED_OUT if timed_out else unread_why(exc)
    finally:
        job.finish()


@router.post("/mail-review/read", response_class=HTMLResponse)
async def mail_review_read(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return _mail_review_response(request)
    existing = st.jobs.get(st.mail_review_job_id)
    if existing is not None and not existing.finished:  # don't start a second domain-wide read
        return _mail_review_response(request, existing, announce=True)
    job = start_job(st.jobs, 0, kind="mail_review", title="Mail review read")
    st.mail_review_job_id = job.id
    job.task = asyncio.create_task(_read_mail_review(job, st, st.connector))
    return _mail_review_response(request, job, announce=True)
