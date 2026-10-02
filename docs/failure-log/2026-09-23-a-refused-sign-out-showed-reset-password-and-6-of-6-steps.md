# 2026-09-23 — A refused sign-out showed "✓ Reset password" and "6 of 6 steps succeeded"

- **Symptom:** a review (F1, F7) made only `gam user <leaver> signout` fail, as a missing
  `admin.directory.user.security` scope does, and ran the real preview → run: the panel said
  "✓ Reset password" and "Offboarding complete — 6 of 6 steps succeeded", with the delete guidance.
  The leaver's sessions stayed open while the mailbox and Drive were handed over. The only trace was
  an `ok: false` `signout_user` row in Audit. A re-run could not retry it: the operator ticks the ✓
  lines, and the sign-out rode on the ✓ reset.
- **Cause:** `GAMConnector.reset_password` ran the sign-out as a best-effort follow-up and returned
  the reset's `ChangeResult`, discarding the sign-out's. `_run_offboard` marks a step from the
  returned result alone, and the preview listed both commands under one step.
- **Why not caught:** `test_a_failed_follow_up_signout_is_audited_but_the_reset_stands` codified the
  swallow at the connector; nothing tested what the run panel shows when one command of a
  two-command step fails. The runbook called it "best-effort" and never said the panel hides it.
- **Fix:** revoking access is its own step, straight after the reset: `gam user <leaver> deprovision
  signout` (grammar 7899), which also deletes app passwords and OAuth tokens and invalidates backup
  codes (the reset left them working). A failure is a `✗` that counts in `failed`, stops nothing, and
  can be re-run alone; `reset_password` runs only the reset. Any failed step now makes the panel
  "incomplete" instead of "complete", and a failed revoke says the leaver may still be signed in.
- **Prevention:** `test_offboard_a_refused_sign_out_is_a_failed_step_not_a_clean_run` (route level,
  the reviewer's repro) and `test_offboard_failed_sign_out_is_a_failed_step_that_stops_nothing`
  (the mock's new `SIGNOUTFAIL` trigger); every offboarding step now has exactly one command, so a
  step's `✓/✗` is its command's. Whether `deprovision signout` works on a live tenant is unproven.
