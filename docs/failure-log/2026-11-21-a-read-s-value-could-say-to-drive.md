# 2026-11-21 — A promoted read's value could say `to_drive`, which GAM reads as `todrive`

- **Symptom:** found by the adversarial review of plan F4's Sheet export, not seen live. With a value slot set to
  `to_drive`, 348 grammar-derived reads built an argv the same slot refuses as `todrive`, at positions GAM reads as
  keywords (`print projects to_drive`, `print chatmembers to_drive`). GAM's `getArgument` (7.52.04) lowercases a keyword
  and drops every `_`, so it would have read `todrive`: the Builder's Run would have made a Sheet with no preview, under
  GAM's defaults (it opens the Mac's browser), audited as a `read`. The same held for `target_folder`, `download_file`
  and `save_attachments`.
- **Cause:** `readbuilder.make_build` compared `value.strip().lower()` with `WRITE_KEYWORDS`, which is GAM's spelling
  without GAM's normalization; `catalog_read` had no check of its own. `audit_read` already dropped `_`, and said why.
- **Why not caught:** the keyword test tried case and spaces (`TargetFolder`, ` install `), never an underscore.
- **Fix:** plan F4's PR. `commands.as_keyword` is GAM's normalization in one place; `make_build` refuses a value whose
  keyword form is a write keyword; `carries_todrive` refuses such an argv in `catalog_read`, `export_to_sheet` and
  `audit_read`, and at the Sheet export's preview.
- **Prevention:** `tests/test_builder.py::test_a_promoted_read_refuses_a_write_keyword_as_a_value` (now with the
  underscore spellings) and `tests/test_sheet_export.py::test_a_read_that_already_exports_is_refused`. Whether GAM parses
  each of those positions as a keyword was not checked per command; the refusal doesn't depend on it.
