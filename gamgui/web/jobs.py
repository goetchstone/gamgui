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
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, TypeVar

from ..core import clock

# Invariant #9: a run where thousands genuinely fail keeps the full count but only a sample of them,
# and the live feed only its newest rows, so the final summary (and each 1s poll) stays small.
RECENT_WINDOW = 12
FAILED_SAMPLE_CAP = 200
DETAIL_CAP = 300   # chars kept of a failure's reason and raw error: GAM echoes the argv (a whole body) on a usage error
TRAY_ROWS = 8      # jobs the header tray lists (#9: it is polled on every page)

# kind -> the status route that renders the job's own panel (its page, /jobs/<id>, loads it from there).
PANELS = {
    "signatures": "/signatures/apply/status",
    "department": "/users/bulk/status",
    "offboard": "/lifecycle/offboard/status",
    "subscribe": "/calendars/share/status",
    "index": "/calendars/index/status",
    "sequence": "/builder/sequence/status",
    "onboard": "/onboard/bulk/status",
}
UNSTOPPABLE = {"index"}          # one domain-wide read, not a loop: there is no "between targets"
CONFIRM_STOP = {"offboard"}      # a stop leaves the leaver half offboarded: ask first


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


J = TypeVar("J", bound=Job)


def register_job(jobs: dict, job: J, keep: int = 10) -> J:
    """Register ``job``, pruning the oldest finished jobs first so the registry can't grow forever."""
    finished = [jid for jid, j in jobs.items() if j.finished]
    for jid in finished[:-keep] if len(finished) > keep else []:
        jobs.pop(jid, None)
    jobs[job.id] = job
    return job


def start_job(jobs: dict, total: int, keep: int = 10, window: int = RECENT_WINDOW,
              title: str = "", kind: str = "") -> BatchJob:
    """Register a fresh ``BatchJob`` whose live feed keeps its newest ``window`` rows."""
    return register_job(jobs, BatchJob(total=total, window=window, title=title, kind=kind), keep=keep)


def tray(jobs: dict) -> Tuple[List[Job], int, int]:
    """The header tray (plan U5): at most ``TRAY_ROWS`` jobs — running first, then finished, newest
    first in each — how many are running, and how many more there are than it lists."""
    newest = list(jobs.values())[::-1]
    running = [j for j in newest if not j.finished]
    rows = (running + [j for j in newest if j.finished])[:TRAY_ROWS]
    return rows, len(running), len(newest) - len(rows)
