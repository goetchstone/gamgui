# 2026-09-23 — The offboarding auto-reply kept the leaver's old vacation restrictions and dates

- **Symptom:** a review (F5) replayed GAM's `setVacation` on stored settings `{restrictToDomain:
  True, endTime: <Aug 2025>}`: the offboarding reply went out still limited to the organization,
  with the past end date — customers get no auto-reply (or nobody does) while the step shows ✓ and
  the preview says "Auto-reply senders will receive …". On the user page, unticking "Domain only" or
  "Contacts only" sent nothing, so the box came back ticked.
- **Cause:** GAM's `vacation` merges (reads the settings, overwrites only the named fields, writes
  them back); `set_vacation` named the flags only when true and the dates only when given. The
  runbook said the opposite ("`vacation on …` replaces the settings").
- **Why not caught:** the mock's vacation handler kept no state and always printed "Updated", so a
  leftover setting was invisible to every test.
- **Fix:** `set_vacation` always sends `contactsonly <bool> domainonly <bool> start <date>|Started
  end <date>|NotSpecified` (grammar 8286-8288); the user page's Vacation form shows the stored
  dates so a blank box means none.
- **Prevention:** the mock merges `vacation` into a per-test state dir like GAM (`GAM_MOCK_STATE`,
  `gam_state` fixture; `test_mock_gam_vacation_merges_like_gam`), and
  `test_offboard_autoreply_does_not_inherit_the_leavers_old_vacation_settings`,
  `test_vacation_form_unticks_and_round_trips_its_dates`. GAM's merge is read from its bytecode; that
  Gmail keeps these fields while the reply is off is not verified live.
