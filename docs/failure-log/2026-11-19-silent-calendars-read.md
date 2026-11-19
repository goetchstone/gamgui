# 2026-11-19 — A calendars read GAM skipped read as "owns none"

- **Symptom:** none seen live. Found by comparing with SwiftGamGui, which already guards it. GAM drops a refused token
  refresh without a word (`validateCalendar`, then `if not cal: continue`): `user L print calendars ownedsecondary` prints its
  titles alone and exits 0. GamGUI parsed that as "owns no secondary calendars". Since #149 the account delete refuses a
  leaver who owns one, so that answer would have let the delete go on and Google delete the calendars with the account.
  The offboarding preview read it the same way, and so offered no hand-over and gave no warning.
- **Cause:** `GAMConnector.owned_calendars` ran through `run_authenticated`, which hands back stdout only. The one thing
  that tells a read from a skip is GAM's "Getting Calendars for" line on stderr, which it prints only once the Calendar
  service is built (`show_gettings`, on by default).
- **Why not caught:** the mock printed the "Getting" line on every `ownedsecondary` read and had no way to skip it, so it
  was more permissive than GAM. The failing exit (Calendar off, exit 73) was tested; the silent exit 0 wasn't.
- **Fix:** `owned_calendars` reads through `run_any_exit` (any non-zero exit still raises), and no rows without the
  "Getting" line raises `Incomplete`. The delete's confirm step, its second read at the click, and the offboarding preview
  each say "Couldn't check whether L owns secondary calendars (GAM answered without reading it…)", never "none".
  `_common.unread_why` holds the fixed words, and it is a checked translator in `tests/test_route_error_text.py`.
- **Prevention:** the mock now skips the read the way GAM does (`GAM_MOCK_STATE/quiet/<u>`: titles alone, exit 0, no
  "Getting" line), as SwiftGamGui's mock already did. Tests: `test_a_calendars_read_gam_skipped_without_a_word_is_said_never_none`
  and `test_a_delete_whose_calendars_read_gam_skipped_says_so`. `test_only_a_reviewed_read_may_use_the_any_exit_door`
  names the new caller. The pre-commit item 16 shape (a read behind a decision, tested on its failing and partial exits)
  covers it; this one's partial exit is a silent zero.
