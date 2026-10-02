# 2026-09-24 — Offboarding: a cut-off run could read "complete"; a failed delegate skipped the calendar sweep; the connected admin could be offboarded

- **Symptom:** a re-verification of the offboarding fixes found three gaps. (1) A run cancelled
  mid-sweep (the app quitting) rendered "Offboarding complete — 6 of 8 steps succeeded" and told the
  operator the manager had a reminder that was never added. (2) A failed delegate also skipped the
  calendar sweep, leaving the leaver on colleagues' calendars for no reason. (3) Nothing stopped an
  offboarding of the admin GamGUI is connected as, whose "Revoke access" deletes GamGUI's own token
  partway through. Also: the "may still be signed in" note showed when the revoke *failed* but not
  when it never ran, and the sweep's text claimed "everyone's calendars" (it is active users'
  primary calendars only).
- **Cause:** the panel's "clean" test was `not failed and not skipped`, and the executor never marked
  unreached steps; `REQUIRES["calacls"]` included the delegate; `check_addresses` knew nothing of the
  connected admin.
- **Why not caught:** the interruption test only checked the audit record, not what the panel says;
  the dependency table was written for the hand-over steps and copied to the sweep.
- **Fix:** `_run_offboard`'s `finally` logs every unfinished step as "interrupted" / "not run:
  interrupted" and the panel requires `applied == total` for "complete" (else "interrupted");
  `REQUIRES["calacls"] = ("password",)`; `check_addresses(..., connected_admin=)` blocks the connected
  admin (from `oauth2.txt`'s ID-token email); the warning also covers a skipped revoke; the sweep's
  text states its real scope. Tests: `test_an_interrupted_offboard_marks_what_it_never_reached`,
  `test_offboard_interrupted_panel_is_not_complete`, `test_offboard_panel_warns_when_revoke_never_ran`,
  `test_the_connected_admin_cannot_be_offboarded`, `test_offboard_refuses_the_connected_admin`,
  `test_offboard_failed_delegate_stops_the_hand_over_but_not_the_sweep`.
- **Prevention:** a panel's "done" claim must be derived from every step being accounted for, not
  from the absence of failures. Still unproven live, like every offboarding step.
