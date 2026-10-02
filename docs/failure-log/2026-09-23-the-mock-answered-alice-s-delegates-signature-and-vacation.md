# 2026-09-23 — The mock answered Alice's delegates, signature and vacation for anyone

- **Symptom:** a review (F20): pointing any of five connector reads (`print delegates`, `show
  signature`, `show vacation`, `print groups member`, `user … print calendaracls`) — or the five
  user-detail panels that call them — at a wrong, hard-coded user left the whole suite green. The
  Delegates or Signature panel could show another user's data in two features in daily use.
- **Cause:** `mock_gam.sh` answered those reads with the same canned output whatever the target, and
  accepted any trailing word (even `formatjson`, which GAM rejects on three of them) — the same lie as
  the group-members bug, fixed for `info user` and `print group-members` but not here. Its read
  handlers also accepted any word after a Builder export's `todrive`.
- **Why not caught:** no test checked the argv of a read or compared two users' results; the one
  offboarding test near it patched `list_delegates` instead of driving the mock.
- **Fix:** those reads are keyed on their target — Alice, Bob and Carol each have their own data, an
  address that isn't a user fails as GAM does (the token error, or "Invalid Input: memberKey" for
  `print groups member`) — and take only the argv the app sends; a `todrive` tail is accepted only on
  a print/report read and only with `tduser`/`tdtitle`.
- **Prevention:** `test_a_per_user_read_asks_gam_about_that_user`, `test_per_user_reads_return_that_users_data`
  (connector), `test_a_detail_panel_reads_the_user_it_is_for` (routes); the malformed-shape and
  todrive cases in `test_mock_gam.py`. All fifteen wrong-target mutations (five connector reads to an
  unknown user and to Alice, five routes to Alice) now fail (scratch run). The failure wording is
  GAM's shape read from its build, not captured from a tenant.
