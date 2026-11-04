# 2026-11-04 — An auto-reply's own text could hide the auto-reply

- **Symptom:** found by SwiftGamGui's security review (its failure log, 2026-10-21, "An auto-reply's own text
  could hide it from the check"), never seen live here. Reproduced on `main` through the Users page with the
  mock's stored setting: a reply that is on, whose text holds the line `Enabled: False`, showed "Off" with no
  "Turn off", so a phishing reply would keep going out. A body faking `Subject:` or `Contacts Only:` changed
  those too, and the form would have saved them back.
- **Cause:** `Vacation.from_show_text` ended the message at any line that looked like a setting, then read that
  line as one. Read at 7.50.00, GAM's `_showVacation` prints the settings, then `Subject`, then `Message` last,
  so such a line can only be the reply's own text.
- **Why not caught:** the tests only ever fed the reader a message with no line like a setting. The reader has
  stopped at settings since it was first written.
- **Fix:** branch `fix/auto-reply-text-hid-the-auto-reply`. Nothing after `Message:` sets a field. Only the banner
  (`Created:`, `Config File:`) still ends the message text, as in SwiftGamGui's reader.
- **Prevention:**
  - `test_vacation_message_cannot_change_a_setting` holds a body faking every setting, on and off.
  - `test_vacation_whose_text_says_it_is_off_can_be_turned_off` holds the page: "Turn off" is offered and runs.
  - SwiftGamGui's `Tests/Fixtures/vacation.json` pins the old reading; its `gen_fixtures.py` rerun is owed (backport
    plan, "Keep Swift's fixtures honest").
  - Offline only: both tests use the mock's copy of GAM's output, whose order was read from GAM's source.
  - Not handled: a subject holding a line break would print above `Message:` and could still fake a setting.
    Whether Gmail stores such a subject is unknown; finding out takes a live write.
  - Not handled: a reply's own line starting `Created:` or `Config File:` still ends the message, so the form shows
    and would save less than is stored. It sets no field, so the reply still reads as on and can be turned off.
    SwiftGamGui's reader does the same.
