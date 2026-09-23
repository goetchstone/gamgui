# Failure log

Institutional memory of what broke and why — so the same shape can't recur
silently. Read the recent entries before working in a domain that has prior
ones. Every incident that reaches a user (a broken test caught late, a live
tenant break, a regression) gets an entry via the
[post-failure skill](../.claude/skills/post-failure/SKILL.md).

**Format** — newest first, five fields:

- **Symptom** — what was actually observed.
- **Cause** — the line / assumption that did it.
- **Why not caught** — the test/validation/gate gap.
- **Fix** — what shipped (link the commit/PR).
- **Prevention** — the tripwire test, hook, skill, or CLAUDE.md rule it fed
  (name it; "nothing yet" is a valid, and telling, answer).

This repo's defining failure class is **"the mock passed, the live tenant
broke"** — see [CLAUDE.md](../CLAUDE.md) "The recurring failure mode". A fix
whose only proof is a greener mock is not proven; say so in the Prevention field.

---

## 2026-09-23 — A hire surnamed "Password" put the temp password in the audit log and the error page

- **Symptom:** a review found that `create_user` failing with GAM's echoed command line on stderr,
  for a hire whose surname is `Password` (or `NotifyPassword`), wrote the one-time password into
  `audit.jsonl` `extra.error` and the onboarding "Couldn't create the account" partial. Reproduced
  offline against a gam that echoes its command line; not seen on the live tenant.
- **Cause:** every secret mask was positional — `errors._scrub_stderr` masks the token after the word
  `password`, `audit.redact_argv` the token after a sensitive key. `lastname Password password <pw>`
  spends the mask on the `password` keyword and leaves `<pw>` bare. (`GAMError.argv` shifts the same
  way, for a `Signature` surname too, but `_run_write` swallows the exception so nothing surfaces
  it.) `create_user`'s `"********"` `audit_argv` covered only the argv, not the error text GAM echoes.
- **Why not caught:** the redaction tests used ordinary names, and the strict mock's usage error
  puts the echoed command on the first stderr line, not the last one that `GAMError.message` keeps.
- **Fix:** redaction by value — `_run_write(..., secrets=[pw])` masks every occurrence in the shown
  argv, the error text / `ChangeResult.detail`, and (via `AuditLog.record(secrets=)`) every audited
  field; `create_user` passes its password. The positional passes stay as the second layer.
- **Prevention:** `test_create_user_failure_redacts_password_by_value` (both surnames, into
  `audit.jsonl`), `test_run_account_failure_error_partial_never_shows_password` (rendered partial),
  `test_positional_redaction_is_shifted_by_a_value_that_spells_a_key` (documents the positional
  limit). A new secret-bearing mutation must pass `secrets=` — the runbooks say so; no drift guard.

## 2026-09-23 — The launch environment could choose, and inject into, the `gam` holding the credentials

- **Symptom:** a review found `runner.py` honoring `GAMGUI_GAM_BINARY` even in the packaged `.app`,
  `_build_env` passing `gam` a copy of the whole `os.environ` (`DYLD_*`, `PYTHON*` included), and
  `build_app.sh` signing both the app and `gam` without the hardened runtime. A same-user
  `launchctl setenv GAMGUI_GAM_BINARY ~/x` would have handed all three plaintext credentials to an
  attacker's binary on the next launch, with no Keychain prompt; `DYLD_INSERT_LIBRARIES` would have
  loaded code into the app the Keychain trusts. Not exploited.
- **Cause:** the override was added for tests and power users and never scoped to a source checkout;
  inheriting the parent environment is `create_subprocess_exec`'s default and nobody narrowed it;
  the signing step was written to quiet the Keychain, not to harden. It also signed `gam` *before*
  the `codesign --force --deep` of the app, which re-signs every nested Mach-O — so the dedicated
  `gam` signing was silently replaced every build.
- **Why not caught:** no test looked at the child's environment or at `sys.frozen`, and nothing
  checks the build's signing (the `.app` build has no automated test).
- **Fix:** the override is ignored when `sys.frozen`; `gam` gets only `ENV_ALLOWLIST` (+ `GAMCFGDIR`,
  `GAM_NO_UPDATE_CHECK`; the mock's `GAM_MOCK_*` only in a source checkout); the new
  `scripts/sign_app.sh` signs app and `gam` with `--options runtime`, `gam` after the deep sign,
  with minimal entitlements (`scripts/app.entitlements`: library validation off only, because a
  self-signed bundle has no Team ID; `gam`: upstream GAM's own set).
- **Prevention:** `tests/test_runner.py::test_gam_inherits_only_the_allowlisted_environment` (a
  real child reports its env, frozen and not) and `::test_binary_override_is_ignored_in_the_packaged_app`;
  `tests/test_build_signing.py` (runtime flag, sign order, entitlement sets, `gam.entitlements` ==
  the vendored binary's). Unproven until a hardened `.app` is built and launched: the signing ran
  only on an ad-hoc copy of a built bundle, and the app-side entitlement choice was measured with a
  hardened copy of the build Python, not the frozen app.

## 2026-09-23 — The GAM release-watch workflow ran an upstream tag as shell code

- **Symptom:** a review found `.github/workflows/gam-watch.yml` expanding
  `${{ steps.check.outputs.latest }}` — the GAM-team/GAM latest release tag — straight into a
  `run:` script, in a job with `contents: write` + `pull-requests: write`, before the attestation
  check. A legal tag such as `v7.49.0$(curl …|sh)` would have executed on the runner. Not exploited.
- **Cause:** GitHub substitutes `${{ }}` into the script text before the shell parses it; the value
  was treated as a trusted version string because it came from the upstream we pin.
- **Why not caught:** nothing looked at workflow files except `test_fetch_gam.py`'s check of the
  `--allow-unpinned` flag; the later "Open the PR" step already used `env:`, so the pattern was
  known but not enforced.
- **Fix:** the tag reaches `bump_gam.py` via `env: LATEST`, and the check step refuses any tag not
  matching `^[0-9]+(\.[0-9]+){2,3}$` (bash `=~`, which anchors the whole string, newlines included)
  before it is written to `$GITHUB_OUTPUT` or used anywhere.
- **Prevention:** `tests/test_workflow_safety.py` fails on any `${{` inside a `run:` block of any
  workflow (plus a self-test that the scanner catches the original shape). The regex was checked
  under bash 3.2 against `$(…)`, an embedded newline, a two-part version and a `v` prefix.

## 2026-09-23 — The mock `gam` succeeded for any write it didn't recognize

- **Symptom:** a review made `tests/fixtures/mock_gam.sh`'s final catch-all fail and 20 tests went
  red. 23 of the 33 write builders the app calls had no handler, so `delete user`, `remove calendars`,
  `delete events` *without* `doit` and a calendar ACL with an invalid role all exited 0; the all-users
  calendar-ACL sweep had a handler that only ever failed, so its success path never ran; and
  `info user <anyone>` returned Alice. The header also claimed a `GAMCFGDIR` check it never made.
- **Cause:** the mock was written read-first — canned output per read, then `echo ok; exit 0` for
  "anything else is a mutation". Every new write inherited a green test without a handler.
- **Why not caught:** nothing tested the mock itself, and web routes answer 200 whether the write
  worked or not, so tests that checked a status code (or that a job *started*) passed either way.
  Re-checked every write builder against `GamCommands.txt` while writing the strict handlers: all 33
  match the grammar, so no live break was hiding behind this one.
- **Fix:** the catch-all fails (`ERROR: mock: unhandled argv`, exit 2); a strict handler per write
  accepts only its grammar shape and fails a malformed one like GAM's usage error; `info user` is
  keyed on the address (unknown → `Does not exist`); every call but `version` must see the
  materialized `oauth2service.json`/`oauth2.txt`; the sweep succeeds unless `OWNACL`/`SWEEPFAIL`.
- **Prevention:** `tests/test_mock_gam.py` — every `GAMCommands` builder must be classified, each
  emitted shape must pass the mock, and malformed shapes (no `doit`, bad role, missing value,
  unhandled argv) must fail. The nine web tests that passed on a failed write now assert real
  success — `tests/helpers.py` `assert_ok_partial` (no amber error box), the argv the mock received
  (`gam_calls`), the audit `ok`, and route-started jobs awaited with `wait_for_job` — and were
  checked by failing every write in the mock: all went red. Still not proof GAM accepts a write —
  the handlers' stderr/exit codes are GAM7's conventions written by hand; only Phase 8 live
  captures prove them.

## 2026-09-23 — The framework's hooks were talking to nobody

- **Symptom:** the session-orient digest, the improve-rules nudge and the pre-commit checklist never
  reached the model. The session transcript recorded every firing with `content: ""` — including a
  nudge (7 fix commits + 3 ledger entries) that fired and was silently dropped. Only the hard `fix:`
  block worked.
- **Cause:** all three hooks printed to **stderr** and exited 0. Claude Code feeds a hook's stderr
  to the model only on a blocking exit 2; for SessionStart it is stdout that becomes context, and a
  non-blocking PreToolUse hook needs JSON `hookSpecificOutput.additionalContext` on stdout.
- **Why not caught:** the hooks were tested by running them in a shell, where stderr is visible — the
  test checked that text was printed, not that it reached the model.
- **Fix:** the hooks now write to the model-visible channel (SessionStart → stdout; PreToolUse →
  JSON `additionalContext`), and the checklist is trimmed to the items relevant to the changed files.
  Verified in-session: the injected checklist arrived as hook context. The nudge now also skips
  RULE-FEEDBACK entries marked **Resolved**.
- **Prevention:** [FRAMEWORK.md](FRAMEWORK.md) §2 now states the channel rule. No automated tripwire
  (the hooks are local, gitignored wiring) — verify a new or changed hook by looking for its text in
  the model's context, not in a terminal.

## 2026-09-22 — "Create the Google account" toggle never hid the name fields on desktop

- **Symptom:** the First/Last name fields (relevant only to account creation) were always visible at
  desktop width; ticking/unticking "Create the Google account" did nothing.
- **Cause:** the container was `class="hidden ... sm:grid ..."`. Tailwind's responsive `sm:grid` (a
  later media-query rule) overrode the base `hidden` at >=640px, so its display was always `grid`;
  the toggle flipped `hidden`, which `sm:grid` kept beating.
- **Why not caught:** every test POSTed form data directly; none rendered the page and checked the
  toggle's computed display at a real width. Found by actually using the app.
- **Fix:** drop the static `sm:grid`; obToggleCreate toggles `grid` (and `hidden`) so only one is
  present at a time. Verified in a 1000px browser: none -> grid -> none.
- **Prevention:** never pair a base display utility (`hidden`) with a responsive one (`sm:grid`) that
  overrides it — toggle the class you mean; a render-and-check-computed-display test would catch it.

## 2026-09-22 — Bulk live-feed retained plaintext temp passwords in memory

- **Symptom:** `OnboardJob.recent` (the polled ✓/✗ feed, bounded to 12) stored each hire's full
  result dict — including `credential.password` — so up to 12 plaintext temp passwords lingered in
  the retained job even after the sheet's one-shot/TTL clear. Never displayed, but held.
- **Cause:** `record()` did `self.recent.append(res)` with the whole result; the feed template only
  reads email/name/ok/errors.
- **Why not caught:** the sheet redaction was the focus; nobody asserted the *feed* copy was clean.
- **Fix:** append only `{email, name, ok, errors}` to `recent`; the password lives briefly only in
  `credentials`. Test `test_bulk_recent_feed_holds_no_plaintext_password`.
- **Prevention:** when a struct carries a secret, copy only the fields a consumer needs into any
  retained/rendered collection — never the whole struct.

## 2026-09-22 — Group picker served the previous tenant's groups after a domain switch

- **Symptom:** after switching domains via /setup/verify, the onboarding group picker kept returning
  the old tenant's groups for up to the 5-minute cache TTL.
- **Cause:** `AppState.group_cache` / `user_cache` are not domain-tagged, and the verify handler
  swapped the connector without busting them (the calendar index self-checks its stored domain, so
  it was already safe).
- **Why not caught:** no test switched tenants and re-read a cache; the picker was new.
- **Fix:** call `st.invalidate_users()` + `st.invalidate_groups()` after a successful verify.
  Test `test_setup_verify_busts_group_cache`.
- **Prevention:** any cross-request cache that isn't tagged with the tenant it was filled for must be
  invalidated on a tenant switch — prefer tagging (as the calendar index does) for new caches.

## 2026-09-22 — Single /run stranded a created account's one-time password on a later failure

- **Symptom:** if the task-list step (or the assignee check) failed AFTER create_user succeeded, the
  response was a bare error box with no credentials sheet, so the temp password — never audited,
  never emailed — was lost; a retry 409s.
- **Cause:** run() validated the assignee and created the task list AFTER the account write, and both
  late failures returned `_err(...)`, a template with no credentials slot. The bulk path
  (_provision_hire) already handled this; run() had diverged.
- **Why not caught:** the mock's catch-all makes `create tasklist` always succeed, so no test could
  express a post-create failure; the single-flow test never patched the runbook call.
- **Fix:** hoist all non-writing validation before any mutation; once an account/membership exists,
  render the credentials sheet plus a task-list-failed banner instead of bare-erroring.
  Test `test_run_keeps_credentials_when_tasklist_fails`.
- **Prevention:** an irreversible write (account creation) must never be followed by a code path that
  can `return _err` and discard the one-shot secret it produced.

## 2026-09-18 — Bulk onboarding applied the role signature to existing accounts

- **Symptom:** a CSV bulk run with `create_account=no` rows re-applied the role's signature
  template to those existing users, overwriting customized signatures; a row with no name
  rendered a blank `{name}`.
- **Cause:** when `_provision_hire` was factored out for the bulk executor, `_apply_signature`
  was placed under `if email:` (any row) instead of inside the account-creation branch, where the
  single `/run` flow keeps it.
- **Why not caught:** the bulk tests only covered `create_account=True` rows for the signature
  path; no test asserted the negative case, and the single-flow test didn't exercise the shared
  helper.
- **Fix:** apply the signature only when `res["account_created"]`; test
  `test_provision_hire_skips_signature_for_existing_account`.
- **Prevention:** when factoring a shared helper out of an existing flow, add a test for each
  branch the original gated (here: the create-only gate), not just the happy path.

## 2026-09-15 — Calendar picker showed only the ID, not the name

- **Symptom:** Searching shared calendars in the role editor showed the opaque
  calendar id (`c_…@group.calendar.google.com`) but not the human-readable name,
  so the operator couldn't tell which calendar they were adding.
- **Cause:** In `_onboard_picker.html` the result row was a flex row with the
  name span `truncate` and the id span `shrink-0`. A long calendar id (~55 chars)
  refused to shrink and consumed the row, truncating the name to nothing. The
  data was fine — every indexed calendar had a name.
- **Why not caught:** the mock-backed browser test had **no calendar index
  built**, so the calendar picker only ever rendered the "no index yet" hint —
  real results with long ids were never displayed. The group picker was tested,
  but group ids are short emails that don't trigger the squeeze. (A "the mock
  lies"-adjacent gap: the offline preview didn't exercise the real data shape.)
- **Fix:** stack the result as name-over-id (each `truncate`s independently), so
  the name is always the prominent line (commit follows).
- **Prevention:** verify a picker/list with the *long* id shape, not just the
  short one; and seed the preview's calendar index with realistic long-id rows.
  Flag: a flex `shrink-0` beside `truncate` lets long content evict the label.

## 2026 — GAM print/show commands reject `formatjson`

- **Symptom:** several `print`/`show` commands (e.g. `print messages`,
  `print delegates`, `show vacation`, `show signature`) failed against a live
  tenant with a usage error, though the mock accepted them.
- **Cause:** those subcommands don't support the `formatjson` output flag we
  append by default for machine-readable parsing.
- **Why not caught:** `tests/fixtures/mock_gam.sh` accepted `formatjson` on any
  command — the mock was more permissive than real GAM, which converts a live
  break into a green test.
- **Fix:** stopped sending `formatjson` on the commands that reject it; parse
  their text output instead.
- **Prevention:** check every new command's flags against the vendored grammar
  `gamgui/resources/gam7/GamCommands.txt` (the source of truth, not memory), and
  make the mock reject what real GAM rejects. Captured as the standing
  invariant "the mock lies" in CLAUDE.md and the memory `gamgui-gam-formatjson`.

<!--
Add new entries ABOVE this comment, newest first. Keep the five-field shape.
One entry per distinct incident.
-->
