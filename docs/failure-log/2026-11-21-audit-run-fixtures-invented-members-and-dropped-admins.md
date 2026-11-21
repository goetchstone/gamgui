# 2026-11-21 — An audit run's fixture invented shared-drive members, and an admin GAM couldn't name was dropped

- **Symptom:** none live; found by review of #172 before merge. The shared drive run flagged "anyone with the link" and
  domain members, and said "0 give access outside the domain or to anyone with the link" as a known answer. A drive's own
  permissions are its members, users and groups only (Google's sharing guide: membership "Applies to `user` or `group` only
  (not domains)"), so those flags could never fire on a real domain. Separately, the admin token review read only
  `assignedToUser`: a role holder GAM couldn't name was left out of "Every admin", their tokens never read, and the run
  said every step was read.
- **Cause:** the ACL fixture was written from Drive's general permission resource (which has `anyone` and `domain` types)
  rather than from what a shared drive's own permissions can hold, and a test passed only on those invented rows. For the
  admins, `_setNamesFromIds` writes `assignedToUnknown` True with `assignedTo` alone when convertUIDtoEmailAddressWithType
  falls back (getUserEmailFromID passes over forbidden, badRequest, backendError, systemError, serviceNotAvailable), and
  exits 0; the code read the address column only.
- **Why not caught:** the mock answered what the fixture said, and the fixture was more permissive than Google. The admins
  mock had no `assignedToUnknown` row, so the silent path was never exercised. GAM's source was read for exits and columns,
  but Google's own rule for which grantees a shared drive can have wasn't checked.
- **Fix:** the run is "Every drive's members": it flags members from outside the domain and says it doesn't read the
  sharing of files inside a drive. The invented rows are gone. `holder_of` joins an unnamed holder's `assignedTo` to the
  directory's `id`; one no read can name stays listed as `ID <id>` with everything unknown, and the run isn't complete.
  A 56/73 token read that names nobody is now unknown whole.
- **Prevention:** the mock emits GAM's `assignedToUnknown` rows (`GAM_MOCK_STATE/admin_unnamed`), and
  `test_a_holder_gam_couldn_t_name_is_joined_by_id_or_kept_as_unknown_never_dropped` and
  `test_the_admin_run_reads_a_holder_gam_couldn_t_name_and_shows_the_rest_as_unknown` hold it. Fixture rows for a new read
  are checked against Google's reference for what that resource can hold, not only against GAM's columns.
