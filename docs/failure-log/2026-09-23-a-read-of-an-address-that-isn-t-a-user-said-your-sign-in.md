# 2026-09-23 — A read of an address that isn't a user said "Your sign-in expired"

- **Symptom:** found while making the mock's per-user reads fail as GAM does (review F20): GAM
  7.48.11 answers `user <x> show vacation|signature`, `print delegates` or `print calendaracls` for
  an address that isn't a user with "User: x, User:, Show Failed: invalid_grant: Invalid email or
  User ID" (exit 50). GamGUI classified it `AUTH_EXPIRED` — "Your sign-in expired. Re-run setup to
  refresh authorization." — for a typo'd or just-deleted user; the admin's sign-in was fine.
- **Cause:** the first `_PATTERNS` entry matched any `invalid_grant`; GAM's per-user token failure
  (`handleOAuthTokenError` → `entityActionFailedWarning` with the token endpoint's words, read
  statically from the vendored build) contains it too.
- **Why not caught:** the mock never failed a read for an unknown user (it answered Alice's data for
  anyone), so no test ever produced GAM's line.
- **Fix:** a `NOT_FOUND` pattern for `invalid_grant: Invalid email or User ID | Not a valid email |
  The account has been deleted`, ahead of the `invalid_grant` one; `invalid_grant: Token has been
  expired or revoked` stays `AUTH_EXPIRED`.
- **Prevention:** `test_an_address_that_is_not_a_user_is_not_found_not_an_expired_sign_in`, and the
  mock now fails its per-user reads this way (next commit). The line's exact wording (the "User:,"
  and "Show Failed" parts) is from GAM's source conventions, not captured from a tenant.
