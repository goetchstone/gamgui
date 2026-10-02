# 2026-09-23 — A malformed hire CSV 500'd the bulk-onboarding preview

- **Symptom:** a review (plan B1) found that uploading a CSV with one cell over 131,072 characters
  to `/onboard/bulk/preview` returned a 500 instead of a row error; the upload had no size limit, so
  any file was read and decoded whole.
- **Cause:** `parse_hire_csv` guarded only the header read with `try`; the `for raw in reader` loop
  sat outside it, so the `csv.Error` the reader raises past the module's field limit escaped to the
  route.
- **Why not caught:** the CSV tests fed only well-formed text, and nothing exercised the `csv`
  module's own failure paths.
- **Fix:** every record is read inside the `try`; a `csv.Error` refuses the whole file with a
  "Row N: couldn't read the CSV" error (never a half import); the route refuses an upload over 1 MB
  before decoding it.
- **Prevention:** `test_parse_hire_csv_refuses_an_unreadable_file_instead_of_raising`,
  `test_bulk_preview_unreadable_or_oversized_csv_is_a_friendly_error`.
