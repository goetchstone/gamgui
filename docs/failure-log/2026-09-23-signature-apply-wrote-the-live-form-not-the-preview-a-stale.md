# 2026-09-23 — Signature Apply wrote the live form, not the preview: a stale "Apply to 1 user" overwrote the company

- **Symptom:** a review (F16/F19) previewed one user, switched Scope to Whole company without
  previewing again, and clicked the still-visible "Apply to 1 user". The dialog said 1 user; every
  active mailbox's signature was overwritten, with no backup. Switching "Which" to another person
  or editing the template after Preview likewise wrote what nobody had previewed.
- **Cause:** the Apply button posts `hx-include="#sig-form"` — the live editor — and `/signatures/apply`
  re-resolved the scope and re-read the template from that form. Its only check was `confirmed=1`
  (which the stale button carries) plus the typed count, and that only above 25 people. Nothing
  cleared the preview when the form changed.
- **Why not caught:** the apply tests posted the same form they had previewed; no test edited the
  form between Preview and Apply. Offboarding had just been fixed for this exact class, route by
  route, and the other confirm steps were not swept.
- **Fix:** the preview holds its template and resolved people under a single-use token
  (`web/previews.py`, shared with offboarding); Apply writes exactly those, and refuses a used,
  expired or missing token or a form whose scope, which-value or template changed ("preview
  again"). The typed count is the previewed one; the scope is never re-resolved at Apply.
- **Prevention:** `test_signatures_apply_runs_only_what_was_previewed` (widened scope, another
  person, edited template → zero writes), `test_signatures_apply_is_single_use`,
  `test_signatures_apply_writes_the_previewed_people_and_asks_their_count`. Offline only: the write
  itself (`set signature`) is confirmed live; the flow change touches no GAM argv.
