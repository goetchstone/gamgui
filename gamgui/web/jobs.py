"""Tiny in-memory batch-job helper for polled-progress bulk operations.

Stored on ``AppState.jobs`` (id -> BatchJob) and rendered by an HTMX-polled partial, so a long
per-user loop reports progress instead of looking frozen. (The signature designer has its own
equivalent; this is the shared version used by newer bulk actions.)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BatchJob:
    id: str
    total: int
    done: int = 0
    applied: int = 0
    failed: List[str] = field(default_factory=list)
    current: str = ""
    finished: bool = False
    error: Optional[str] = None
    log: List[str] = field(default_factory=list)  # per-step outcome lines (multi-step routines)
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run


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
