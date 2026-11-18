# 2026-11-18 — A store file that couldn't be read loaded the defaults, and the next save wrote over it

- **Symptom:** found by the swallowed-read scan (plan item C), never seen live. If `onboarding.json` (roles, welcome
  emails) or `signatures.json` (signature templates) couldn't be read — a read the OS refused, a file cut off
  mid-write, one that wasn't a store — Onboarding and Signatures showed the starters as if nothing had been saved, and
  the next save of any role or template wrote the starters plus that one change over the operator's file.
- **Cause:** both stores' `_load` caught every exception and returned a copy of the seed, with no word and nothing
  kept. A read error and "no file yet" were the same answer, and the next `_save` acted on it. The sibling stores
  (`offboard_templates.py`, `roster_maps.py`) already moved an unreadable file aside; these two predate that.
- **Why not caught:** the store tests covered a corrupt file only to show the seed loaded, which was the bug's own
  behaviour. Nothing asked what happened to the operator's file afterwards. This is invariant 10's shape (e395b6e:
  any sqlite3 error read as a corrupt roster, and the roster deleted) in two older stores.
- **Fix:** the seed loads only when there is no file. A file that can't be read, isn't JSON or isn't a store is moved
  to `<name>.unreadable-<time>` (`set_aside`) before the seed loads, and both screens say where it went.
- **Prevention:** `test_a_store_that_cant_be_read_is_set_aside_and_never_saved_over` (both stores, three ways to be
  unreadable, and a save afterwards), `test_the_screens_say_a_store_file_was_set_aside`. The general tripwire is
  `tests/test_swallowed_reads.py`, which found these two and no longer lists them.
