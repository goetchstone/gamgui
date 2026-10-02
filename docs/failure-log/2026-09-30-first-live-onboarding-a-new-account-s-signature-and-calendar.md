# 2026-09-30 — First live onboarding: a new account's signature and calendar were refused

- **Symptom:** the first real onboarding (one hire, account created) first failed "Create Failed:
  Domain user limit reached. Contact Support." under a generic "GAM reported an error"; after a
  license was freed the account, both groups, the checklist and the welcome email went through, but
  the role's signature failed 1 s after the create ("User Set Failed: access_denied: Requested client
  not authorized", exit 50) and a calendar subscribe 7 s after it ("Calendar Service/App not enabled",
  exit 73). The signature failure never showed on the result: the sheet only lists an applied one.
- **Cause:** Google takes minutes to set a new account up before it will issue a token to act as it
  or accept Calendar changes for it; onboarding ran those steps straight after `create user` and
  treated the refusal as final. The license refusal had no pattern, so it was `UNKNOWN`.
- **Why not caught:** the mock's `create user` made an account that was usable at once — more
  permissive than Google (CLAUDE.md "the mock lies"); nothing in it could refuse a license either.
- **Fix:** on an account the run created, those refusals park the step (`core/onboarding.py`
  `not_ready_yet`) and a background `FinishJob` retries it for ~7 minutes, then says what to do by
  hand, with GAM's last error; the single result and the bulk panel show a "Waiting for Google" panel.
  `LICENSE_LIMIT` is a kind with a remediation, and a failed create now reports a recognised kind's
  remediation instead of GAM's raw line; a CSV run skips its later new accounts untried and still runs
  the other rows. A signature that fails for any other reason now shows "not applied" on the sheet.
  An adversarial review of the first cut caught three things, fixed before commit: the license
  refusal stopped rows that create no account, Stop on a bulk run still started the retry job, and
  giving up dropped GAM's error.
- **Prevention:** the mock lags a created account the way this run did (`$GAM_MOCK_STATE/
  new_account_lag`, live wording and exit codes) and refuses `*USERLIMIT*`; tests
  `test_a_new_accounts_signature_and_calendar_wait_for_google_then_apply`,
  `test_an_existing_account_is_never_parked`,
  `test_no_license_left_skips_the_later_accounts_but_not_the_other_rows`,
  `test_a_stopped_bulk_run_lists_what_waits_for_google_instead_of_retrying`,
  `test_stop_ends_the_wait_for_google_at_once`. The retry itself is proven offline only — how long
  Google really takes is unmeasured, and ~7 minutes may be short on a slow day.
