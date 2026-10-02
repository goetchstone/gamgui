# 2026-09-25 — a preview or Builder read that straddled a tenant switch landed on the new tenant

- **Symptom:** (re-check of review 2: R5, R11, finding G1) two races left by the tenant-switch fix.
  A preview read the directory on tenant A, a switch to B landed before it held its values, and the
  preview was bound to B — so its confirm step then ran A's people (or a bare name GAM completes with
  B's domain) through B's credentials. And a Builder read in flight across a switch wrote A's rows into
  `builder_last_result` after `AppState.activate` had cleared it, so B's table and CSV served A's rows.
- **Cause:** `Previews.hold` stamped the tenant active *when it held*, not the one the request's reads
  were made on; `activate` clears the store first, so the hold afterwards was a fresh B-bound entry that
  nothing refused. The Builder's `_render_read` stored whatever came back with no check at all.
- **Why not caught:** `tests/test_tenant_switch.py` switched only *between* requests (preview, switch,
  confirm) — never between a request's read and its hold — and the read test covered the user and group
  caches, not the Builder's result.
- **Fix:** every preview route captures `AppState.tenant_key()` (a switch count and the domain, so a
  switch away and back still counts) before its first read and passes it as `hold(..., tenant=)`; a
  preview whose request started before a switch is refused when taken ("The active domain changed after
  the preview"). `/builder/run` captures it too and `_render_read` neither keeps nor shows a result when
  it no longer matches ("The active domain changed while this ran").
- **Prevention:** `test_a_preview_whose_directory_read_straddles_a_switch_runs_nothing` and
  `test_a_builder_read_that_straddles_a_switch_keeps_and_shows_nothing` switch inside the read; an AST
  tripwire, `test_every_route_hold_passes_the_tenant_its_request_started_on`, fails when a route's
  `hold(` call omits `tenant=`. Offline only — no live tenant switch was exercised.
