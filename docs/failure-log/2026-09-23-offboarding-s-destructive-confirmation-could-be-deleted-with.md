# 2026-09-23 — Offboarding's destructive confirmation could be deleted with every test green

- **Symptom:** a review (F21): replacing `guard.enforce(...)` in `/lifecycle/offboard/run` with
  `None` left the suite green (886 passed), and a Run posted with a valid preview token but no
  `confirmed=1` started the offboarding. A mutation pass over all eleven confirmation checks found
  the same for `/users/bulk/apply` (the bulk department job); four more were caught only by a
  per-feature test outside the tripwire.
- **Cause:** `test_write_routes_guarded.py`'s refusal test posts the bare form, which carries no
  preview token. Both routes check the token after the guard, so without the guard the token check
  refused the POST instead — the test passed for the wrong reason.
- **Why not caught:** "a bare POST writes nothing" was taken to mean "the guard works"; nobody
  removed one call site at a time to see which test fails.
- **Fix:** `test_the_confirm_step_without_its_confirmation_runs_no_write` posts the confirm step each
  preview really renders, token included, minus only `CONFIRMATION` (`confirmed`, `confirm`,
  `confirm_count`, `confirm_email`), and requires zero writes, no job and no audit record.
- **Prevention:** that test; with it, removing any of the eleven checks fails the tripwire (scratch
  mutation run, 2026-09-23). A refusal test must hold everything constant except the one thing
  under test.
