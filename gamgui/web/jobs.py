"""Tiny in-memory batch-job helper for polled-progress bulk operations.

Stored on ``AppState.jobs`` (id -> BatchJob) and rendered by an HTMX-polled partial, so a long
per-user loop reports progress instead of looking frozen. (The signature designer has its own
equivalent; this is the shared version used by newer bulk actions.)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import List, Optional

from ..core.gam.errors import ACCOUNT_WIDE_KINDS

# Invariant #9: a run where thousands genuinely fail keeps the full count but only a sample of names,
# so the final summary (and each poll) stays small. Same cap as signatures' ApplyJob / OnboardJob.
FAILED_SAMPLE_CAP = 200


@dataclass
class BatchJob:
    id: str
    total: int
    done: int = 0
    applied: int = 0
    failed_total: int = 0
    failed: List[str] = field(default_factory=list)  # capped sample — record with fail(), never .append
    skipped: List[str] = field(default_factory=list)  # not run: a step it relies on failed (offboarding's few)
    current: str = ""
    finished: bool = False
    error: Optional[str] = None
    log: List[str] = field(default_factory=list)  # per-step outcome lines (multi-step routines)
    interrupted: bool = False  # cut off (the app quit) before every step was accounted for
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run

    def fail(self, item: str) -> None:
        self.failed_total += 1
        if len(self.failed) < FAILED_SAMPLE_CAP:
            self.failed.append(item)


def stop_reason(kind, remediation: str, left: int) -> Optional[str]:
    """Why a bulk loop stops here, or None to carry on. A failure of an ``ACCOUNT_WIDE_KINDS`` kind
    (sign-in expired, GAM not set up, a scope not granted) fails every remaining target the same way,
    so running on only buries the one cause under ``left`` identical failures. Every per-user loop
    (signatures, bulk department, calendar fan-out, bulk onboarding) asks this after each write."""
    if kind not in ACCOUNT_WIDE_KINDS:
        return None
    rest = f" The remaining {left} {'was' if left == 1 else 'were'} not attempted." if left > 0 else ""
    return f"Stopped: {remediation}{rest}"


def register_job(jobs: dict, job, keep: int = 10):
    """Register ``job`` (anything with ``.id`` and ``.finished``), pruning the oldest finished jobs
    first so the registry can't grow forever. Shared by ``start_job`` and feature-specific job
    types (e.g. onboarding's ``OnboardJob``)."""
    finished = [jid for jid, j in jobs.items() if getattr(j, "finished", False)]
    for jid in finished[:-keep] if len(finished) > keep else []:
        jobs.pop(jid, None)
    jobs[job.id] = job
    return job


def start_job(jobs: dict, total: int, keep: int = 10) -> BatchJob:
    """Register a fresh job, pruning the oldest finished ones so the registry can't grow forever."""
    return register_job(jobs, BatchJob(id=secrets.token_urlsafe(8), total=total), keep=keep)
