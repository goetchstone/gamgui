# 2026-11-18 — A signature scope's failed group lookup read as "Applies to 0 users"

- **Symptom:** found by the swallowed-read scan (plan item C), never seen live. Previewing a signature for a group whose
  members couldn't be read (rate-limited, a refused scope, a group that doesn't exist) showed "Applies to 0 users" and
  no Apply button, as if the group were empty.
- **Cause:** `_matched` in `web/routes/signatures.py` caught every exception from `list_group_members` and returned
  `[]`, which the preview counts.
- **Why not caught:** the signature tests scoped by company, OU and a group that reads; none by a group whose read
  fails. Nothing is written on the wrong answer, which is why it never stood out, but the count is invariant 10's
  "never reads as none".
- **Fix:** `_matched` lets the failure through; the preview says "Couldn't read the members of <group>, so who it
  applies to isn't known", with what to do about it.
- **Prevention:** `test_a_group_scope_whose_members_cant_be_read_is_never_zero_people` (a rate limit and a group that
  doesn't exist). The general tripwire is `tests/test_swallowed_reads.py`, which found it and no longer lists it.
