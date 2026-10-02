# 2026-09-25 — Web test fixtures read the operator's real app data, and could write their audit log

- **Symptom:** a review (R7) found that `tests/test_users_web.py`'s `client` fixture showed the
  operator's own onboarding roles, groups, a calendar and a signature template on `/onboard`. None of
  it was fixture data. After a `/setup/switch` under that fixture, the new connector's audit log was
  the operator's real `~/Library/Application Support/GamGUI/audit.jsonl`.
- **Cause:** a store built without a path (`RunbookStore()`, `SignatureStore()`, `AuditLog()` inside
  `GAMConnector` when `/setup/verify` or `/setup/switch` builds one, a `GAMRunner` with no `base_dir`)
  falls back to `app_data_dir()`, which is under the real HOME. Only `test_setup_web`'s `ctx` and a
  few `test_setup` tests pointed HOME at `tmp_path`. The rest left it at the operator's home.
- **Why not caught:** nothing checked where a test's files went. The tests passed because the
  operator's real data happened to be valid. A guard-only run (the hook below, no HOME redirect)
  failed 15 existing tests across 8 files: mkdirs of the real app-data dir and its `run/` folder, and
  an open of the real `~/.gam` from `/setup`. The new `client` fixture test, run the same way, was
  refused its opens of the real `onboarding.json` and `signatures.json`.
- **Fix:** an autouse fixture in `tests/conftest.py` (`_hermetic_home`) sets HOME, XDG_DATA_HOME and
  LOCALAPPDATA to each test's `tmp_path` and clears `$GAMCFGDIR`. The `client` and
  `unconnected_client` fixtures hand every store an explicit `tmp_path` path.
- **Prevention:** `tests/conftest.py` registers an audit hook, with the real roots taken at import.
  It refuses any open, mkdir, listdir, remove or sqlite connect under the real app-data dir or `~/.gam`
  before the access happens. `_no_real_app_data` then fails the test, even when the code under test
  swallowed the refusal. `tests/test_hermetic.py` pins the redirect and the hook.
  `test_the_client_fixture_keeps_every_store_in_tmp_path` pins the fixture. CONTRIBUTING's
  Conventions section states the rule.
