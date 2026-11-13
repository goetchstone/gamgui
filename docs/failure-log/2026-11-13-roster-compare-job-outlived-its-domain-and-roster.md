# 2026-11-13 — A compare job outlived its domain and its roster

- **Symptom:** found by the SwiftGamGui session while porting C3 and C4, never seen live. After a tenant switch, Compare
  on the new domain showed the old domain's job still reading and could not start its own until it finished. After a new
  upload, Compare reused the job of the earlier roster, and a failure of that job showed against the new roster. A
  compare running across a switch read the new tenant's Classroom and directory, against the old domain's roster.
- **Cause:** `activate` cleared `roster_compare` (the result) but not `roster_compare_job`. `compare` and
  `compare_status` used whichever job that named, with no check of its domain or roster; `_plan_for`'s SHA-256 check
  guarded the result only. `_compare` read `st.connector` after its first await instead of the connector it started on.
- **Why not caught:** the tests checked that a new upload or a switch drops the result, never what happens to a job still
  running or failed when they do.
- **Fix:** `roster_compare_sha` names the roster a job is of, `activate` clears both, and `_compare_job` returns a job
  only while it is this domain's compare of the roster kept now. `_compare` takes the connector it started on, refuses a
  roster changed as it started, and keeps its result only while it is still the domain's compare of that roster. A
  verifier found Purge left the change list (its students' addresses) in memory, back on reading the same bytes again:
  Purge now drops it, and a compare running through a purge keeps nothing. Also from the Swift review:
  held-back kinds carry `⚠` and an sr-only ", held back", not amber text alone.
- **Prevention:** `test_a_tenant_switch_drops_the_old_domains_compare`,
  `test_a_compare_of_an_earlier_roster_is_neither_waited_on_nor_its_failure_shown`,
  `test_a_compare_that_lost_its_place_keeps_no_result`, `test_held_back_rows_say_so_with_a_mark_and_to_a_screen_reader`,
  `test_purge_drops_the_change_list_so_the_same_bytes_never_bring_it_back`,
  `test_a_purge_during_a_compare_keeps_nothing_from_it`.
  State held for a background job needs the same key as the result it makes: what drops one drops the other.
