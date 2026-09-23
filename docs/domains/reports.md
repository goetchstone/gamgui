# Domain: Reports / insights

**One line:** Read-only directory-insight screen ("stuff the Admin Console buries") — buckets the cached user list into security/activity/completeness findings, plus a lazy per-user storage & mail usage report.

**Owns invariant(s):** none directly (it is read-only; leans on inv. 1 argv-only, inv. 3 only-reads-run, inv. 9 bounded output).
**Enforcement home:** `tests/test_reports.py` (pure bucketing + usage parsing); `tests/test_command_contract.py` guards the `"report users"` grammar token. No route/live test.

## Files
- `gamgui/core/reports.py` — pure logic: `build_reports()`, `parse_usage()`, dataclasses `Report`/`UsageRow`, constants `REPORT_FIELDS`, `USAGE_PARAMS`, `INACTIVE_DAYS`. No GAM calls.
- `gamgui/web/routes/reports.py` — two GET routes: `/reports` (page) and `/reports/usage` (lazy partial).
- `gamgui/core/connectors/gam_connector.py` — `usage_report()` (the only GAM call) + `_csv_from()` helper.
- `gamgui/core/gam/commands.py` — `GAMCommands.report_users(date, params)` argv builder; `CACHE_FIELDS`.
- `gamgui/web/templates/reports.html`, `_usage_report.html` — render (autoescaped text, no `tojson`, inv. 8 not in play).
- `tests/test_reports.py` — unit tests for the pure functions.

## How it works
`/reports` pulls the shared user cache via `st.users()` (one `gam print users` with `CACHE_FIELDS`, a superset of `REPORT_FIELDS`) and calls `build_reports(users)`, which loops once and buckets **active** users into 9 `Report`s: `no_2sv`, `inactive`, `admins`, `no_recovery`, `suspended`, plus directory-completeness `no_title`/`no_department`/`no_phone`/`no_location`. Suspended users `continue` early so they only land in the `suspended` bucket. `_parse_dt()` normalizes `lastLoginTime` (a `None`/unparseable/old time => inactive). `/reports/usage` is loaded separately (slower): the connector's `usage_report()` runs `report users date <D> parameters <p1,p2,...>` and, because Reports-API data lags ~2-3 days, walks back `date = today-2 ... today-7` (default `max_lookback=6`) until a date returns rows; `_csv_from()` strips GAM's leading progress line, `parse_usage()` converts MB->GB and sorts by quota desc, and the route caps at `[:25]`.

## Invariants & the failure history
- **Read-only only (inv. 3):** `report users` is a usage read; nothing here mutates, so no guard/`ChangePreview` path. Do not add a write here — it would need the chokepoint (inv. 2).
- **argv-only (inv. 1):** `report_users` puts `params` as one comma-joined argv element; never shell-spliced.
- **Bounded output (inv. 9 / scale memory):** usage is truncated to 25 rows in the route; the whole page rides the in-memory user cache (added in `f7ab17b` to scale the Users list to tens of thousands) rather than re-querying.
- History: screen introduced in `8ad3047`; storage/mail usage in `b241913`; directory-completeness buckets in `b30eeb6`; `no_location` in `4a2c274`. `convertmbtogb` is intentionally **not** passed — GB is computed in `parse_usage` (`round(mb/1024,1)`), so the builder stays MB.

## Gotchas / mock-lies traps
`tests/fixtures/mock_gam.sh` matches only `report users` and dumps a **fixed 3-row CSV, ignoring `date` and `parameters`**. So live-untested here:
- The **lookback loop** in `usage_report` — the mock returns data on the first date, so the "recent dates lag, walk back" logic and the empty `{"date":"","rows":[]}` fallback never run in tests.
- A **typo'd parameter name** in `USAGE_PARAMS` still passes green (the mock never reads what you asked for). Verify names against the vendored grammar `report users ... [(fields|parameters <String>)]` in `gamgui/resources/gam7/GamCommands.txt:4781`.
- `parse_usage` keys by column **name**, so the mock's column order (which differs from `USAGE_PARAMS`) doesn't catch an ordering bug — real GAM also keys by header, so that's fine, but don't rely on the mock to prove field wiring.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_reports.py` covers bucketing, suspended exclusion, never-logged-in=>inactive, and usage sort/GB conversion — all pure, offline. `test_command_contract.py` fails if a GAM bump renames a word the `report users` builder emits. The routes render against the mock (`test_users_web.py` `test_reports_page_renders` / `test_usage_report_renders` / `test_reports_requires_connection`, `test_polish.py` `test_reports_page_renders_with_mock_fixtures`). **Not covered by any test:** the connector `usage_report()` date-walk against real lag, and empty-data behavior — these need a real tenant (`report users` is read-only, so it's safe to run under `scripts/acceptance.py`).

## To do common tasks here
- **Add a directory-insight bucket:** append a list + condition in `build_reports()` (`gamgui/core/reports.py`), add its `Report(...)` to the returned list, ensure its source field is in `CACHE_FIELDS`/`REPORT_FIELDS` and on `GAMUser` (`core/gam/models.py`), then add a case to `tests/test_reports.py`; the card loop in `reports.html` is already generic.
- **Add a usage parameter:** add the `service:metric` string to `USAGE_PARAMS`, a field on `UsageRow` + its `_int(...)` in `parse_usage`, a column in `_usage_report.html`, and — because the mock won't catch a bad name — confirm the parameter exists in the vendored grammar and run it live once.
- **Change the inactivity window:** `INACTIVE_DAYS` in `core/reports.py` (threaded through as `inactive_days`).
