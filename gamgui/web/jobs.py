"""The in-memory, polled progress record every bulk operation shares.

A job lives on ``AppState.jobs`` (id -> job) and is rendered by an HTMX-polled partial, so a long
per-target loop reports progress instead of looking frozen. ``Job`` is the one bounded base (invariant
#9); ``BatchJob`` adds the per-step log of a short multi-step routine, and onboarding's ``OnboardJob``
its account tallies and credentials sheet.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import List, Optional, TypeVar

from ..core import clock
from ..core.gam.errors import ACCOUNT_WIDE_KINDS

# Invariant #9: a run where thousands genuinely fail keeps the full count but only a sample of them,
# and the live feed only its newest rows, so the final summary (and each 1s poll) stays small.
RECENT_WINDOW = 12
FAILED_SAMPLE_CAP = 200
DETAIL_CAP = 300   # chars kept of a failure's reason and raw error: GAM echoes the argv (a whole body) on a usage error


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
    cancel_requested: bool = False   # for a Stop control (plan U5); no loop reads it yet
    interrupted: bool = False        # cut off (the app quit) before every step was accounted for
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


@dataclass
class BatchJob(Job):
    log: List[str] = field(default_factory=list)      # per-step outcome lines (offboarding's handful of steps)
    skipped: List[str] = field(default_factory=list)  # not run: a step it relies on failed (offboarding's few)


def stop_reason(kind, remediation: str, left: int) -> Optional[str]:
    """Why a bulk loop stops here, or None to carry on. A failure of an ``ACCOUNT_WIDE_KINDS`` kind
    (sign-in expired, GAM not set up, a scope not granted) fails every remaining target the same way,
    so running on only buries the one cause under ``left`` identical failures. Every per-user loop
    (signatures, bulk department, calendar fan-out, bulk onboarding) asks this after each write."""
    if kind not in ACCOUNT_WIDE_KINDS:
        return None
    rest = f" The remaining {left} {'was' if left == 1 else 'were'} not attempted." if left > 0 else ""
    return f"Stopped: {remediation}{rest}"


J = TypeVar("J", bound=Job)


def register_job(jobs: dict, job: J, keep: int = 10) -> J:
    """Register ``job``, pruning the oldest finished jobs first so the registry can't grow forever."""
    finished = [jid for jid, j in jobs.items() if j.finished]
    for jid in finished[:-keep] if len(finished) > keep else []:
        jobs.pop(jid, None)
    jobs[job.id] = job
    return job


def start_job(jobs: dict, total: int, keep: int = 10, window: int = RECENT_WINDOW) -> BatchJob:
    """Register a fresh ``BatchJob`` whose live feed keeps its newest ``window`` rows."""
    return register_job(jobs, BatchJob(total=total, window=window), keep=keep)
