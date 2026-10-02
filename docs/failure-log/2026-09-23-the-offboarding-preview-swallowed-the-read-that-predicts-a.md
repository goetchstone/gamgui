# 2026-09-23 — The offboarding preview swallowed the read that predicts a failed delegate

- **Symptom:** a review (F9) made the preview's `gam user <leaver> print delegates` fail as for a
  mailbox without Gmail ("failedPrecondition - Mail service not enabled"): the preview showed no
  warning and offered Run. Run would reset the password (irreversible), then fail the delegate with
  the same error and stop — the leaver locked out, nothing handed over.
- **Cause:** `_already_delegate` answered only "is the manager already a delegate?" and returned
  `False` on any exception, so a failed read looked like "not a delegate yet".
- **Why not caught:** the only test of that read covered the already-a-delegate answer; nothing
  made the read fail.
- **Fix:** `_delegate_warning` returns the preview's warning: the existing one, or — when the read
  fails — "Couldn't read <leaver>'s mail delegates (<GAM's error>) … will likely fail too — after
  the password has been reset". A warning, not a block: the read can fail transiently.
- **Prevention:** `test_offboard_preview_warns_when_the_leavers_delegates_cannot_be_read`. The first
  live checklist says to fix the cause before Run.
