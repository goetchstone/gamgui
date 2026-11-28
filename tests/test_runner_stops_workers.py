"""A cut-off `gam` takes its worker processes with it (failure-log 2026-11-28).

GAM's own parallel loops (`csv`, `batch`, auto-batch) fork workers that each load the credentials. The runner killed only
the parent on a timeout or a cancel, so the workers ran on with their tokens in memory while EphemeralConfig wiped the
directory under them, and could still make calls. The runner now starts gam in a session of its own and kills the group.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import GAMRunner

WORKER_THEN_WAIT = """#!/bin/sh
# A stand-in for gam's parallel loop: a worker that would outlive the parent, then the parent waiting on it.
sleep 30 &
echo $! > "$GAM_MOCK_STATE/worker.pid"
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _fake_gam(tmp_path: Path) -> Path:
    gam = tmp_path / "gam_with_a_worker.sh"
    gam.write_text(WORKER_THEN_WAIT)
    gam.chmod(gam.stat().st_mode | stat.S_IXUSR)
    return gam


def _worker(state: Path) -> int:
    for _ in range(100):
        if (state / "worker.pid").exists() and (state / "worker.pid").read_text().strip():
            return int((state / "worker.pid").read_text())
        time.sleep(0.02)
    raise AssertionError("the stand-in never started its worker")


def _gone(pid: int) -> bool:
    for _ in range(100):
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def state(tmp_path, monkeypatch):
    s = tmp_path / "gam_state"
    s.mkdir()
    monkeypatch.setenv("GAM_MOCK_STATE", str(s))
    return s


async def test_a_timeout_stops_gam_s_workers_too(vault, domain, tmp_path, state):
    runner = GAMRunner(vault=vault, gam_binary=_fake_gam(tmp_path), base_dir=tmp_path, timeout=1)
    started = time.monotonic()
    with pytest.raises(GAMError) as err:
        await runner.run_authenticated(domain, ["csv", "-", "gam", "user", "~primaryEmail", "print", "forward"])
    assert err.value.kind == GAMErrorKind.TIMEOUT
    # It once waited out the worker (it holds gam's pipes), so a "timed out" run went on to the end, credentials and all.
    assert time.monotonic() - started < 10
    worker = _worker(state)
    assert _gone(worker), "gam's worker outlived the cut-off, holding the credentials it loaded"


async def test_a_cancel_stops_gam_s_workers_too(vault, domain, tmp_path, state):
    runner = GAMRunner(vault=vault, gam_binary=_fake_gam(tmp_path), base_dir=tmp_path, timeout=30)
    task = asyncio.create_task(runner.run_authenticated(domain, ["csv", "-", "gam", "user", "~primaryEmail", "print", "forward"]))
    worker = await asyncio.to_thread(_worker, state)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 10
    assert _gone(worker), "gam's worker outlived the cancel (the app quitting), holding the credentials it loaded"
