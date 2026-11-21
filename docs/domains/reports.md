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
- `gamgui/core/mail_review.py` — pure: accounts for every account the two domain-wide mail reads listed (`account_for`), forwarding that is on, delegates. `_mail_review.html`, `tests/test_mail_review.py` (see "Forwarding & mail delegates" below).
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
- **Add a named report:** a `NamedReport` in `NAMED_REPORTS` with its syntax up to the first option, and the token and scope
  from GAM's glapi tables (not a guess); `tests/test_named_reports.py` checks it is one runnable read.

## More reports (roadmap 3.1)
Nine domain-wide reads the Builder already runs, named on the Reports page (`core/reports.py` `NAMED_REPORTS`): licences,
shared drives, their permissions and organizers, org units, mobile devices, group memberships, data transfers and Google's
alerts. No new parser and no new route: each row links to `/builder?cid=<id>`, which opens that command's form (the Builder
page's `cid`; an id the catalog doesn't hold opens none), where it runs, filters and exports like any read.
- **Named by syntax, not `raw.N`.** The ids move with each GAM bump, so `named_report_command` matches the command's syntax
  up to its first option and wants exactly one runnable read; `test_every_named_report_is_one_read` fails a bump that renames
  one or makes it ambiguous, and the page says "this GAM version has no single command for it" rather than open another.
- **The access each needs, from GAM's own tables** (gamlib glapi, read statically from the vendored 7.52.03 build):
  licences, org units, mobile devices, group memberships and data transfers use the **admin token** (`_CLIENT_SCOPES`, picked
  when the admin token is created); shared drives and alerts use the **service account** (`_SVCACCT_SCOPES`). For an
  admin-token read the row says whether `oauth2.txt` records the scope (`admin_scope_held`: its `.readonly` form counts; a
  token whose scopes can't be read is unknown, never "held"). Delegation can only be seen by GAM's service-account check, so a
  service-account row names its scope and says Setup lists it. Drive was already delegated; the Alert Center's `apps.alerts`
  is offered beside the devices scope (`setup.OPTIONAL_DWD_SCOPES`), never asked of verify, so only that read is refused
  without it.
- **Not run on a real domain yet.** The mock models none of the nine (it fails anything it doesn't handle); check each by eye
  on its first live run.

## Inactive → suspend (roadmap 3.3, D3)
The one write on this screen. The Inactive card's **Suspend these…** posts `/reports/inactive/preview`, which:
- **reads the directory again** (`st.users(force=True)`): the card counted a cached list, and the people a suspend acts on
  are the ones this read found (`reports.inactive_to_suspend`), held under a single-use token (flow `suspend_inactive`,
  keyed on the window the preview used, posted back as `days`: the card posts none, which is 90; an audit run posts its
  own, one of `INACTIVE_WINDOWS`). Apply suspends exactly that held list, never a rebuilt one
  (`test_someone_who_went_inactive_since_the_preview_isnt_suspended`);
- **flags new hires**: Inactive includes accounts that never signed in, so anyone made inside the window, or when isn't
  known, is marked *new* and the list opens. `creationTime` is in `CACHE_FIELDS` for this (`GAMUser.creation_time`);
- **leaves the connected admin out by name**, matched by Google account ID as well as address (`connector.connected_admin`).
  The ID match needs the read to ask for `id` (`CACHE_FIELDS`; the API returns only what's named, failure-log 2026-11-19).
  An admin GamGUI can't confirm offers nothing. The chokepoint's own refusal (`cut_off_refusal` in `_run_write`) still holds
  for every write.

`/reports/inactive/apply` takes the token, then `guard.enforce(plan_suspend(held), confirm_step=True,
typed_count_above=COUNT_CONFIRM_ABOVE)`: ten or more need `confirm` typed (destructive and bulk), more than 25 the count
too. The job (`suspend_inactive`, stoppable) runs `bulk.suspend_users`: `plan_suspend` → `apply` → `_run_write` per account,
stopping at an account-wide failure (`stop_reason`); each accepted suspend is patched into the cache, and any failure drops
the cached list (a failed write's outcome isn't known). GATED in `tests/test_write_routes_guarded.py`; tests in
`tests/test_inactive_suspend.py`. **Not run on a real domain yet:** suspend itself is "not yet" in the README.

## No 2SV / No recovery → a reminder email (roadmap 3.3, D3)
The fix is the person's to make, so these two cards each carry **Email them a reminder…**: a form with a subject and a
message, starting from `reports.REMINDERS` (plain words and a link to the person's Google Security page). **Preview** posts
`/reports/remind/preview`, which reads the directory again (`st.users(force=True)`), lists the bucket's people as that read
found them (`reports.to_remind`), and holds them under a single-use token keyed on the bucket and the exact text (flow
`remind_bucket`). **Send** posts the card's live form with the token (`/reports/remind/apply`), so text edited since the
preview is refused ("The message changed after the preview"), never sent. The guard needs `confirmed=1`, and the count
typed past 25 (`COUNT_CONFIRM_ABOVE`). The job (`remind`, stoppable; the bucket rides on `job.bucket` for the panel and the
jobs tray) runs `bulk.send_reminders`: one `sendemail` per person through `_run_write` (`send_reminder_email`, audited),
from the admin GamGUI is connected as. The operator's text becomes HTML only by escaping it and keeping its line breaks
(`reminder_html`): nothing they type is markup. GATED in the tripwire; tests in `tests/test_remind_buckets.py`. **Not run on
a real domain yet:** the same `sendemail` as onboarding's confirmed welcome email, but this route hasn't sent one.

## The completeness buckets as sheets to fill in (roadmap 3.3, D3)
No title, department, phone or location: the fix is data only the organisation has, so each of those cards (when it lists
anyone) links to `/reports/fill-in.csv?key=<bucket>`, a `download` link (the app window would show a CSV as a page,
`tests/test_app_window_downloads.py`). The sheet is the card's own people, from the same cached directory, by address, in
`FILL_IN_COLUMNS`: `email`, `name` (for whoever fills it in), then `title`, `department`, `phone`, `location`, `org_unit`,
the columns bulk apply from CSV (roadmap 4.6) reads back by name. Every value goes through `csv_safe`. Only those four
buckets download (400 otherwise), and a directory that couldn't be read answers in plain text (502), never a sheet holding
only its titles, which would read as "everyone has one" (invariant 10). Tests: `tests/test_fill_in_csv.py`.

## Forwarding & mail delegates (roadmap 3.2)
Reports' panel for every active account's forwarding that is on, and every mail delegate: `all users print forward
enabledonly` and `all users print delegates shownames` (`GAMCommands.all_users_print_forward/_delegates`; neither takes
`formatjson`, the old trap). One Gmail call per account each, so like connected apps it reads only when asked, as a
background job (`mail_review`, unstoppable: one read, no "between targets"), kept per connector (`AppState.mail_review`,
dropped on a switch, and not kept when the connection changed mid-read). Each row opens the person.
- **Every account GAM listed is accounted for** (`mail_review.account_for`). Read statically from the vendored 7.52.03 build
  (printShowForward, printShowDelegates): in print mode GAM announces each account on stderr before reading it ("Getting
  Forward Enabled for <u> (i/N)", "Getting all Delegates for <u> (i/N)"), warns by name for one it can't read ("User: <u>,
  Gmail Service/App not enabled"), and skips a refused token without a word (`if not gmail: continue`). So an account is
  read, unread with a reason, or skipped (counted from "Got N Users" and the counter, as it can't be named). The panel says
  "read X of N", names the unread, counts the skipped, and only says "none of the accounts read forwards" when some were.
- **The connector runs both through `run_any_exit`** (`_mail_read`): the per-account exits (`PER_USER_RCS`) are read line by
  line; any other raises. A run that announced no account, or wrote no table (GAM writes the titles even with no row), is
  `Incomplete`, said in fixed words (`unread_why`), never "none".
- **Not covered, and the panel says so:** Gmail filters that forward, which `forward off` doesn't stop either; a person's
  filters are on their page.
- **The mock** answers both as GAM does (`all users print forward|delegates`): the active users, each "Getting" line, Gmail
  off by name (`GAM_MOCK_STATE/nogmail/<u>`, exit 73), the silent skip (`quiet/<u>`), the titles even with no row.
- **Not run on a real domain yet.** Read only; check the counts against the Admin console on its first run.

## Audit runs (plan F3, first half)
`/reports/runs` (linked from Reports): ready-made lists of reads, run when the operator asks. This half has two;
shared drive permissions, Classroom and admin tokens come next.
- **Files.** `core/audit_runs.py` (pure: `RUNS`, each step's argv `step_argv`, the joins and flags), `GAMConnector.audit_read`
  (the one read path), `web/routes/audit_runs.py` (page, `/start`, `/status`, `/table.csv`), `_audit_run.html`,
  `audit_runs.html`, `tests/test_audit_runs.py`, `tests/fixtures/licenses.csv`.
- **A run never writes.** Each step is a `print` read without `todrive` (`audit_read` refuses anything else before GAM runs)
  and leaves F1's record: action `read`, `extra` = `command` (`<run>/<step>`, e.g. `license_usage/licenses`), `origin`
  `audit_run`, `domain`, and `rows` or `error`, never the output. `/reports/runs/start` is LOCAL_ONLY in the guard tripwire.
  Inactive users' **Suspend these…** posts Reports' own `/reports/inactive/preview` with the run's `days` (one of
  `INACTIVE_WINDOWS`; none posted is the card's 90): the directory is read again for that window and the token is keyed on
  it, so Apply with another window is refused.
- **The job.** Kind `audit_run`, the run's key in `job.bucket`, one step per `job.record`. Stop is checked before each step:
  the step in flight finishes, the rest are `not_run`. A failed step is `unknown` and the run goes on. One of each run at a
  time (a second press joins it). Kept per connector in `AppState.audit_runs` (dropped on `activate`); a run that ends
  after a switch isn't kept.
- **Unknown, never none (invariant 10).** Any exit but 0 is unknown: `print users` that loses a page warns, writes the rows
  it had and exits 50 (doPrintUsers), and that partial table is never used. Output without the step's key column
  (`primaryEmail`, `userId`) is unknown. A directory with no account, or no seat at all, is unknown: with no product named,
  `print licenses` passes over a product Google refuses without a word or a code (doPrintLicenses, `suppressErrorMsg`).
  Every table says which step it needs and why it's unknown; its CSV answers 409 in words, never a sheet of titles.
- **Bounded (invariant 9).** The poll draws the steps only. A finished table draws `ROWS_SHOWN` (100) rows; its CSV
  (`download` link, formula-safe) is the whole of it. Each step's table is kept whole in memory for the run.
- **Joins, on Google's fields** (cited in the module docstring): Inactive is `build_reports`' bucket for the window, never
  signed in first; `new account` when `creationTime` is inside the window or missing, `admin` for `isAdmin` or
  `isDelegatedAdmin`. License usage joins `userId` (lowercased by GAM; Google: "the user's current primary email address")
  to `primaryEmail`, lowercased: seats per SKU, seats held by a `suspended` account, seats on an address the directory
  read didn't list. A directory that's unknown leaves the per-SKU counts with "unknown" in the last two columns.
- **The mock.** `print licenses` (bare only) prints `licenses.csv`; `GAM_MOCK_STATE/nolicensing` exits 15 as a token without
  the License Manager scope; `licenses_silent` is titles alone, exit 0; `licenses_partial` drops one product, exit 0 (a
  clean-looking partial read, so every license table carries the caveat). `print users` with `directory_fails` writes one
  row and exits 50.
- **Not run on a real domain yet.** `print licenses` has never run live: check the counts against Billing → Licenses on
  its first run. The directory read is the cache's own, which has.
