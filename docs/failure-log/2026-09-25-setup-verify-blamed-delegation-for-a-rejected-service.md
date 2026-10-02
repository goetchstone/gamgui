# 2026-09-25 — Setup verify blamed delegation for a rejected service-account key

- **Symptom:** a re-check of the G2 change (verify reads GAM's PASS/FAIL table on a non-zero exit)
  found a rejected service-account key reported as "Domain-Wide Delegation isn't authorized yet — use
  the link below", with no link: GAM's key check fails and it stops before the scope table.
- **Cause:** `_check_result` treated any FAIL row as a delegation failure; only scope rows are.
- **Why not caught:** the mock's `check serviceaccount` only modelled failing scopes, never a failing
  key or clock row.
- **Fix:** a failing non-scope row (system time, private key) is named in the summary with what to do,
  and no delegation link is offered; the mock models a rejected key (`badkey` admins: the key row
  FAILs, exit 1, no scope table). Test
  `test_verify_names_a_rejected_key_instead_of_asking_for_delegation` (fails on the G2 code). Same
  commit round: the calendar access lists' Remove buttons name their row.
- **Prevention:** when parsing a multi-part status table, classify each row before summarising it —
  "any FAIL" is not one failure kind. Unproven live: GAM's exact output for a rejected key.
