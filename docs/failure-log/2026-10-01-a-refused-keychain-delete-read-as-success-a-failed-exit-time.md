# 2026-10-01 — A refused Keychain delete read as success; a failed exit-time wipe was silent

- **Symptom:** found in review, not by an operator. `_KeyringBackend.delete_password` treated every
  exception as "item absent", so a denied or locked Keychain, or no keyring backend at all, let
  `SecretsVault.delete` and `clear_domain` return normally with the secret still in the Keychain, and
  `clear_domain` then dropped the domain from the index. Separately, `wipe_live_configs` (the atexit
  backstop) did `except Exception: pass`, and `_shred_dir` ignores a failed `rmtree`, so a credential
  dir left on disk at exit went unreported.
- **Cause:** `except Exception` around `keyring.delete_password`, on the assumption that only absence
  raises. keyring 25's macOS backend raises one `PasswordDeleteError` for every failed
  `SecItemDelete`, chained from `api.NotFound` (-25300), `api.KeychainDenied` (-128),
  `api.SecAuthFailure` (-25293, -67030) or `api.Error` (any other status, e.g. -25308 for a locked
  Keychain with no UI). Both blocks carried `# noqa: S110` from the lint change, which recorded the
  swallow without questioning it.
- **Why not caught:** the suite never exercised `_KeyringBackend` (every test uses `InMemoryBackend`,
  whose delete cannot fail), and the one wipe-failure test asserted only that nothing raised, not that
  anyone was told.
- **Fix:** only a `PasswordDeleteError` whose cause carries errSecItemNotFound (-25300) is a no-op;
  anything else propagates, and `clear_domain` keeps the domain listed. `wipe_live_configs` writes the
  dir's path and the exception's type name (never its message) to stderr, and reports a dir still on
  disk after the shred; it still never raises. Review found the common path had the same gap: the
  per-call `_wipe()` dropped the dir from `_LIVE` before shredding, so a removal that failed there
  was silent and the atexit backstop never saw it. It now reports a dir that survives and keeps it in
  `_LIVE` for the exit-time retry (a symlink in its place is not ours, and is not reported).
  `clear_domain` deletes `oauth2service` first, so a denial part-way never leaves the
  impersonate-anyone key behind with the lesser credentials gone; a non-macOS keyring backend, whose
  failed delete carries no Security status, is checked with a read before it is called a miss. No
  route removes credentials today (`clear_domain` has
  no caller outside tests), so there was no success message to correct; a future caller gets the error.
- **Prevention:** `tests/test_vault.py`'s fake keyring raises the macOS backend's exact shape, and
  `test_fake_raises_what_the_real_macos_backend_raises` (macOS only, which CI's macOS leg runs) drives
  keyring's real `delete_password` with only `SecItemDelete` stubbed and fails if the fake drifts;
  `test_keyring_backend_over_the_real_macos_backend` classifies the real errors.
  `test_atexit_wipe_reports_a_dir_it_could_not_remove` holds the exit-time report end to end;
  `test_a_per_call_wipe_that_leaves_the_dir_reports_it_and_keeps_it_for_atexit` holds the common path.
  Unproven live: the statuses come from keyring's `api.py`, not from a denial captured on a real
  Keychain.
