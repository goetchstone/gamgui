# 2026-09-23 — The env-allowlist tripwire checked the allowlist against itself

- **Symptom:** a review (F22): adding `PYTHONPATH`, `DYLD_INSERT_LIBRARIES`, `GAMCFGSECTION`,
  `GAM_CSV_OUTPUT_QUOTE_CHAR` and `SSL_CERT_FILE` to `runner.ENV_ALLOWLIST` left
  `tests/test_runner.py` green (14 passed) — the process holding the plaintext credentials would
  have inherited them.
- **Cause:** `test_gam_inherits_only_the_allowlisted_environment` computed what the child may see
  from `ENV_ALLOWLIST`, the value under test, and never asserted the hostile keys were absent. Its
  child, `/usr/bin/env`, is a SIP platform binary, so a leaked `DYLD_*` could not be seen anyway.
- **Why not caught:** the test did catch a full revert to `os.environ.copy()`, which is the change
  it was written against; nobody mutated the allowlist itself.
- **Fix:** the expected sets are literals in the test (`EXPECTED_PASSTHROUGH`, `EXPECTED_MOCK_ONLY`),
  pinned by `test_the_env_allowlist_is_the_reviewed_one`; the child test asserts no hostile key
  arrives; `test_no_dyld_variable_reaches_gam` uses the venv's Python (`-I`), which sees `DYLD_*`
  (and aborts on a leaked `DYLD_INSERT_LIBRARIES`).
- **Prevention:** those three tests; the widening mutation now fails five of them (checked in a
  scratch copy). A tripwire whose expectation is imported from the code it guards is a tautology.
