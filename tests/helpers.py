"""Assertions that make a web test prove a write actually worked.

Routes answer 200 whether GAM succeeded or not — a failure renders an amber error partial instead of
the success one — so a status code proves nothing. These helpers check what the operator would see,
what the mock `gam` really received, and what the audit log recorded.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import List

# Every error/warning partial (_action_result.html's failure branch, _error.html, the board and job
# panels) carries this class pair; no success partial does.
_AMBER = "border-amber-300 bg-amber-50"


def assert_ok_partial(r) -> None:
    """The response rendered a success partial, not the amber error box."""
    assert r.status_code == 200, r.status_code
    if _AMBER in r.text:
        tail = r.text[r.text.index(_AMBER):]
        message = re.sub(r"<[^>]+>", " ", tail.split(">", 1)[-1])
        raise AssertionError("error partial rendered: " + " ".join(message.split())[:300])


def read_gam_calls(log: Path) -> List[List[str]]:
    """Every argv the mock `gam` received (GAM_MOCK_ARGV_LOG), oldest first, minus `version` probes."""
    if not log.exists():
        return []
    parts = log.read_bytes().split(b"\0")
    calls, i = [], 0
    while i < len(parts) - 1:
        n = int(parts[i])
        calls.append([p.decode("utf-8") for p in parts[i + 1:i + 1 + n]])
        i += 1 + n
    return [c for c in calls if c != ["version"]]


_READ_VERBS = {"print", "show", "info", "report", "check"}


def gam_writes(calls: List[List[str]]) -> List[List[str]]:
    """Just the mutating calls: a read names its verb in the first three tokens (`print users`,
    `user x show vacation`, `calendars x print events`, `all users print calendars`)."""
    return [c for c in calls if not _READ_VERBS & set(c[:3])]


def wait_for_job(client, job, timeout: float = 10.0) -> None:
    """Await a route-started background job on the TestClient's own event loop.

    Awaiting the task (rather than polling the status endpoint) is deterministic, and a finished job
    leaves no subprocess in flight at teardown — the condition behind the old CI hangs. The fixture
    must be a context-managed TestClient, whose portal (and loop) outlives each request.
    """
    async def _wait() -> None:
        await asyncio.wait_for(asyncio.shield(job.task), timeout)

    client.portal.call(_wait)
