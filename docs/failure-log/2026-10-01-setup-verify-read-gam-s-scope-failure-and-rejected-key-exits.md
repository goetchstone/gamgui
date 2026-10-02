# 2026-10-01 — Setup verify read GAM's scope-failure and rejected-key exits as 1; the build uses 10 and 16

- **Symptom:** found by the improve-rules pass, not by an operator: on a real tenant, a scope not
  authorized still made Setup's verify show "GAM failed (unknown, exit=10)" — the generic error the
  2026-09-25 fix ("Setup verify hid which scopes failed…") was meant to replace — and a rejected
  service-account key showed GAM's "Does not exist or has invalid format" line instead of naming the
  key check. Neither was ever seen live; both entries say the failing shapes were "read from the
  vendored build, not captured".
- **Cause:** `core/setup.py` set `SCOPES_NOT_AUTHORIZED_RC = 1`, with a comment that USAGE_ERROR_RC and
  ACTION_FAILED_RC "are 1 as well". The vendored 7.48.14 build's table says 10, 2 and 50. A rejected
  key goes through `invalidOauth2serviceJsonExit` (stderr error + instructions, exit
  `OAUTH2SERVICE_JSON_REQUIRED_RC`, 16), not a silent exit 1. The message texts had been read from the
  build; the exit codes and streams were guessed, and the mock was taught the guess.
- **Why not caught:** the mock lied in exactly the way CLAUDE.md warns about — `mock_gam.sh` exited 1
  on both paths, and every test asserted the mock's code (`exit_code == 1`), so the tests agreed with
  the mock instead of with GAM. GamCommands.txt proves syntax only; nothing checked a status code
  against the build.
- **Fix:** the constants are the build's (10, 16); verify reads either exit as the check's answer
  only when stdout carries the PASS/FAIL rows (`CHECK_ANSWER_RCS`), so a missing key file (16, empty
  stdout) stays a plain error, showing GAM's `ERROR:` line: the instructions GAM prints after it
  (`Msg.INSTRUCTIONS_OAUTH2SERVICE_JSON`) are skipped like progress chatter, where the service-account
  pattern had read "…to create and authorize a Service account." as the error line to quote. The
  mock exits 10 on a failing scope; on a rejected key it prints the rows, then GAM's stderr error and
  instructions, and exits 16; a missing key file or `oauth2.txt` exits 16 too (it had guessed 12). The
  tests compare against the constants and build errors with `from_run`, as the runner does.
- **Prevention:** `tests/test_gam_exit_codes.py` reads the build's `*_RC` table out of the vendored
  binary's PyInstaller archive and fails if any module-level `NAME_RC` the app defines differs from it,
  and holds the mock's rejected-key and missing-credentials exits to the build (the failing-scope exit
  is held by `test_mock_gam.py` through the constant). It covers those paths only — the mock's other
  `exit N` literals (2, 50, 56, 73) were checked by hand against the table on 2026-10-01 and are not
  guarded. The build half first skipped without PyInstaller, which CI's test jobs do not install; it now
  reads the archive with the stdlib, and `gam-compat` (so every GAM bump PR) and gam-watch's suite set
  `EXIT_CODES_REQUIRE_GAM`, which fails rather than skips (`test_workflow_safety.py` holds both). Still
  unproven live: no failing scope or rejected key has been captured from a real tenant.
