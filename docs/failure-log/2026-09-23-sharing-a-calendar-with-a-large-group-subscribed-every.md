# 2026-09-23 — Sharing a calendar with a large group subscribed every member with no confirm

- **Symptom:** found reading the guard policy against the routes: `/calendars/share` to a group
  granted the ACL and started a background job subscribing every member straight from the Share
  button. A 300-member all-staff group is 300 writes to 300 calendar lists on one click, while
  guard policy asks for a confirm for any LOW write to 10 or more targets.
- **Cause:** the route was classified as a single-target LOW write (one ACL). The fan-out was added
  later and resolved the group *after* the grant, so there was never a count to confirm against.
- **Why not caught:** `test_write_routes_guarded.py` listed `/calendars/share` as a LOW write, and
  the mock's groups had two members, below the threshold.
- **Fix:** `/calendars/share` resolves the members first; past the bulk threshold it writes nothing
  and renders a confirm step with the count and the first ten members, holding them under a
  single-use preview token. `/calendars/share/group` takes the token (a changed calendar, group or
  role is refused), enforces `confirmed=1` server-side, then grants and fans out. A smaller group
  or one person runs as before.
- **Prevention:** `/calendars/share/group` is GATED in the tripwire (bare POST, unconfirmed step,
  edited form and replay all write nothing); `test_calendars_share_to_a_large_group_asks_first_with_the_member_count`;
  the mock gained a 12-member group (`allhands@example.com`). Checked in a browser against the
  mock. The fan-out's live behaviour on a real group is still unproven.
