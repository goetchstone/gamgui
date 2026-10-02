# 2026-09-23 — The mock `gam` succeeded for any write it didn't recognize

- **Symptom:** a review made `tests/fixtures/mock_gam.sh`'s final catch-all fail and 20 tests went
  red. 23 of the 33 write builders the app calls had no handler, so `delete user`, `remove calendars`,
  `delete events` *without* `doit` and a calendar ACL with an invalid role all exited 0; the all-users
  calendar-ACL sweep had a handler that only ever failed, so its success path never ran; and
  `info user <anyone>` returned Alice. The header also claimed a `GAMCFGDIR` check it never made.
- **Cause:** the mock was written read-first — canned output per read, then `echo ok; exit 0` for
  "anything else is a mutation". Every new write inherited a green test without a handler.
- **Why not caught:** nothing tested the mock itself, and web routes answer 200 whether the write
  worked or not, so tests that checked a status code (or that a job *started*) passed either way.
  Re-checked every write builder against `GamCommands.txt` while writing the strict handlers: all 33
  match the grammar, so no live break was hiding behind this one.
- **Fix:** the catch-all fails (`ERROR: mock: unhandled argv`, exit 2); a strict handler per write
  accepts only its grammar shape and fails a malformed one like GAM's usage error; `info user` is
  keyed on the address (unknown → `Does not exist`); every call but `version` must see the
  materialized `oauth2service.json`/`oauth2.txt`; the sweep succeeds unless `OWNACL`/`SWEEPFAIL`.
- **Prevention:** `tests/test_mock_gam.py` — every `GAMCommands` builder must be classified, each
  emitted shape must pass the mock, and malformed shapes (no `doit`, bad role, missing value,
  unhandled argv) must fail. The nine web tests that passed on a failed write now assert real
  success — `tests/helpers.py` `assert_ok_partial` (no amber error box), the argv the mock received
  (`gam_calls`), the audit `ok`, and route-started jobs awaited with `wait_for_job` — and were
  checked by failing every write in the mock: all went red. Still not proof GAM accepts a write —
  the handlers' stderr/exit codes are GAM7's conventions written by hand; only Phase 8 live
  captures prove them.
