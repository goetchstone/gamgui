# 2026-09-23 — The stale-config sweep zeroed files through a planted `gamcfg-*` symlink

- **Symptom:** a review found that a `gamcfg-*` symlink in the runtime dir (`…/GamGUI/run`) made the
  shutdown sweep (`max_age_seconds=0`) overwrite every file in the link's target with zeros; a
  symlinked file inside a real `gamcfg-*` dir got its target zeroed the same way, and a FIFO
  `.gamgui.pid` would hang the startup sweep. Reproduced offline with sentinel files; not seen live.
- **Cause:** `sweep_stale_configs` tested `child.is_dir()` (follows links) and `_shred_dir` walked
  `path.iterdir()` and zeroed each `is_file()` via `open(child, "r+b")` — every step followed a
  symlink, and `_owner_pid` read the marker with a blocking, link-following `read_text()`. The
  `rmtree` at the end already refused links, which hid that the zeroing before it did not.
- **Why not caught:** the sweep tests only ever built real dirs with real files; nothing planted a
  link or a non-regular file in the runtime dir.
- **Fix:** the sweep skips `is_symlink()` entries; `_shred_dir` opens the dir
  `O_DIRECTORY|O_NOFOLLOW` and zeroes each entry through that descriptor with
  `O_NOFOLLOW|O_NONBLOCK`, regular files only; `_owner_pid` reads the marker the same way.
- **Prevention:** `test_shutdown_sweep_never_follows_a_symlinked_gamcfg_entry`,
  `test_shred_dir_does_not_zero_a_symlinked_file_inside_the_dir`,
  `test_shred_dir_leaves_a_symlink_given_as_the_dir_alone`,
  `test_sweep_does_not_follow_a_symlinked_pid_marker`, `test_sweep_is_not_blocked_by_a_fifo_pid_marker`
  in `tests/test_ephemeral.py`; the secrets runbook records the rule.
