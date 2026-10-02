# 2026-09-25 — the onboarding CSV template sent a sample hire's sign-in details to gmail.com

- **Symptom:** (review 2: R15, R16) the downloadable hire CSV template's sample row had
  `notify` = a gmail.com address. The guide says a `notify` address is where Google emails the new
  hire's sign-in details, so an operator who kept that row and changed only the name and email would
  have sent a real account's sign-in to whoever owns that mailbox. Separately, the mock's mailbox
  row carried an Amazon SES Message-ID whose embedded timestamp matched its Date header exactly —
  pasted from a real message into a public repo.
- **Cause:** example data written with a real, registrable mail provider (and, for the mock, real
  headers with only the sender and recipient swapped) instead of the RFC 2606 `example.*` domains.
  `tests/test_onboarding.py` used gmail.com addresses the same way.
- **Why not caught:** nothing checked the domains in shipped sample data or fixtures; the private-term
  hook blocks named terms, not the shape "an address at a real provider".
- **Fix:** the template's `notify` is `jordan.personal@example.net`, the tests use `example.net`, and
  the mock's Return-Path is a made-up `<fixture-bounce-0001@bounces.vendor.example>`;
  `test_search_messages_runs_and_surfaces_return_path` pins the new local part.
- **Prevention:** `test_the_csv_template_only_uses_reserved_example_domains` (every address in the
  template is at example.com/.net/.org) and `test_example_data_never_uses_a_real_mail_provider`
  (no tracked text file holds an address at gmail, SES, Outlook, iCloud, Yahoo and the like).
