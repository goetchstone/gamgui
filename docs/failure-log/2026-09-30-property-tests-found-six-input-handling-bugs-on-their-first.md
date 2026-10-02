# 2026-09-30 — Property tests found six input-handling bugs on their first run

- **Symptom:** the first Hypothesis properties (`test_props_parsing.py`, written for fuzzing) failed
  on real code: `parse_records` raised on a CSV cell over 131,072 characters, on a bare CR, and on
  JSON nested deeper than the stack, and turned NDJSON holding a raw U+2028 into a bogus record;
  `parse_hire_csv` filled every column a CSV lacked — `notify` included, where the new hire's temp
  password is sent — from a column whose header is empty; and `render` (welcome email) and
  `render_signature` expanded a `{token}` inside a value (a name typed as `{phone}`).
- **Cause:** `csv` under its default field limit and without `newline=""`; `_try_json` catching only
  `ValueError`; `str.splitlines()` splitting on Unicode line separators; `fieldmap.get(key, "")`,
  whose default is the empty header's key; one `str.replace` per variable in turn.
- **Why not caught:** every test fed hand-written inputs of the shapes the author had in mind; none
  generated a long cell, an empty header, a stray CR or a value holding braces.
- **Fix:** the parser reads with `newline=""` and a limit raised for that read only, catches
  `RecursionError`, and splits NDJSON on `"\n"`; a missing hire column reads as blank; both renderers
  substitute in one pass. The six properties now pass as regression tests.
- **Prevention:** the property files run in every suite (deterministic `suite` profile), and CI's
  `fuzz` job drives them with Atheris; `tests/test_fuzz_targets.py` keeps every property fuzzable.
  On Python 3.10 the `csv` module still refuses a NUL byte (3.11 dropped that) — GAM doesn't print
  one, so the properties leave NUL out on 3.10 rather than pin it. And the PR's py3.10 legs went red
  on the tests' own CSV: 3.10's `csv.writer` leaves a lone CR unquoted when lines end in LF (3.11+
  quote it), so the generated input — not the parser — was malformed there. The properties now write
  their CSV with a small writer that quotes the same on every Python. Lesson: a generator built on the
  stdlib inherits its version quirks — run new properties on the floor Python (3.10) before pushing
  (`docker run --platform linux/amd64 python:3.10-slim`, bootstrapping `requirements/pip.txt` first).
