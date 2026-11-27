# 2026-11-27 — The fast signature write put GAM's csv options out of order, so GAM refused it after the backup read

- **Symptom:** found before any live run. SwiftGamGui's port of 5.4 read `openCSVFileReader` and saw that GamGUI's
  `set_signatures_csv` (`csv - columndelimiter , quotechar " noescapechar true gam user …`) can't be parsed. GAM checks
  `columndelimiter`, then `noescapechar`, then `quotechar`, once each. `noescapechar`, coming after `quotechar`, is left
  over, and `doCSV`'s `checkArgumentPresent('gam', required=True)` stops with "Invalid choice (noescapechar): Expected
  <gam>", exit 2. Live, every fast run would have read and kept everyone's signature, then set no one.
- **Cause:** the options were written in an order that read well, not GAM's. `checkArgumentPresent` looks only at the
  current argument, so the sequence in `openCSVFileReader` is the order GAM accepts. GamCommands.txt line 1422 lists them in
  that order too, but nothing compares option order against the grammar.
- **Why not caught:** the mock matched one exact argv list (`argv == CSV_SIGNATURES`), copied from the builder, so it
  accepted the builder's order and refused nothing else. The grammar contract checks keywords and the command head, not the
  order of options.
- **Fix:** the builder puts the options in `openCSVFileReader`'s order. `mock_gam_loops.csv_options` reads them as GAM does
  (once each, in sequence, `gam` required after) and gives GAM's usage error, exit 2, for anything left over. It also
  reads the rows with the options it parsed, so leaving out `noescapechar` eats a backslash in the mock as it would live.
  The two GAM sources it follows are `checkArgumentPresent` and `openCSVFileReader`.
- **Prevention:** `tests/test_mock_gam.py` refuses #193's order and another out-of-order form with GAM's words, and
  `test_without_noescapechar_gam_s_reader_eats_a_backslash` holds the reader to the options. An audit of every builder for
  the same class (options GAM reads once each in a fixed order) ran with this fix. The lesson for RULE-FEEDBACK: a mock
  handler that matches one exact argv list vouches for nothing but that list. Where GAM parses in sequence, the mock should
  parse in sequence too.
