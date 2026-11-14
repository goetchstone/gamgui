"""Incident logs: pull every log an incident needs, by account and by the attacker's address, and export what came back.

Reads only, as a background job: a pull for every account can take many reads a log, and the parsing runs off the event loop. Rules:
``core/incident/logs.py`` and ``pull.py``. Runbook: docs/domains/incident.md.
"""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core.gam.commands import is_one_address
from ...core.gam.models import primary_of_every_address
from ...core.csv_safe import csv_safe
from ...core.incident import groups, logs, pull, reader, reads, runs, store
from ...core.incident.report import got_it_table
from ...core.phishing import MOST_AT_ONCE
from ...core.incident.logs import report_time
from ..jobs import start_job
from ..server import TEMPLATES
from .incident_kept import read_back, reopen_remembered
from ._common import NOT_CONNECTED, admin_address, error_partial, incident_store, known_sign_ins, nobody_has_checks, open_incident_record

router = APIRouter(prefix="/incident/logs")

_SHOWN = 100     # rows drawn a log: the CSV has them all
_PARTS = ("findings", "respond")     # Investigate's and Respond's of a reading
_PANEL = "_incident_logs.html"


def _running(st):
    job = st.jobs.get(st.incident_pull_job)
    return job if job is not None and not job.finished else None


def _shown_job(st):
    """What the logs screen shows going on: a pull, or an incident's saved logs still being read, which is what it will then show."""
    opening = st.jobs.get(st.incident_open_job)
    return _running(st) or (opening if opening is not None and not opening.finished else None)


def _context(st, job=None, narrowing=None, narrowed=None) -> dict:
    return {"job": job, "pull": st.incident_pull, "progress": st.incident_progress, "names": logs.NAMES, "shown": _SHOWN, "report_time": report_time,
            "window_words": logs.window_words, "narrowing": narrowing, "narrowed": narrowed}


@router.get("", response_class=HTMLResponse)
async def page(request: Request, accounts: str = "", ip: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    reopen_remembered(st)
    narrowing = pull.Filter.of_query(request.query_params)
    held = st.incident_pull
    narrowed = await asyncio.to_thread(lambda: [one for one in (narrowing.applied(log) for log in held.logs) if one]) if narrowing and held is not None else None
    return TEMPLATES.TemplateResponse(request, "incident_logs.html", {
        "connected": st.connector is not None, "applications": logs.APPLICATIONS, "defaults": logs.INCIDENT_DEFAULT, "hours": logs.HOURS,
        "default_hours": logs.DEFAULT_HOURS, "window_words": logs.window_words, "accounts": accounts, "ip": ip, "here": "/incident/logs", "open_incident": open_incident_record(st),
        **_context(st, _shown_job(st), narrowing, narrowed)})


async def _pull(job, st, conn, query: pull.Query, into: pull.Pull, record_id: str, adding: Optional[pull.Pull] = None) -> None:
    def stopped() -> bool:
        return job.cancel_requested

    def finished() -> None:
        job.done += 1

    try:
        await pull.pull_logs(conn, query, into, st.incident_progress, stopped=stopped, finished=finished)
        # What this read adds to wasn't saved when it's on screen and not from the evidence: it goes first, or the evidence shown next would lose it.
        adding_saved = True
        if record_id and adding is not None and not adding.from_evidence:
            adding_saved = await _keep(job, st, conn, adding, record_id)
        said = job.error
        await _keep(job, st, conn, into, record_id)
        job.error = " ".join(s for s in (said, None if job.error == said else job.error) if s) or None
        # Whatever no incident took: a narrower read never takes away what the wider one found.
        if adding is not None and st.connector is conn and st.incident_open == record_id and (st.incident_pull is into or not adding_saved):
            st.incident_pull, unread = pull.and_unread(await asyncio.to_thread(pull.merged, [adding, st.incident_pull]), into)
            job.error = " ".join((*([job.error] if job.error else []), *unread)) or None
            st.incident_reading = None
    finally:
        job.finish()


async def _keep(job, st, conn, read: pull.Pull, record_id: str) -> bool:
    """A read made while an incident was open, saved to that incident, whichever is open by now, before it ends, and added to what's shown in memory from the very text written: no saved read is read
    again, which took minutes in SwiftGamGui (its failure log 2026-10-23). All of them are read back only when that could differ: the evidence isn't read, a read was added beside this one, or this
    read's name isn't last. Whatever can't be saved is still shown, said so, and what it couldn't read is still said once the evidence shows instead. False when it couldn't be saved."""
    if not record_id:
        return True
    job.title = "Save this pull to the incident"
    opening = st.jobs.get(st.incident_open_job)
    if opening is not None and not opening.finished and opening.task is not None:
        await asyncio.wait({opening.task})      # what it's reading would replace what this adds
    kept, why = incident_store(st)
    name, export, others = "", None, set()
    try:
        if kept is None:
            raise store.Problem(why)
        export = await asyncio.to_thread(pull.as_export, read)
        if export is not None:
            name = await asyncio.to_thread(kept.keep, export, record_id)
            others = set(await asyncio.to_thread(kept.names, record_id)) - {name}
    except (store.Problem, OSError) as exc:
        job.error = f"This pull wasn't saved to the incident: {exc}"
        return False

    def elsewhere() -> bool:
        return st.incident_open != record_id or st.connector is not conn

    if elsewhere():
        return True
    if export is not None:
        saved = await asyncio.to_thread(pull.pull_of_export, export)
        known = {*st.incident_saved, *st.incident_saved_unreadable}
        if saved is None or st.incident_evidence is None or others != known or any(earlier >= name for earlier in st.incident_saved):
            if not await read_back(st, conn, kept, record_id):
                return True
        else:
            together = await asyncio.to_thread(pull.merged, [st.incident_evidence, saved])
            if elsewhere():
                return True
            st.incident_evidence = together
            st.incident_saved = [*st.incident_saved, name]
            if (swept := pull.swept_address(export.about)):
                st.incident_swept = {*st.incident_swept, swept}
    if st.incident_evidence is not None:
        st.incident_pull, unread = pull.and_unread(st.incident_evidence, read)
        job.error = " ".join(unread) or job.error
        st.incident_reading = None
    return True


def _start(st, conn, query: pull.Query, into: pull.Pull, title: str, adding: Optional[pull.Pull] = None):
    reopen_remembered(st)       # a tab left open across a restart: its pull belongs to the incident this comes back to
    st.incident_pull = into
    st.incident_progress = {a: pull.Progress() for a in query.applications}
    job = start_job(st.jobs, len(query.applications), kind="incident_logs", title=title)
    st.incident_pull_job = job.id
    job.task = asyncio.create_task(_pull(job, st, conn, query, into, st.incident_open, adding))
    return job


@router.post("/pull", response_class=HTMLResponse)
async def start_pull(request: Request, accounts: Annotated[str, Form()] = "", ip: Annotated[str, Form()] = "",
                     hours: Annotated[int, Form()] = logs.DEFAULT_HOURS) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    form = await request.form()
    wanted = [str(v) for v in form.getlist("log") if str(v) in logs.NAMES]
    if not wanted:
        return error_partial(request, "Tick at least one log to pull.")
    if hours not in logs.HOURS:
        return error_partial(request, "Pick how far back to read.")
    ip = ip.strip()
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return error_partial(request, f"“{ip}” isn't one IP address.")
    users = logs.accounts_typed(accounts)
    if len(users) > pull.MOST_ACCOUNTS:
        return error_partial(request, f"At most {pull.MOST_ACCOUNTS} accounts at once: pull the rest after these, or every account by the attacker's address.")
    if bad := next((u for u in users if not is_one_address(u)), None):
        return error_partial(request, f"“{bad}” isn't one email address.")
    if existing := _running(st):
        return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, existing))
    # One address that isn't a user fails the whole log in GAM: said here, by name, rather than as every log's failure.
    # Read fresh before refusing: an account the attacker made since the list was read is just who to look at. Chrome's log finds nothing for a typo, without a word, so unchecked isn't safe.
    if users:
        try:
            known = {u.primary_email.lower() for u in await st.users(stale_ok=True)}
            if any(u.lower() not in known for u in users):
                known = {u.primary_email.lower() for u in await st.users(force=True)}
        except Exception as exc:  # noqa: BLE001 - said, whatever it was
            return error_partial(request, f"Couldn't read the directory to check those addresses: {reads.in_words(exc)}")
        if unknown := [u for u in users if u.lower() not in known]:
            return error_partial(request, f"Not in the directory: {', '.join(unknown)}. Check the address, or use the primary one, not an alias.")
    query = pull.Query(tuple(wanted), tuple(users), hours, ip)
    title = f"Pull {len(wanted)} log{'' if len(wanted) == 1 else 's'}"
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, _start(st, conn, query, pull.Pull(query), title)))


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    held = st.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, held if held is not None and held.kind in ("incident_logs", "incident_open") else None))


def _merged(old: pull.Log, log: pull.Log, until: datetime, now: datetime) -> pull.Log:
    seen = {r.identity for r in old.rows or []}
    rows = logs.newest_first([*(old.rows or []), *(r for r in log.rows or [] if r.identity not in seen)])
    through = log.through if log.capped else until if until < now else None     # a window ended short of now leaves the rest still to read
    return pull.Log.read(old.application, rows, old.hours, log.capped or until < now, through, old.covered, whole=(*old.whole, *log.whole))


async def _read_rest(job, st, conn, held: pull.Pull, application: str, since: datetime, record_id: str) -> None:
    """One log from where it stopped to now: added to what's shown, each event once, read through the later of the two. Gmail reads at most 30 days at once: its window ends 30
    days past where it stopped, never starts later than that, or the stretch between would be skipped and the log claimed whole."""
    now = datetime.now(timezone.utc)
    until = min(now, since + timedelta(hours=logs.GMAIL_MAX_HOURS)) if application == "gmail" else now
    query = pull.Query((application,), held.query.users, held.query.hours, held.query.ip, since, until)
    st.incident_progress = {application: pull.Progress()}

    def stopped() -> bool:
        return job.cancel_requested

    try:
        log = await pull.read_log(conn, application, query, now, progress=st.incident_progress[application], stopped=stopped)
        old = held.log(application)
        if old is None or st.incident_pull is not held:
            return
        if log.rows is None:
            job.error = f"Couldn't read the rest of the {old.name} log: {log.problem}"
            return
        merged = await asyncio.to_thread(_merged, old, log, until, now)
        # A new pull, not this one changed: this one can be the incident's evidence, which stays what reading it back gives.
        shown = replace(held, logs=[merged if x.application == application else x for x in held.logs])
        await asyncio.to_thread(shown.work_out_addresses)
        st.incident_pull = shown
        st.incident_reading = None
        # Saved as what it read, stopped where the two stopped: put together with what's saved, one read whole makes the log whole.
        rest = pull.Pull(query, [pull.Log.read(application, log.rows or [], old.hours, merged.capped, merged.through)], ended=True, at=now)
        await _keep(job, st, conn, rest, record_id)
    finally:
        job.done += 1
        job.finish()


@router.post("/rest", response_class=HTMLResponse)
async def read_the_rest(request: Request, application: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    held = st.incident_pull
    log = held.log(application) if held else None
    if log is None or log.through is None:
        return error_partial(request, "That log has nothing left to read: pull it again.")
    if existing := _running(st):
        return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, existing))
    job = start_job(st.jobs, 1, kind="incident_logs", title=f"Read the rest of the {log.name} log")
    st.incident_pull_job = job.id
    job.task = asyncio.create_task(_read_rest(job, st, conn, held, application, log.through, st.incident_open))
    return TEMPLATES.TemplateResponse(request, _PANEL, _context(st, job))


def _when(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _people_of(users) -> dict:
    """Each person's addresses to their own: their aliases, and the domain aliases Google adds (`nonEditableAliases`, which GAM returns with the aliases asked for). A person's own address is never taken by
    another's alias."""
    return primary_of_every_address(users)


async def _context_for(st, held: pull.Pull) -> reader.Context:
    """What the pull says that its events don't. The directory, when it can be read: until it is, a group can't be told from a person."""
    try:
        people = _people_of(await st.users(stale_ok=True))
    except Exception:  # noqa: BLE001 - said on the to-do list: read the directory to finish
        people = {}
    capped = {}
    for log in held.logs:
        dates = [r.date for r in log.rows or [] if r.date]
        if log.capped and not log.through and dates:    # read newest first to its cap: its oldest event is as far back as it got
            capped[log.application] = min(dates)
        elif log.capped and log.through:
            capped[log.application] = log.through
    conn = st.connector
    domain = conn.domain if conn else ""
    known, _ = known_sign_ins(st)
    nobody, _ = nobody_has_checks(st)
    pulled = await asyncio.to_thread(runs.latest_runs, conn.audit.path, domain) if conn else []
    record = open_incident_record(st)
    warnings: dict = {}
    for picked in record.warnings if record else ():
        warnings.setdefault(reader.bare_message_id(picked.email), set()).add(reader.bare_message_id(picked.warning))
    groups_read = _groups_read(st)
    gmail = held.log("gmail")
    return reader.Context(pulled_by=held.query.ip, read={log.application for log in held.logs if log.rows is not None}, capped=capped, covered={log.application: set(log.covered) for log in held.logs if log.covered},
                          domains={d.lower() for d in await st.tenant_domains()}, people=people or None, swept=set(st.incident_swept), pulled=pulled,
                          warnings=warnings, phish=record.phish if record else None, groups={g: list(m) for g, m in groups_read.members.items()}, everyone=set(groups_read.everyone),
                          gmail_whole=list(gmail.whole) if gmail else [],
                          theirs={reader.key(m.account, m.address) for m in known.for_domain(domain)} if known else set(),
                          nobody_has=[reader.NobodyHas(c.message_id, c.at, c.by, c.gone) for c in nobody.for_domain(domain)] if nobody else [])


def _groups_read(st) -> groups.GroupsRead:
    if st.incident_groups is None:
        st.incident_groups = groups.GroupsRead()
    return st.incident_groups


@router.get("/groups/status", response_class=HTMLResponse)
async def groups_status(request: Request, job: str = "", part: str = "") -> HTMLResponse:
    """A small poll while the groups are read, and the whole reading once, when they are: refreshing it each second would lose what's being typed there and read the pull again each time."""
    held = request.app.state.gamgui.jobs.get(job)
    return TEMPLATES.TemplateResponse(request, "_incident_groups_status.html", {"groups_job": held if held is not None and held.kind == "incident_groups" else None, "part": part if part in _PARTS else "",
                                                                                 "on_incident": "/incident" in request.headers.get("HX-Current-URL", "")})


async def _read_groups(job, conn, wanted, read: groups.GroupsRead) -> None:
    def note() -> None:
        job.done += 1
        job.total = max(job.total, job.done)

    try:
        await groups.read_groups(conn, wanted, read, note=note)
    finally:
        job.finish()


def _read_groups_behind(st, reading: reader.Reading):
    """The groups the emails went to, read in the background; the screen then asks again and the reading counts each by its members. Until they're read, each counts on the warning's send, as it says."""
    going = st.jobs.get(st.incident_groups_job)
    if going is not None and not going.finished:
        return going
    read = _groups_read(st)
    wanted = groups.to_ask(reading.reaches, read)
    if not wanted or st.connector is None:
        return None
    job = start_job(st.jobs, len(wanted), kind="incident_groups", title="Read the members of the groups it went to")
    st.incident_groups_job = job.id
    job.task = asyncio.create_task(_read_groups(job, st.connector, wanted, read))
    return job


async def _current_reading(st) -> Optional[reader.Reading]:
    """The reader's findings, worked out once for a pull and what's known beside it, and off the event loop: it's linear in the events, and a whole pull holds hundreds of thousands. None before a
    pull has ended. A new mark, or a run in the audit log (back from Phishing), makes a new context, so a new reading."""
    held = st.incident_pull
    if held is None or not held.ended or _running(st):      # one still saving and merging has ended its reads, and isn't yet what's shown
        return None
    context = await _context_for(st, held)
    key = (held.run, id(held.logs), context)
    if st.incident_reading is None or st.incident_reading[0] != key:
        def read_pull() -> reader.Reading:
            return reader.read([reader.event_from_row(r) for log in held.logs for r in log.rows or []], context)

        st.incident_reading = (key, await asyncio.to_thread(read_pull))
    return st.incident_reading[1]


def _ticked(st, reading: reader.Reading, record) -> reader.Reading:
    """With what the open incident marks done by hand: cheap, so a tick never reads the pull again."""
    return reading.ticking([reader.Ticked(t.kind, t.words, t.basis, t.how, t.at, t.by) for t in record.ticks]) if record else reading


def _shows_query(shown: reader.Shown) -> str:
    return pull.Filter(shown.accounts, shown.addresses, shown.logs, shown.apps, shown.agent).query


async def _settled_reading(st) -> Optional[reader.Reading]:
    opening = st.jobs.get(st.incident_open_job)
    if opening is not None and not opening.finished and opening.task is not None:
        await asyncio.wait({opening.task})      # what it's reading is what this reads
    return await _current_reading(st)


async def _reading_panel(request: Request, part: str, problem: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    reading = await _settled_reading(st)
    record = open_incident_record(st)
    known, known_problem = known_sign_ins(st)
    nobody, nobody_problem = nobody_has_checks(st)
    domain = st.connector.domain if st.connector else ""
    groups_job = _read_groups_behind(st, reading) if reading and part != "findings" else None
    # A read of the groups can end during the reading above: that reading is then of the members before it, and nothing is left to ask for it again.
    if reading and groups_job is None and part != "findings" and st.incident_reading[0][2].groups != _groups_read(st).members:
        reading = await _current_reading(st)
        groups_job = _read_groups_behind(st, reading) if reading else None
    said = [known_problem, nobody_problem,
            f"A list of marked sign-ins held something else, and was moved aside as {known.moved_aside}." if known and known.moved_aside else "",
            f"A list of what Find showed gone held something else, and was moved aside as {nobody.moved_aside}." if nobody and nobody.moved_aside else ""]
    return TEMPLATES.TemplateResponse(request, "_incident_reading.html", {
        "reading": _ticked(st, reading, record) if reading else None, "pulling": _running(st) is not None, "when": _when, "part": part if part in _PARTS else "", "problem": problem, "store_problems": [s for s in said if s],
        "marks": known.for_domain(domain) if known else [], "ticking": record is not None, "shows_query": _shows_query, "most_emails": MOST_AT_ONCE,
        "groups_job": groups_job, "group_problems": _groups_read(st).problems, "group_nameless": _groups_read(st).nameless, "got_it": reader.got_it(reading.reaches) if reading else [], "reach_summary": reader.reach_summary, "received_words": reader.received_words, "outcome_words": reader.outcome_words,
        "focus": request.method == "POST" or part != "respond"})   # opening Respond doesn't take the keyboard from its page


@router.get("/reading", response_class=HTMLResponse)
async def reading(request: Request, part: str = "") -> HTMLResponse:
    return await _reading_panel(request, part)


@router.post("/theirs", response_class=HTMLResponse)
async def mark_theirs(request: Request, account: Annotated[str, Form()] = "", address: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "",
                      part: Annotated[str, Form()] = "") -> HTMLResponse:
    """A sign-in Google marked that was the person's own: their word, on this Mac, shown with its note and undone from there."""
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    known, why = known_sign_ins(st)
    if known is None:
        return await _reading_panel(request, part, why)
    # Only what the reading offers, as Mark Done is: a mark clears a suspect, so it's never a pair no log asked about, or a warning Google raised that isn't their own sign-in.
    held = await _current_reading(st)
    offered = {(a.lower(), f.addresses[0]) for f in held.findings if f.addresses for a in f.markable} | {(t.accounts[0].lower(), a) for t in held.to_do if t.accounts for a in t.theirs} if held else set()
    if (account.strip().lower(), address.strip()) not in offered:
        return await _reading_panel(request, part, "That sign-in isn't one the logs ask about: read them again.")
    try:
        known.mark(st.connector.domain, account, address, note)
    except store.Problem as exc:
        return await _reading_panel(request, part, str(exc))
    return await _reading_panel(request, part)


@router.post("/unmark", response_class=HTMLResponse)
async def unmark(request: Request, mark: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    known, why = known_sign_ins(st)
    problem = why
    if known is not None and any(m.id == mark for m in known.for_domain(st.connector.domain)):
        try:
            known.unmark(mark)
        except store.Problem as exc:
            problem = str(exc)
    return await _reading_panel(request, part, problem)


@router.post("/tick", response_class=HTMLResponse)
async def tick(request: Request, kind: Annotated[str, Form()] = "", words: Annotated[str, Form()] = "", basis: Annotated[str, Form()] = "", how: Annotated[str, Form()] = "",
               part: Annotated[str, Form()] = "") -> HTMLResponse:
    """A to-do item marked done by hand, kept with the incident: only one on the list now, of a kind that can take it. A lock, a read, or anything else the logs show done never can."""
    st = request.app.state.gamgui
    if st.connector is None:
        return error_partial(request, NOT_CONNECTED)
    reading = await _settled_reading(st)
    kept, why = incident_store(st)
    record = open_incident_record(st)
    if kept is None or record is None:
        return await _reading_panel(request, part, why or "Open an incident first: what's marked done is kept with it.")
    left = _ticked(st, reading, record).to_do if reading else ()
    if not any((t.kind, t.words, t.basis) == (kind, words, basis) and t.tickable for t in left):
        return await _reading_panel(request, part, "That item can't be marked done: the logs show it, or it has changed. Read it again.")
    try:
        kept.save(replace(record, ticks=(*record.ticks, store.Tick(kind, words, basis, how.strip(), datetime.now(timezone.utc), admin_address(st)))))
    except store.Problem as exc:
        return await _reading_panel(request, part, str(exc))
    return await _reading_panel(request, part)


@router.post("/untick", response_class=HTMLResponse)
async def untick(request: Request, kind: Annotated[str, Form()] = "", words: Annotated[str, Form()] = "", basis: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> HTMLResponse:
    st = request.app.state.gamgui
    kept, why = incident_store(st)
    record = open_incident_record(st)
    if kept is None or record is None:
        return await _reading_panel(request, part, why or "Open an incident first: what's marked done is kept with it.")
    try:
        kept.save(replace(record, ticks=tuple(t for t in record.ticks if (t.kind, t.words, t.basis) != (kind, words, basis))))
    except store.Problem as exc:
        return await _reading_panel(request, part, str(exc))
    return await _reading_panel(request, part)


async def _pick(request: Request, part: str, change) -> HTMLResponse:
    """A pick kept with the open incident: which email was the warning, which started it. ``change`` makes the record as it should be, given the reading, or says why it can't."""
    st = request.app.state.gamgui
    reading = await _settled_reading(st)     # first: what's saved is the record as it is after this wait, never as it was before it
    kept, why = incident_store(st)
    record = open_incident_record(st)
    if kept is None or record is None:
        return await _reading_panel(request, part, why or "Open an incident first: what's picked is kept with it.")
    try:
        kept.save(change(record, reading))
    except store.Problem as exc:
        return await _reading_panel(request, part, str(exc))
    return await _reading_panel(request, part)


@router.post("/warning", response_class=HTMLResponse)
async def pick_warning(request: Request, email: Annotated[str, Form()] = "", warning: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> HTMLResponse:
    """Which email warned the people the attacker's reached: only one the logs offer for it. It's checked again at every reading, and counts only on the log's own word."""
    def change(record, reading):
        reach = next((r for r in reading.reaches if r.message_id == email), None) if reading else None
        if reach is None or not any(c.message_id == warning for c in reach.candidates):
            raise store.Problem("That email isn't one sent after it to someone it reached: read the logs again.")
        return replace(record, warnings=(*(w for w in record.warnings if (w.email, w.warning) != (email, warning)), store.Warning(email, warning)))

    return await _pick(request, part, change)


@router.post("/unwarning", response_class=HTMLResponse)
async def unpick_warning(request: Request, email: Annotated[str, Form()] = "", warning: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> HTMLResponse:
    return await _pick(request, part, lambda record, _: replace(record, warnings=tuple(w for w in record.warnings if (w.email, w.warning) != (email, warning))))


@router.post("/phish", response_class=HTMLResponse)
async def pick_phish(request: Request, message: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> HTMLResponse:
    """Which email started it, or none: only one the logs list. Only the operator says which it was."""
    def change(record, reading):
        if not message:
            return replace(record, phish=None)
        way_in = reading.way_in if reading else None
        if way_in is None or not any(c.message_id == message for c in way_in.candidates):
            raise store.Problem("That email isn't one the logs list as a way in: read the logs again.")
        return replace(record, phish=message)

    return await _pick(request, part, change)


def _partial(st, reaches=None) -> bool:
    """Whether a list of who an email reached can't be everyone: a send naming no one, an address that doesn't read, a group not read or still being read, the Gmail log short or not read. It goes in the file's name."""
    held = st.incident_reading[1] if st.incident_reading else None
    reaches = (held.reaches if held else ()) if reaches is None else reaches
    read, job, pulled = _groups_read(st), st.jobs.get(st.incident_groups_job), st.incident_pull
    gmail = pulled.log("gmail") if pulled else None
    return bool(gmail is None or gmail.rows is None or gmail.capped or (job is not None and not job.finished) or any(r.unnamed or r.unreadable for r in reaches)
                or any(x.address in read.problems or x.address in read.nameless for r in reaches for x in r.recipients))


@router.get("/got-it.csv")
async def got_it_csv(request: Request) -> Response:
    """Everyone the attacker's emails got to, once each, for a mail merge or a contacts import: its first column is `Email`."""
    st = request.app.state.gamgui
    held = await _current_reading(st)
    if held is None:
        return Response("Pull the logs first.", status_code=404, media_type="text/plain")
    table = got_it_table(reader.got_it(held.reaches), None)
    name = f"who-got-it{'-partial' if _partial(st, held.reaches) else ''}.csv"
    return Response(table.csv_text(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@router.get("/reach.csv")
async def reach_csv(request: Request, message: str = "") -> Response:
    """Who one email reached, every address, whatever the screen shows. It says which email and, in its name, when it isn't everyone: a file gets passed around without its page."""
    st = request.app.state.gamgui
    held = await _current_reading(st)
    reach = next((r for r in held.reaches if r.message_id == message), None) if held else None
    if reach is None:
        return Response("That email isn't in the logs read, or a pull is going on.", status_code=404, media_type="text/plain")
    partial = _partial(st, [reach])
    buffer = io.StringIO()
    csv.writer(buffer).writerows([("email", "Message-ID", *reader.REACH_COLUMNS), *([csv_safe(c) for c in (reach.subject, reach.message_id, *row)] for row in reach.rows)])
    name = f"who-it-reached-{reader.fingerprint(reach.message_id)[:8]}{'-partial' if partial else ''}.csv"
    return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@router.post("/again", response_class=HTMLResponse)
async def pull_again(request: Request, accounts: Annotated[str, Form()] = "", part: Annotated[str, Form()] = "") -> Response:
    """The logs a to-do item says stopped short, again for just the accounts that matter, over the window shown: a read, added to what's shown. Those in the directory, 20 at most."""
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return error_partial(request, NOT_CONNECTED)
    held = st.incident_pull
    wanted = [str(v) for v in (await request.form()).getlist("log") if str(v) in logs.NAMES]
    if held is None or not wanted:
        return await _reading_panel(request, part, "Pull the logs first.")
    if _running(st):
        return await _reading_panel(request, part, "A pull is going on: wait for it to finish, then pull again.")
    try:
        directory = {u.primary_email.lower() for u in await st.users(stale_ok=True)}
    except Exception as exc:  # noqa: BLE001 - said, whatever it was
        return await _reading_panel(request, part, f"Couldn't read the directory to check those addresses: {reads.in_words(exc)}")
    typed = logs.accounts_typed(accounts)
    users = [u for u in typed if u.lower() in directory][:pull.MOST_ACCOUNTS]
    if not users:
        return await _reading_panel(request, part, "None of those accounts is in the directory.")
    # Over the window shown, the one the first read looked at, never one that's moved on since. The rest past 20 stay to do, the log still short for them.
    hours = min(held.query.hours, max(logs.HOURS))
    end = held.at or datetime.now(timezone.utc)
    query = pull.Query(tuple(wanted), tuple(users), hours, "", end - timedelta(hours=hours), end)
    job = _start(st, conn, query, pull.Pull(query), f"Pull {len(wanted)} log{'' if len(wanted) == 1 else 's'} again", adding=held)
    if missing := [u for u in typed if u.lower() not in directory]:
        job.error = f"Not in the directory, so not read: {', '.join(missing)}."
    return Response(status_code=204, headers={"HX-Redirect": "/incident/logs"})


@router.get("/log.csv")
async def log_csv(request: Request, application: str = "") -> Response:
    held = request.app.state.gamgui.incident_pull
    log = held.log(application) if held else None
    if log is None or log.nothing_kept:     # never an empty CSV that would pass as no events
        return Response(f"That log wasn't read. {log.how_far if log else ''}".strip(), status_code=404, media_type="text/plain")
    name = log.file_name
    if narrowing := pull.Filter.of_query(request.query_params):     # just the events shown, and it says so: a file gets passed around without its page
        log = await asyncio.to_thread(narrowing.applied, log)
        if log is None or log.nothing_kept:
            return Response("None of that log's events are those.", status_code=404, media_type="text/plain")
        name = name.removesuffix(".csv") + "-narrowed.csv"
    text = await asyncio.to_thread(pull.csv_of, log)
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


def _zip(held: pull.Pull, stamp: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for log in held.logs:
            if not log.nothing_kept:
                archive.writestr(log.file_name, pull.csv_of(log))
        archive.writestr("about.txt", pull.about(held, stamp))
    return buffer.getvalue()


@router.get("/export.zip")
async def export_all(request: Request) -> Response:
    st = request.app.state.gamgui
    held = st.incident_pull
    if held is None or not held.ended or _running(st):      # one still saving and merging isn't yet what's shown
        return Response("There's no finished pull to export.", status_code=404, media_type="text/plain")
    now = datetime.now().astimezone()
    data = await asyncio.to_thread(_zip, held, now.strftime("%Y-%m-%d %H:%M %Z"))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=incident-logs-{now.strftime('%Y-%m-%d-%H%M')}.zip"})
