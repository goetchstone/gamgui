# 2026-09-23 — The calendar sweep failed on a user without Calendar and tolerated any 403

- **Symptom:** a review (F8) fed the sweep GAM's per-user "User: dave@example.com, Calendar
  Service/App not enabled (4/120)" line (read from the vendored build's `userServiceNotEnabledWarning`)
  next to the benign ones: "✗ Remove from everyone's calendars — GAM failed (unknown…)", on every
  run, in any tenant with one active user whose Calendar is off (`all users` = every active user).
  The reverse too: every `PERMISSION_DENIED` line was tolerated, so a real 403 removing the leaver
  from some colleague's calendar counted as a clean sweep.
- **Cause:** the "Service/App not enabled" line matched no pattern (UNKNOWN); and the leaver's
  own-ACL refusal was mapped to the generic `PERMISSION_DENIED`, so tolerating it tolerated every 403.
- **Why not caught:** the mock's multi-user sweep stderr held only not-shared and own-ACL lines, and
  the tolerance tests passed a kind, not a real line.
- **Fix:** two kinds of their own — `SERVICE_NOT_ENABLED` (regex `Service/App not enabled`, so GAM's
  account-wide "Calendar not enabled. Please run …" stays UNKNOWN) and `OWN_ACL` — and the sweep
  tolerates exactly `SWEEP_TOLERATED` = not-found, no-Calendar, own-ACL.
- **Prevention:** `test_remove_from_all_calendars_tolerates_benign` (real lines),
  `test_remove_from_all_calendars_does_not_tolerate_a_permission_refusal`,
  `test_a_user_without_the_service_is_its_own_kind`; the mock's `SWEEPBENIGN` prints the
  no-Calendar line. GAM's line is read from bytecode, not captured live.
