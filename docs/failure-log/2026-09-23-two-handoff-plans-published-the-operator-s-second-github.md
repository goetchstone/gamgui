# 2026-09-23 — Two handoff plans published the operator's second GitHub account

- **Symptom:** a review (F28) found the push instruction in `docs/plans/2026-09-18-…` and
  `2026-09-22-…` switching `gh` between two named accounts, the second of them the operator's other
  GitHub account, in this public repo since 2026-09-18. This branch edited both files' headers and
  left the line in place.
- **Cause:** a session-specific push command was pasted verbatim into handoff plans, which are
  written for the next session but are tracked, public files.
- **Why not caught:** the local private-term tripwire (`.claude/private-terms`) lists company and
  path terms, not account handles; no test looked at tracked docs for account details.
- **Fix:** both plans say "push as the repo owner"; no handle, no switch command. Git history keeps
  the old line (rewriting it was not proposed).
- **Prevention:** `tests/test_polish.py::test_no_tracked_file_names_a_github_account_to_switch_to`
  fails on any tracked text file with a `gh auth switch` that names an account. Adding the handle to
  the gitignored `.claude/private-terms` is the operator's call.
