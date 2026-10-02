# 2026-09-25 — The onboarding email check backtracked quadratically (CodeQL py/polynomial-redos)

- **Symptom:** GitHub code scanning flagged `gamgui/core/onboarding.py`'s `_EMAIL_RE` (open since
  2026-09-22). Reproduced: `"!@!." + "!." * 20000` took 2.5 s, growing quadratically; at 200,000 it ran
  into the test timeout. The same check also let `a@example..com`, `a@.example.com` and a trailing dot
  through.
- **Cause:** `[^@\s,]+\.[^@\s,]+` let the domain match each of its dots in two places.
- **Why not caught:** the address tests used ordinary addresses; nobody read the open CodeQL alert.
  The input only ever comes from the operator, so the practical risk was a hung request, not an attack.
- **Fix:** domain labels exclude "." (`[^@\s,.]+(?:\.[^@\s,.]+)+`), so each dot has one place to match.
  Tests `test_looks_like_email_is_linear_on_a_hostile_address` and `test_looks_like_email_cases` (both fail
  on the old pattern).
- **Prevention:** check the repo's open code-scanning alerts when reviewing a branch
  (`gh api repos/<owner>/<repo>/code-scanning/alerts`); a regex with two adjacent classes that can both
  match the separator is the shape to avoid.
