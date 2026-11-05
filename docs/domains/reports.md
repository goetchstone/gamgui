# Domain: Reports / insights

**One line:** Read-only directory-insight screen ("stuff the Admin Console buries") — buckets the cached user list into security/activity/completeness findings, plus a lazy per-user storage & mail usage report and, when asked, every account's connected apps and AI agents.

**Owns invariant(s):** none directly (it is read-only; leans on inv. 1 argv-only, inv. 3 only-reads-run, inv. 9 bounded output).
**Enforcement home:** `tests/test_reports.py` (pure bucketing + usage parsing); `tests/test_command_contract.py` guards the `"report users"` grammar token. No route/live test.

## Files
- `gamgui/core/reports.py` — pure logic: `build_reports()`, `parse_usage()`, dataclasses `Report`/`UsageRow`, constants `REPORT_FIELDS`, `USAGE_PARAMS`, `INACTIVE_DAYS`. No GAM calls.
- `gamgui/web/routes/reports.py` — two GET routes: `/reports` (page) and `/reports/usage` (lazy partial).
- `gamgui/core/connectors/gam_connector.py` — `usage_report()` (the only GAM call) + `_csv_from()` helper.
- `gamgui/core/gam/commands.py` — `GAMCommands.report_users(date, params)` argv builder; `CACHE_FIELDS`.
- `gamgui/web/templates/reports.html`, `_usage_report.html` — render (autoescaped text, no `tojson`, inv. 8 not in play).
- `tests/test_reports.py` — unit tests for the pure functions.
- `gamgui/core/connected_apps.py` — pure: grants grouped by app, reach and kinds of broad access, `Summary` and its sentences. Ported from SwiftGamGui's `GamEngine/ConnectedApps.swift`.
- `gamgui/web/templates/_connected_apps.html`, `tests/test_connected_apps.py`, `tests/fixtures/tokens.csv` — the panel, its tests, the mock's grants.

## How it works
`/reports` pulls the shared user cache via `st.users(stale_ok=True)` — past the 5-min TTL it renders the old list while one refresh runs behind it, and the header's "Directory as of … · Refresh" (`/reports?refresh=1`, a forced re-read) makes that visible (plan U12b) (one `gam print users` with `CACHE_FIELDS`, a superset of `REPORT_FIELDS`) and calls `build_reports(users)`, which loops once and buckets **active** users into 9 `Report`s: `no_2sv`, `inactive`, `admins`, `no_recovery`, `suspended`, plus directory-completeness `no_title`/`no_department`/`no_phone`/`no_location`. Suspended users `continue` early so they only land in the `suspended` bucket. `_parse_dt()` normalizes `lastLoginTime` (a `None`/unparseable/old time => inactive). `/reports/usage` is loaded separately (slower): the connector's `usage_report()` runs `report users date <D> parameters <p1,p2,...>` and, because Reports-API data lags ~2-3 days, walks back `date = today-2 ... today-7` (default `max_lookback=6`) until a date returns rows; `_csv_from()` strips GAM's leading progress line, `parse_usage()` converts MB->GB and sorts by quota desc, and the route caps at `[:25]`.

## Invariants & the failure history
- **Read-only only (inv. 3):** `report users` is a usage read; nothing here mutates, so no guard/`ChangePreview` path. Do not add a write here — it would need the chokepoint (inv. 2).
- **argv-only (inv. 1):** `report_users` puts `params` as one comma-joined argv element; never shell-spliced.
- **Bounded output (inv. 9 / scale memory):** usage is truncated to 25 rows in the route; the whole page rides the in-memory user cache (added in `f7ab17b` to scale the Users list to tens of thousands) rather than re-querying.
- History: screen introduced in `8ad3047`; storage/mail usage in `b241913`; directory-completeness buckets in `b30eeb6`; `no_location` in `4a2c274`. `convertmbtogb` is intentionally **not** passed — GB is computed in `parse_usage` (`round(mb/1024,1)`), so the builder stays MB.

- **The usage date is the rows' own (failure log 2026-11-04).** GAM walks back to the newest day with data by itself, so `usage_report` reads the day from the rows' `date` column rather than naming the day it asked for.

## Connected apps and agents
A check of the whole tenant. `gam print tokens` is one Directory API call per account, suspended accounts aside (GAM's default entity, read in its source at 7.50.00), so:
- **It reads only when asked.** `POST /reports/connected-apps/read` starts a background job (kind `connected_apps`, no Stop: one subprocess, no loop) and a second press joins the first. Loading Reports runs nothing. `tests/test_write_routes_guarded.py` lists the route as local: it reads, and keeps the result in memory.
- **It's kept in memory, for one connector.** `AppState.connected_apps` is `(apps, when)`. `activate()` drops it, and a read that ends after a switch isn't kept, so one tenant's apps can't show on another. Quitting forgets it.
- **Bounded (inv. 9).** The poll is a spinner. The finished panel renders once, naming six people per app, then "and N more". A failed read again leaves the last read showing.
- **An app's name is its maker's words**, and so can be an attacker's. It stays autoescaped, and AI is judged by whole words of the name alone: say "by name", never "is AI".
- **One account can't sink the read.** GAM's `entityUnknownWarning` (an account deleted mid-run, say) lets it carry on, write the CSV and exit 56, or 73 outside the primary domain. `GAMConnector.connected_apps` keeps those rows and marks the read incomplete, and the panel says so. Any other exit, or a CSV with no rows, fails whole with Try again: "no apps" there would be a false all-clear. Reading the kept CSV happens off the event loop.
- **It can run out of time.** GAM reads one account at a time and writes its CSV only at the end, so `DOMAIN_WIDE_TIMEOUT` (an hour) keeps nothing past several thousand accounts, and the panel says trying again won't help. Reading a large domain would need batches, by OU say: not built.
- **A read that outlives its connection** says "Not kept" in the tray instead of "Done".
- **Announced only when asked.** The panel arrives by htmx after the page, so anything marked `data-announce` would be spoken on every visit. Only a poll or the press that started the read marks it.
- Not ported: the on-device model's wording. Per person (Incident's Check Account) comes with plan E1.

## Gotchas / mock-lies traps
`tests/fixtures/mock_gam.sh` matches only `report users` and dumps a **fixed 3-row CSV, ignoring `date` and `parameters`**. So live-untested here:
- The **lookback loop** in `usage_report` — the mock returns data on the first date, so the "recent dates lag, walk back" logic and the empty `{"date":"","rows":[]}` fallback never run in tests.
- A **typo'd parameter name** in `USAGE_PARAMS` still passes green (the mock never reads what you asked for). Verify names against the vendored grammar `report users ... [(fields|parameters <String>)]` in `gamgui/resources/gam7/GamCommands.txt:4781`.
- `parse_usage` keys by column **name**, so the mock's column order (which differs from `USAGE_PARAMS`) doesn't catch an ordering bug — real GAM also keys by header, so that's fine, but don't rely on the mock to prove field wiring.

## Testing / live-verification status
Connected apps: offline only, and not yet live in either app. `scripts/acceptance.py` runs it. Its live check: the apps should match the Admin console's Security → API controls → App access control (Accessed apps), and the summary's counts the list beneath it.

`.venv/bin/python -m pytest -q tests/test_reports.py` covers bucketing, suspended exclusion, never-logged-in=>inactive, and usage sort/GB conversion — all pure, offline. `test_command_contract.py` fails if a GAM bump renames a word the `report users` builder emits. The routes render against the mock (`test_users_web.py` `test_reports_page_renders` / `test_usage_report_renders` / `test_reports_requires_connection`, `test_polish.py` `test_reports_page_renders_with_mock_fixtures`). **Not covered by any test:** the connector `usage_report()` date-walk against real lag, and empty-data behavior — these need a real tenant (`report users` is read-only, so it's safe to run under `scripts/acceptance.py`).

## To do common tasks here
- **Add a directory-insight bucket:** append a list + condition in `build_reports()` (`gamgui/core/reports.py`), add its `Report(...)` to the returned list, ensure its source field is in `CACHE_FIELDS`/`REPORT_FIELDS` and on `GAMUser` (`core/gam/models.py`), then add a case to `tests/test_reports.py`; the card loop in `reports.html` is already generic.
- **Add a usage parameter:** add the `service:metric` string to `USAGE_PARAMS`, a field on `UsageRow` + its `_int(...)` in `parse_usage`, a column in `_usage_report.html`, and — because the mock won't catch a bad name — confirm the parameter exists in the vendored grammar and run it live once.
- **Change the inactivity window:** `INACTIVE_DAYS` in `core/reports.py` (threaded through as `inactive_days`).
