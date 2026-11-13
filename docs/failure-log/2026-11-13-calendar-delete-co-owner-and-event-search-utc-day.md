# 2026-11-13 — Calendar delete acted as a co-owner; event search read a day as UTC

- **Symptom:** found by the SwiftGamGui review of its calendar port, never seen live. (1) Deleting a
  secondary calendar acted as the first active owner in its ACL, which may be a co-owner, not the
  calendar's data owner. A co-owner may not be allowed to delete it, and a suspended data owner was
  silently swapped for a co-owner. (2) Event search sent the date picker's bare `yyyy-mm-dd`. GAM reads that
  as midnight in its own timezone (UTC unless configured), and Google keeps an event whose end is after
  `timeMin`, so in New York "on or after the 13th" listed an 8-9 pm meeting on the 12th.
- **Cause:** (1) `_resolve_delete_owner` picked from the ACL's owner rules; nothing read `dataOwner`.
  (2) The route passed the dates through as typed.
- **Why not caught:** the mock let any account `remove calendars` and ignored `after`/`before` entirely.
  Two more mock lies: it printed "Service not applicable" where GAM's `Msg.SERVICE_NOT_APPLICABLE` is
  "Service not applicable/Does not exist".
- **Fix:** the delete reads `calendars <id> print settings fields id,dataowner formatjson` (`dataowner` is
  in GAM 7.52.01's `CALENDAR_SETTINGS_FIELDS_CHOICE_MAP`, though the reference omits it) and acts only as
  that account; a suspended or missing data owner is refused, never swapped. The detail page's delete zone
  gives the same answer (it decided from the ACL owners and could offer a delete the preview refused), and a
  failed directory read says so instead of naming the data owner as suspended (both found by a verifier
  pass). Event search sends a picked
  day as the operator's local midnight with that day's offset. The mock answers `dataOwner`, refuses a
  `remove calendars` by anyone else (wording approximate), filters events by instant as Google does, and
  prints GAM's full not-applicable message. Its date check takes the grammar's other `<Time>` spellings,
  refuses a day that isn't one on macOS as GNU date and GAM do (BSD date rolls Feb 30 to Mar 2), and
  answers "not modelled" for relative times rather than a usage error GAM wouldn't give.
- **Prevention:** `test_calendars_delete_acts_as_the_data_owner_not_the_first_owner`,
  `test_calendars_delete_never_swaps_a_suspended_data_owner_for_a_co_owner`,
  `test_calendars_event_search_sends_the_operators_midnight`, and the mock-shape tests beside them. Still
  unproven live: a delete by the data owner when a co-owner exists, and the date window in a non-UTC zone.
