# 2026-09-23 — One benign "Does not exist" line made the offboarding calendar sweep's partial failure a success

- **Symptom:** a review found that the all-users calendar-ACL sweep, which tolerates `NOT_FOUND` and
  `PERMISSION_DENIED`, would report "Completed (best-effort)" for a run where one user's delete
  really failed, provided some other user's line said "Does not exist". Reproduced offline with the
  mock's new `SWEEPMIXED` trigger (a not-applicable user, a real "Internal error encountered" and the
  leaver's own-ACL refusal, exit 50) — the step came back `ok`, audited `tolerated: true`. Not seen live.
- **Cause:** `classify_stderr` searched the *whole* stderr for the first pattern in `_PATTERNS` order,
  and `_run_write` tolerated on that single `kind`. A multi-entity command prints one line per entity,
  so the first tolerable match hid every other line; an unrecognized line never counted at all.
- **Why not caught:** the mock's sweep failures were single-line (`OWNACL`, `SWEEPFAIL`), and every
  classifier test was one line — nothing fed it the multi-line, multi-entity stderr a real sweep emits.
- **Fix:** classify per line (unmatched → `UNKNOWN`; GAM's `Getting all`/`Got N` progress chatter
  skipped); `GAMError.kinds` carries every line's kind and `kind` the most severe (`_SEVERITY`);
  `_run_write` tolerates only when `kinds ⊆ tolerate_kinds`; `.message` shows the last line of the
  reported kind rather than the stderr's (possibly benign) tail. This commit.
- **Prevention:** `tests/test_errors.py` (mixed-stderr cases) and `tests/test_lifecycle.py`
  `test_offboard_calendar_sweep_multi_user_stderr` via the mock's `SWEEPBENIGN`/`SWEEPMIXED`, plus
  `test_offboard_safety.py` `test_remove_from_all_calendars_needs_every_line_tolerable`. The
  multi-line stderr is hand-written in GAM's shape, not a live capture: whether a real sweep prints
  another kind of stderr line (it would now fail the step, visibly) is unproven until the first real
  offboarding.
