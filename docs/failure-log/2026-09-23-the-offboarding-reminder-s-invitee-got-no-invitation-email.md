# 2026-09-23 — The offboarding reminder's invitee got no invitation email

- **Symptom:** a review (F11): with "Also invite to the reminder (IT/HR)" filled, the reminder ran
  `… add event primary … attendee it@example.com` and nothing else. GAM 7.48.11's `add event`
  defaults `sendUpdates` to `none`, so IT/HR got no email — the event appeared silently on their
  calendar — while the form said "invite" and the first-live-run checklist expected "the invitee got
  the invite".
- **Cause:** `add_calendar_event` never emitted an `<EventNotificationAttribute>`.
- **Why not caught:** the tests checked that the attendee was passed, not whether GAM would notify
  them; GAM's default is only visible in its source.
- **Fix:** with an attendee, the builder adds `sendupdates all` after the event attributes (grammar
  6459-6460, 6469).
- **Prevention:** `test_lifecycle_commands` and `test_offboard_reminder_invites_notify_target`
  assert the argv ends `attendee <e> sendupdates all`. Delivery of the email is not verified live.
