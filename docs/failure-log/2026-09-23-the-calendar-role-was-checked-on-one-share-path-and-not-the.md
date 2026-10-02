# 2026-09-23 — The calendar role was checked on one share path and not the other

- **Symptom:** the 10/10 review (plan item Q7) found `/users/calendar/add` handed any posted `role`
  straight to `gam user … add calendaracls primary <role> …`, while `/calendars/share` silently
  replaced an unknown role with `reader` — one path relied on GAM to refuse a bad value, the other
  shared at a level nobody chose. Found by reading the code; not seen live.
- **Cause:** the check lived in one route (`ACL_ROLES` in `calendars.py`) instead of in the
  `GAMCommands` builders both paths share, unlike `add_group_member`'s `_validate_role`.
- **Why not caught:** no test posted an out-of-grammar role to either route; the strict mock rejects
  one, but only as a GAM error after the argv was already built and sent.
- **Fix:** `add_calendar_acl` / `add_calendar_acl_cal` validate against `CALENDAR_ACL_ROLES` (the
  grammar's `<CalendarACLRole>`, verbatim) and raise `ValueError`; both routes render it as
  "Couldn't share calendar: …" with no `gam` call; the silent coercion is gone. This commit.
- **Prevention:** `tests/test_commands.py::test_calendar_acl_role_is_validated_in_the_builder`,
  `tests/test_users_web.py::test_calendar_share_refuses_a_role_outside_the_grammar` (both routes),
  and `tests/test_command_contract.py::test_calendar_acl_roles_match_grammar_and_mock` (the
  builder's set = the mock's = the grammar's). Mock-only proof: no role has been shared live.
