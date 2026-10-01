# Domain: Secrets

**One line:** Stores GAM's three credentials in the macOS Keychain and materializes them into a short-lived `0700`/`0600` `GAMCFGDIR` for exactly one `gam` call, then wipes it.

**Owns invariant(s):** #4 (Keychain-only; `0700` temp `GAMCFGDIR` with `0600` files, wiped via `atexit` + owner-PID marker). Adjacent invariant #5 (inode-based import bound) lives in `gamgui/core/setup.py`, not here.
**Enforcement home:** `tests/test_vault.py` (backend + cache), `tests/test_ephemeral.py` (perms, wipe, sweep, atexit end-to-end). No dedicated drift guard; run under the offline suite.

## Files
- `gamgui/core/secrets/vault.py` — `SecretsVault` over a pluggable `VaultBackend` (`_KeyringBackend` = macOS Keychain, `InMemoryBackend` = tests). Per-domain items `client_secrets`/`oauth2`/`oauth2service`; in-process sliding TTL cache; domain index. `oauth_admin_email(domain)` returns the connected admin's address — the `email` claim GAM keeps beside the token in `oauth2.txt` (`decoded_id_token`, the key the vendored 7.48.11 build writes; `id_token` when that holds the claims) — lowercased, or "" when it can't be read; the one non-secret claim, parsed in memory, never the token, and an undecoded JWT is not decoded. The signature designer uses it to open "Specific user (test)" on the operator (review F18).
- `gamgui/core/secrets/ephemeral.py` — `EphemeralConfig` context manager (the `GAMCFGDIR`), plus module-level `wipe_live_configs`, `sweep_stale_configs`, `app_runtime_dir`, and the `_LIVE` registry.
- `gamgui/core/secrets/__init__.py` — empty.

## How it works
`SecretsVault.get/set/delete` map `(domain, name)` to Keychain service `gamgui:<domain>`, username = credential name; `FILENAMES` maps each name to the file GAM expects. `get` serves from a monotonic-clock cache (`DEFAULT_CACHE_TTL=300s`, sliding, env `GAMGUI_SECRET_CACHE_TTL`, `0` disables) so a burst of calls doesn't re-prompt the Keychain; `clear_cache()` is an explicit "lock". `GAMRunner.run_authenticated` (in `gamgui/core/gam/runner.py`) enters `EphemeralConfig(vault, domain, base_dir=...)`: `__enter__` refuses if `_REQUIRED=("oauth2service","oauth2")` are missing, `mkdtemp(prefix="gamcfg-")`, `chmod 0700`, registers the realpath in `_LIVE`, writes the `.gamgui.pid` marker and each credential via `_write_secret` (opened `O_CREAT|0o600`). `__exit__` calls `_write_back_refreshed_token` (GAM rewrites `oauth2.txt` on refresh; persisted back to the vault only if the SHA-256 changed) then `_wipe` (`_shred_dir` best-effort zeroes the regular files, `rmtree`, discards from `_LIVE`).

## Invariants & the failure history
- **#4 Keychain-only, never persisted elsewhere.** Files exist only for one call's lifetime. Real protection is short lifetime + `0700`/`0600` + private location, *not* crypto shredding (APFS/SSD make secure-erase unreliable — `_shred_dir` overwrites best-effort anyway). `0700` keeps other *users* out, not same-user processes: during a call any of them can read the files. The Keychain item's ACL is the real same-user boundary (SECURITY.md → Known limitations).
- **Two wipe backstops beyond `__exit__`.** The server runs in a daemon thread the interpreter kills mid-call, so `__exit__` can be skipped. (1) `atexit.register(wipe_live_configs)` shreds everything still in `_LIVE`; `gamgui/app.py` `stop()` also calls it explicitly at shutdown. (2) `sweep_stale_configs` removes orphaned `gamcfg-*` dirs from a SIGKILL — run at `gamgui/web/server.py` startup (`AppState.create`) and again by `app.py` `stop()` with `max_age_seconds=0`. History: `8d230ef` added the sweep; `f090d04` closed a "credential leak" (the daemon-thread gap).
- **Bounded PID trust (guards invariant #4 against a subtle leak).** A dir's `.gamgui.pid` protects it only while the PID looks alive **and** age < `_LIVE_PID_TRUST_SECONDS` (24h). PIDs are recycled after reboot, so an orphan's number gets reused by an unsignalable process that `_pid_alive` must read as alive — the ceiling stops such a dir keeping plaintext forever. This ceiling is deliberately independent of `max_age_seconds` so the shutdown sweep (`max_age_seconds=0`) still can't shred a *second live instance's* in-flight dir. `_owner_pid` rejects `<=0` (those mean process-group to `os.kill`).
- **The sweep and the shred never follow a symlink.** `sweep_stale_configs` skips any `gamcfg-*` entry that `is_symlink()`; `_shred_dir` opens the dir `O_DIRECTORY|O_NOFOLLOW` (a link there is left alone) and zeroes each entry through that descriptor with `O_NOFOLLOW|O_NONBLOCK`, regular files only; `rmtree` refuses a symlinked top level and only unlinks links below it; `_owner_pid` reads the marker the same way. History: a `gamcfg-*` symlink in the runtime dir made the shutdown sweep zero every file in its target, and a FIFO `.gamgui.pid` would hang the startup sweep. mkdtemp only ever makes real dirs, so a link there is never ours.
- **The overwrite is bounded** (2026-09-23). `_zero_file` writes zeros in `_ZERO_CHUNK` (64 KiB)
  pieces and stops after `_ZERO_MAX_BYTES` (1 MiB); every file we write there is a few KB, so no
  credential is cut short. It used to allocate the whole size at once (`b"\x00" * st_size`): a huge
  sparse file planted in a `gamcfg-*` dir crashed the startup sweep with `MemoryError` (it catches
  only `OSError`), or on macOS — whose `malloc` grants even a TiB — memset the whole size. Uncapped
  chunks would fill the disk instead, so don't drop the cap.
- **All-or-nothing `__enter__`.** If materialization dies partway (e.g. `KeyboardInterrupt`), `__enter__` wipes and re-raises so the caller never gets a path it can't `__exit__` — otherwise a half-populated dir strands in `_LIVE` for the process lifetime.
- **Registration order:** `_LIVE.add` happens *before* any secret is written, so the `atexit` backstop can never miss a populated dir.
- **A failed exit-time wipe is reported, never raised** (2026-10-01). `wipe_live_configs` writes the
  dir's path and the exception's *type name* to stderr (never its message, which could echo file
  data), and also reports a dir still on disk after `_shred_dir`, which ignores a failed `rmtree`. It
  still never raises, so the real exit isn't masked. It used to be `except Exception: pass`, so
  plaintext left at exit was invisible.
- **Only "no such item" is a no-op on delete** (2026-10-01). keyring's macOS backend raises the same
  `PasswordDeleteError` for every failed `SecItemDelete`, chained (`raise … from`) from the
  Security-API error whose first arg is the OSStatus: `api.NotFound` (-25300), `api.KeychainDenied`
  (-128), `api.SecAuthFailure` (-25293, -67030), `api.Error` (anything else, e.g. -25308 for a locked
  Keychain with no UI). `_KeyringBackend.delete_password` swallows only the -25300 cause; anything
  else, `NoKeyringError` included, propagates, and `clear_domain` keeps the domain in the index when a
  delete fails. It used to catch every `Exception`, so a refused delete read as success with the
  secret still in the Keychain. Nothing in the app removes credentials yet (`clear_domain` has no
  caller outside tests); a route that adds it must show this error, not a success message. A
  non-macOS keyring backend signals absence differently, so there an absent item raises (fails loud).
- **No Touch ID gate: it was tried and removed.** `bc563f4` added `gamgui/core/biometrics.py`;
  `ff60d86` removed it, along with the `pyobjc-framework-LocalAuthentication` dependency and
  `NSFaceIDUsageDescription`. The gate only guarded app launch, it was built to fail open, and
  `GAMGUI_NO_BIOMETRICS=1` turned it off (see the module docstring at
  `git show ff60d86^:gamgui/core/biometrics.py`, which calls it a convenience, "not the boundary").
  It never replaced the Keychain's own per-credential prompts, so it added a step without guarding
  any credential. The Keychain item's ACL is the boundary (see #4 above). What stops the repeated
  prompts is the stable `GamGUI Local` signature, which lets "Always Allow" persist across launches
  and rebuilds (`scripts/build_app.sh` picks it up; README → "Stop the Keychain prompts"). The
  in-process secret cache only covers a burst of calls within one session. Revisit this only if
  biometrics can be bound to the credential read itself, as a user-presence access control on the
  Keychain item. `_KeyringBackend` stores items with `keyring.set_password`, and keyring's macOS
  backend takes no access-control argument, so that would need a Security-framework call of its own.

## Gotchas / mock-lies traps
- `tests/fixtures/mock_gam.sh` fails any call but `version` unless `GAMCFGDIR` is set and holds non-empty `oauth2service.json` + `oauth2.txt`, and with `GAM_MOCK_REFRESH` rewrites `oauth2.txt` — so the materialization + write-back path *is* exercised offline. (Until 2026-09-23 its header claimed the check but never made it.) But the mock cannot prove real GAM writes `oauth2.txt` on refresh with the same filename/format, or that the Keychain backend prompts/permits as expected. The suite never touches the real Keychain (vault tests use `InMemoryBackend`). Only `_KeyringBackend.delete_password` is exercised: over a fake keyring that raises the macOS backend's exact error shape, and, on macOS, over keyring's real backend code with only `SecItemDelete` stubbed (`test_fake_raises_what_the_real_macos_backend_raises` fails if the fake drifts more permissive). Keychain behavior (prompts, `keyring` availability, item ACLs, which status a real denial returns) is otherwise unverified by tests; trust only from real use.
- macOS-only: `os.kill(pid, 0)`, `atexit`, APFS realpath/firmlink semantics assumed. Keep platform specifics here in the shell, not `core/` callers.
- Cache is per-`SecretsVault` instance and in-memory only; not shared across processes. `list_domains` tolerates a corrupt JSON index by returning `[]`.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_vault.py tests/test_ephemeral.py` — fully offline. `test_ephemeral.py` covers perms, wipe-on-exception, missing-cred raise, token write-back, all sweep branches (dead/live/recycled-PID/corrupt-marker/in-use), `wipe_live_configs`, an out-of-process `test_atexit_hook_wipes_dir_on_interpreter_exit` proving credentials don't survive interpreter exit, and the error paths: a file that can't be overwritten is still removed, a vanished dir, one failed wipe not stopping `wipe_live_configs`, an entry vanishing mid-sweep, an unlistable runtime dir; and the symlink/FIFO cases — a symlinked `gamcfg-*` dir, a symlinked file inside a dir, a symlink handed to `_shred_dir`, a symlinked or FIFO pid marker (sentinel files outside the runtime dir stay intact); and a planted 256 MiB sparse file (the overwrite stays under 1 MiB of memory and doesn't fill the disk; the sweep still removes its dir), plus a multi-chunk file zeroed to the byte; a failed wipe reported on stderr by path and exception type, never message (`test_wipe_live_configs_reports_a_failed_wipe_on_stderr`), no stderr at all (`test_wipe_live_configs_survives_having_no_stderr`), and an out-of-process exit whose dir can't be removed (`test_atexit_wipe_reports_a_dir_it_could_not_remove`). `test_vault.py` holds the delete classification: an absent item is a no-op, a denied/locked/auth-failed delete or no keyring backend raises, `clear_domain` skips absent items and keeps the domain listed when a delete fails, and the two macOS-only tests above pin the fake to keyring's real backend. The runner's timeout path wiping the dir is in `test_runner.py`. **Untested against a real tenant:** the Keychain backend itself, GAM's actual `oauth2.txt` refresh contract, and the `decoded_id_token.email` layout `oauth_admin_email` reads (taken from the vendored build's key names, not from a real file; when it doesn't match, the designer falls back to a blank choice, never to someone else).

## To do common tasks here
- **Add a credential type:** extend `FILENAMES` in `vault.py` (and `_REQUIRED` in *both* `vault.py` and `ephemeral.py` if it gates auth); `_check_name`/`CREDENTIAL_NAMES` follow automatically. Update `tests/conftest.py`'s `vault` fixture.
- **Change wipe/sweep policy:** edit `_shred_dir` / `sweep_stale_configs` / `_LIVE_PID_TRUST_SECONDS` in `ephemeral.py`; re-check every sweep test branch and the `max_age_seconds=0` shutdown-sweep invariant. Callers: `gamgui/app.py` `stop()`, `gamgui/web/server.py` `AppState.create`.
- **Change caching:** `SecretsVault.get`/`DEFAULT_CACHE_TTL`/`GAMGUI_SECRET_CACHE_TTL`; keep `test_vault.py`'s counting-backend tests green.
- **Consume credentials for a call:** don't read the vault directly for a `gam` run — go through `GAMRunner` (which owns the `EphemeralConfig` lifecycle).
