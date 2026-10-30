# 2026-10-30 — GAM's CSV escapes: the parser dropped and mangled escaped values

- **Symptom:** found by SwiftGamGui while porting the parser (its failure log, 2026-10-10, "GAM's CSV
  escapes"), never seen live here. Reproduced on `main` with bytes the vendored 7.49.02 build's own Python
  wrote: of three `print … formatjson` rows, the one whose JSON held a quote (`Ann "Q" Lee`) was dropped
  without a word, a backslash came back doubled (`C:\\dir`), and a newline in a value came back as the two
  characters `\n`. Plain CSV (delegates, messages) doubled backslashes too. Through the app: a user, group
  or member whose name, title or description holds a quote vanished from the list, the cache and every
  bulk scope built from it.
- **Cause:** GAM writes every CSV in its `setDialect`, which adds `escapechar='\\'` unless
  `csv_output_no_escape_char` is on, and it is off by default. Its writer (Python 3.15 at 7.49.02) then
  doubles each backslash and writes a quote inside a JSON string as `\\""`. `core/gam/parser.py` read with
  Python's default dialect, which has no escape character.
- **Why not caught:** every CSV in the suite came from the same blind spot. The mock printed `print users`
  as NDJSON, which GAM never does, so the CSV path was never exercised end to end. The property tests'
  writer (`tests/test_props_parsing.py::_csv`) claimed to write as GAM's Python does but never doubled a
  backslash. No test input came from GAM's own writer.
- **Fix:** branch `fix/gam-csv-escape-char`. `_parse_csv` reads with `escapechar="\\"`, and the argv is
  unchanged. The mock's `print users` now prints GAM's `primaryEmail,JSON` CSV, escaped as GAM escapes it
  (`gam_cell`, ported from SwiftGamGui's mock), and carol's fixture record carries a quoted title and a
  backslash in her department. The property tests write GAM's output with the escape (`_gam_csv`), while
  the hire CSV, a spreadsheet's, keeps none.
- **Prevention:**
  - `scripts/gam_rc_table.py`, run with the build's Python in CI's real-reference job, now refuses a build
    whose `setDialect` constants or `csv_output_no_escape_char` default moved, and emits samples written in
    that dialect. `tests/test_gam_csv_dialect.py` reads them back with the parser (required under
    `EXIT_CODES_REQUIRE_GAM`), and holds the mock's `print users` bytes to Python's writer in GAM's dialect.
  - `tests/test_parser.py` pins 7.49.02's exact bytes offline. `test_list_users_reads_values_gam_escaped`
    and the property round trips fail without the fix.
  - Still open: the mock's other `formatjson` reads (groups, members, cros, resources, calendars, events,
    Drive files) still print NDJSON (backport plan A5). And this is proven against GAM's writer, not a
    tenant: a live read-only check of a user whose title holds a quote is still owed (`live-verify`).
