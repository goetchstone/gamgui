# Domain: Lifecycle — Offboarding

**One line:** The ordered "a user is leaving" routine — reset password, revoke access (sessions, app
passwords, backup codes, OAuth tokens), turn off mail forwarding, delegate + auto-reply the mailbox, transfer Drive/Calendar,
sweep the user off everyone's calendars, and drop a dated reminder on the manager — plus the
separate, gated account **delete** that IT runs later.

**Owns invariant(s):** the two domain rules called out in the task — **delete is gated on
data-transfer completion** (advisory) and **the all-users calendar-ACL sweep is best-effort**
(tolerates own-calendar / not-shared / no-Calendar, nothing else). Both flow through the write chokepoint (CLAUDE.md #2) and the
combined transfer service list is one argv element (CLAUDE.md #1).
**Enforcement home:** `tests/test_offboard_safety.py`, `tests/test_lifecycle.py`; the mock
`tests/fixtures/mock_gam.sh` reproduces the real 409 and cannotChangeOwnAcl stderr.

## Files
- `gamgui/core/lifecycle.py` — pure step builder. `build_offboard_steps(...)`, `fill_autoreply`,
  `OffboardStep` dataclass (its `commands` = the exact argv(s) it runs), `command_line` (argv → the
  quoted, redacted `gam …` line the preview shows), `check_addresses` (both addresses against the
  directory → `AddressCheck` errors/warnings), `DEFAULT_SUBJECT` / `DEFAULT_MESSAGE`. No scheduler,
  no persisted state.
- `gamgui/web/routes/lifecycle.py` — `/lifecycle` page + `/offboard/{preview,autoreply,run,status}`.
  Executes the steps as a progress-tracked `BatchJob` (`_run_offboard`); name-resolution helpers;
  `_running` (one offboarding per leaver).
- `gamgui/web/routes/users.py` (lines ~425-455) — the **delete** flow: `delete_zone`,
  `delete_confirm` (warns on pending transfers), `delete_apply` (type-the-exact-email confirm —
  `guard.enforce`'s rule for every account delete, so the Builder's "Delete account" asks for the
  address and warns on a pending transfer too).
- `gamgui/core/connectors/gam_connector.py` — the real mutations: `transfer_data`,
  `remove_from_all_calendars`, `reset_password`, `revoke_access`, `forward_off`, `delete_user`,
  `add_delegate`, `set_vacation`, `add_calendar_event`; all via `_run_write` (`tolerate_kinds`). `incomplete_transfers_for` is a
  **read** (direct `run_authenticated`, not `_run_write`) that the delete gate calls.
- `gamgui/core/gam/commands.py` (~275-335) — argv builders (`create_datatransfer`,
  `remove_all_calendar_acls`, `print_datatransfers`, `delete_user`, `reset_password`,
  `deprovision_user`, `forward_off`, …).

## How it works
**Both addresses are checked first**, by the preview and again by the run (`_check` →
`lifecycle.check_addresses`, against the cached `gam print users`, ≤5 min old). Blocked, before any
write: an address not in the directory, an alias (the message names the primary), the same account
twice, and a directory that can't be read (fails closed). Warned in the preview, not blocked: a
super-admin or delegated-admin leaver (offboarding doesn't remove the role), an already-suspended
leaver (mailbox steps may fail), a suspended manager (delegate/transfer/reminder go there), a
manager who is already the leaver's delegate (the preview only; see "Re-running after a failure"), and
a `print delegates` read of the leaver that failed — the delegate step uses the same Gmail access, so
it will likely fail after the irreversible reset (the warning quotes GAM's error; the read was once
swallowed and the preview looked clean, failure-log 2026-09-23). The
steps then act on the directory's primary addresses, whatever case was typed. A typo'd manager used
to be accepted and half-offboard the account (failure-log 2026-09-23).

`build_offboard_steps` returns 8 ordered `OffboardStep`s (`password`, `revoke`, `forward`,
`delegate`, `vacation`, `transfer`, `calacls`, `reminder`), each a `lambda conn: conn.<method>(...)` plus
`commands`, the one argv that method runs (one command per step, so each has its own `✓/✗`), built from the same `GAMCommands` builders and values. It is pure and
testable; the route runs it. The **preview** lists every step with its exact command
(`command_line`: one `gam …` line per argv, each element shell-quoted so a subject with spaces is
visibly one argument; a value after a sensitive key masked as the audit log masks it, GAM's own
`password random` shown as is — offboarding carries no secret). Display only: nothing is ever run
through a shell. `test_offboard_preview_commands_are_what_runs` holds the preview to what the mock
receives, in order. `offboard_run` first refuses a POST without `confirmed=1`
(`guard.enforce`, the leaver declared `DESTRUCTIVE`: the preview's Run button posts it, and
`hx-disabled-elt` stops a double-click starting a second run — a bare POST once ran the whole
routine, failure-log 2026-09-23). **Run executes exactly what was previewed**: the preview holds the
steps it built (`_Preview`) under a single-use token that the Run button posts (`hx-vals`) — the
shared `AppState.previews` store (`web/previews.py`: at most 8 per flow, for `PREVIEW_TTL`, 15 min),
which the other confirm steps now use too. Run refuses a missing, used or expired
token, and a live form (`hx-include`) that no longer matches the previewed one (`_form_key`) — edit a
field after Preview and you must preview again. It then re-checks the previewed addresses against the
directory, refuses a leaver whose offboarding is still running (below), and hands the held steps,
never rebuilt ones, to `start_job` and `_run_offboard` (once it
rebuilt them from the live form, failure-log 2026-09-23). An emptied subject/message runs the default
text, as the auto-reply block shows. `_run_offboard` runs each step in order and appends a `✓/✗`
line to `job.log`; **a step whose `requires` did not all succeed is not run** (a `–` line, listed in
`job.skipped`, and the panel says "Offboarding stopped"). The green "Offboarding complete" panel
means every step succeeded; any `✗` makes it "incomplete" (amber, with the recovery steps). The
"timer" is the last step: a calendar reminder event on the manager's calendar `days` out — there is
no app-side scheduler. The final
account **delete** is a distinct guarded action on the user detail page (`delete_user`,
`RiskLevel.DESTRUCTIVE`).

**One offboarding per leaver at a time.** `AppState.offboard_jobs` maps a leaver to their running
job; while it runs, a Preview or a Run for that leaver is refused with the running job's own
progress panel (`_offboard_running.html`), the way back to it after a reload or leaving the page.
Two held previews once both ran, interleaved: two resets, transfers, sweeps and reminders
(failure-log 2026-09-23). The check and the registration have no `await` between them.

### When a step fails
`lifecycle.REQUIRES`, pinned by `test_offboard_step_dependencies_are_the_documented_ones`. Until
2026-09-23 no failure stopped anything, so a failed reset or a bad manager still ran every later
step (failure-log).

| Step | If it fails | Why |
|---|---|---|
| Reset password | **stop** — nothing else runs | The lock is the point. Nothing may announce the departure or move data while the old password still works, and a first-step failure (wrong credentials, a missing user) usually fails every step. |
| Revoke access & sign out | continue — `✗`, and the run is not "complete" | Runs straight after the reset, before anything is handed over, and nothing but the reset can stop it. It gates nothing: the reset has already stopped new sign-ins, and its likeliest failure (a missing `admin.directory.user.security` scope) would otherwise strand the mailbox with no delegate. The panel says the leaver may still be signed in. Until 2026-09-23 the sign-out rode inside the reset and a failure was swallowed (failure-log). |
| Turn off forwarding | continue — `✗`, and the run is not "complete" | Like the revoke: straight after the reset, stopped by nothing but the reset, gating nothing. Runs whether or not forwarding was on — the leaver can switch it on until the sign-out, so a preview read could be stale. |
| Set delegate | **stop** — nothing else runs | The first write to the manager, who also receives the transfer and the reminder. |
| Auto-reply | continue | Nothing depends on it; senders get no auto-reply until it's re-run. |
| Transfer Drive & Calendar | continue, **but no reminder** | The reminder asks the manager to approve deletion, and deleting before the transfer loses the files for good. A transfer that was never created leaves nothing for the delete screen's pending-transfer warning to find. |
| Remove from everyone's calendars | continue | The account is locked, so a leftover share grants nothing; the reminder doesn't depend on it. |
| Manager reminder | (last) | — |

### Re-running after a failure
The form has one "already done" box per step (`lifecycle.STEP_NAMES`, posted as `done`). A ticked
step is not run and **counts as succeeded** for the steps that require it; the preview shows it
struck through, without a command, and the ticks are part of the frozen form (tick one after Preview
and Run refuses). All eight ticked is refused ("nothing to run"). **Tick exactly the `✓` lines of the
failed run** — ticking a step that did not succeed tells the routine it did (tick the transfer and
the reminder runs without one). What each step does if run again after it succeeded (GAM 7.48.11):

| Step | Run again after it succeeded | Tick it? |
|---|---|---|
| Reset password | Harmless: `password random` makes a new password each time. | optional |
| Revoke access & sign out | Harmless: whatever app passwords and tokens are left are deleted, the backup codes are invalidated again, sessions are ended again. | optional |
| Turn off forwarding | Harmless: `forward off` when it's off changes nothing. **Leave it unticked if "Revoke access" failed** — a session left open could have switched forwarding back on. | optional |
| Set delegate | **Fails.** GAM catches the Gmail API's `alreadyExists` and reports "Add Failed" with exit 50 (`processDelegates` → `entityActionFailedWarning`, read statically from the vendored build's bytecode) — and a failed delegate stops the routine. The preview warns when the manager is already a delegate (`_delegate_warning`, a `print delegates` read), which also covers a manager who had access before offboarding started. | **yes** |
| Auto-reply | Harmless: the command names every setting (on, subject, message, every sender, no start or end date), so a re-run writes the same reply. GAM itself *merges* `vacation` into the stored settings — see Gotchas. | optional |
| Transfer Drive & Calendar | **Fails** while the first is still in progress (409 "already in progress", mock `CONFLICT409`), which also skips the reminder. After the first completes, a new one moves what the leaver still owns — normally nothing (Data Transfer API semantics, unverified live). | **yes** |
| Remove from everyone's calendars | Harmless: users without an ACL for the leaver answer "does not exist" and users without Calendar "Calendar Service/App not enabled", both tolerated. Takes as long as the first time. A sweep that timed out partway should be re-run. | optional |
| Manager reminder | **Duplicates**: `add event` without an `id` creates a second event. | **yes** |

A failed step changed nothing, except a sweep stopped by its timeout (partly done — run it again).

## Grammar check of every step (GAM 7.48.11, re-verified 2026-09-23)
Each argv our builder emits, next to its line in the vendored `gamgui/resources/gam7/GamCommands.txt`
(line numbers at this pin). Where the grammar was ambiguous the vendored GAM build's own argument
parser was read statically (its bytecode, never run). No mismatch found.

| Step | argv we emit | Grammar | Verdict |
|---|---|---|---|
| Reset password | `update user <leaver> password random changepassword off` | `gam update user <UserItem> [ignorenullpassword] <UserAttribute>*` (5942); `<UserBasicAttribute>` (5823): `(password (random [<Integer>])\|…)`, `(changepassword\|changepasswordatnextlogin <Boolean>)` | Match. GAM generates the password; we never see it. |
| Revoke access & sign out | `user <leaver> deprovision signout` | `gam <UserTypeEntity> deprovision\|deprov [popimap] [signout] [turnoff2sv]` (7899) | Match. Per user, GAM (`deprovisionUser`, read statically) deletes every app password, invalidates the backup codes, deletes every OAuth token, then with `signout` calls `users.signOut` (as `gam <UserTypeEntity> signout`, 8938, does). A failed listing or sign-out is reported against the user (exit 50) and ends that user's remaining parts; one app password or token that fails to delete is reported and the rest go on. **Not** `turnoff2sv` — it would weaken a locked account, and nobody needs to sign in as the leaver (the manager gets delegation). **Not** `popimap` — POP/IMAP need the password, an app password or a token, all revoked here; it would add two Gmail settings writes for nothing. |
| Turn off forwarding | `user <leaver> forward off` | `gam <UserTypeEntity> forward <FalseValues>` (7998); `<FalseValues>= false\|off\|no\|disabled\|0` (22) | Match. GAM (`setForward`) sends `updateAutoForwarding` with `enabled: false` and shows the result; a user without Gmail is "Service/App not enabled", exit 73. The same builder as the Builder's "Turn off forwarding". |
| Delegate | `user <leaver> add delegate <manager>` | `gam <UserTypeEntity> create\|add delegate\|delegates [convertalias] <UserEntity>` (7910) | Match; `convertalias` optional, unused. |
| Auto-reply | `user <leaver> vacation on subject <S> message <M> html contactsonly false domainonly false start Started end NotSpecified` | `gam <UserTypeEntity> vacation [<Boolean>] [subject <String>] [<VacationMessageContent> …] [html [<Boolean>]] [contactsonly [<Boolean>]] [domainonly [<Boolean>]] [start\|startdate <Date>\|Started] [end\|enddate <Date>\|NotSpecified]` (8283-8288); `<VacationMessageContent>` ::= `(message\|textmessage\|htmlmessage <String>)\|…` | Match, in grammar order; no `formatjson`. GAM's `getYYYYMMDD` compares `Started`/`NotSpecified` case-insensitively and returns no date for them, which clears a stored one. Why every setting is named, and `html`: see Gotchas. |
| Transfer Drive + Calendar | `create datatransfer <leaver> drive,calendar <manager>` | `gam create\|add datatransfer\|transfer <OldOwnerID> <DataTransferServiceList> <NewOwnerID> [private\|shared\|all] [release_resources] (<ParameterKey> <ParameterValue>)* [wait …]` (3598) | Match; the service list is ONE element. No privacy keyword: see Gotchas. |
| Calendar sweep | `all users delete calendaracls primary <leaver>` | `gam <UserTypeEntity> delete calendaracls <UserCalendarEntity> <CalendarACLRole>] <CalendarACLScopeEntity>` (6327) | Match. The grammar line lost its `[`: the role is optional, as in `calendars … delete acls [<CalendarACLRole>]` (1689) and in GAM's parser (`getChoice(…, defaultChoice=None)`, then the scope, then no extra arguments). `all users` = `<UserTypeEntity>`, `primary` = `<UserCalendarEntity>`, a bare address = `<CalendarACLScope>` (a user). |
| Manager reminder | `user <manager> add event primary summary <S> start allday <D> end allday <D+1> [description <T>] [attendee <E>]` | `gam <UserTypeEntity> create\|add event <UserCalendarEntity> [id <String>] <EventAttribute>+ [<EventNotificationAttribute>]` (6469); `<EventAttribute>` (6391): `summary`, `start\|starttime (allday <Date>)`, `end\|endtime (allday <Date>)`, `description`, `attendee <EmailAddress>` | Match. The end date is exclusive, so a one-day event on D. |
| Delete (later, user detail page) | `delete user <leaver>` | `gam delete user <UserItem> [noactionifalias]` (5964) | Match; `noactionifalias` unused (the page passes the primary address). |
| Undelete (Builder) | `undelete user <email>` | `gam undelete user <UserItem> [ou\|org\|orgunit <OrgUnitPath>]` (5965) | Match. |
| Delete gate (read) | `print datatransfers olduser <leaver>` | `gam print datatransfers\|transfers [todrive …] [olduser\|oldowner <UserItem>] …` (3603) | Match. |

## Invariants & the failure history
- **Delete gated on transfer completion (advisory).** `delete_confirm` calls
  `incomplete_transfers_for(email)` and loudly warns if a Drive/Calendar transfer is still running —
  deleting then permanently loses the un-transferred data. It is a warning + type-the-email confirm,
  **not a hard block**; `incomplete_transfers_for` returns `[]` on any read error, so an inability to
  check never blocks deletion (it just can't warn). Added in `b26da7e`.
- **Calendar sweep is best-effort.** `remove_from_all_calendars` runs `all users delete calendaracls
  primary <email>` and passes `tolerate_kinds=SWEEP_TOLERATED` to `_run_write`: NOT_FOUND = that
  user never shared with the leaver; SERVICE_NOT_ENABLED = a user without Calendar ("User: x,
  Calendar Service/App not enabled", exit 73 — `all users` is every active user, so a Calendar-off
  OU or a licence without Calendar is enough); OWN_ACL = the leaver's OWN primary calendar
  (`cannotChangeOwnAcl`, exit 50). All expected, so the step still counts as success and is audited
  `ok=True, tolerated=True`. Anything else fails it: a real SCOPE_MISSING/AUTH_EXPIRED, a 403 for
  some user (PERMISSION_DENIED), an unrecognized line. Until 2026-09-23 a user without Calendar
  failed the step on every run, and every PERMISSION_DENIED line was tolerated, so a real 403 read
  as a clean sweep (failure-log). The sweep
  prints one stderr line per user, so tolerance needs **every** error line to be tolerable
  (`GAMError.kinds`, 2026-09-23): one unrecognized per-user failure among the benign lines fails the
  step and is the detail shown. Before that, the first "Does not exist" line classified the whole
  stderr `NOT_FOUND` and a partial failure was reported as success.
- **The sweep is one domain-wide call, so it gets a domain-wide timeout.** GAM visits every user in
  turn inside one process (~0.5–1 s each); under the runner's 120s default a sweep of a few hundred
  users was killed partway (failure-log 2026-09-23). `remove_from_all_calendars` passes
  `DOMAIN_WIDE_TIMEOUT` (1 h, `core/gam/runner.py`). A timeout is a plain step failure — never
  tolerated — and its log line reads "timed out after 60 min and was stopped; it may have done part
  of its work", so some users may still share with the leaver; the routine goes on to the reminder.
  The write lock is held for the whole sweep, so other writes in the app wait behind it.
- **One transfer, not two (CLAUDE.md #1 + #2).** `26eee5b` (post-mortem from a real audit log):
  offboarding fired two `create datatransfer` calls (drive, then calendar); Google DTS allows one
  in-flight transfer per user, so the second returned "409: already in progress" and was silently
  ignored. Fix: a single call with the comma-joined service list `"drive,calendar"` — a valid
  `<DataTransferServiceList>` that rides as **one argv element**. Reduced offboard from 7 steps to 6.
- **Ending access is its own step, and its failure is a failure (2026-09-23).** The sign-out used to
  run inside `reset_password` as a best-effort follow-up whose `ChangeResult` was discarded: a
  refused sign-out showed `✓ Reset password` and "complete — 6 of 6 steps succeeded" while the
  leaver's sessions stayed open, and a re-run (tick the `✓` lines) could never retry it. Reset +
  sign-out also left app passwords, backup codes, connected apps' OAuth tokens and mail forwarding
  working. Now `revoke` runs `deprovision signout` and `forward` runs `forward off`, each its own
  step; `reset_password` runs only the reset.
- The one-transfer commit also fixed the sweep misclassification: `classify_stderr` mapped "Cannot
  change your own access level" to UNKNOWN (no 403 token), so the already-present PERMISSION_DENIED tolerance never
  fired. `errors.py` mapped `cannotChangeOwnAcl` → `PERMISSION_DENIED`; since 2026-09-23 it is its own
  kind, `OWN_ACL`, and the sweep no longer tolerates PERMISSION_DENIED.

## Gotchas / mock-lies traps
- **`set_vacation` rejects `formatjson`** (see MEMORY / the formatjson gotcha) — the mock can't catch
  that; verify against the vendored grammar `gamgui/resources/gam7/GamCommands.txt`.
- **GAM's `vacation` merges, it does not replace.** `setVacation` (read statically from the vendored
  build) reads the stored settings (`getVacation`), overwrites only the fields the command names and
  writes the result back. The auto-reply once named only on/subject/message, so a leaver who had ever
  answered only people in their organization (`restrictToDomain`) or only their contacts, or set a
  last day that is now past, kept it: customers got no auto-reply, or nobody did, and the step
  showed ✓ (failure-log 2026-09-23). `GAMCommands.set_vacation` now always sends `contactsonly
  <bool> domainonly <bool> start <date>|Started end <date>|NotSpecified` — which also fixed the user
  page's Vacation form, where unticking "Domain only" sent nothing. That form now shows the stored
  dates (`Vacation.start`/`.end`), so a blank box means "no date", not "keep a hidden one". The mock
  merges the same way when `GAM_MOCK_STATE` is set (the `gam_state` fixture), and `show vacation`
  then prints the stored settings with GAM's Start/End Date lines.
- **What the routine does not cut off.** Sign-in through a third-party identity provider (SSO)
  doesn't use the Google password: disable the user there too. Gmail filters that forward mail are
  not touched (`forward off` is the account's auto-forward setting only): look at them before the run (Builder → Users → Gmail - Filters → Show filters, a
  read). Admin roles aren't removed either (the preview warns).
- `SIGNOUTFAIL` in an address makes the mock refuse the sign-out (`signout`, and `deprovision … signout`
  after its other parts) the way a missing security scope does: "Sign Out Failed: Not Authorized to
  access this resource/api", exit 50. The deprovision handler's stdout is GAM's shape, not captured.
  `FWDFAIL` makes `forward` fail as for a user without Gmail (exit 73); `forward` also fails for a
  `missing` user now, as GAM does.
- The mock only 409s when the old-owner email contains the literal `CONFLICT409`. The `all users
  delete calendaracls` sweep succeeds by default; `OWNACL` in the address emits the exact own-ACL
  stderr (exit 50, tolerated) and `SWEEPFAIL` a scope error (not tolerated); `SWEEPBENIGN` a
  multi-user stderr where every line is tolerable (not-shared, a user without Calendar, the own ACL)
  and `SWEEPMIXED` the same plus one real per-user
  failure (not tolerated); `SWEEPSLOW` sleeps so the timeout fires. Every step's write has a strict
  handler that fails a malformed argv. It does **not** model real DTS async timing, partial
  multi-app transfer failures, or per-user calendar iteration — a green sweep/transfer test proves
  classification/argv, not that a live tenant transfers cleanly.
- **Route tests offboard fixture users** (`carol@` leaves, `alice@` takes over): the directory check
  refuses anyone else. The executor tests call `build_offboard_steps` + `_run_offboard` directly, so
  they can use any address — including the mock's trigger substrings above.
- **The transfer names no Drive privacy level.** GAM 7.48.11 sends `PRIVACY_LEVEL` only when
  `private|shared|all` is given (`all` = `PRIVATE,SHARED`; read from the vendored build's parser) —
  without one, the Data Transfer API's own default decides whether files the leaver *shared* move to
  the manager. Unverified which; check the manager's Drive after the first live run, before the
  account is deleted. GAM also refuses a transfer to the same user (the step fails with a usage error).
- **The auto-reply is sent as HTML** (`html`): line breaks typed into the message collapse and `<`/`&`
  are markup, while the preview block shows the line breaks. A one-paragraph message is unaffected.
- `incomplete_transfers_for` reads `overallTransferStatusCode` (falling back to `status`) and treats
  anything not `"completed"` as pending; a real tenant's status vocabulary is the source of truth.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_lifecycle.py tests/test_offboard_safety.py` (offline: mock
gam + in-memory Keychain). Covered: step order/keys, the single combined-service transfer + audit
argv, the second-same-user 409 still failing hard, sweep tolerance (own-ACL, not-found, a user
without Calendar) vs. real auth errors and a per-user 403, a mixed multi-user stderr (all-tolerable vs. one real failure), the sweep's long
timeout and a timeout as a clear step failure (`test_offboard_sweep_timeout_is_a_clear_step_failure`),
auto-reply substitution, the auto-reply not inheriting the leaver's old vacation settings (stateful
mock, `test_offboard_autoreply_does_not_inherit_the_leavers_old_vacation_settings`), reminder invitee, `incomplete_transfers_for` filtering, the directory check
(unknown/alias/same-account blocked on preview and run, admin/suspended warnings), the frozen preview
(Run's writes = the previewed lines, `test_offboard_run_executes_exactly_the_previewed_commands`; an
edited form, a used/expired token and a directory change are refused), the dependency rules (a failed
reset / delegate / transfer against the mock's `missing` and `CONFLICT409` triggers; a refused
sign-out is a `✗` that stops nothing, `test_offboard_failed_sign_out_is_a_failed_step_that_stops_nothing`,
and the panel then isn't "complete", `test_offboard_a_refused_sign_out_is_a_failed_step_not_a_clean_run`;
forwarding turned off after the revoke, and its failure stopping nothing,
`test_offboard_turns_off_forwarding_and_a_failure_stops_nothing`), the re-run ticks
(`test_offboard_rerun_runs_only_the_steps_not_ticked_done`, the already-a-delegate warning), the
unreadable-delegates warning (`test_offboard_preview_warns_when_the_leavers_delegates_cannot_be_read`),
one run per leaver (`test_offboard_refuses_a_second_run_for_a_leaver_whose_offboarding_is_running`),
and the preview's commands: each step's exact `gam` line in the page, and the previewed argv = what the mock received
(`test_offboard_preview_commands_are_what_runs`).
**Not proven offline** (the mock lies): a live DTS transfer of a real user's Drive+Calendar, the
actual per-user calendar sweep at domain scale, `deprovision signout` and `forward off` (never run
live: their output, and `deprovision` for a user with no app passwords or tokens), and `delete_user`
itself. Per CLAUDE.md, these must be run against a **throwaway** account before being trusted.

## First live run checklist
Offboarding a real user is the live test (plan D8). Keep this page open.

**In the preview, before Run**
- The header names the right person and says "8 steps" (nothing ticked as already done).
- Look at the leaver's Gmail filters (Builder → Users → Gmail - Filters → Show filters): a filter
  that forwards mail keeps forwarding after the routine turns auto-forwarding off. Delete it by hand
  (Gmail settings, or the Admin console) if there is one. Builder → Users → Gmail - Forwarding →
  Show tells you whether auto-forwarding is on now, and to where — note it if so.
- Every warning is dealt with: a super-admin leaver's role revoked first (and another super admin
  exists; GamGUI isn't connected as the leaver — "Revoke access" would delete GamGUI's own token); a manager who is already a delegate → tick
  "Set delegate"; "Couldn't read … mail delegates" → fix what it quotes (Gmail off for the leaver, a
  missing Gmail scope) and preview again, or the delegate step fails after the reset.
- Each `gam` line: the leaver everywhere, the manager in the delegate, transfer and reminder;
  `drive,calendar` as one argument; the auto-reply text as senders should read it (sent as HTML, so
  line breaks collapse); the reminder date and invitee.
- Expect the calendar sweep to take minutes (one call that visits every user; up to 1 h), with other
  writes in the app waiting behind it. Don't close the app mid-run. Left the page or reloaded? Enter
  the same two addresses and Preview: it shows the running offboarding's progress instead.

**After the run** — the panel should say "Offboarding complete — 8 of 8 steps succeeded"; GamGUI →
Audit shows eight `ok` records, one per step. Then check in Google, not just in GamGUI:
- Sign-in: the leaver's old password no longer works (the Admin console's admin audit log —
  Reporting → Audit and investigation — lists the password change and the sign-out). The user's
  Security panel in the Admin console lists no app passwords and no connected apps. If they sign
  in through a third-party identity provider, disable them there.
- Forwarding: Builder → Users → Gmail - Forwarding → Show says forwarding is off.
- Mailbox: the manager's Gmail account switcher offers the leaver's mailbox (delegation can take a
  while to appear); an email to the leaver from an account **outside your domain** (a personal
  address) gets the auto-reply — outside, because a reply limited to the organization would still
  answer a colleague. GamGUI → the user → Vacation responder shows it on, "Domain only" and
  "Contacts only" unticked and no dates.
- Transfer: the Builder's Data Transfers → Print (a read, `gam print datatransfers`, every transfer)
  shows it `completed` after ~10–25 min; the manager's My Drive then has a folder of the leaver's files. Check whether files the
  leaver had **shared** moved too (the privacy-level gotcha above) and whether their secondary
  calendars are now the manager's. **Record what you find in the README's live-verification status.**
- Calendar sweep: a colleague who had shared a calendar with the leaver no longer lists them
  (Calendar → Settings → Share with specific people).
- Reminder: the all-day event is on the manager's calendar on the date; the invitee got the invite.

**If a step fails** — the panel names it, its `✗` line says why, and `–` lines were not run (the
"When a step fails" table). Nothing is half-done except a sweep stopped by its timeout.
1. Fix the cause. Auth/scope errors: re-run setup. Reset failed: check the leaver's account — nothing
   else ran. Revoke access failed: the leaver may still be signed in — usually the
   `admin.directory.user.security` scope (re-do the domain-wide delegation step); the user's page has
   "Sign out everywhere" for the sessions alone. Delegate failed: "already exists" means it's done (tick it); "does not exist" or a
   suspended account: fix the manager or leaver. Transfer 409: a transfer is already running — wait
   for `completed` (Data Transfers → Print), then tick it.
2. Tick the steps whose lines are `✓` (and any step you've decided to skip — ticked means "don't run,
   treat as done"), Preview again, check the header says "N of 8 steps", Run.
3. **Don't delete the account** until the transfer shows `completed`.

**Wrong person offboarded?** There is no undo routine. By hand: set a new password in the Admin
console; remove the delegate and turn the auto-reply off on the user's detail page; delete the
reminder event. Calendar shares removed by the sweep are gone — colleagues must re-share. A transfer
can't be reversed by a second transfer (one from the manager back would move *all* the manager's
files): move the leaver's folder back by hand.

## To do common tasks here
- **Add/reorder an offboard step:** edit `build_offboard_steps` in `core/lifecycle.py` (add an
  `OffboardStep` whose lambda calls a connector method), add the connector method + its
  `GAMCommands` argv builder if new, then update `tests/test_lifecycle.py` (order + right-method
  assertions).
- **Change what the sweep tolerates:** edit `SWEEP_TOLERATED` in `gam_connector.py` and, if a
  new stderr phrase is involved, the regex table in `core/gam/errors.py`; extend
  `tests/test_offboard_safety.py`.
- **Change the delete gate:** edit `delete_confirm`/`delete_apply` in `web/routes/users.py` (and the
  Builder's `_pending_transfers`, which warns the same way) and `incomplete_transfers_for` in the
  connector. The type-the-email confirm lives in `guard.enforce` (`typed_emails`) — keep it there, so
  every route that deletes an account gets it — and never let a read-error hard-block deletion.
- **Auto-reply wording/placeholders:** `DEFAULT_SUBJECT` / `DEFAULT_MESSAGE` and `fill_autoreply`
  (`{employee}`, `{manager}`, `{contact}`) in `core/lifecycle.py`.
