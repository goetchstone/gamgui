# 2026-10-01 — A hook test ran its own test cases, a live acceptance pass among them

- **Symptom:** an agent testing a new credential-blocking hook ran its "should block / should allow"
  command table for real: listings of the GAM config and app-data dirs, reads of credential paths
  (none existed), a tail of the audit log (tenant addresses into the session transcript), `make gam`,
  the suite, and `scripts/acceptance.py` against the live tenant until it was stopped. No write ran.
- **Cause:** the cases sat in a quoted heredoc, and one case was itself a heredoc ending on the same
  `EOF` line. That closed the outer heredoc early, so every later `B|cmd` line ran as a pipeline.
- **Why not caught:** nothing between the model and the shell knew a credential path or a live run;
  those rules lived only in the live-verify skill, which wasn't loaded, and auto mode ran the batch
  without a prompt.
- **Fix:** a local `credential-guard.sh` PreToolUse hook (Keychain reads, shell `gam`, inline Python
  importing the vault/runner/AppState, credential paths, the real app, and `acceptance.py` without
  `GAMGUI_LIVE_OK=1`) plus `permissions.deny` Read rules for the same paths; see FRAMEWORK.md.
- **Prevention:** the hook, 50 cases fed to it as JSON. Never write commands under test as shell
  text: what the shell can parse, it can run.
