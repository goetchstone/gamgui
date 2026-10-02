# 2026-09-25 — A non-numeric page, size or desc in the Users URL returned a raw 422

- **Symptom:** (review 2: R6) `/users?page=abc&size=x` — a hand-edited or cut-off address — answered
  with FastAPI's JSON 422 instead of the list; `/users/table` did the same.
- **Cause:** `users_page` and `users_table` typed `page`, `desc` and `size` as `int`, so FastAPI refused
  a non-number before `_list_state` — written to fall back on any bad value — ever ran. Only the detail
  link's `back=` query went through the lenient path.
- **Why not caught:** the URL-state tests fed unknown *names* (a bogus sort, an unlisted size) that
  parse as the declared type; none fed a non-number.
- **Fix:** both routes take every view value (and `refresh`) as text and let `_list_state` coerce it.
- **Prevention:** `tests/test_users_list.py::test_a_garbled_view_in_the_url_opens_the_default_view`
  (both routes). Offline-only by nature — no GAM call involved.
