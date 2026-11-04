# 2026-11-04 — The usage report's "as of" named the day asked for

- **Symptom:** found by SwiftGamGui (its `audit-reports.md`, "Deviations from GamGUI"), never seen live here. Reproduced
  on `main`: Reports read "as of" two days ago while the rows were from another day.
- **Cause:** `GAMConnector.usage_report` returned the date it asked for. GAM walks back to the newest day with data on
  its own (`doReport`, `_checkDataRequiredServices`, `_adjustTryDate`), and each row has its own `date` column.
- **Why not caught:** the only test checked that rows came back. The mock's rows were already dated 2026-06-16 whatever
  was asked; nothing compared the label with them.
- **Fix:** branch `fix/usage-report-as-of-date`. `_usage_day` takes the rows' newest `date`, or the day asked for when
  no row has one.
- **Prevention:** `test_usage_report` holds the label to the mock's rows, and
  `test_usage_day_is_the_rows_own_or_failing_that_the_day_asked` the fallback. Offline only: the walk-back was read from
  GAM's source, not seen on a tenant.
