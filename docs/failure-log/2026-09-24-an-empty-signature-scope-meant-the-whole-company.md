# 2026-09-24 — An empty signature scope meant the whole company

- **Symptom:** `core/signatures.match_scope` returned every active user for an OU, department,
  location or group scope with no value — only "Specific user" was guarded. A tenant with no
  departments leaves the "Which" select empty and hidden, so Preview on "Department" resolved to the
  whole company (the count showed it, and more than 25 needs the typed count, but the scope was wrong).
- **Cause:** the fall-through `return active` at the end of `match_scope`.
- **Why not caught:** tests covered each scope *with* a value.
- **Fix:** only `company` returns everyone; any other scope without a value, or an unknown scope,
  matches nobody. Test `test_match_scope_with_no_value_matches_nobody` (fails on the old code). Also
  replaced two real store-town names in `tests/test_signatures.py`/`test_reports.py` with generic ones.
- **Prevention:** a scope resolver fails closed: "everyone" must be asked for by name.
