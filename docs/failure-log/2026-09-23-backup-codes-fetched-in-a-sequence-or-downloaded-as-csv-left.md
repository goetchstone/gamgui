# 2026-09-23 — Backup codes fetched in a sequence, or downloaded as CSV, left no sensitive record

- **Symptom:** found reading the D3 audit path (review follow-up): `show backupcodes` added as a
  Builder sequence step ran through `apply()` and was filed as `apply`, so an audit search for
  "sensitive" missed it; and the result table's **Download CSV** of `print backupcodes` handed the
  codes out as a file with no record at all (the Sheet export of the same result is audited).
- **Cause:** `_run_sequence` sent every step to `conn.apply`, which knows argv, not catalog
  commands, so it could not tell a sensitive read from any other step; `/builder/export.csv` served
  `builder_last_result` without knowing which command produced it.
- **Why not caught:** the sensitive-read tests covered `/builder/run` only; the runbook even listed
  the sequence gap as "not covered".
- **Fix:** a sequence step whose catalog command is sensitive runs through `catalog_read`
  (`sensitive_read`, never the output); the stored result remembers a sensitive read's command,
  argv and target, and its CSV download records `sensitive_csv_export` (row count, never the rows)
  via `GAMConnector.audit_sensitive_csv`, or is refused when there is no connector to audit through.
- **Prevention:** `test_builder.py::test_a_sensitive_read_in_a_sequence_is_audited_as_one`,
  `test_the_csv_download_of_a_sensitive_result_is_audited` (both fail on the old code),
  `test_the_csv_download_of_a_plain_result_is_not_audited`; `audit_sensitive_csv` is named in the
  `AUDITED_READS` allowlist of the `audit.record` tripwire.
