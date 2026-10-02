# 2026-09-30 — `make gam` deleted the committed command catalog

- **Symptom:** after pulling the automated GAM 7.48.14 bump and running `make gam`, two tests failed
  (the Builder catalog load and the guide's Builder labels) and a contract test skipped with
  "command_catalog.json not generated"; `git status` showed the tracked catalog deleted.
- **Cause:** `scripts/fetch_gam.sh` installs with `rm -rf "$DEST"`, and `command_catalog.json` is the
  one committed file in that directory.
- **Why not caught:** the fetch tests start from an empty `resources/gam7`; the runbook's "then run
  build_command_catalog.py" step hid it, since regenerating restores a byte-identical file.
- **Fix:** the install keeps the existing catalog across the wipe. Test
  `test_a_reinstall_keeps_the_committed_command_catalog` (fails on the old script).
- **Prevention:** a vendoring step that clears a directory must not own a directory holding tracked
  files — keep generated-but-committed files out, or carry them across explicitly.
