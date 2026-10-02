# 2026-09-23 — Offboarding accepted a departing user or manager the directory had never heard of

- **Symptom:** reading offboarding before its first live run (plan U2) found both routes took the two
  addresses as typed. A typo'd manager (`alcie@` for `alice@`) previewed fine — the name lookup just
  fell back to the raw address — and a run then reset the leaver's password and signed them out, and
  only afterwards failed to delegate, transfer or remind: a half-offboarded account. An alias of the
  leaver would have run too, and the calendar sweep matches ACLs by primary address. Not seen live.
- **Cause:** `_resolve_name` swallowed "not found" by design (it only feeds the auto-reply text), and
  nothing else looked either address up.
- **Why not caught:** the tests used `leaver@`/`mgr@example.com`, which are not in the fixture
  directory — the suite itself depended on unknown addresses being accepted, and the mock's writes
  succeed for any address without `missing` in it.
- **Fix:** `lifecycle.check_addresses` — the preview and the run both look both addresses up in the
  cached directory and refuse an unknown address, an alias (naming its primary) or the same account
  twice, before any write; a directory that can't be read blocks too. They warn, without blocking,
  on a super/delegated-admin or already-suspended leaver and a suspended manager. The steps act on
  the directory's primary address. This commit.
- **Prevention:** `test_offboard_blocks_an_address_the_directory_does_not_confirm` (preview and run,
  no write), `test_offboard_blocks_when_the_directory_cannot_be_read`,
  `test_offboard_preview_warns_but_does_not_block`; the route tests now offboard fixture users.
