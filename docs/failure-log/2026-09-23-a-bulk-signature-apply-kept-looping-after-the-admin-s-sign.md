# 2026-09-23 — A bulk signature apply kept looping after the admin's sign-in expired

- **Symptom:** found in review follow-up, reproduced with the mock: a company-wide signature
  apply whose first write fails with `invalid_grant: Token has been expired or revoked` went on to
  try every remaining mailbox, each failing the same way. On a domain-wide run that is thousands
  of identical failures around the one cause, and minutes of calls that could not succeed. The
  bulk department job, the calendar group fan-out and bulk onboarding had the same loop.
- **Cause:** each loop only knew `ok`: `ChangeResult` carried the remediation text but not the
  `GAMErrorKind`, so a loop couldn't tell "this user wasn't found" from "nothing will work now".
- **Why not caught:** the loop tests used per-user failures (a missing address), which rightly
  keep going; nothing failed the connection mid-run.
- **Fix:** `ChangeResult.kind` carries the kind; `gam.errors.ACCOUNT_WIDE_KINDS` (AUTH_EXPIRED,
  NOT_AUTHENTICATED, SCOPE_MISSING) and `web/jobs.py` `stop_reason` stop the signature, bulk
  department, calendar fan-out and bulk-onboarding loops at the first such failure: "Stopped:
  <remediation> The remaining N were not attempted." The signature panel lists the failure that
  stopped it with GAM's error.
- **Prevention:** `tests/test_bulk_stop.py` (each loop × each account-wide kind stops after one
  attempt; a per-target failure does not), `test_gam_connector.py::test_a_failed_write_carries_its_error_kind`,
  `test_users_web.py::test_signatures_apply_stops_when_the_sign_in_has_expired` (through the route,
  fails on the old code). Classification is by GAM's stderr text; which real failures GAM reports
  that way mid-run is not proven live.
