# 2026-09-23 — The signature designer opened on "Whole company"

- **Symptom:** a review (plan U6) found the designer's scope defaulting to "Whole company", and the
  route defaults (`scope_type="company"`) agreeing: Preview → Apply → one browser `confirm()`
  overwrote every mailbox's signature, with no backup — the first apply of a new template was a
  company-wide one unless the operator remembered to switch scope.
- **Cause:** the scope list was ordered for the finished roll-out, not the first try; the guard treats
  a signature as a LOW write, so any count needed only the Apply click.
- **Why not caught:** no test looked at the default; the guard had no rule between "a click" and the
  destructive typed word.
- **Fix:** the page and both routes default to "Specific user (test)"; above
  `guard.COUNT_CONFIRM_ABOVE` (25) people the operator types the count, and `/signatures/apply`
  checks it server-side against the count it resolves at apply time (`guard.enforce(...,
  typed_count_above=)`).
- **Prevention:** `test_signatures_designer_defaults_to_one_user`,
  `test_signatures_apply_over_the_threshold_needs_the_count_typed`, and the guard unit test
  `test_enforce_typed_count_only_above_the_opted_in_threshold`.
