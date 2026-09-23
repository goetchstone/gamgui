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
those; the argv-only property is structural (single `_exec`), not asserted by a lint.

## Files
- `gamgui/core/gam/runner.py` — `GAMRunner`; `_exec` is the only `create_subprocess_exec`. Locates
  the binary, builds env, enforces timeout, holds `_write_lock`. `strip_cfgdir_noise` scrubs GAM's
  per-call config banner.
- `gamgui/core/gam/errors.py` — `GAMError`, `GAMErrorKind`, `classify_stderr`, ordered `_PATTERNS`,
  `_REMEDIATION` map.
- `gamgui/core/gam/parser.py` — `parse_records`/`parse_one`; tolerant JSON/NDJSON/CSV normalizer.
- `gamgui/core/gam/models.py` — tolerant dataclasses (`GAMUser`, `GAMGroup`, `GroupMember`,
  `Vacation`, `CalendarACL`, `ResourceCalendar`, `UserCalendar`, `CalendarEvent`) built from parsed
  dicts (or text for `Vacation`).

## How it works
Callers (mostly `core/connectors/gam_connector.py`) build an argv list with a `GAMCommands` static
method and call `runner.run_authenticated(domain, argv, serialize=...)`. That opens an
`EphemeralConfig` context (vault → `0700` dir with `0600` credential files), runs `_exec`, and on
exit writes any refreshed `oauth2.txt` back to the vault and wipes the dir. `_exec` spawns via
`create_subprocess_exec(str(gam_binary), *argv, ...)` with a captured env carrying `GAMCFGDIR`; a
`wait_for` timeout kills the process and raises a `TIMEOUT` `GAMError`. Non-zero exit →
`GAMError.from_run` (stderr classified by first-match regex). Success stdout is de-noised, then the
connector runs it through `parse_records`/`parse_one` and `Model.from_json`. Two side paths:
`run_in_cfgdir` (setup wizard, explicit persistent cfgdir, returns raw `RunResult`) and `version`
(no credentials).

## Invariants & the failure history
- **argv-only (#1).** Every operator value is one argv element; `_exec` never joins a string or
  invokes a shell. This is the whole point of the single boundary — don't add a second spawn.
- **`serialize=True` write-lock.** Mutations pass `serialize=True`, taking `_write_lock` so two
  writes can't race the *same* ephemeral `GAMCFGDIR` — and, critically, can't race the oauth2.txt
  refresh write-back into the vault (GAM rewrites `oauth2.txt` on token refresh; `EphemeralConfig`
  persists it on exit). Reads run unlocked.
- **`strip_cfgdir_noise`.** Because each call gets a brand-new `GAMCFGDIR`, GAM prints `Created:`/
  `Config File: ..., Initialized` banners on *stdout* every time. Left in, they leaked into text
  parsers (the vacation message). `models.Vacation.from_show_text` also stops capture on those
  banner prefixes as defense-in-depth.
- **Error ordering.** `_PATTERNS` is first-match-wins, specific→generic. The
  `cannot change your own access level | cannotChangeOwnAcl` pattern sits *before* the generic 403
  because GAM's own-ACL-deletion refusal carries no 403/forbidden token; the all-users calendar
  offboard sweep relies on it mapping to `PERMISSION_DENIED`.
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
  `mock_gam.sh` (notfound/scope/rate/auth, plus a generic catch-all) and the six `test_errors.py`
  cases are exercised — real tenants emit far more.
- **The mock is strict, and must stay so** (2026-09-23). `tests/fixtures/mock_gam.sh` fails any argv
  it has no handler for (`ERROR: mock: unhandled argv`, exit 2); every write the app issues has a
  handler that accepts only its grammar shape and fails a malformed one like GAM's usage error
  (`Missing argument` / `Invalid choice` / `Invalid argument`, exit 2); every call but `version`
  must find `oauth2service.json` + `oauth2.txt` in `$GAMCFGDIR`; `info user` is keyed on the
  address (unknown → `Does not exist`). `tests/test_mock_gam.py` is the tripwire: a new
  `GAMCommands` builder must be classified there, and its shapes must pass the mock. The stderr
  wording/exit codes are GAM7's conventions written by hand, not a live capture.
- **Seeing what GAM received:** the `gam_calls` fixture (`tests/conftest.py`) sets
  `GAM_MOCK_ARGV_LOG`; the mock appends each argv (NUL-separated) and `tests/helpers.py`
  `read_gam_calls` parses it. `MOCKSLEEP <secs> [pidfile]` hangs on purpose for the timeout path.
- **CSV `JSON`-column merge.** `_parse_csv` keeps plain sibling columns (owning user/key) and lets
  the JSON blob win on conflict; multi-entity output (`all users print calendars`) depends on this.
  A mock that returns a bare JSON object per row where GAM returns the `key,JSON` CSV would hide a
  break.
- **Timeout raises `TIMEOUT` directly** in `_exec` (exit_code `None`), never touching
  `classify_stderr`; `from_run` independently also maps a `None` exit_code to `TIMEOUT`.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_runner.py tests/test_errors.py tests/test_parser.py
tests/test_models.py` — fully offline (mock `gam` + in-memory Keychain). Coverage: argv reaches the
binary, the four classified failure kinds, banner stripping, and the oauth2 write-back
(`GAM_MOCK_REFRESH` → vault value changes) under `serialize=True`. Untrusted until run live: the
real stderr wording behind each `GAMErrorKind` (only 6 lines mocked), and every `formatjson`/text
output shape against an actual tenant — passing tests here do **not** prove a real GAM write worked.

## To do common tasks here
- **Add an error kind:** add the enum in `GAMErrorKind`, a `_REMEDIATION` entry, and an ordered
  `_PATTERNS` regex (place specific-before-generic), then a case in `test_errors.py` and a
  `MOCKFAIL <kind>` branch in `mock_gam.sh`.
- **Support a new output shape:** extend `parser.py` (`parse_records` branches) or add/extend a
  dataclass `from_json` in `models.py`; add a fixture-backed test in `test_parser.py`/`test_models.py`.
- **New command call site:** don't touch this domain — add the argv builder in `commands.py`, call
  `run_authenticated(..., serialize=True)` for mutations via `gam_connector._run_write`.
- **Never** add a second subprocess spawn or interpolate an operator value into a string (breaks #1).
