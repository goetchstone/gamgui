# 2026-09-23 — A hire surnamed "Password" put the temp password in the audit log and the error page

- **Symptom:** a review found that `create_user` failing with GAM's echoed command line on stderr,
  for a hire whose surname is `Password` (or `NotifyPassword`), wrote the one-time password into
  `audit.jsonl` `extra.error` and the onboarding "Couldn't create the account" partial. Reproduced
  offline against a gam that echoes its command line; not seen on the live tenant.
- **Cause:** every secret mask was positional — `errors._scrub_stderr` masks the token after the word
  `password`, `audit.redact_argv` the token after a sensitive key. `lastname Password password <pw>`
  spends the mask on the `password` keyword and leaves `<pw>` bare. (`GAMError.argv` shifts the same
  way, for a `Signature` surname too, but `_run_write` swallows the exception so nothing surfaces
  it.) `create_user`'s `"********"` `audit_argv` covered only the argv, not the error text GAM echoes.
- **Why not caught:** the redaction tests used ordinary names, and the strict mock's usage error
  puts the echoed command on the first stderr line, not the last one that `GAMError.message` keeps.
- **Fix:** redaction by value — `_run_write(..., secrets=[pw])` masks every occurrence in the shown
  argv, the error text / `ChangeResult.detail`, and (via `AuditLog.record(secrets=)`) every audited
  field; `create_user` passes its password. The positional passes stay as the second layer.
- **Prevention:** `test_create_user_failure_redacts_password_by_value` (both surnames, into
  `audit.jsonl`), `test_run_account_failure_error_partial_never_shows_password` (rendered partial),
  `test_positional_redaction_is_shifted_by_a_value_that_spells_a_key` (documents the positional
  limit). A new secret-bearing mutation must pass `secrets=` — the runbooks say so; no drift guard.
