# 2026-09-23 — Offboarding said "end sessions (locks sign-in)" but left forwarding and tokens working

- **Symptom:** a review (F6) printed every command the routine runs: a password reset and a
  sign-out, nothing else that cuts access. The step text said "end sessions (locks sign-in; mailbox
  stays live)". A leaver who forwarded mail to a personal address kept receiving company mail until
  the account was deleted, 30+ days later (the mailbox stays live on purpose, for the delegate), and
  a connected app with a Drive or Calendar grant kept working. Nothing in the preview or the
  first-live-run checklist mentioned either.
- **Cause:** the routine was built around the hand-over (delegate, transfer, reminder); "locks
  sign-in" was written from the password reset alone, and forwarding is a mailbox setting that a
  reset and a sign-out don't touch.
- **Why not caught:** the tests pin each step's argv and order, not what the routine leaves open;
  the runbook's checklist checked sign-in, delegate, auto-reply, transfer, sweep and reminder only.
- **Fix:** the revoke step (previous entry) deletes app passwords, backup codes and OAuth tokens and
  signs out; a new "Turn off forwarding" step runs `gam user <leaver> forward off` (grammar 7998)
  straight after it, whether or not forwarding was on (the leaver could switch it on until the
  sign-out). A failure is a `✗` that stops nothing. The reset's text now says what it does ("the old
  password stops working"); the runbook names what is still open — Gmail filters that forward, and
  sign-in through a third-party identity provider — and the checklist has the operator look.
- **Prevention:** `test_offboard_turns_off_forwarding_and_a_failure_stops_nothing` (with the mock's
  new `FWDFAIL`, and `forward` now failing for a missing user as GAM does). Filters and SSO are a
  checklist item, not a test. `forward off` on a live mailbox is unproven.
