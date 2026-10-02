# 2026-09-15 — Calendar picker showed only the ID, not the name

- **Symptom:** Searching shared calendars in the role editor showed the opaque
  calendar id (`c_…@group.calendar.google.com`) but not the human-readable name,
  so the operator couldn't tell which calendar they were adding.
- **Cause:** In `_onboard_picker.html` the result row was a flex row with the
  name span `truncate` and the id span `shrink-0`. A long calendar id (~55 chars)
  refused to shrink and consumed the row, truncating the name to nothing. The
  data was fine — every indexed calendar had a name.
- **Why not caught:** the mock-backed browser test had **no calendar index
  built**, so the calendar picker only ever rendered the "no index yet" hint —
  real results with long ids were never displayed. The group picker was tested,
  but group ids are short emails that don't trigger the squeeze. (A "the mock
  lies"-adjacent gap: the offline preview didn't exercise the real data shape.)
- **Fix:** stack the result as name-over-id (each `truncate`s independently), so
  the name is always the prominent line (commit follows).
- **Prevention:** verify a picker/list with the *long* id shape, not just the
  short one; and seed the preview's calendar index with realistic long-id rows.
  Flag: a flex `shrink-0` beside `truncate` lets long content evict the label.
