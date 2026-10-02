# 2026-09-23 — The offboarding transfer left the Drive privacy level to the API's default

- **Symptom:** a review (F10): the transfer ran `create datatransfer <leaver> drive,calendar
  <manager>` with no `private|shared|all`. GAM then sends no `PRIVACY_LEVEL`, so whether the files
  the leaver had shared move was up to the Data Transfer API's undocumented default; anything left
  behind is lost at delete, and the delete gate passes on a `completed` transfer. The runbook's
  "check whether shared files moved too" needed a list of those files nobody had.
- **Cause:** the builder had no way to name a privacy level; the runbook recorded the gap as a
  gotcha instead of closing it.
- **Why not caught:** nothing asserted the privacy level; the mock accepted any keyword after the
  new owner (even `all` on a Calendar-only transfer, which GAM refuses).
- **Fix:** `create_datatransfer(…, privacy=)` (validated against `private|shared|all`, grammar
  3599); offboarding sends `all`. The mock refuses a privacy level without Drive, as GAM's
  `_assignAppParameter` does. The runbook's check is now a count of what the leaver still owns
  (`show filecounts`, expect 0).
- **Prevention:** `test_offboard_transfer_step_invokes_combined_service_list` (audited argv ends in
  `all`), `test_lifecycle_commands`, the mock-rejects case in `test_mock_gam.py`. The API's behaviour
  with `all` is not verified live — the first offboarding's file count is the check.
