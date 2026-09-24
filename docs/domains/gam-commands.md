# Domain: GAM Commands (argv construction)

**One line:** Every `gam` invocation in the app is built here as an explicit argv *list* by a
`GAMCommands` static method, so operator-supplied values ride as single list elements and never touch
a shell.

**Owns invariant(s):** #1 (argv-only, one list element per value). Also the single-source-of-truth
`EXPECTED_GAM_VERSION` pin that invariant #7 (fail-closed vendor) and the three drift guards key off.
**Enforcement home:** `tests/test_commands.py` (per-builder arg-shape + injection assertions),
`tests/test_command_contract.py` (`test_required_command_tokens_present`,
`test_builder_commands_match_a_grammar_line`, `test_catalog_matches_grammar`,
`test_pinned_version_consistent`), and `tests/test_builder.py` (slot value → one argv element at the
Builder layer). No runtime hook — the shape is frozen by tests, not asserted in prod.

## Files
- `gamgui/core/gam/commands.py` — the ONLY argv construction. `class GAMCommands` (static methods,
  one per GAM sub-command), the `EXPECTED_GAM_VERSION` constant, field-set tuples
  (`USER_LIST_FIELDS`, `CACHE_FIELDS`, …), the role sets `GROUP_ROLES` / `CALENDAR_ACL_ROLES`, and three
  helpers: `_validate_role`, `_validate_calendar_role`, `build_user_query`.
- `tests/test_commands.py` — asserts each builder's exact argv, including "poison stays one element."
- `tests/test_command_contract.py` — no-credential drift guards (the generated grammar contract,
  version consistency, catalog↔grammar count).
- `tests/test_builder.py` — the Builder/catalog flow; relevant here for the injection guarantee that a
  slot value lands as exactly one argv element (e.g. `test_slot_value_is_a_single_argv_element`).

## How it works
Each method returns `List[str]` (e.g. `print_users()` → `["print","users","fields",...,"formatjson"]`).
Callers never build argv themselves: read paths call `runner.run_authenticated(domain, GAMCommands.X(...))`
directly; mutations go `GAMCommands.X(...)` → `ChangePreview` → `guard.evaluate()` →
`_run_write(...)` in `gam_connector.py` (invariant #2). The curated Builder catalog
(`core/catalog/catalog.py`) binds each `build.*` id to a `lambda s: GAMCommands.X(s[...])`, so even the
UI's assembled commands come from these same methods. `build_user_query` turns the search box into a
Directory API query string (prefix `email:tok* givenName:tok* …`); `_validate_role` gates
`add_group_member` to `member|manager|owner`, and `_validate_calendar_role` gates `add_calendar_acl` /
`add_calendar_acl_cal` to the grammar's `<CalendarACLRole>` (`CALENDAR_ACL_ROLES`). Both raise
`ValueError` before anything runs; the route catches it and renders a friendly error.

## Invariants & the failure history
- **#1 argv-only.** A value like `"a@x.com; rm -rf /"` is one element, never interpolated — proven by
  `test_slot_value_is_a_single_argv_element` and `test_calendar_share_id_is_single_arg_not_shell`.
- **`EXPECTED_GAM_VERSION` = the single source of truth** (currently `7.48.11`). `test_pinned_version_consistent`
  fails unless `scripts/fetch_gam.sh` (`TAG="v…"`) and `tests/fixtures/mock_gam.sh` agree;
  `test_catalog_matches_grammar` fails if the committed catalog's version/command-count drifts from a
  fresh parse of the vendored grammar. Bump only via the README runbook (step 1 fails by design).
- **The grammar contract is generated, not a hand list.** It used to be `REQUIRED_TOKENS`: bare
  substrings anywhere in a 391 KB grammar, with 60 of ~99 builder keywords (`doit`, `eventid`,
  `returnidonly`, `notifypassword`, `sendupdates`, …) never tracked. Now the two contract tests call
  every `GAMCommands` static method with `<param>` placeholders — both sides of each bool, each
  optional value given and omitted, every value of a validated enum (`ENUM_ARGS`) — and assert (a)
  every keyword it emits is a whole word in `GamCommands.txt` (a comma field list: each field,
  case-insensitively) and (b) its leading words (entity prefix + the next two) match the head of a real
  `gam …` line, honouring `create|add` alternations, `<UserTypeEntity>` = `user <x>`/`all users`, and
  choice-of-words slots like `[<Boolean>]`. The second test first proves it bites on renamed words.
  A new builder is covered automatically; a new *validated* argument fails with `ValueError` until
  its values go in `ENUM_ARGS`.
- **`create svcacct` vs `check serviceaccount`** — GAM's nouns are *not* symmetric; `check_svcacct`
  deliberately uses `serviceaccount` (see the `check_svcacct` builder); the grammar contract checks
  both spellings against their own `gam …` lines.
- **`remove calendars` ≠ `delete calendars`** (footgun, verified against GAM7 source): `remove_calendar`
  PERMANENTLY deletes a secondary calendar (impersonating an owner); `delete calendars` only drops
  it from one user's list (no builder: the app never unsubscribes anyone, and the mock refuses it).
  No `doit` on `remove calendars` — GAM7 rejects extra args. See
  `test_calendar_delete_is_remove_calendars_not_delete_calendars`.
- **`create datatransfer` service list is ONE element** (`"drive,calendar"`) — splitting it caused
  Google 409 "transfer already in progress"; pinned by `test_lifecycle_commands`.
- **An operator-chosen enum is validated in the builder, not the route.** The calendar role was
  checked (by silently coercing to `reader`) in `/calendars/share` but passed through untouched by
  `/users/calendar/add` — a check in one route is a check some other path skips (failure-log
  2026-09-23). `test_calendar_acl_role_is_validated_in_the_builder`; and
  `test_calendar_acl_roles_match_grammar_and_mock` pins `CALENDAR_ACL_ROLES` to the mock's
  `ACL_ROLES` and (when vendored) to every `<CalendarACLRole>` definition in the grammar.
- **Grammar spelling** — the reference reads `create|add user` / `create|add group`; the leading-words
  match reads each `|` as a choice, so `create user` matches it. The grammar also has typos the
  parser tolerates (`<FalseValues>=` for `::=`, an unopened `<CalendarACLRole>]`).
- **What the contract can't see:** the order and pairing of options past the leading words
  (`vacation … html` is only checked as "`html` is a grammar word"), field-name validity per command,
  and anything about GAM's behaviour. For the writes, the strict `mock_gam.sh` handlers check option
  order and pairing (hand-written from the grammar); a grammar-validating mock for everything is plan
  item T7, and GAM's behaviour needs live runs.

## Gotchas / mock-lies traps
- **`formatjson` is not universal.** `print messages`, `print delegates`, `show vacation`,
  `show signature` REJECT `formatjson` (GAM errors "format json is invalid"), so those builders emit
  CSV/text and the code parses that. Most of `mock_gam.sh`'s read handlers are canned and accept
  any trailing word, so a mock pass proves nothing here — check `gamgui/resources/gam7/GamCommands.txt`,
  the source of truth. The per-user reads (`info user`, `show vacation|signature`, `print delegates`,
  `print groups member`, `user … print calendaracls`) are the exception: keyed on their target (each
  fixture user has different data; an address that isn't a user fails as GAM does), and all but
  `info user` accept only the argv the app sends, so `print delegates`, `show vacation` and `show signature` reject
  `formatjson` as GAM does (and `print groups member` any option — stricter than the grammar, the
  safe direction). A `todrive` tail is accepted only on a print/report read and only with the
  attributes the Builder emits (`tduser`, `tdtitle`). (Its *write* handlers are strict:
  a new mutating builder needs a handler that accepts only its grammar shape, or the mock fails it —
  and `tests/test_mock_gam.py` fails until the builder is classified there.)
- The module docstring flags the mutating sub-syntax (group membership, signature flags) as
  needing live re-verification against the pinned GAM each bump — the arg-shape tests only pin *our
  intended* form, not that GAM accepts it.
- `mock_gam.sh` echoes `GAM 7.48.11 - mock`; `EXPECTED_GAM_VERSION` is matched as a substring against
  live `gam version` for a fail-soft runtime check.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_commands.py tests/test_command_contract.py tests/test_builder.py`
runs fully offline (mock GAM + in-memory Keychain). The grammar contract tests and
`test_catalog_matches_grammar` **skip** when the grammar isn't vendored (a clean clone) — locally they
run once `make gam` has vendored it, and in CI in the `gam-compat` job that fetches the real binary
(the non-blocking latest-GAM preview runs the two contract tests too). Passing tests do NOT prove a GAM write works: every
mutating builder (delete_user, datatransfer, remove_calendar, group membership, signature/forward/
vacation flags) is unproven until run against a **throwaway** tenant per the README live-verification
status. Read-only builders are safe to exercise via `scripts/acceptance.py`.

## To do common tasks here
- **Add a new GAM command:** add a `GAMCommands.<name>()` static method returning an argv list (each
  operator value its own element), add an arg-shape test in `tests/test_commands.py`, list any
  argument it validates in `ENUM_ARGS` (`tests/test_command_contract.py` — the grammar contract picks
  the builder up by itself), and classify it in `tests/test_mock_gam.py` (a write also needs a strict
  `mock_gam.sh` handler). Call it from the app in the same change:
  `test_mock_gam.py::test_every_builder_has_a_caller_in_the_app` fails on a builder nothing in
  `gamgui/` references (six sat unused until plan Q11 removed them — `update_user`,
  `unsubscribe_calendar`, `delete_forwarding_address`, and the setup trio `create_project`,
  `oauth_create`, `create_svcacct`, which the setup screen never used: it shows those commands as
  text). To surface it in the UI, wire a
  curated entry in `core/catalog/catalog.py` (`build.*` → `lambda`) — see the `add-builder-command`
  skill; the connector must route any mutation through `_run_write` (invariant #2). Verify a mutation
  live on a throwaway before relying on it.
- **Bump the pinned GAM version:** change `EXPECTED_GAM_VERSION` here, follow the README "Updating GAM"
  runbook (step 1 fails by design), and let the three drift guards catch renamed/removed sub-commands.
