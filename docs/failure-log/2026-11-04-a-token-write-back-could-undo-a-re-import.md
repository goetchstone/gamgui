# 2026-11-04 — A token write-back could undo a re-import

- **Symptom:** found by SwiftGamGui (its `directory-home.md`, "Fixed in PR #8's review"), never seen live here. Reproduced on
  `main` in `tests/test_ephemeral.py`: a token re-imported or removed during a call was replaced by that call's refreshed
  one, so a re-import could end with the old admin's token beside the new key, and a removed token came back.
- **Cause:** `_write_back_refreshed_token` compared GAM's file with what it materialized, never with what the vault holds
  now, then set the vault. Reads run unserialized, so a slow one can span an import.
- **Why not caught:** the test only refreshed with nothing else happening.
- **Fix:** branch `fix/token-write-back-race`. `SecretsVault.replace_if_current` reads the store, not the cache, and sets
  under the lock every write now takes; the write-back goes through it. When two calls refresh, the second leaves the
  first's, and either token is good. Secrets stay in the Keychain: nothing new is persisted.
- **Prevention:** `test_a_refresh_does_not_undo_a_re_import`, `test_a_refresh_does_not_bring_back_a_removed_token`,
  `test_the_write_back_compares_the_store_not_a_stale_cache`, `test_replace_if_current_is_one_step_under_writes`.
  Offline only, against the in-memory Keychain. The lock covers one process. A second one writing the Keychain between
  the look and the set isn't covered, and nothing rules one out: there is no single-instance guard, the orphan sweep in
  `ephemeral.py` allows for a second instance, and `scripts/acceptance.py` builds its own vault.
