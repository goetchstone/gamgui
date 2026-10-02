# 2026-09-25 — Setup verify hid which scopes failed and GAM's Authorize link behind a generic error

- **Symptom:** (re-check G2) when a delegated scope was not authorized, Setup's verify showed "GAM
  failed (unknown, exit=1)" and nothing else — no FAIL rows, no Authorize link — though GAM had printed
  both. The operator could not tell which scopes to authorize.
- **Cause:** `gam user <admin> check serviceaccount …` exits `SCOPES_NOT_AUTHORIZED_RC` (1) when a
  scope fails and prints its PASS/FAIL table and the Admin-console link on stdout (`printLine`);
  `GAMRunner.run_authenticated` raised `GAMError` on any non-zero exit with stderr only, and `verify()`
  showed `exc.message` — the stdout was dropped before `_parse_check`/`_extract_auth_url` saw it.
- **Why not caught:** the mock never failed verify's own check (its tenant authorized exactly
  `DWD_SCOPES`); only a bare check failed, and its test asserted the exit code, not what reached the
  operator. The runbook recorded the gap as "Known" rather than a test.
- **Fix:** `GAMError` carries GAM's de-noised stdout (scrubbed, out of `message` and the repr);
  `verify()` reads it as the check's answer when the exit is `SCOPES_NOT_AUTHORIZED_RC` and stdout holds
  PASS/FAIL rows (exit 1 is also GAM's usage/action-failed code), never ok, naming "N of M scopes" with
  the FAIL rows and GAM's link — the direct admin.google.com one preferred over gam-shortn. The strict
  mock's `*partialdwd*` tenant lacks two scopes and prints what the vendored build prints on a FAIL
  (short link, then the admin.google.com link with the checked scopes + userinfo.email and `authuser`),
  on stdout, exit 1.
- **Prevention:** `test_verify_shows_the_failed_scopes_and_gams_link_when_a_scope_fails`,
  `test_verify_with_a_failed_scope_shows_it_and_the_link_and_stays_disconnected` (page + no connector),
  `test_verify_a_non_check_failure_stays_a_plain_error`, and
  `test_mock_check_serviceaccount_fails_a_scope_the_tenant_did_not_authorize` for the mock's shape.
