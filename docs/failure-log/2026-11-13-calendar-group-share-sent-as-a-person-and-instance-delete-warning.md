# 2026-11-13 — A group share sent as a person's rule; an occurrence's delete warned of the series

- **Symptom:** found by the SwiftGamGui port of the calendar screens, never seen live. (1) Sharing a
  calendar with a group's bare address added the rule as GAM reads a bare address, `user:<address>`. That
  gives no member access, so each member's subscribe would answer `Add Failed: Not Found`, exit 50, while
  the mock said `Added`. (2) Deleting one moved occurrence of a recurring event warned that the whole series
  would go. `events.delete` on an occurrence's own id cancels that occurrence only.
- **Cause:** (1) `_grant_and_subscribe` sent the scope as typed. `_subscribers_for` had resolved the
  address to a group to fan out the subscribes, but the ACL never used that answer. (2) `is_recurring` is
  true for a series and for one of its occurrences (`recurringEventId`), and the template warned on it.
- **Why not caught:** the mock let `user <u> add calendars <c>` succeed for anyone, so a share whose rule
  reached nobody still read as every member subscribed. The mock lied, and was more permissive than Google.
  The events fixture had no occurrence.
- **Fix:** a bare address that resolved to a group is sent as `group:<address>`, an empty group's included
  (a verifier found that one still went as a person: a member lookup that answers with no rows means a
  group, while one that fails stays a person). The delete preview says
  "this occurrence only" for an occurrence and keeps the series warning for a series. The mock prints a
  calendar ACL scope as Google keeps it (a bare address is `user:<address>`). With `GAM_MOCK_STATE` it keeps
  the rules written to a calendar and refuses a subscribe that none of them grants, and a delete of a rule the calendar doesn't hold:
  `Not Found`, exit 50.
- **Prevention:** `test_subscribe_needs_access_and_a_bare_group_address_is_a_persons_rule`,
  `test_calendars_share_bare_group_address_grants_the_group_not_a_person`,
  `test_calendars_event_delete_of_one_occurrence_says_only_that_one_goes`. Still unproven live: the README's
  "Share with a group" row stays not yet. Users › Calendar's share form still sends what is typed; it asks
  for a person, and a group's bare address there becomes a person's rule.
