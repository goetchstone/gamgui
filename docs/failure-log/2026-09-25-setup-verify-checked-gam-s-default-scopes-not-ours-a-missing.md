# 2026-09-25 — Setup verify checked GAM's default scopes, not ours; a missing scope always blamed delegation

- **Symptom:** (review follow-up to U10a) `verify()` ran a bare `gam user <admin> check serviceaccount`,
  which checks GAM's own, larger default service-account scope set — an operator who authorized exactly
  the scopes the setup step pre-fills (`DWD_SCOPES`) could be told delegation isn't authorized. Every
  `SCOPE_MISSING` error said "Re-do the Domain-Wide Delegation step", which cannot grant an admin-token
  scope such as `admin.directory.user.security` (offboarding's sign-out).
- **Cause:** the builder took no scopes, though the grammar has `check serviceaccount (scope|scopes
  <APIScopeURLList>)*`; the remediation text predates the split between client-access (admin token,
  `gam oauth create`) and service-account (delegation) scopes.
- **Why not caught:** the mock lied — its `check serviceaccount` answered "All scopes PASS" to any
  shape and listed `admin.directory.user`/`admin.directory.group` as delegation PASS lines, scopes GAM
  refuses to check there (they are client-access scopes), in a `label: PASS` form GAM doesn't print.
- **Fix:** `GAMCommands.check_svcacct(admin, scopes)` sends `scopes <comma list>`; `verify()` passes
  `DWD_SCOPES`. The mock prints what the vendored build's `checkServiceAccount` prints for the scopes
  asked (sorted, `{scope:73} PASS (j/n)`), refuses a non-service-account scope as an invalid choice, and
  FAILs a bare check on GAM's extra defaults with exit 1. The `SCOPE_MISSING` remediation names both
  places (`gam oauth create` for admin scopes, the delegation step for per-user ones) and any scope URL
  GAM's stderr names.
- **Prevention:** `test_verify_checks_exactly_the_prefilled_scopes` pins the argv and a pass against a
  tenant that authorized only `DWD_SCOPES`; the mock-shape tests in `tests/test_mock_gam.py` and the
  grammar sweep keep the mock to GAM's refusal of client-access scopes.
