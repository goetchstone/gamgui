# Domain: GAM Runner

**One line:** The single subprocess boundary to the `gam` binary — spawns it, materializes/tears
down the ephemeral `GAMCFGDIR` per call, classifies failures into a stable error taxonomy, and
normalizes raw stdout into typed records/models.

**Owns invariant(s):** #1 (argv-only, never a shell — this is the *only* `create_subprocess_exec`)
and the `serialize=True` write-lock. Adjacent to #4 (secrets materialized via `EphemeralConfig`,
owned by `core/secrets/`) and #2 (the mutation chokepoint runs *through* this runner, but lives in
`gam_connector.py`).
**Enforcement home:** `tests/test_runner.py`, `tests/test_errors.py`, `tests/test_parser.py`,
`tests/test_models.py` — all offline against `tests/fixtures/mock_gam.sh`. No drift guard beyond
those; the argv-only property is structural (single `_exec`), not asserted by a lint — but who may
call `_exec` is: `test_command_contract.py::test_gam_is_spawned_only_through_run_authenticated_or_version`
fails on any caller beyond `run_authenticated`'s body and `version`. The env
allowlist and the frozen-app binary lock are tripwired in `test_runner.py`
(`test_the_env_allowlist_is_the_reviewed_one`, `test_gam_inherits_only_the_allowlisted_environment`,
`test_no_dyld_variable_reaches_gam`, `test_binary_override_is_ignored_in_the_packaged_app`).

## Files
- `gamgui/core/gam/runner.py` — `GAMRunner`; `_exec` is the only `create_subprocess_exec`. Locates
  the binary, builds the allowlisted env (`ENV_ALLOWLIST`, plus `MOCK_ENV` outside the `.app`),
  enforces timeout, holds `_write_lock`. `strip_cfgdir_noise` scrubs GAM's per-call config banner.
- `gamgui/core/gam/errors.py` — `GAMError` (`kind` + per-line `kinds`), `GAMErrorKind`,
  `classify_stderr`, ordered `_PATTERNS`, `_SEVERITY`, `_PROGRESS_LINE`, `_REMEDIATION` map.
- `gamgui/core/gam/parser.py` — `parse_records`/`parse_one`; tolerant JSON/NDJSON/CSV normalizer.
- `gamgui/core/gam/models.py` — tolerant dataclasses (`GAMUser`, `GAMGroup`, `GroupMember`,
  `Vacation`, `CalendarACL`, `ResourceCalendar`, `UserCalendar`, `CalendarEvent`) built from parsed
  dicts (or text for `Vacation`).

## How it works
Callers (mostly `core/connectors/gam_connector.py`) build an argv list with a `GAMCommands` static
method and call `runner.run_authenticated(domain, argv, serialize=...)`. That opens an
`EphemeralConfig` context (vault → `0700` dir with `0600` credential files), runs `_exec`, and on
exit writes any refreshed `oauth2.txt` back to the vault and wipes the dir. `_exec` spawns via
`create_subprocess_exec(str(gam_binary), *argv, ...)` with an allowlisted env carrying `GAMCFGDIR`;
a `wait_for` timeout kills the process and raises a `TIMEOUT` `GAMError` whose text says how long it
ran and that it may have done part of its work. The timeout is `DEFAULT_TIMEOUT` (120s) unless the
caller passes one: a domain-wide `all users …` call (the offboarding calendar sweep, the calendar-index
scan) passes `DOMAIN_WIDE_TIMEOUT` (1 h), because GAM walks every user in turn. Non-zero exit →
`GAMError.from_run` (stderr classified line by line; `kind` is the most severe line). Success stdout
is de-noised, then the connector runs it through `parse_records`/`parse_one` and `Model.from_json`.
One side path: `version` (no credentials). `run_in_cfgdir` (any argv against an explicit, persistent
cfgdir, meant for a setup wizard that never used it) is gone: it had no caller in the app, and it was
a door to `_exec` that the chokepoint tripwires couldn't see and that `EphemeralConfig` never wiped.

## Invariants & the failure history
- **argv-only (#1).** Every operator value is one argv element; `_exec` never joins a string or
  invokes a shell. This is the whole point of the single boundary — don't add a second spawn.
- **`gam` gets an allowlisted environment, and the `.app` ignores `GAMGUI_GAM_BINARY`** (2026-09-23).
  `gam` holds all three plaintext credentials, so the launch environment must not steer it. Until
  then `_build_env` copied all of `os.environ` (`DYLD_*`, `PYTHON*`, a parent PyInstaller's `_PYI_*`,
  GAM's own `GAM_CSV_*`/`GAMCFGSECTION` output switches) and the override was honored even when
  frozen — a same-user `launchctl setenv GAMGUI_GAM_BINARY ~/x` would have handed the credentials to
  any binary on the next launch, with no Keychain prompt. Now only `PATH HOME LANG LC_ALL LC_CTYPE
  TMPDIR USER` and the proxy variables (GAM's httplib2 takes its proxy only from the environment)
  pass; `GAMCFGDIR` and `GAM_NO_UPDATE_CHECK` are set by us. The mock's `GAM_MOCK_FIXTURES/REFRESH/
  ARGV_LOG` pass only in a source checkout (`sys.frozen` unset); real GAM ignores them anyway. A new
  variable GAM genuinely needs goes into `ENV_ALLOWLIST` deliberately, never a prefix match — and
  into the test's literal `EXPECTED_PASSTHROUGH` too: the test once computed its expectation from
  `ENV_ALLOWLIST` itself, so adding `PYTHONPATH`/`DYLD_*`/`SSL_CERT_FILE` to the allowlist passed it.
  `/usr/bin/env` can't show `DYLD_*` (SIP strips them from a platform binary before it runs), so a
  second child, the venv's own Python (`-I`), checks those.
- **`serialize=True` write-lock.** Mutations pass `serialize=True`, taking `_write_lock` so two
  writes can't race the *same* ephemeral `GAMCFGDIR` — and, critically, can't race the oauth2.txt
  refresh write-back into the vault (GAM rewrites `oauth2.txt` on token refresh; `EphemeralConfig`
  persists it on exit). Reads run unlocked.
- **`strip_cfgdir_noise`.** Because each call gets a brand-new `GAMCFGDIR`, GAM prints `Created:`/
  `Config File: ..., Initialized` banners on *stdout* every time. Left in, they leaked into text
  parsers (the vacation message). `models.Vacation.from_show_text` also stops capture on those
  banner prefixes as defense-in-depth.
- **Error ordering.** Within a line, `_PATTERNS` is first-match-wins, specific→generic. The
  `cannot change your own access level | cannotChangeOwnAcl` pattern sits *before* the generic 403
  because GAM's own-ACL-deletion refusal carries no 403/forbidden token; it maps to its own kind,
  `OWN_ACL`, so the all-users calendar offboard sweep can tolerate exactly that refusal and not a
  real 403 (`PERMISSION_DENIED`, which it used to tolerate wholesale — failure-log 2026-09-23).
  `SERVICE_NOT_ENABLED` is GAM's per-user "User: x, Calendar Service/App not enabled"
  (`userServiceNotEnabledWarning`, exit 73); the regex is `Service/App not enabled` on purpose, so
  GAM's account-wide "Calendar not enabled. Please run "gam update project"…" stays `UNKNOWN`. A
  missing or unreadable credentials file ("Client OAuth2 File: …/oauth2.txt, Does not exist", GAM's
  `exitIfNoOauth2Txt`/`invalidOauth2TxtExit`) is matched *before* the not-found pattern, whose words it
  also contains, so it is `NOT_AUTHENTICATED` ("complete setup"), not `NOT_FOUND`. The reverse for a
  per-user Gmail/Calendar command naming an address that isn't a user: GAM can't get a token for it
  and reports "User: x, User:, Show Failed: invalid_grant: Invalid email or User ID" (also "Not a
  valid email", "The account has been deleted"; `handleOAuthTokenError` → `entityActionFailedWarning`,
  exit 50, read from the vendored build). That pattern sits *before* the `invalid_grant` one, so it is
  `NOT_FOUND`, not `AUTH_EXPIRED` ("your sign-in expired, re-run setup") — the admin's sign-in is fine.
- **Classified per line, not per stderr** (2026-09-23). A multi-entity command (`all users ...`)
  prints one stderr line per entity, and the whole text used to be classified by the first pattern
  matching *anywhere* — so one "Does not exist" line made a stderr that also held a real failure
  `NOT_FOUND`, and the offboarding calendar sweep (which tolerates that) reported a partial failure
  as success. Now every non-blank line is classified (an unmatched one is `UNKNOWN`), GAM's
  `Getting all …`/`Got N …` progress chatter (`show_gettings`, on stderr) is skipped, `GAMError.kinds`
  holds every line's kind and `kind` the most severe by `_SEVERITY` (account-wide failures, then
  `UNKNOWN`, then the per-entity `PERMISSION_DENIED`, `OWN_ACL`, `SERVICE_NOT_ENABLED`, `NOT_FOUND`).
  `_run_write` tolerates only when
  `kinds ⊆ tolerate_kinds` — never test `.kind` for tolerance. `.message` shows the last line of the
  reported kind, not the stderr's tail (which can be a benign notice). Fail-closed on purpose: a
  benign stderr line we don't recognize makes a best-effort step fail visibly, not pass silently.
- **Tolerant models.** GAM's JSON keys drift by command/version, so `_get` tries several key
  spellings and everything lands in `raw`. `CalendarACL.role` is lowercased on purpose so the
  load-bearing owner guard doesn't break on a casing change.

## Gotchas / mock-lies traps
- **`formatjson` is not universal.** Some print/show commands reject it (`show vacation`/`signature`,
  `print messages`/`delegates`) and return text/plain CSV — hence `Vacation.from_show_text` and the
  CSV branch in the parser. The mock cannot catch a formatjson rejection; check the vendored grammar
  `gamgui/resources/gam7/GamCommands.txt`, not memory (matches CLAUDE.md's "the mock lies").
- **stderr wording is Google's, not stable.** `classify_stderr` matches on human text; a Google
  phrasing change silently degrades a kind to `UNKNOWN`. Only the four `MOCKFAIL` kinds in
  `mock_gam.sh` (notfound/scope/rate/auth, plus a generic catch-all) and the `test_errors.py`
  cases (single lines plus the mixed multi-entity stderr) are exercised — real tenants emit far more.
- **The mock is strict, and must stay so** (2026-09-23). `tests/fixtures/mock_gam.sh` fails any argv
  it has no handler for (`ERROR: mock: unhandled argv`, exit 2); every write the app issues has a
  handler that accepts only its grammar shape and fails a malformed one like GAM's usage error
  (`Missing argument` / `Invalid choice` / `Invalid argument`, exit 2); every call but `version`
  must find `oauth2service.json` + `oauth2.txt` in `$GAMCFGDIR`; `info user` is keyed on the
  address (unknown → `Does not exist`). `tests/test_mock_gam.py` is the tripwire: a new
  `GAMCommands` builder must be classified there, and its shapes must pass the mock. The stderr
  wording/exit codes are GAM7's conventions written by hand, not a live capture. The sweep's
  `SWEEPBENIGN`/`SWEEPMIXED` triggers emit a multi-line, multi-entity stderr (all tolerable / one real
  failure among them) for the per-line classification.
- **Seeing what GAM received:** the `gam_calls` fixture (`tests/conftest.py`) sets
  `GAM_MOCK_ARGV_LOG`; the mock appends each argv (NUL-separated) and `tests/helpers.py`
  `read_gam_calls` parses it. `MOCKSLEEP <secs> [pidfile]` hangs on purpose for the timeout path.
- **CSV `JSON`-column merge.** `_parse_csv` keeps plain sibling columns (owning user/key) and lets
  the JSON blob win on conflict; multi-entity output (`all users print calendars`) depends on this.
  A mock that returns a bare JSON object per row where GAM returns the `key,JSON` CSV would hide a
  break.
- **A cancelled call stops `gam` too** (2026-09-23, review F3). Quitting the app cancels in-flight
  job tasks (`server._lifespan`); `CancelledError` is a `BaseException`, so it skipped the timeout's
  `except` and `gam` kept running on the credentials it had loaded while `EphemeralConfig` wiped the
  dir under it. `_exec` now kills and reaps it (`_stop`: the SIGKILL is sent first, synchronously, so
  a second cancellation during the reap can't leave it running) and re-raises; the connector audits
  the interrupted write (see connectors-chokepoint). Don't narrow that `except BaseException` to
  `Exception`.
- **Timeout raises `TIMEOUT` directly** in `_exec` (exit_code `None`), never touching
  `classify_stderr`; `from_run` independently also maps a `None` exit_code to `TIMEOUT`. A
  best-effort caller never tolerates it (it is not a per-entity notice). Anything stderr held before
  the kill is lost.
- **A domain-wide call under the default timeout is a bug.** `all users …` makes one API call per
  user inside one process, so 120s ended a few-hundred-user sweep partway. Pass
  `DOMAIN_WIDE_TIMEOUT` (`test_offboard_safety.py::test_domain_wide_calls_get_the_long_timeout`);
  it is a guess from ~0.5–1 s/user, not a measured bound — a tenant past several thousand users
  needs it raised.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_runner.py tests/test_errors.py tests/test_parser.py
tests/test_models.py` — fully offline (mock `gam` + in-memory Keychain). Coverage: argv reaches the
binary, the four classified failure kinds, per-line classification of a mixed stderr
(`test_errors.py`; end to end through the sweep in `test_lifecycle.py`
`test_offboard_calendar_sweep_multi_user_stderr`), banner stripping, the oauth2 write-back
(`GAM_MOCK_REFRESH` → vault value changes) under `serialize=True`, and the timeout path
(`MOCKSLEEP`: `TIMEOUT` raised with a "stopped after N" message, the process killed and reaped, the
`GAMCFGDIR` still wiped, the write lock released —
`test_timeout_kills_gam_wipes_the_config_and_frees_the_write_lock`; the same for a cancelled call —
`test_cancel_kills_gam_wipes_the_config_and_frees_the_write_lock`; the sweep's own timeout via the
mock's `SWEEPSLOW` in `test_lifecycle.py::test_offboard_sweep_timeout_is_a_clear_step_failure`), and the env
allowlist (pinned as a literal set in the test; a real child, `/usr/bin/env`, reports exactly what it
received, frozen and not, and the venv's Python reports any `DYLD_*`). Untrusted
until run live: whether real GAM needs any variable outside the allowlist (none known), the
real stderr wording behind each `GAMErrorKind` (only a handful of lines mocked), whether a real
multi-entity sweep prints any stderr line beyond progress chatter and per-entity failures (one would
fail the best-effort step — visibly), and every `formatjson`/text
output shape against an actual tenant — passing tests here do **not** prove a real GAM write worked.

## To do common tasks here
- **Add an error kind:** add the enum in `GAMErrorKind`, a `_REMEDIATION` entry, a `_SEVERITY`
  rank, and an ordered `_PATTERNS` regex (place specific-before-generic), then a case in
  `test_errors.py` and a `MOCKFAIL <kind>` branch in `mock_gam.sh`.
- **Support a new output shape:** extend `parser.py` (`parse_records` branches) or add/extend a
  dataclass `from_json` in `models.py`; add a fixture-backed test in `test_parser.py`/`test_models.py`.
- **New command call site:** don't touch this domain — add the argv builder in `commands.py`, call
  `run_authenticated(..., serialize=True)` for mutations via `gam_connector._run_write`.
- **Never** add a second subprocess spawn or interpolate an operator value into a string (breaks #1).
