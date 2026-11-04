# 2026-11-04 — The audit export dropped the screen's search and filter

- **Symptom:** found by SwiftGamGui (its `audit-reports.md`, "Deviations from GamGUI"). Reproduced on `main`: with a search
  and "Failures only" on, Export handed over the whole log.
- **Cause:** `/audit/export.csv` takes `q` and `failed`, but the page's link was a fixed address in the page header, which
  the filter's partial never re-renders. The page said so: "filters aren't applied to the download in v1".
- **Why not caught:** the route's tests called it directly, with its own parameters.
- **Fix:** branch `fix/audit-export-filters`. The link is in `_audit_rows.html`, rendered with the search and filter each
  time, and reads "Export matches" when one is on. Clear them for the whole log. The export never caps at a page.
- **Prevention:** `test_the_export_link_takes_the_screens_search_and_filter` and `test_the_export_is_what_the_filter_shows`.
  The link keeps `download`, which `tests/test_app_window_downloads.py` holds. No GAM involved.
