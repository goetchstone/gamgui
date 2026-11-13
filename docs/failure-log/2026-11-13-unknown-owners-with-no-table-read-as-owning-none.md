# 2026-11-13 — Unknown owners with no table read as owning none

- **Symptom:** found by the SwiftGamGui session while porting C1.4 to C1.6, never seen live. If `print courses …
  owneremail` exits 56 with only unknown-owner warnings and prints no table, the offboarding preview's read of the classes a
  leaver owns would say they own none: the silent "none" the 2026-11-11 fix was meant to end.
- **Cause:** the 2026-11-11 fix accepted exit 56 only with a table. Without one it fell through to `GAMError.from_run`, and
  the warning line classifies as `NOT_FOUND`, which `courses_owned_by` reads as "Classroom has never seen them".
- **Why not caught:** the fix's tests covered the warning with a table and with other lines, not the warning alone with
  an empty stdout. GAM normally prints the header, so the case is rare.
- **Fix:** `GAMConnector._courses_read` raises `Incomplete` when exit 56 carries only unknown-owner warnings and no table.
  The warnings mean GAM found classes, so no table is a broken read, never "none". Both callers already say any failure
  in words.
- **Prevention:** `test_unknown_owners_with_no_table_never_read_as_owning_none`. A read's every exit, with and without a
  table, is a case to test, which is the 2026-11-06 rule-feedback entry's shape again.
