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

## 2026-09-23 — Bulk department Apply wrote an edited form under the previous preview's dialog

- **Symptom:** a review (F17) previewed Department "Sales" on alice, then pasted alice and carol,
  typed "Finance", and clicked the stale "Apply to 1 user". The dialog said "Set Department to
  'Sales' on 1 user(s)?"; both people got Finance.
- **Cause:** the Apply button posts `hx-include="#bulk-form"`, and `/users/bulk/apply` re-resolved
  the targets and the department from that live form; `confirmed=1` was its only check.
- **Why not caught:** the apply test posted the form it had previewed; nothing edited it in between.
- **Fix:** the preview holds its department and people under a single-use token
  (`web/previews.py`); Apply sets exactly those (titles re-read from the directory, as the job
  keeps each title), and refuses a used, expired or missing token, an edited department, list or
  group, or a previewed person no longer active ("preview again").
- **Prevention:** `test_bulk_store_apply_runs_only_what_was_previewed`,
  `test_bulk_store_apply_is_single_use`. Offline only; no GAM argv changed.

## 2026-09-23 — Onboarding's Run created an account nobody had previewed

- **Symptom:** a review (F15) previewed a tasks-only hire (the button read "Create the task list"),
  then ticked "Create the Google account", filled first/last, and clicked that same button. A real
  account was created — temp password, OU, signature, groups, calendars — none of it previewed.
  On `main` the same clicks were refused ("Preview first — creating an account needs confirmation").
  An email or assignee retyped after Preview, or the role template saved again, also ran unpreviewed.
- **Cause:** this branch's guard sweep (a31b146) replaced the account-specific gate (`confirm=1`
  rendered only when the preview showed account creation) with one `guard.enforce(confirm_step=True)`
  and made the `confirmed=1` hidden input unconditional, while the Run form still posted the live
  form (`hx-include="#onboard-form"`) and `/run` rebuilt the hire and re-read the role from the store.
- **Why not caught:** the run tests posted straight to `/onboard/run` with `confirmed=1`; none
  previewed one form and ran another, and the tripwire only checks that a bare POST writes nothing.
- **Fix:** `/preview` holds the hire, the role template and the welcome template under a single-use
  token (`web/previews.py`); `/run` runs exactly those and refuses a used, expired or missing token,
  or any field changed since the preview ("preview again").
- **Prevention:** `test_run_executes_only_the_previewed_hire` (account ticked, email/assignee
  retyped, welcome added → zero writes, nothing audited), `test_run_uses_the_role_as_previewed`,
  `test_run_is_single_use`. Offline only; no GAM argv changed.

## 2026-09-23 — Signature Apply wrote the live form, not the preview: a stale "Apply to 1 user" overwrote the company

- **Symptom:** a review (F16/F19) previewed one user, switched Scope to Whole company without
  previewing again, and clicked the still-visible "Apply to 1 user". The dialog said 1 user; every
  active mailbox's signature was overwritten, with no backup. Switching "Which" to another person
  or editing the template after Preview likewise wrote what nobody had previewed.
- **Cause:** the Apply button posts `hx-include="#sig-form"` — the live editor — and `/signatures/apply`
  re-resolved the scope and re-read the template from that form. Its only check was `confirmed=1`
  (which the stale button carries) plus the typed count, and that only above 25 people. Nothing
  cleared the preview when the form changed.
- **Why not caught:** the apply tests posted the same form they had previewed; no test edited the
  form between Preview and Apply. Offboarding had just been fixed for this exact class, route by
  route, and the other confirm steps were not swept.
- **Fix:** the preview holds its template and resolved people under a single-use token
  (`web/previews.py`, shared with offboarding); Apply writes exactly those, and refuses a used,
  expired or missing token or a form whose scope, which-value or template changed ("preview
  again"). The typed count is the previewed one; the scope is never re-resolved at Apply.
- **Prevention:** `test_signatures_apply_runs_only_what_was_previewed` (widened scope, another
  person, edited template → zero writes), `test_signatures_apply_is_single_use`,
  `test_signatures_apply_writes_the_previewed_people_and_asks_their_count`. Offline only: the write
  itself (`set signature`) is confirmed live; the flow change touches no GAM argv.

## 2026-09-23 — Add delegate sent any string to GAM's user list

- **Symptom:** a review (plan: delegation polish) found `/users/delegate/add` checked only that the
  box wasn't blank. GAM reads the value as a `<UserList>` (grammar: `"<UserItem>(,<UserItem>)*"`,
  `<UserItem>` = an address, an id or a bare string), so a bare name became `name@<domain>` and a
  comma delegated the mailbox to two accounts; an address outside the directory reached GAM, and any
  error replaced the whole delegates panel (list and form) with an amber box. Remove ran on one click.
  `looks_like_email` — shared with onboarding — also let a comma through.
- **Cause:** the route relied on the form's `<input type="email">`, which only the browser enforces —
  the server checked for a blank value and nothing else; `_EMAIL_RE` excluded `@` and whitespace, not
  `,`.
- **Why not caught:** the delegate tests posted only well-formed addresses; the mock accepts any
  delegate string not containing `missing`.
- **Fix:** `_check_delegate` refuses a non-address, a comma, the owner itself and an alias (naming
  the primary) before any `gam` call; an address not in the cached directory, or a suspended one,
  needs an explicit "Add anyway"; errors render inside the panel, with GAM's line in the disclosure;
  Remove asks first (`hx-confirm`, like the calendar-access Remove). `_EMAIL_RE` excludes `,`.
- **Prevention:** `test_add_delegate_refuses_a_bad_address_before_gam`,
  `test_add_delegate_outside_the_directory_needs_an_ok`, `test_add_delegate_warns_on_a_suspended_account`,
  `test_delegate_remove_asks_before_it_runs`, and the comma row in
  `test_parse_hire_csv_rejects_invalid_email_targets`. Unverified live: what Gmail returns for an
  outside-domain or suspended delegate.

## 2026-09-23 — A malformed hire CSV 500'd the bulk-onboarding preview

- **Symptom:** a review (plan B1) found that uploading a CSV with one cell over 131,072 characters
  to `/onboard/bulk/preview` returned a 500 instead of a row error; the upload had no size limit, so
  any file was read and decoded whole.
- **Cause:** `parse_hire_csv` guarded only the header read with `try`; the `for raw in reader` loop
  sat outside it, so the `csv.Error` the reader raises past the module's field limit escaped to the
  route.
- **Why not caught:** the CSV tests fed only well-formed text, and nothing exercised the `csv`
  module's own failure paths.
- **Fix:** every record is read inside the `try`; a `csv.Error` refuses the whole file with a
  "Row N: couldn't read the CSV" error (never a half import); the route refuses an upload over 1 MB
  before decoding it.
- **Prevention:** `test_parse_hire_csv_refuses_an_unreadable_file_instead_of_raising`,
  `test_bulk_preview_unreadable_or_oversized_csv_is_a_friendly_error`.

## 2026-09-23 — The signature designer opened on "Whole company"

- **Symptom:** a review (plan U6) found the designer's scope defaulting to "Whole company", and the
  route defaults (`scope_type="company"`) agreeing: Preview → Apply → one browser `confirm()`
  overwrote every mailbox's signature, with no backup — the first apply of a new template was a
  company-wide one unless the operator remembered to switch scope.
- **Cause:** the scope list was ordered for the finished roll-out, not the first try; the guard treats
  a signature as a LOW write, so any count needed only the Apply click.
- **Why not caught:** no test looked at the default; the guard had no rule between "a click" and the
  destructive typed word.
- **Fix:** the page and both routes default to "Specific user (test)"; above
  `guard.COUNT_CONFIRM_ABOVE` (25) people the operator types the count, and `/signatures/apply`
  checks it server-side against the count it resolves at apply time (`guard.enforce(...,
  typed_count_above=)`).
- **Prevention:** `test_signatures_designer_defaults_to_one_user`,
  `test_signatures_apply_over_the_threshold_needs_the_count_typed`, and the guard unit test
  `test_enforce_typed_count_only_above_the_opted_in_threshold`.

## 2026-09-23 — The operator's company branding crept back into the public repo

- **Symptom:** a review (plan U3) found the operator's company in the public UI and seed data after
  the June de-branding: its town as the Department placeholder, its email domain in the bulk-edit
  placeholder, its vendor names in the seeded onboarding role and the steps placeholder, an internal
  calendar name in the Calendars placeholder and a connector comment, a real employee's address in
  a test, and a local home path in `abapit_connector.py`'s docstring. The UI also still spoke of a
  "store" (one tenant's department model), not a department.
- **Cause:** new screens were written with the operator's real data as examples; the de-branding rule
  lived only in session memory, not in anything that runs on a commit.
- **Why not caught:** no check looked at added lines for the operator's terms.
- **Fix:** generic `example.com` / "Sales" / "Training Calendar" placeholders, fixtures and tests; a
  neutral seed role (CRM login, laptop, team channel); the UI says "Department" ("Bulk: set
  department", "Save title & department"); the local path is gone from the docstring. Git history
  keeps the old strings (rewriting it was not proposed).
- **Prevention:** the local pre-commit hook's private-term tripwire (`.claude/private-terms`,
  gitignored, so the terms never enter the repo) refuses a commit that adds one; the
  web-screens-jobs runbook's traps list says placeholders stay generic.

## 2026-09-23 — A failed offboarding step never stopped the steps that relied on it

- **Symptom:** reading offboarding before its first live run (plan U2) found `_run_offboard` ran all
  six steps whatever happened ("never abort the routine"). A failed password reset still set the
  "no longer with the company" auto-reply and moved the Drive of an account that could still sign
  in; a failed delegate (a bad manager) still transferred to and reminded that manager; and a failed
  transfer still put the "approve the deletion" reminder on the manager's calendar — while the
  delete screen's pending-transfer warning finds nothing for a transfer that was never created, so
  that deletion would lose the files. Not seen live.
- **Cause:** "report every failure" was implemented as "run every step"; no step declared what it
  relies on.
- **Why not caught:** the executor tests only asserted runs where every step succeeds, or where the
  failing step (the sweep) is one nothing depends on.
- **Fix:** each `OffboardStep` carries `requires` (`lifecycle.REQUIRES`): a failed reset or delegate
  stops the routine, a failed transfer skips only the reminder, a failed auto-reply or sweep is
  reported and the rest runs. A step not run is logged `–` and listed; the final panel says
  "stopped" and, after any failure, not to delete the account yet. The runbook's "When a step fails"
  table gives each rule's reason. This commit.
- **Prevention:** `test_offboard_step_dependencies_are_the_documented_ones` pins the table;
  `test_offboard_failed_reset_stops_the_routine`, `test_offboard_failed_delegate_stops_before_the_rest`,
  `test_offboard_failed_transfer_skips_only_the_reminder`, `test_offboard_stopped_panel_says_what_did_not_run`.

## 2026-09-23 — Offboarding's Run executed the live form, not the preview

- **Symptom:** reading offboarding before its first live run (plan U2) found the preview's Run button
  posted `hx-include="#offboard-form"` and the run route rebuilt the steps from it. Change the manager
  (or the subject, or the days) after Preview, click Run on the preview still on screen, and the
  routine ran against values nobody had previewed. Separately, an emptied subject showed the default
  in the auto-reply block but the vacation command sent `subject ''`. Not seen live.
- **Cause:** the preview and the run each built the steps independently from whatever the form held
  at that moment; nothing tied a run to the preview it came from.
- **Why not caught:** every test posted the same values to both routes, so the two builds always
  agreed; nothing edited the form in between.
- **Fix:** the preview holds the steps it built under a single-use, 15-minute token posted by the Run
  button; Run refuses a missing/used/expired token and a live form that differs from the previewed
  one, re-checks the addresses, and runs the held steps. The subject/message defaults are applied
  before the steps are built. This commit.
- **Prevention:** `test_offboard_run_executes_exactly_the_previewed_commands` (the run's writes, as
  `gam …` lines, equal the preview's), `test_offboard_run_refuses_a_form_edited_after_the_preview`,
  `test_offboard_run_needs_a_fresh_unused_preview`, `test_offboard_run_rechecks_the_directory`,
  `test_offboard_preview_runs_the_default_text_for_an_emptied_field`.

## 2026-09-23 — Offboarding accepted a departing user or manager the directory had never heard of

- **Symptom:** reading offboarding before its first live run (plan U2) found both routes took the two
  addresses as typed. A typo'd manager (`alcie@` for `alice@`) previewed fine — the name lookup just
  fell back to the raw address — and a run then reset the leaver's password and signed them out, and
  only afterwards failed to delegate, transfer or remind: a half-offboarded account. An alias of the
  leaver would have run too, and the calendar sweep matches ACLs by primary address. Not seen live.
- **Cause:** `_resolve_name` swallowed "not found" by design (it only feeds the auto-reply text), and
  nothing else looked either address up.
- **Why not caught:** the tests used `leaver@`/`mgr@example.com`, which are not in the fixture
  directory — the suite itself depended on unknown addresses being accepted, and the mock's writes
  succeed for any address without `missing` in it.
- **Fix:** `lifecycle.check_addresses` — the preview and the run both look both addresses up in the
  cached directory and refuse an unknown address, an alias (naming its primary) or the same account
  twice, before any write; a directory that can't be read blocks too. They warn, without blocking,
  on a super/delegated-admin or already-suspended leaver and a suspended manager. The steps act on
  the directory's primary address. This commit.
- **Prevention:** `test_offboard_blocks_an_address_the_directory_does_not_confirm` (preview and run,
  no write), `test_offboard_blocks_when_the_directory_cannot_be_read`,
  `test_offboard_preview_warns_but_does_not_block`; the route tests now offboard fixture users.

## 2026-09-23 — The domain-wide offboarding sweep ran under the 120s per-call timeout

- **Symptom:** reading offboarding end to end before its first live run (plan C4a) found the
  all-users calendar-ACL sweep (`all users delete calendaracls primary <leaver>`) — one `gam` process
  that makes an API call per user — ran under the runner's 120s default. At ~0.5–1 s per user, any
  domain past a few hundred users would have had the sweep killed partway, every time, reported only
  as "GAM failed (timeout): command timed out". The calendar-index scan (`all users print
  calendars`) had the same bound. Found by reading the code; not seen live.
- **Cause:** `_run_write` and `scan_all_calendars` never passed a `timeout`, so every call got
  `DEFAULT_TIMEOUT`, which was sized for a single-entity call.
- **Why not caught:** the mock answers the sweep instantly, whatever the domain size; nothing
  asserted which timeout a domain-wide call ran under.
- **Fix:** `DOMAIN_WIDE_TIMEOUT` (1 h) in `core/gam/runner.py`, passed explicitly by the sweep (via
  a new `_run_write(timeout=)`) and the scan; the timeout message now says how long it ran and that
  the command may have done part of its work. A timeout stays a plain, untolerated step failure.
  This commit.
- **Prevention:** `tests/test_offboard_safety.py::test_domain_wide_calls_get_the_long_timeout` (every
  `all users` call carries the long timeout, per-user calls keep the default),
  `tests/test_lifecycle.py::test_offboard_sweep_timeout_is_a_clear_step_failure` (the mock's
  `SWEEPSLOW` sleeps; the step fails with "timed out after … stopped", the reminder still runs).
  Mock-only proof: 1 h is an estimate from ~0.5–1 s/user, not a measured sweep.

## 2026-09-23 — The calendar role was checked on one share path and not the other

- **Symptom:** the 10/10 review (plan item Q7) found `/users/calendar/add` handed any posted `role`
  straight to `gam user … add calendaracls primary <role> …`, while `/calendars/share` silently
  replaced an unknown role with `reader` — one path relied on GAM to refuse a bad value, the other
  shared at a level nobody chose. Found by reading the code; not seen live.
- **Cause:** the check lived in one route (`ACL_ROLES` in `calendars.py`) instead of in the
  `GAMCommands` builders both paths share, unlike `add_group_member`'s `_validate_role`.
- **Why not caught:** no test posted an out-of-grammar role to either route; the strict mock rejects
  one, but only as a GAM error after the argv was already built and sent.
- **Fix:** `add_calendar_acl` / `add_calendar_acl_cal` validate against `CALENDAR_ACL_ROLES` (the
  grammar's `<CalendarACLRole>`, verbatim) and raise `ValueError`; both routes render it as
  "Couldn't share calendar: …" with no `gam` call; the silent coercion is gone. This commit.
- **Prevention:** `tests/test_commands.py::test_calendar_acl_role_is_validated_in_the_builder`,
  `tests/test_users_web.py::test_calendar_share_refuses_a_role_outside_the_grammar` (both routes),
  and `tests/test_command_contract.py::test_calendar_acl_roles_match_grammar_and_mock` (the
  builder's set = the mock's = the grammar's). Mock-only proof: no role has been shared live.

## 2026-09-23 — A bulk job where everyone failed listed every address in its final panel

- **Symptom:** the 10/10 review (plan item B2) found `BatchJob.failed` unbounded: the bulk
  department job, the calendar-share subscribe fan-out, the Builder sequence and offboarding
  `.append`ed every failure, and `_bulk_apply.html` / `_calendar_subscribe_job.html` joined the whole
  list — a domain-wide run that failed for thousands of users rendered thousands of addresses (and
  the subscribe poll counted them with `| length`). Found by reading the code; not seen live.
- **Cause:** invariant #9 was applied to `BatchJob.log` and to signatures' `ApplyJob` /
  onboarding's `OnboardJob`, but the shared `BatchJob` had no failure cap of its own, so each route
  appended directly.
- **Why not caught:** the scale tests covered `ApplyJob` and the subscribe *log*; nothing drove a
  `BatchJob` past a cap with every item failing.
- **Fix:** `BatchJob.fail()` keeps the full `failed_total` and a sample capped at
  `FAILED_SAMPLE_CAP` (200, same as the other two jobs); the four routes call it, and the two
  final panels print the total plus "… +K more". This commit.
- **Prevention:** `tests/test_users_web.py` —
  `test_batch_job_failures_stay_bounded_and_render_the_overflow`,
  `test_run_subscribe_caps_its_failed_sample`, and the source tripwire
  `test_no_route_appends_to_a_batch_jobs_failed_list_directly`.

## 2026-09-23 — Backup codes, browser tokens and file downloads ran from the Builder with no audit trail

- **Symptom:** the 10/10 review (plan item S9) confirmed that auto-promotion made `show`/`print
  backupcodes` (2-Step Verification bypass codes for any user), `show`/`print browsertokens` (Chrome
  enrollment tokens) and `get drivefile`/`get document` (any user's file contents) one click away in
  the Builder, and that running them left nothing in the audit log — a read of account-takeover
  material was indistinguishable from never having happened. Found by reading the code; not seen live.
- **Cause:** the "reads are open, only writes are audited" trade-off assumed a read's worst case is
  disclosure *to the operator*. These reads' output is itself a credential or a document, so who
  pulled them, and when, is exactly what an audit is for.
- **Why not caught:** the auto-promotion boundary test checks only that a promoted command is
  `READ_ONLY`; nothing classified reads by what their output is.
- **Fix:** operator decision D3 — keep them buildable, audit them. `SENSITIVE_READS` in `catalog.py`
  flags the six by verb + object (`CatalogCommand.sensitive`); `GAMConnector.catalog_read` records
  `sensitive_read` (target, argv, `extra.command`, ok/error — never the output) and a `todrive`
  export of one is `sensitive_export`. This commit.
- **Prevention:** `tests/test_builder.py` — `test_sensitive_reads_are_flagged_and_still_buildable`
  pins the six syntax heads (a GAM bump that renames one fails instead of silently losing its
  audit), `test_a_sensitive_read_is_audited_without_its_output` greps the audit file for the mock's
  canned codes, plus the failed-read and export cases; `AUDITED_READS` in the `audit.record` tripwire.
  Still unaudited: a sensitive read inside a Builder sequence is recorded only as `apply` (argv
  intact), and the CSV download of a tabular result.

## 2026-09-23 — Two writes ran un-audited: the Builder's Sheet export and reset_password's sign-out

- **Symptom:** the 10/10 review (plan item Q6) found two GAM writes that left no audit record. A
  Builder read with "Export to a Google Sheet" appended `todrive [tduser <user>]` and ran through
  `runner.run_authenticated` as a read — creating a Sheet, possibly in another user's Drive. And
  `reset_password` followed a successful reset with `gam user <x> signout` in a `try/except: pass`,
  serialized but never recorded. Found by reading the code; not seen live.
- **Cause:** "every mutation is audited" was enforced only by `test_audit_record_only_in_run_write_or_allowlist`,
  which looks for `audit.record(` outside `_run_write`. A write that never calls `record()` at all is
  invisible to it. The export was modelled as a read because the command it extends is a read; the
  sign-out was treated as fire-and-forget.
- **Why not caught:** no test enumerated the `run_authenticated(` call sites, and the export and
  offboarding tests asserted the GAM argv, not the audit trail.
- **Fix:** the export is `GAMConnector.export_to_sheet` → `_run_write` (audited `export_to_sheet`,
  target = the Sheet's owner; `ChangeResult.output` carries the Sheet URL); the sign-out calls
  `self.signout_user` (`_run_write`), audited `ok: false` on failure without failing the reset. The
  Builder's plain reads go through `catalog_read`, which refuses a non-`READ_ONLY` command. This commit.
- **Prevention:** `tests/test_command_contract.py::test_every_run_authenticated_call_is_the_chokepoint_or_a_read`
  — every `run_authenticated(` in `gamgui/` is `_run_write`, a named allowlist entry, or traces its
  argv to a builder classified as a read in `tests/test_mock_gam.py` without the write lock (it
  names both old call sites when run against the previous code). Plus the audit assertions in
  `test_builder.py` (export ok/failed, plain read unaudited) and `test_gam_connector.py`
  (sign-out ok/failed). Mock-proven only; neither write has run live from these paths.

## 2026-09-23 — Five write routes ran on a bare POST: the confirmation lived only in the templates

- **Symptom:** the 10/10 review (plan item S4) posted straight to `/users/suspend/apply`,
  `/users/bulk/apply`, `/calendars/event/delete`, `/lifecycle/offboard/run` and `/signatures/apply`
  with no confirmation field, and each ran: a suspend, an event delete, a company-wide signature
  overwrite, a full offboarding. `/onboard/run` without `create_account` did the same (task list,
  groups, calendars, welcome email). Offline against the mock; not seen live. Reaching them needs
  the launch token and a same-origin request, so the exposure was a mis-wired control or a stale
  form, not a remote caller — but a guard the server doesn't check is not a guard.
- **Cause:** those routes called `guard.evaluate` (if at all) only to *render* the confirm step; the
  apply route trusted that the POST came from it. Builder, account delete and calendar delete had
  their own server-side checks, so the pattern existed — it just wasn't one helper every route used.
- **Why not caught:** every route test posted the happy path; nothing posted a bare POST and asserted
  zero writes, and nothing enumerated the routes, so a new write route inherited no check by default.
- **Fix:** `guard.enforce(previews, form, confirm_step=)` — destructive → `confirmed=1`, destructive
  and bulk → typed "confirm", `confirm_step` routes (bulk jobs, onboarding) → `confirmed=1` always —
  called before the first GAM write by suspend, bulk department, event delete, offboarding (as
  destructive to the leaver), signatures, onboarding single + bulk, and the Builder's run/sequence.
  The confirm steps post the field (`hx-vals` / hidden input; onboarding's `confirm=1` became
  `confirmed=1`). Every button that starts a write or a job now has `hx-disabled-elt` (plan U13), so
  a double-click can't start two offboardings. This commit.
- **Prevention:** `tests/test_write_routes_guarded.py` — enumerates every POST route from the app's
  OpenAPI schema (and cross-checks the `@router.post` count), requires each to be exempt-with-reason
  or GATED; a bare POST to each gated route must reach the mock with zero writes, the confirm step
  the preview actually renders must run the write, and every template control posting to a write
  must carry `hx-disabled-elt`. `tests/test_guard.py` covers `enforce` itself. Mock-proven only:
  whether each write works live is unchanged (README live-verification status).

## 2026-09-23 — One benign "Does not exist" line made the offboarding calendar sweep's partial failure a success

- **Symptom:** a review found that the all-users calendar-ACL sweep, which tolerates `NOT_FOUND` and
  `PERMISSION_DENIED`, would report "Completed (best-effort)" for a run where one user's delete
  really failed, provided some other user's line said "Does not exist". Reproduced offline with the
  mock's new `SWEEPMIXED` trigger (a not-applicable user, a real "Internal error encountered" and the
  leaver's own-ACL refusal, exit 50) — the step came back `ok`, audited `tolerated: true`. Not seen live.
- **Cause:** `classify_stderr` searched the *whole* stderr for the first pattern in `_PATTERNS` order,
  and `_run_write` tolerated on that single `kind`. A multi-entity command prints one line per entity,
  so the first tolerable match hid every other line; an unrecognized line never counted at all.
- **Why not caught:** the mock's sweep failures were single-line (`OWNACL`, `SWEEPFAIL`), and every
  classifier test was one line — nothing fed it the multi-line, multi-entity stderr a real sweep emits.
- **Fix:** classify per line (unmatched → `UNKNOWN`; GAM's `Getting all`/`Got N` progress chatter
  skipped); `GAMError.kinds` carries every line's kind and `kind` the most severe (`_SEVERITY`);
  `_run_write` tolerates only when `kinds ⊆ tolerate_kinds`; `.message` shows the last line of the
  reported kind rather than the stderr's (possibly benign) tail. This commit.
- **Prevention:** `tests/test_errors.py` (mixed-stderr cases) and `tests/test_lifecycle.py`
  `test_offboard_calendar_sweep_multi_user_stderr` via the mock's `SWEEPBENIGN`/`SWEEPMIXED`, plus
  `test_offboard_safety.py` `test_remove_from_all_calendars_needs_every_line_tolerable`. The
  multi-line stderr is hand-written in GAM's shape, not a live capture: whether a real sweep prints
  another kind of stderr line (it would now fail the step, visibly) is unproven until the first real
  offboarding.

## 2026-09-23 — The loopback gate answered any `Host`, and exempted any path starting "/static"

- **Symptom:** a review found that `/healthz` answered a request whose `Host` was a DNS-rebound name
  (`evil.example:<port>` resolving to `127.0.0.1`) — a page on that name is same-origin with itself,
  so it could scan for our random port. And the static bypass would have waved a future `/staticfoo`
  or `/static-export` route through without the token. Reproduced offline with `TestClient`; not
  seen live.
- **Cause:** `TokenGateMiddleware.dispatch` (`web/server.py`) checked Origin and the token but never
  `Host` — the Origin comparison even took Host as "our authority" on trust. The static exemption was
  `path.startswith("/static")`, a string prefix rather than a path segment.
- **Why not caught:** every web test ran under TestClient's `Host: testserver`, which the gate had
  no opinion about; no test sent a foreign Host or a route sharing the `/static` prefix.
- **Fix:** `create_app(state, *, allowed_hosts)` — required, a bare string refused. `app.py` picks
  the port first and passes `loopback_hosts(port)` (`127.0.0.1:<port>`, `localhost:<port>`); the
  route suites pass `TEST_HOSTS` (`testserver`) explicitly. Host is checked first, before even
  `/healthz`. The bypass matches `/static` or `/static/…` only.
- **Prevention:** `tests/test_server_security.py` now runs the real `loopback_hosts` rule:
  `test_a_foreign_host_is_refused_everywhere` (7 hosts × 4 paths incl. `/healthz`),
  `test_both_loopback_spellings_of_our_port_are_accepted`,
  `test_allowed_hosts_is_required_and_never_a_bare_string`,
  `test_a_path_that_merely_starts_with_static_still_needs_the_token`; reverting either line fails
  them (mutation-checked). A real browser's Host under rebinding is not re-checked automatically.

## 2026-09-23 — A FIFO named `oauth2service.json` hung the credentials import, and the app with it

- **Symptom:** a review found that importing from a folder whose `oauth2service.json` is a FIFO
  never returned, and every other request stalled behind it until a writer opened the FIFO.
  Reproduced offline with `os.mkfifo`; not seen live.
- **Cause:** `_open_in_dir` (`core/setup.py`) opened the file blocking and only then did the
  `S_ISREG` check — the comment even said "a fifo would block", but the open that blocks runs first.
  `POST /setup/import` called the synchronous `import_dir` straight on the event loop, so one blocked
  `open` froze the whole server.
- **Why not caught:** the import tests covered symlinks, unreadable and vanishing files, never a
  non-regular one; and no route test checked where the blocking work runs.
- **Fix:** open `O_NONBLOCK` (a FIFO opens at once and is refused as not regular; the wipe's
  `O_WRONLY` fails `ENXIO`), then restore blocking mode for the regular file; the route runs
  `import_dir` via `asyncio.to_thread`. Invariant #5's pin + `O_NOFOLLOW` are unchanged.
- **Prevention:** `test_a_fifo_named_like_a_credential_is_refused_without_blocking` (core),
  `test_import_route_refuses_a_fifo_credential_quickly` and
  `test_import_route_runs_the_filesystem_import_off_the_event_loop` (route); the `fifo` fixture's
  watchdog releases a blocked reader so a regression fails on time instead of hanging the suite.

## 2026-09-23 — The stale-config sweep zeroed files through a planted `gamcfg-*` symlink

- **Symptom:** a review found that a `gamcfg-*` symlink in the runtime dir (`…/GamGUI/run`) made the
  shutdown sweep (`max_age_seconds=0`) overwrite every file in the link's target with zeros; a
  symlinked file inside a real `gamcfg-*` dir got its target zeroed the same way, and a FIFO
  `.gamgui.pid` would hang the startup sweep. Reproduced offline with sentinel files; not seen live.
- **Cause:** `sweep_stale_configs` tested `child.is_dir()` (follows links) and `_shred_dir` walked
  `path.iterdir()` and zeroed each `is_file()` via `open(child, "r+b")` — every step followed a
  symlink, and `_owner_pid` read the marker with a blocking, link-following `read_text()`. The
  `rmtree` at the end already refused links, which hid that the zeroing before it did not.
- **Why not caught:** the sweep tests only ever built real dirs with real files; nothing planted a
  link or a non-regular file in the runtime dir.
- **Fix:** the sweep skips `is_symlink()` entries; `_shred_dir` opens the dir
  `O_DIRECTORY|O_NOFOLLOW` and zeroes each entry through that descriptor with
  `O_NOFOLLOW|O_NONBLOCK`, regular files only; `_owner_pid` reads the marker the same way.
- **Prevention:** `test_shutdown_sweep_never_follows_a_symlinked_gamcfg_entry`,
  `test_shred_dir_does_not_zero_a_symlinked_file_inside_the_dir`,
  `test_shred_dir_leaves_a_symlink_given_as_the_dir_alone`,
  `test_sweep_does_not_follow_a_symlinked_pid_marker`, `test_sweep_is_not_blocked_by_a_fifo_pid_marker`
  in `tests/test_ephemeral.py`; the secrets runbook records the rule.

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
