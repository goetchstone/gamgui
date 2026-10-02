# 2026-09-23 — Quitting mid-write left gam running and the write unaudited

- **Symptom:** a review (F3) proved it with the mock: cancel a `_run_write` of the offboarding
  calendar sweep (as quitting does: `server._lifespan` cancels in-flight jobs) and the audit log
  holds nothing for it, not even a failure, while the `gam` child is still alive after its
  `GAMCFGDIR` was wiped. The sweep runs up to an hour and deletes ACLs across the domain, so
  "quit mid-run" leaves partial changes with no record.
- **Cause:** `asyncio.CancelledError` is a `BaseException`. `runner._exec` killed `gam` only on
  `asyncio.TimeoutError`, and `_run_write` audited only in `except Exception`, so a cancellation
  skipped both.
- **Why not caught:** only the timeout path was tested (`MOCKSLEEP`); nothing cancelled a call, and
  the lifespan's cancel had no test at all.
- **Fix:** `_exec` kills and reaps `gam` on any other exception (`_stop`: SIGKILL first, then wait)
  and re-raises; `_run_write` records `ok: false` with `INTERRUPTED` ("interrupted … may have done
  part of its work") and re-raises.
- **Prevention:** `test_runner.py::test_cancel_kills_gam_wipes_the_config_and_frees_the_write_lock`
  (process gone, dir wiped, lock free) and
  `test_lifecycle.py::test_quitting_mid_offboard_stops_the_sweep_and_audits_it` (a real `TestClient`
  shutdown mid-sweep: the sweep audited interrupted, the steps before it `ok`). Both fail on the old
  code. Mock-only: how real GAM reacts to SIGKILL partway through `all users` is unobserved.
