# 2026-11-14 — A roster alias planned its own account's removal; the risk check's bases were too wide

- **Symptom:** found by the SwiftGamGui session's review of its Classroom port, never seen live (no Education tenant).
  (1) A SIS that lists a teacher by an old address that is now an alias (`jdoe@` for `jane.smith@`) read as "no account":
  she was dropped from the class's SIS teachers, another SIS teacher became owner (hand-over step 07), and `jane.smith@`
  was removed from the class she owned (08); a student the same way (10). The plan's note said such people "aren't added
  (nor removed)". Without the directory the same thing happened unnoted. (2) Removing every co-teacher of 400 classes
  (~4% of teachers *plus* students) wasn't held back, and archived managed classes counted in both risk bases, diluting
  every share a little more each year.
- **Cause:** (1) the compare built the directory as a set of primary addresses, and the planner compared SIS addresses to
  Classroom's (which are always primaries) as plain strings. The user cache already read `aliases`; nothing used them,
  and `GAMUser` dropped `nonEditableAliases` (alias-domain addresses), which GAM's `fields aliases` also returns
  (`USER_FIELDS_CHOICE_MAP`, 7.52.01 build). (2) `_flags` measured `remove_teacher` against `enrolled` (teachers and
  students), and `managed`/`enrolled` counted ARCHIVED classes, which nothing is archived or removed from.
- **Why not caught:** every planner test used one address per person, so a primary and its alias never met. The risk
  tests had one class and one role. This is invariant 10's shape: a read that answers for less than it seems (primaries
  only) stood as "not an account", and that answer drove a removal.
- **Fix:** `directory_from` maps every address (primary, aliases, non-editable aliases) to its account's primary, and
  `plan` reads each SIS address through it before comparing; a set is refused as a directory (`TypeError`). Without the
  directory, a class whose roster names an address Classroom doesn't list in it gets no removal and no owner change
  (noted). The compare reads the directory now (`st.users(force=True)`), not from the 5-minute cache: a cached list can miss
  an alias added or an account renamed minutes ago (found by this fix's review). Risk bases: active managed classes only;
  teacher removals against their teachers.
- **Prevention:** `test_a_teacher_listed_by_an_alias_keeps_the_class_she_owns`,
  `test_without_the_directory_a_class_with_an_unmatched_address_keeps_its_people_and_owner`,
  `test_a_set_of_addresses_is_refused_as_a_directory`, `test_compare_reads_the_directory_now_not_from_the_cache`, `test_compare_reads_a_sis_alias_as_its_account` (through the mock,
  and it checks the read asks GAM for `aliases`: the mock prints them whatever is asked),
  `test_removing_every_co_teacher_is_flagged_against_the_teachers_not_everyone`,
  `test_archived_classes_dont_dilute_the_risk_bases`. Mock-only proof: no Education tenant to run it on.
- **Would a rule have caught it?** Yes, if invariant 10 is read as covering identity too: "not found under this address"
  is unknown until every address an account has was checked.
- **Enforcement home if changed:** skill (`pre-commit`, for any compare keyed on addresses).
