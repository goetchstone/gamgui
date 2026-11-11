# 2026-11-11 — A deleted class owner hid every class

- **Symptom:** found by the SwiftGamGui session's review of its Classroom port against GAM 7.51.00, never seen live (the
  operator has no Education domain). On a school domain with one class owned by a deleted account, a teacher search, a class
  card or the Classroom read would have failed with "Classroom doesn't know …", every class lost. Worse, the offboarding
  preview's read of the classes a leaver owns treats "not found" as "owns none", so it would have said the leaver owns no
  classes: a silent "none", the very failure that read exists to prevent.
- **Cause:** with `owneremail`, GAM looks up each class's owner; for one it can't find, `_convertCourseUserIdToEmailName`
  writes the address as "Unknown user" and calls `entityDoesNotHaveItemWarning` ("Course: <id>, Owner ID: <id>, Does not
  exist"), which sets `ENTITY_DOES_NOT_EXIST_RC` (56). GAM still prints every class. `run_authenticated` raises on any
  non-zero exit, and 56 classifies as `NOT_FOUND`.
- **Why not caught:** the mock had no deleted owner, and C1.1–C1.2's reading of the build covered a missing class or teacher
  (the same exit) but not this warning path.
- **Fix:** `GAMConnector._courses_read` reads with `run_any_exit` and accepts exit 56 only when every line GAM reported is
  that warning (`errors.only_unknown_owners`) and a table came back; anything else raises as before. `Course` never keeps
  "Unknown user" as an address (`_owner_address`); the owner is still known by `ownerId`, which the Classes screen names from
  the directory cache.
- **Prevention:** the mock models it (`$GAM_MOCK_STATE/ownergone/<owner id>`, as SwiftGamGui's does);
  `test_a_deleted_owner_never_hides_the_classes`, `test_a_deleted_owner_never_reads_as_the_leaver_owning_nothing`,
  `test_only_an_unknown_owner_warning_lets_the_table_stand`. Reading a build: follow every warning a read can raise, not only
  its errors, since a warning can still set the exit code.
