"""The in-memory, polled progress record every bulk operation shares.

A job lives on ``AppState.jobs`` (id -> job) and is rendered by an HTMX-polled partial, so a long
per-target loop reports progress instead of looking frozen. ``Job`` is the one bounded base (invariant
#9); ``BatchJob`` adds the per-step log of a short multi-step routine, and onboarding's ``OnboardJob``
its account tallies and credentials sheet. When a per-user loop stops early is ``core/bulk.py``
``stop_reason``, or ``stop_requested`` once the operator pressed Stop. The header's jobs tray
(``tray``, plan U5) lists them on every page.
"""

from __future__ import annotations

import secrets
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple, TypeVar

from ..core import clock

# Invariant #9: a run where thousands genuinely fail keeps the full count but only a sample of them,
# and the live feed only its newest rows, so the final summary (and each 1s poll) stays small.
RECENT_WINDOW = 12
FAILED_SAMPLE_CAP = 200
DETAIL_CAP = 300   # chars kept of a failure's reason and raw error: GAM echoes the argv (a whole body) on a usage error
TRAY_ROWS = 8      # jobs the header tray lists (#9: it is polled on every page)
KEEP_FINISHED = 10 # finished jobs kept, and as many finished one-step changes

# kind -> the status route that renders the job's own panel (its page, /jobs/<id>, loads it from there).
PANELS = {
    "signatures": "/signatures/apply/status",
    "department": "/users/bulk/status",
    "offboard": "/lifecycle/offboard/status",
    "subscribe": "/calendars/share/status",
    "index": "/calendars/index/status",
    "connected_apps": "/reports/connected-apps",
    "phishing_find": "/phishing/find/status",
    "phishing": "/phishing/trash/status",
    "sequence": "/builder/sequence/status",
    "onboard": "/onboard/bulk/status",
    "finish": "/onboard/finish/status",   # steps retried until Google has set a new account up
}
UNSTOPPABLE = {"index", "connected_apps"}   # one domain-wide read, not a loop: there is no "between targets"
CONFIRM_STOP = {"offboard"}      # a stop leaves the leaver half offboarded: ask first
ONE_STEP = "change"              # a change made from a screen's button, not a loop: no panel of its own, and no Stop

# A write in one of these is already that job's row. A task copies the context it's made in, and a route makes a job's
# task just after registering it, so this reaches the job's task and any it starts, and never another request.
_in_job: ContextVar[bool] = ContextVar("gamgui_in_job", default=False)
_request_change: ContextVar[Optional["Job"]] = ContextVar("gamgui_request_change", default=None)

# What the tray calls each write. tests/test_jobs_tray.py holds every action the connector runs to this.
CHANGE_WORDS = {
    "add_calendar_acl": "Share their calendar",
    "add_calendar_acl_cal": "Share a calendar",
    "add_calendar_event": "Add a calendar event",
    "add_delegate": "Add a mail delegate",
    "add_group_member": "Add to a group",
    "apply": "Builder change",
    "clear_recovery": "Clear recovery email and phone",
    "clear_vacation": "Turn off an auto-reply",
    "create_user": "Create an account",
    "delete_calendar": "Delete a calendar",
    "delete_event": "Delete an event",
    "delete_user": "Delete an account",
    "forward_off": "Turn off forwarding",
    "remove_calendar_acl": "Unshare their calendar",
    "remove_calendar_acl_cal": "Unshare a calendar",
    "remove_delegate": "Remove a mail delegate",
    "remove_from_all_calendars": "Remove from everyone's calendars",
    "remove_group_member": "Remove from a group",
    "onboard_runbook": "Create a task list",
    "reset_password": "Reset a password",
    "revoke_access": "Revoke access",
    "send_welcome_email": "Send a welcome email",
    "set_organization": "Set title or department",
    "set_password": "Set a temporary password",
    "set_signature": "Set a signature",
    "set_vacation": "Set an auto-reply",
    "signout_user": "Sign out everywhere",
    "subscribe_calendar": "Add a calendar to their list",
    "delete_filters": "Remove filters that forward",
    "hide_from_directory": "Hide from the directory",
    "imap_off": "Turn off IMAP",
    "pop_off": "Turn off POP",
    "transfer_calendar_ownership": "Hand over secondary calendars",
    "transfer_data": "Transfer data",
    "trash_message": "Trash an email's copies",
    "trash_spam_messages": "Trash an email's copies in Spam",
    "update_phones": "Change the phone a signature shows",
}


@dataclass
class Outcome:
    """One target's result: a row of the live feed and, when it failed, of the failed sample."""

    item: str
    ok: bool
    reason: str = ""   # a failure's remediation, in words
    detail: str = ""   # a failure's raw GAM error, for a collapsed "GAM's error"


@dataclass
class Job:
    total: int
    id: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    done: int = 0
    applied: int = 0
    failed_total: int = 0
    failed: List[Outcome] = field(default_factory=list)   # capped sample — fill it with record(), never .append
    recent: List[Outcome] = field(default_factory=list)   # rolling feed of the newest `window` outcomes, newest last
    window: int = RECENT_WINDOW
    current: str = ""
    finished: bool = False
    finished_at: float = 0.0
    error: Optional[str] = None
    cancel_requested: bool = False   # Stop pressed (plan U5): each loop checks it before its next target
    interrupted: bool = False        # cut off (the app quit) before every step was accounted for
    title: str = ""                  # what the tray calls it ("Offboarding carol@example.com")
    kind: str = ""                   # a PANELS key: which screen's panel shows it
    retry: object = None             # what "Retry the N that failed" re-runs: a signature's template, a department
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run

    def record(self, item: str, ok: bool, reason: str = "", detail: str = "") -> None:
        """One target done: tally it, keep a failure in the capped sample, and roll the live feed."""
        outcome = Outcome(item, ok, (reason or "")[:DETAIL_CAP], (detail or "")[:DETAIL_CAP])
        self.done += 1
        if ok:
            self.applied += 1
        else:
            self.failed_total += 1
            if len(self.failed) < FAILED_SAMPLE_CAP:
                self.failed.append(outcome)
        self.recent.append(outcome)
        if len(self.recent) > self.window:
            del self.recent[0]

    def finish(self) -> None:
        self.current = ""
        self.finished = True
        self.finished_at = clock.now()

    @property
    def more(self) -> int:
        """Failures counted but past the sample: the "+K more" a final panel prints."""
        return self.failed_total - len(self.failed)

    @property
    def failed_items(self) -> List[str]:
        return [f.item for f in self.failed]

    @property
    def can_retry(self) -> bool:
        """A finished run whose failed sample holds every failure (#9 caps it): only then is a retry of
        the sample a retry of all of them."""
        return self.finished and self.failed_total > 0 and not self.more and self.retry is not None

    @property
    def status_url(self) -> str:
        return f"{PANELS[self.kind]}?job={self.id}" if self.kind in PANELS else ""

    @property
    def can_stop(self) -> bool:
        return not self.finished and self.kind in PANELS and self.kind not in UNSTOPPABLE

    @property
    def confirm_stop(self) -> bool:
        return self.kind in CONFIRM_STOP


@dataclass
class BatchJob(Job):
    log: List[str] = field(default_factory=list)      # per-step outcome lines (offboarding's handful of steps)
    skipped: List[str] = field(default_factory=list)  # not run: a step it relies on failed (offboarding's few)
    not_applicable: List[str] = field(default_factory=list)  # offboarding step keys left out on purpose


J = TypeVar("J", bound=Job)


def _prune(jobs: dict, keep: int) -> None:
    """Keep the newest ``keep`` finished jobs, and as many one-step changes: ten small edits must not push out a bulk
    run's result and its Retry."""
    for changes in (True, False):
        finished = [jid for jid, j in jobs.items() if j.finished and (j.kind == ONE_STEP) is changes]
        for jid in finished[:-keep] if len(finished) > keep else []:
            jobs.pop(jid, None)


def register_job(jobs: dict, job: J, keep: int = KEEP_FINISHED) -> J:
    """Register ``job``, pruning the oldest finished jobs first so the registry can't grow forever."""
    _prune(jobs, keep)
    jobs[job.id] = job
    try:
        asyncio.current_task()
    except RuntimeError:     # no event loop: nothing here to start a job's task, and no request's context to mark
        return job
    _in_job.set(True)
    return job


def start_job(jobs: dict, total: int, keep: int = KEEP_FINISHED, window: int = RECENT_WINDOW,
              title: str = "", kind: str = "") -> BatchJob:
    """Register a fresh ``BatchJob`` whose live feed keeps its newest ``window`` rows."""
    return register_job(jobs, BatchJob(total=total, window=window, title=title, kind=kind), keep=keep)


def watch_writes(jobs: dict) -> Callable[[str, str], Optional[Callable[[bool, str], None]]]:
    """``GAMConnector.on_write``: list a write made outside any job, so the tray is everything that ran since launch.
    A request's writes share a row: sharing a calendar is an access rule and a subscribe, one change to the person
    who clicked. Its title holds the first write's words and target, never its argv."""
    def row_for(action: str, target: str) -> Job:
        held = _request_change.get()
        if held is not None and held.id in jobs:
            return held
        words = CHANGE_WORDS.get(action, action.replace("_", " ").capitalize())
        row = Job(total=0, kind=ONE_STEP, title=f"{words}: {target}")
        _prune(jobs, KEEP_FINISHED)
        jobs[row.id] = row
        _request_change.set(row)
        return row

    def on_write(action: str, target: str) -> Optional[Callable[[bool, str], None]]:
        if _in_job.get():
            return None
        change = row_for(action, target)
        change.total += 1
        change.finished = False

        def ended(ok: bool, what_to_do: str) -> None:
            change.record(target, ok, what_to_do or "")
            change.finish()
        return ended
    return on_write


def retry_key(job_id: str) -> Tuple[str, str]:
    """The preview-store form key a retry's preview holds its people under — the job, not a live form."""
    return ("retry", job_id)


def retry_of(jobs: dict, job_id: str, kind: str) -> Tuple[Optional[Job], Optional[str]]:
    """The finished ``kind`` job whose failures a retry previews (plan U5), or why there is none."""
    job = jobs.get(job_id)
    if job is None or job.kind != kind or not job.finished or job.retry is None:
        return None, "That run is no longer available — run it again from the start."
    if job.more:
        return None, (f"Only {len(job.failed)} of the {job.failed_total} failures were kept, so a retry "
                      "can't cover them all — run it again from the start.")
    if not job.failed_total:
        return None, "Nothing failed in that run."
    return job, None


def tray(jobs: dict) -> Tuple[List[Job], int, int]:
    """The header tray (plan U5): at most ``TRAY_ROWS`` jobs — running first, then finished jobs, then finished one-step
    changes, newest first in each, so small edits can't push a bulk run's result and Retry off it — how many are running,
    and how many more there are than it lists."""
    newest = list(jobs.values())[::-1]
    running = [j for j in newest if not j.finished]
    finished = [j for j in newest if j.finished]
    rows = (running + [j for j in finished if j.kind != ONE_STEP] + [j for j in finished if j.kind == ONE_STEP])[:TRAY_ROWS]
    return rows, len(running), len(newest) - len(rows)
