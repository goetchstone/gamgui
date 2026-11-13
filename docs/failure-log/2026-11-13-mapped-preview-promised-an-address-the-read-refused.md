# 2026-11-13 — The map preview promised an address the read refused

- **Symptom:** found by the SwiftGamGui session while porting C5, never seen live. An address with a non-ASCII K
  (U+212A KELVIN SIGN) showed in the map preview's three rows as a plain ASCII address, "teacher", and the read then left
  the row out as `bad_email`. The preview promised what the read wouldn't do.
- **Cause:** `preview` lowercased the address for display without checking it. Lowercasing folds the Kelvin sign into
  an ASCII k; `read_mapped` checks the address as given (`roster._one_address`) and refuses it.
- **Why not caught:** the preview's tests used clean addresses; the as-given check was tested only on the read.
- **Fix:** `_left_out` applies the read's row checks, in order, to each preview row (no class ID, no address, not one
  address, the address as given), shows why a row is left out, and lowercases only an address that passed. Also from
  that message: an invalid `\u` escape in `tests/test_roster_mapped.py` that printed a SyntaxWarning.
- **Prevention:** `test_the_preview_leaves_out_what_the_read_refuses`,
  `test_the_preview_leaves_out_a_row_with_no_class_id_once_that_column_is_mapped`,
  `test_the_map_preview_says_a_row_the_read_will_leave_out`. A preview runs the same checks as what it previews, never a
  cleaned-up display of the input.
