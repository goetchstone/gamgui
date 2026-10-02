# 2026-09-23 — The README promised a confirmed data transfer; the Builder ran one on a bare POST

- **Symptom:** a review (F26) posted `cid=build.transfer_data` with the old and new owners to
  `/builder/run` without `confirmed`: a `create datatransfer` ran and was audited. The README said
  data transfer and "bulk operations" run behind preview → confirmation and that the server refuses
  a request that skipped it; `/calendars/share` to a group also fans out with no confirmation.
- **Cause:** `build.transfer_data` was curated `RiskLevel.LOW`, and by the guard's policy a
  single-target LOW write needs nothing; the README sentence was written from the intent, not the
  policy table.
- **Why not caught:** no test holds the README's list of guarded actions to the catalog's risk
  levels; the tripwire gated `/builder/run` with a suspend.
- **Fix:** `build.transfer_data` is `DESTRUCTIVE` (a transfer can't be undone by a second one), so
  its preview asks for Confirm & run and ten in a sequence need the typed word; every Builder
  mutation also runs only from its preview (previous entry). The README names what is guarded —
  the bulk jobs by name — and says single-target changes, a group calendar share included, run
  without a confirmation.
- **Prevention:** `test_a_data_transfer_is_confirmed_like_a_destructive_change`. The README list
  itself is still prose; nothing checks it mechanically.
