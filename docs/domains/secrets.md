# Domain: Secrets

**One line:** Stores GAM's three credentials in the macOS Keychain and materializes them into a short-lived `0700`/`0600` `GAMCFGDIR` for exactly one `gam` call, then wipes it.

**Owns invariant(s):** #4 (Keychain-only; `0700` temp `GAMCFGDIR` with `0600` files, wiped via `atexit` + owner-PID marker). Adjacent invariant #5 (inode-based import bound) lives in `gamgui/core/setup.py`, not here.
**Enforcement home:** `tests/test_vault.py` (backend + cache), `tests/test_ephemeral.py` (perms, wipe, sweep, atexit end-to-end). No dedicated drift guard; run under the offline suite.

## Files
- `gamgui/core/secrets/vault.py` — `SecretsVault` over a pluggable `VaultBackend` (`_KeyringBackend` = macOS Keychain, `InMemoryBackend` = tests). Per-domain items `client_secrets`/`oauth2`/`oauth2service`; in-process sliding TTL cache; domain index.
- `gamgui/core/secrets/ephemeral.py` — `EphemeralConfig` context manager (the `GAMCFGDIR`), plus module-level `wipe_live_configs`, `sweep_stale_configs`, `app_runtime_dir`, and the `_LIVE` registry.
- `gamgui/core/secrets/__init__.py` — empty.

## How it works
`SecretsVault.get/set/delete` map `(domain, name)` to Keychain service `gamgui:<domain>`, username = credential name; `FILENAMES` maps each name to the file GAM expects. `get` serves from a monotonic-clock cache (`DEFAULT_CACHE_TTL=300s`, sliding, env `GAMGUI_SECRET_CACHE_TTL`, `0` disables) so a burst of calls doesn't re-prompt the Keychain; `clear_cache()` is an explicit "lock". `GAMRunner.run_authenticated` (in `gamgui/core/gam/runner.py`) enters `EphemeralConfig(vault, domain, base_dir=...)`: `__enter__` refuses if `_REQUIRED=("oauth2service","oauth2")` are missing, `mkdtemp(prefix="gamcfg-")`, `chmod 0700`, registers the realpath in `_LIVE`, writes the `.gamgui.pid` marker and each credential via `_write_secret` (opened `O_CREAT|0o600`). `__exit__` calls `_write_back_refreshed_token` (GAM rewrites `oauth2.txt` on refresh; persisted back to the vault only if the SHA-256 changed) then `_wipe` (`_shred_dir` best-effort zeroes files, `rmtree`, discards from `_LIVE`).

## Invariants & the failure history
- **#4 Keychain-only, never persisted elsewhere.** Files exist only for one call's lifetime. Real protection is short lifetime + `0700`/`0600` + private location, *not* crypto shredding (APFS/SSD make secure-erase unreliable — `_shred_dir` overwrites best-effort anyway).
- **Two wipe backstops beyond `__exit__`.** The server runs in a daemon thread the interpreter kills mid-call, so `__exit__` can be skipped. (1) `atexit.register(wipe_live_configs)` shreds everything still in `_LIVE`; `gamgui/app.py` `stop()` also calls it explicitly at shutdown. (2) `sweep_stale_configs` removes orphaned `gamcfg-*` dirs from a SIGKILL — run at `gamgui/web/server.py` startup (`AppState.create`) and again by `app.py` `stop()` with `max_age_seconds=0`. History: `8d230ef` added the sweep; `f090d04` closed a "credential leak" (the daemon-thread gap).
- **Bounded PID trust (guards invariant #4 against a subtle leak).** A dir's `.gamgui.pid` protects it only while the PID looks alive **and** age < `_LIVE_PID_TRUST_SECONDS` (24h). PIDs are recycled after reboot, so an orphan's number gets reused by an unsignalable process that `_pid_alive` must read as alive — the ceiling stops such a dir keeping plaintext forever. This ceiling is deliberately independent of `max_age_seconds` so the shutdown sweep (`max_age_seconds=0`) still can't shred a *second live instance's* in-flight dir. `_owner_pid` rejects `<=0` (those mean process-group to `os.kill`).
- **All-or-nothing `__enter__`.** If materialization dies partway (e.g. `KeyboardInterrupt`), `__enter__` wipes and re-raises so the caller never gets a path it can't `__exit__` — otherwise a half-populated dir strands in `_LIVE` for the process lifetime.
- **Registration order:** `_LIVE.add` happens *before* any secret is written, so the `atexit` backstop can never miss a populated dir.

## Gotchas / mock-lies traps
- `tests/fixtures/mock_gam.sh` fails any call but `version` unless `GAMCFGDIR` is set and holds non-empty `oauth2service.json` + `oauth2.txt`, and with `GAM_MOCK_REFRESH` rewrites `oauth2.txt` — so the materialization + write-back path *is* exercised offline. (Until 2026-09-23 its header claimed the check but never made it.) But the mock cannot prove real GAM writes `oauth2.txt` on refresh with the same filename/format, or that the Keychain backend prompts/permits as expected. `_KeyringBackend` is **never** touched by the suite (all tests use `InMemoryBackend`); Keychain behavior (prompts, `keyring` availability, item ACLs) is unverified by tests — trust only from real-tenant use.
- macOS-only: `os.kill(pid, 0)`, `atexit`, APFS realpath/firmlink semantics assumed. Keep platform specifics here in the shell, not `core/` callers.
- Cache is per-`SecretsVault` instance and in-memory only; not shared across processes. `list_domains` tolerates a corrupt JSON index by returning `[]`.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_vault.py tests/test_ephemeral.py` — fully offline. `test_ephemeral.py` covers perms, wipe-on-exception, missing-cred raise, token write-back, all sweep branches (dead/live/recycled-PID/corrupt-marker/in-use), `wipe_live_configs`, and an out-of-process `test_atexit_hook_wipes_dir_on_interpreter_exit` proving credentials don't survive interpreter exit. **Untested against a real tenant:** the Keychain backend itself and GAM's actual `oauth2.txt` refresh contract.

## To do common tasks here
- **Add a credential type:** extend `FILENAMES` in `vault.py` (and `_REQUIRED` in *both* `vault.py` and `ephemeral.py` if it gates auth); `_check_name`/`CREDENTIAL_NAMES` follow automatically. Update `tests/conftest.py`'s `vault` fixture.
- **Change wipe/sweep policy:** edit `_shred_dir` / `sweep_stale_configs` / `_LIVE_PID_TRUST_SECONDS` in `ephemeral.py`; re-check every sweep test branch and the `max_age_seconds=0` shutdown-sweep invariant. Callers: `gamgui/app.py` `stop()`, `gamgui/web/server.py` `AppState.create`.
- **Change caching:** `SecretsVault.get`/`DEFAULT_CACHE_TTL`/`GAMGUI_SECRET_CACHE_TTL`; keep `test_vault.py`'s counting-backend tests green.
- **Consume credentials for a call:** don't read the vault directly for a `gam` run — go through `GAMRunner` (which owns the `EphemeralConfig` lifecycle).
