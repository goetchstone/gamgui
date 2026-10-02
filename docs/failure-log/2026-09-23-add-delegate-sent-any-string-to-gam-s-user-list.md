# 2026-09-23 — Add delegate sent any string to GAM's user list

- **Symptom:** a review (plan: delegation polish) found `/users/delegate/add` checked only that the
  box wasn't blank. GAM reads the value as a `<UserList>` (grammar: `"<UserItem>(,<UserItem>)*"`,
  `<UserItem>` = an address, an id or a bare string), so a bare name became `name@<domain>` and a
  comma delegated the mailbox to two accounts; an address outside the directory reached GAM, and any
  error replaced the whole delegates panel (list and form) with an amber box. Remove ran on one click.
  `looks_like_email` — shared with onboarding — also let a comma through.
- **Cause:** the route relied on the form's `<input type="email">`, which only the browser enforces —
  the server checked for a blank value and nothing else; `_EMAIL_RE` excluded `@` and whitespace, not
  `,`.
- **Why not caught:** the delegate tests posted only well-formed addresses; the mock accepts any
  delegate string not containing `missing`.
- **Fix:** `_check_delegate` refuses a non-address, a comma, the owner itself and an alias (naming
  the primary) before any `gam` call; an address not in the cached directory, or a suspended one,
  needs an explicit "Add anyway"; errors render inside the panel, with GAM's line in the disclosure;
  Remove asks first (`hx-confirm`, like the calendar-access Remove). `_EMAIL_RE` excludes `,`.
- **Prevention:** `test_add_delegate_refuses_a_bad_address_before_gam`,
  `test_add_delegate_outside_the_directory_needs_an_ok`, `test_add_delegate_warns_on_a_suspended_account`,
  `test_delegate_remove_asks_before_it_runs`, and the comma row in
  `test_parse_hire_csv_rejects_invalid_email_targets`. Unverified live: what Gmail returns for an
  outside-domain or suspended delegate.
