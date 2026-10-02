# 2026-09-23 — The domain-wide offboarding sweep ran under the 120s per-call timeout

- **Symptom:** reading offboarding end to end before its first live run (plan C4a) found the
  all-users calendar-ACL sweep (`all users delete calendaracls primary <leaver>`) — one `gam` process
  that makes an API call per user — ran under the runner's 120s default. At ~0.5–1 s per user, any
  domain past a few hundred users would have had the sweep killed partway, every time, reported only
  as "GAM failed (timeout): command timed out". The calendar-index scan (`all users print
  calendars`) had the same bound. Found by reading the code; not seen live.
- **Cause:** `_run_write` and `scan_all_calendars` never passed a `timeout`, so every call got
  `DEFAULT_TIMEOUT`, which was sized for a single-entity call.
- **Why not caught:** the mock answers the sweep instantly, whatever the domain size; nothing
  asserted which timeout a domain-wide call ran under.
- **Fix:** `DOMAIN_WIDE_TIMEOUT` (1 h) in `core/gam/runner.py`, passed explicitly by the sweep (via
  a new `_run_write(timeout=)`) and the scan; the timeout message now says how long it ran and that
  the command may have done part of its work. A timeout stays a plain, untolerated step failure.
  This commit.
- **Prevention:** `tests/test_offboard_safety.py::test_domain_wide_calls_get_the_long_timeout` (every
  `all users` call carries the long timeout, per-user calls keep the default),
  `tests/test_lifecycle.py::test_offboard_sweep_timeout_is_a_clear_step_failure` (the mock's
  `SWEEPSLOW` sleeps; the step fails with "timed out after … stopped", the reminder still runs).
  Mock-only proof: 1 h is an estimate from ~0.5–1 s/user, not a measured sweep.
