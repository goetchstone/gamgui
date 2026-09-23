# Domain: Web Screens & Jobs

**One line:** The FastAPI/HTMX screens (Users, Groups, Calendars, plus Lifecycle/Signatures) that render read-only pages and drive GAM mutations, and the tiny in-memory `BatchJob` registry that lets long per-user loops report polled progress instead of hanging the request.

**Owns invariant(s):** #9 (bound anything polled) — every live progress feed keeps a fixed rolling window and caps retained lists so a domain-wide run doesn't grow each 1s poll with the user count.
**Enforcement home:** `tests/test_users_web.py` — all three scale guards live there: `test_run_subscribe_bounds_its_log_at_scale`, `test_apply_job_record_stays_bounded_at_scale`, `test_signatures_apply_final_summary_caps_failed_list` (plus the cross-tenant `test_calendars_search_ignores_other_domains_index`). `tests/test_calendar_index.py` covers the persistent index itself (CRUD, atomic swap, owner-only file, scan), not the #9 guards. No drift-guard/hook — plain pytest.

## Files
- `gamgui/web/jobs.py` — `BatchJob` dataclass + `start_job(jobs, total, keep=10)`; the shared polled-progress record on `AppState.jobs`.
- `gamgui/web/routes/users.py` — list/search/detail + actions (signature, delegate, groups, calendar ACLs, org/role, vacation, suspend via guard, delete, bulk-store job).
- `gamgui/web/routes/calendars.py` — find calendar, ACL detail, event search/delete, secondary-calendar delete, share+subscribe fan-out job, persistent index rebuild job.
- `gamgui/web/routes/groups.py` — drag-and-drop membership board (`/groups`); add/remove via `members_mutate`.
- `gamgui/web/templates/_bulk_apply.html`, `_calendar_subscribe_job.html`, `_calendar_index_job.html` — the self-polling HTMX partials.
- `gamgui/web/routes/signatures.py` — its own richer `ApplyJob`/`record()` (not `BatchJob`); the reference bounding implementation.

## How it works
Routes read from the cached directory (`request.app.state.gamgui.users()`) so pages/searches are instant; mutations are HTMX POSTs that swap a small result region. A long per-user loop (`_run_bulk_store`, `_run_subscribe`, `_build_index`) is fired with `asyncio.create_task`, its handle stashed on `job.task` (strong ref so it isn't GC'd), and the route returns a partial that self-polls a `.../status?job=<id>` endpoint on `hx-trigger="load delay, every 1s"` until `job.finished`, then renders a final panel with no more polling. `start_job` prunes the oldest finished jobs (keep=10) so the registry can't grow forever; `_lifespan` cancels any in-flight `job.task` on shutdown.

## Invariants & the failure history
- **#9, bounded polls.** `_run_subscribe` keeps `del job.log[:-_SUBSCRIBE_LOG_WINDOW]` (12 rows). `ApplyJob` caps `recent` at `_RECENT_WINDOW=12` and `failed` at `_FAILED_SAMPLE_CAP=200` while keeping full counts in `failed_total`. These exist because a domain-wide run once made each 1s poll's HTML grow with the user count.
- **Guarded mutations (invariant #2).** Suspend goes `plan_suspend` → `guard.evaluate` → `conn.apply`; it never bypasses the chokepoint. Calendar delete and user delete re-resolve/re-validate server-side and never trust the client-supplied identity (`_resolve_delete_owner`, exact-case `DELETE` / exact-email confirm).
- **Cross-tenant safety.** `_index_ready` serves search only when `status.domain == audit_domain`; an index built for another tenant is treated as "needs rebuild" (`test_calendars_search_ignores_other_domains_index`).
- **Directory data into attributes (invariant #8):** templates put Google-sourced strings in autoescaped contexts; never `| tojson` in a double-quoted attribute.

## Gotchas / mock-lies traps
- `BatchJob.failed` is **not** capped (only `log` is): `_run_bulk_store`/`_run_subscribe` `.append` it unbounded, and the final `_bulk_apply.html`/`_calendar_subscribe_job.html` `join` the whole list. Live progress stays bounded, but a run where thousands genuinely fail bloats the *final* summary. `ApplyJob` (signatures) is the one that caps this — copy its pattern for any new BatchJob feed.
- **A 200 proves nothing.** Every route answers 200 whether the write worked or not — a failure renders the amber error partial. Assert success with `tests/helpers.py`: `assert_ok_partial(r)` (no amber box), the `gam_calls` fixture + `gam_writes()` (the exact argv the mock received), and the audit record's `ok`. The mock fails any argv it has no handler for, so an unhandled write can no longer "succeed".
- **Never poll a job's status endpoint in a loop under `TestClient`** (that hung CI for 6h, before the fixtures were context-managed). To assert what a route-started job did, `wait_for_job(client, job)` awaits its task on the client's own loop, then render the status once; the signature apply, bulk store, offboarding and builder-sequence route tests do this. Other job routes (calendar share fan-out, index rebuild, bulk onboarding) still only assert the job started, with completion covered by executor tests.
- Share/subscribe, secondary-calendar delete, event delete, org/vacation writes are only exercised against `tests/fixtures/mock_gam.sh`. Per invariant #3 and CLAUDE.md "the mock lies," a mutation is unproven until run against a throwaway real tenant — check `gamgui/resources/gam7/GamCommands.txt`, not memory, for syntax (e.g. formatjson-reject quirks).
- Bare-address ambiguity: an ACL scope like `sales@example.com` may be a person or a group; `_subscribers_for` resolves it against the cached directory first (a known account is always a person) before a member lookup, because "member lookup errored" is a weak signal — mistaking a person for an empty group silently skips the subscribe that is the whole feature.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_users_web.py tests/test_calendar_index.py`. Both use an in-memory vault + `mock_gam.sh` fixtures and a context-managed `TestClient`. Fully offline-proven: rendering, filtering/pagination, guard gating, confirm gates, cross-tenant index filtering, and the bounding invariants. **Not proven offline:** that any calendar/user/vacation/org/subscribe mutation actually succeeds on a live domain, and the polled loops running to completion under real GAM.

## To do common tasks here
- **Add a new polled bulk action:** add a `_run_<x>(job, ...)` background coroutine that appends bounded feed rows (copy `_run_subscribe`'s `del job.log[:-WINDOW]`, or use `ApplyJob` if you also need a capped failed sample); a POST route that calls `start_job` + `asyncio.create_task` and returns a self-polling partial; a `/status` GET; the partial template with `hx-trigger="load delay, every 1s"` that stops when `job.finished`. Add a scale test that runs the coroutine directly and asserts the window length.
- **Add a user/calendar action:** new `@router.post` in `users.py`/`calendars.py` → `_conn(request)` guard → connector call → check `result.ok` → re-render the relevant partial via the shared `_*_partial`/`_detail_ctx` helper; add the connector method + argv builder + curation elsewhere (see `docs/builder-commands.md`).
- **Change a screen's layout:** edit the page/partial under `gamgui/web/templates/`; keep it fitting the fixed 13" window (paginate/contain — see commit `6a1dc11`).
