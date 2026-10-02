# 2026-09-24 — Preview and cache expiry stopped counting while the Mac slept

- **Symptom:** a re-verification measured `time.monotonic()` on the build Mac missing ~264 hours of
  system sleep. Every expiry used it: a 15-minute preview left open over a closed lid stayed runnable
  when the lid opened (signatures runs the people it held at preview time), the 5-minute directory
  cache that offboarding's address checks read stayed "fresh", and the 15-minute temp-password sheet
  outlived its window.
- **Cause:** on macOS `time.monotonic()` is `mach_absolute_time()`, which pauses during sleep.
- **Why not caught:** expiry tests monkeypatch the TTL, never the clock's behaviour across sleep.
- **Fix:** `gamgui/core/clock.py` `now()` — `CLOCK_MONOTONIC` on macOS (counts sleep), `CLOCK_BOOTTIME`
  on Linux — used by `web/previews.py`, `core/usercache.py` and the onboarding credentials TTL. Test
  `tests/test_previews.py::test_time_asleep_counts_toward_expiry` (fails on the old store).
- **Prevention:** `clock.py`'s docstring: TTLs use `clock.now()`; `time.monotonic()` only for timeouts
  of work in progress.
