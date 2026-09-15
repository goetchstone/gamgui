# Domain: Setup / Credentials Import

**One line:** Turns GAM's terminal-only authorization into a guided wizard — inspect a config dir, import `oauth2service.json` / `oauth2.txt` / `client_secrets.json` into the Keychain (with a fd-pinned, race-safe read), hand out the fresh-setup commands, and verify Domain-Wide Delegation.

**Owns invariant(s):** #5 (inode-based `(st_dev, st_ino)` import bound; directory pinned to a descriptor; each credential file opened `O_NOFOLLOW`). Also exercises #4 (secrets land only in the Keychain; staging plaintext is wiped).
**Enforcement home:** `tests/test_setup.py` (24 tests) + `tests/test_setup_web.py` (bounds/firmlink/NFD cases via the FastAPI route). No drift guard or CI hook beyond the offline pytest suite.

## Files
- `gamgui/core/setup.py` — the whole domain: `SetupService` + the module-level fd/identity helpers.
- `gamgui/web/routes/setup.py` — the screen: `GET /setup`, `POST /setup/import|fresh|verify`; calls `candidate_dirs`, `import_dir`, `setup_commands`, `dwd_details`, `verify`.
- `gamgui/core/secrets/vault.py` — `FILENAMES` (logical name → GAM filename) and `_REQUIRED = ("oauth2service", "oauth2")`; `SecretsVault.set/get`.
- `gamgui/core/secrets/ephemeral.py` — `app_runtime_dir()`; `managed_setup_dir()` is its sibling `.../setup` (`0700`).
- `tests/test_setup.py`, `tests/test_setup_web.py` — see below.

## How it works
`SetupService.import_dir(path, domain)` is the spine. It calls `resolve_dir` (expand `~`, `resolve()`, then bounds-check the **resolved** path against `_allowed_root_ids()`, then require exists+is_dir), pins the directory with `_pin_bounded_dir` (open `O_DIRECTORY`, then `_fd_within_roots` walks up via `openat(fd, "..")` proving it sits inside an allowed root), reads each of the three `FILENAMES` with `_read_credential` (single component, `O_NOFOLLOW`, `fstat` regular-file check, read from the *same* fd), `vault.set`s the bytes, and — only if the dir `_is_managed` (our staging dir, matched by inode) — wipes each imported plaintext file with `_wipe_file`. Bounds are computed by `allowed_roots()` = home (`_home_root`) plus a sane `$GAMCFGDIR` (`_root_is_sane`). `verify()` runs `GAMCommands.check_svcacct(admin)` and parses PASS/FAIL with `_parse_check`, surfacing a DWD auth URL via `_extract_auth_url` on failure.

## Invariants & the failure history
- **Bound by identity, not strings (#5).** `_within_roots` / `_reaches` / `_spellings` compare `(st_dev, st_ino)`, because `resolve()` doesn't fix case, NFC≠NFD bytes name the same dir, and `/Users` is an APFS *firmlink* — so `/System/Volumes/Data/...` is the same dir by a longer ancestor chain. `$GAMCFGDIR=/System` once put the real `/etc` back in reach via that firmlink chain; `_spellings` enumerates the volume spellings to catch it.
- **Pin first, prove second (TOCTOU).** A name-check-then-name-open race let a mid-request rename of the dir into a symlink read out-of-bounds bytes into the Keychain. `_pin_bounded_dir` opens the fd, *then* proves bounds from the fd; after the pin there is no name left to swap.
- **`O_NOFOLLOW` per file.** A credential file that is itself a symlink is not importable at all (even in-bounds) — closes the re-pointable indirection.
- **`_root_is_sane` on `$GAMCFGDIR` only.** Must be an existing *directory* (a file-as-root matched its own inode and a credential symlink imported its bytes) and must not be an ancestor of home or of `_SENSITIVE_DIRS` (`/etc /var /usr /System /Library /dev`). Home is trusted as-is on purpose: applying the rules to a launchd home of `/` emptied the root list and refused the app's own staging dir. Blank/unset `$GAMCFGDIR` must never become `/`.
- **Wipe only what you imported (#4).** `_wipe_file` re-`fstat`s the fd against the exact inode read, inside the pinned dir; swapping the staging dir for a symlink once made GamGUI zero/unlink another dir's files. `_is_managed` compares by inode (from `dir_fd` when pinned), not string — a case-variant spelling once skipped the wipe and stranded plaintext.
- **Fail-closed helpers.** `_fs_id` returns `None` on any stat failure; every helper treats `None`/errors as "no match" so a race or permission error can only *narrow* what's allowed.

## Gotchas / mock-lies traps
- The mock cannot touch this domain's core risk: it is all real-filesystem behavior (inodes, firmlinks, `O_NOFOLLOW`, TOCTOU), not GAM output. Tests use real `tmp_path` dirs and monkeypatched `HOME`, not `mock_gam.sh`.
- Case-sensitivity and `/private/etc/passwd` presence differ per machine — several tests `pytest.skip` on a case-sensitive volume or a missing `/etc/passwd`; a green run may have skipped the interesting case.
- `verify()` runs a **real** `gam check serviceaccount`; against the mock it only proves parsing (`_parse_check`, `_extract_auth_url`), not that DWD is actually authorized. The `.app` launched from Finder has no `LSEnvironment`, so `$GAMCFGDIR` isn't seen unless launched from the exporting shell — `resolve_dir`'s error message says so; keep that caveat.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_setup.py tests/test_setup_web.py` — fully offline (in-memory `InMemoryBackend` vault, no real GAM). Covers: fd-pin swap-after-check and swap-after-pin (`_after_the_bounds_check` / `_after_the_pin`), symlinked-credential rejection, wipe-never-destroys / wipe-only-imported-inodes, NFD + data-volume + case-variant home spellings, and overbroad-`$GAMCFGDIR` rejection for `/ /private /etc /System/Volumes/Data` etc. **Not proven live:** an import from a real admin's `~/.gam` and a real `check serviceaccount` against a tenant — the import bytes and the DWD verify path have only ever run against fixtures.

## To do common tasks here
- **Add/rename a credential file:** edit `FILENAMES` in `secrets/vault.py` (and `_REQUIRED` if it gates readiness) — `import_dir`, `inspect`, and the wipe all iterate `FILENAMES`, no other change needed.
- **Widen/narrow where imports may live:** edit `allowed_roots()` and `_root_is_sane()`; add a matching bounds case to `test_setup_web.py`. Do **not** rewrite the identity checks as string comparisons (invariant #5).
- **Change verify parsing / auth-URL detection:** `_parse_check`, `_STATUS_RE`, `_AUTH_URL_RE`, `_extract_auth_url` in `setup.py`; assert in `test_parse_check_*` / `test_extract_auth_url`.
- **Change the fresh-setup commands shown:** `setup_commands()` (keep GAM7 order: `create project <admin>`, `oauth create`, `create svcacct`); guarded by `test_setup_commands_shape`.
- **Touch the route/screen:** `web/routes/setup.py` + its `setup.html` (full page) / `_dwd.html` / `_commands.html` / `_verify.html` templates; keep the import gated through `resolve_dir` so the UI never offers a folder the import would refuse.
