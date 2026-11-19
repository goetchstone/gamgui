# 2026-11-18 — A calendar index built without its rooms said nothing, so a room read as not there

- **Symptom:** found by the swallowed-read scan (plan item C), never seen live. When a rebuild couldn't list room
  calendars (no Resource Calendar API, a refused scope, a rate limit), it indexed the shared calendars, said "Indexed N
  calendars", and every later search found no room, as if the domain had none.
- **Cause:** `scan_all_calendars` caught any failure of `list_resources` and passed, on purpose (rooms are a bonus and
  shouldn't cost the domain-wide user scan), but recorded nothing, so the index couldn't tell "no rooms" from "rooms
  unread".
- **Why not caught:** the scan's test used a mock whose room list always reads; the index kept no record of what a
  rebuild missed.
- **Fix:** the scan still keeps the shared calendars, but names "rooms" as unread; the index keeps that
  (`IndexStatus.rooms_unread`, cleared by the next whole rebuild); the rebuild's log and the Calendars page say no
  room is in the index and to rebuild.
- **Prevention:** `test_a_room_list_that_failed_is_named_unread_and_the_rest_kept`,
  `test_the_index_keeps_whether_its_rooms_were_read`, `test_an_index_built_without_its_rooms_says_so`,
  `test_a_rebuild_that_couldnt_list_rooms_says_so_and_keeps_it`. The general tripwire is
  `tests/test_swallowed_reads.py`, which found it and no longer lists it.
