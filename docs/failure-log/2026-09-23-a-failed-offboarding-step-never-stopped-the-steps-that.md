# 2026-09-23 — A failed offboarding step never stopped the steps that relied on it

- **Symptom:** reading offboarding before its first live run (plan U2) found `_run_offboard` ran all
  six steps whatever happened ("never abort the routine"). A failed password reset still set the
  "no longer with the company" auto-reply and moved the Drive of an account that could still sign
  in; a failed delegate (a bad manager) still transferred to and reminded that manager; and a failed
  transfer still put the "approve the deletion" reminder on the manager's calendar — while the
  delete screen's pending-transfer warning finds nothing for a transfer that was never created, so
  that deletion would lose the files. Not seen live.
- **Cause:** "report every failure" was implemented as "run every step"; no step declared what it
  relies on.
- **Why not caught:** the executor tests only asserted runs where every step succeeds, or where the
  failing step (the sweep) is one nothing depends on.
- **Fix:** each `OffboardStep` carries `requires` (`lifecycle.REQUIRES`): a failed reset or delegate
  stops the routine, a failed transfer skips only the reminder, a failed auto-reply or sweep is
  reported and the rest runs. A step not run is logged `–` and listed; the final panel says
  "stopped" and, after any failure, not to delete the account yet. The runbook's "When a step fails"
  table gives each rule's reason. This commit.
- **Prevention:** `test_offboard_step_dependencies_are_the_documented_ones` pins the table;
  `test_offboard_failed_reset_stops_the_routine`, `test_offboard_failed_delegate_stops_before_the_rest`,
  `test_offboard_failed_transfer_skips_only_the_reminder`, `test_offboard_stopped_panel_says_what_did_not_run`.
