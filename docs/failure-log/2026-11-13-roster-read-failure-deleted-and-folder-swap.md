# 2026-11-13 — A failing disk read deleted a good roster, and a swapped folder could be written into

- **Symptom:** found by the SwiftGamGui session's third attack on its C2 port, never seen live. A disk I/O error, a
  busy or locked file, or an interrupted read of a good kept roster deleted it as "couldn't be read". And with #120's
  folder lock, a roster folder moved or swapped while a save waited for the lock would be written into: the roster
  landing where no later look, expiry or Purge reaches, and two processes locking different folders.
- **Cause:** `_meta` purged on any `sqlite3.Error`, which covers SQLite's I/O, busy and interrupt codes as much as a
  corrupt file. `_held` opened the folder by path after `lstat` and never checked that what it locked was what `lstat`
  saw, or still at the path once locked; every file call after it goes by path.
- **Why not caught:** the damage tests made the file bad (a dropped table, garbage bytes), never the read fail. The
  lock's test held the lock, never moved the folder.
- **Fix:** `_damaged` tells bad content (SQLite's codes for a missing table, corrupt, mismatch, format, not a
  database; a stored value json can't read) from a failing read. Only bad content is deleted; a failing read keeps the
  roster and says to try again, and the page still shows Purge for a roster file it couldn't read (`present`). A
  verifier then found the keep had no end: a stray rollback journal SQLite can't roll back read only
  (`SQLITE_READONLY_ROLLBACK`), or a file no one can open, fails every read, so its stored expiry is never read.
  `_unread` keeps an unreadable roster only while the file's own mtime is inside `KEEP_DAYS` (the app never rewrites
  it in place). Compare says a roster it can't read is the roster, not GAM. `_held` checks the opened descriptor's
  `(st_dev, st_ino)` against `lstat`, `fchmod`s the descriptor, and after the flock checks the path still names it,
  refusing otherwise.
- **Prevention:** `test_a_read_the_disk_failed_keeps_the_roster_and_says_try_again` (eight codes),
  `test_damage_is_told_from_a_failing_read_without_sqlites_code`, `test_a_folder_swapped_as_it_is_opened_is_refused`,
  `test_a_folder_moved_while_a_save_waits_is_never_written_into`,
  `test_a_kept_roster_that_cant_be_read_just_now_still_shows_purge`,
  `test_a_roster_that_stays_unreadable_is_still_deleted_after_its_days` (a real hot journal, and no permission). A
  delete triggered by an error needs the error to be about the thing deleted, and a keep for a failing read still
  needs an end; a lock on a path needs the path to still name what was locked.
