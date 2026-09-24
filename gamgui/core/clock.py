"""A monotonic clock that keeps counting while the Mac sleeps.

``time.monotonic()`` is ``mach_absolute_time()`` on macOS, which stops during system sleep: a
15-minute preview left open over a closed lid stayed runnable hours later, and a 5-minute directory
cache stayed "fresh" (failure-log 2026-09-24). ``CLOCK_MONOTONIC`` counts sleep on macOS; Linux's
equivalent is ``CLOCK_BOOTTIME`` (its ``CLOCK_MONOTONIC`` doesn't). Use this for any expiry measured
against the wall of human time — TTLs, "expires after" — and ``time.monotonic()`` only for timeouts
of work in progress.
"""

from __future__ import annotations

import time

_CLOCK = getattr(time, "CLOCK_BOOTTIME", None)
if _CLOCK is None:
    _CLOCK = getattr(time, "CLOCK_MONOTONIC", None)


def now() -> float:
    """Seconds on a clock that never goes backwards and includes time spent asleep."""
    return time.clock_gettime(_CLOCK) if _CLOCK is not None else time.monotonic()
