# 2026-09-24 — A per-user 403 could pass as a tolerable "not found", and GAM's counter read as a status code

- **Symptom:** found reading `core/gam/errors.py` (plan Q12's follow-up), not seen live. A per-user
  stderr line carrying both a 403/forbidden token and "not found" classified `NOT_FOUND` — the kind the
  offboarding calendar sweep tolerates — so a real refusal could pass as a benign notice. Checking that
  the fix changed no realistic classification turned up a second fault: GAM ends each per-entity line
  of a multi-user run with a counter, " (403/1200)", which `\b403\b`, `\b404\b` and `\b429\b` read as a
  status code. On a domain past 404 users, a real error on the 404th became a tolerable `NOT_FOUND`;
  a benign notice on the 429th became `RATE_LIMITED` and failed the sweep.
- **Cause:** first-match-wins with the not-found pattern ahead of the 403 one; and the status-code
  patterns matched bare numbers anywhere in the line.
- **Why not caught:** every hand-written sample used small counters ("(4/120)", "(3/3)") and no line
  mixed the two kinds.
- **Fix:** the 403 pattern (and the own-ACL one ahead of it) moved before not-found; `_classify_line`
  strips GAM's `(i/count)` counter before any pattern runs. Tests
  `test_a_refusal_that_also_says_not_found_is_a_refusal` and `test_gams_entity_counter_is_not_a_status_code`
  (three of their cases fail on the old order).
- **Prevention:** those tests. The mock never emits a counter past 4, and no real GAM stderr from a
  large domain has been captured: the counter's shape is the one the existing samples and the mock
  use, not observed live.
