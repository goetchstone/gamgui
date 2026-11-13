# 2026-11-13 — A crashed roster save outlived it, and the lock held one process only

- **Symptom:** found by the SwiftGamGui session while porting C2, never seen live. A save cut off between write and rename
  left its `.saving-*` file, a whole student roster, in the roster folder with no Purge shown, until a later save or purge
  of that same domain. And two GamGUI processes sharing the folder (a dev run beside the installed app) could each sweep
  the other's save mid-write: its rename then failed, or Purge reported done while a fresh snapshot landed.
- **Cause:** only `save` and `purge` swept, and only that domain's leftovers; a look (`meta`, `load`, `issues`) returned
  None without sweeping. The lock was `threading.RLock`, which no other process sees.
- **Why not caught:** the sweep's test cut off a save and then saved or purged, the two calls that swept, never a look.
  Every test ran in one process, where the thread lock was enough.
- **Fix:** `RosterStore._held` takes `_LOCK` and an exclusive `flock` on the folder for every save, look and purge, and
  `_meta` sweeps every `.saving-*` file in the folder, of any domain, on each look. Under the flock none is mid-write.
- **Prevention:** `test_a_save_a_crash_cut_off_is_swept_by_the_next_look` and
  `test_a_look_waits_for_another_process_holding_the_folder` (a flock on its own descriptor stands in for the other
  process). A temp file of sensitive data needs its sweep on the path the user takes, and its lock as wide as whatever
  shares the folder.
