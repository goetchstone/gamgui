# Domain: Lifecycle — Offboarding

**One line:** The ordered "a user is leaving" routine — reset password, delegate + auto-reply the
mailbox, transfer Drive/Calendar, sweep the user off everyone's calendars, and drop a dated reminder
on the manager — plus the separate, gated account **delete** that IT runs later.

**Owns invariant(s):** the two domain rules called out in the task — **delete is gated on
data-transfer completion** (advisory) and **the all-users calendar-ACL sweep is best-effort**
(tolerates own-calendar / not-shared). Both flow through the write chokepoint (CLAUDE.md #2) and the
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
  Executes the steps as a progress-tracked `BatchJob` (`_run_offboard`); name-resolution helpers.
- `gamgui/web/routes/users.py` (lines ~425-455) — the **delete** flow: `delete_zone`,
  `delete_confirm` (warns on pending transfers), `delete_apply` (type-the-exact-email confirm).
- `gamgui/core/connectors/gam_connector.py` — the real mutations: `transfer_data`,
  `remove_from_all_calendars`, `reset_password`, `delete_user`, `add_delegate`, `set_vacation`,
  `add_calendar_event`; all via `_run_write` (`tolerate_kinds`). `incomplete_transfers_for` is a
  **read** (direct `run_authenticated`, not `_run_write`) that the delete gate calls.
- `gamgui/core/gam/commands.py` (~275-325) — argv builders (`create_datatransfer`,
  `remove_all_calendar_acls`, `print_datatransfers`, `delete_user`, `reset_password`, …).

## How it works
**Both addresses are checked first**, by the preview and again by the run (`_check` →
`lifecycle.check_addresses`, against the cached `gam print users`, ≤5 min old). Blocked, before any
write: an address not in the directory, an alias (the message names the primary), the same account
twice, and a directory that can't be read (fails closed). Warned in the preview, not blocked: a
super-admin or delegated-admin leaver (offboarding doesn't remove the role), an already-suspended
leaver (mailbox steps may fail) and a suspended manager (delegate/transfer/reminder go there). The
steps then act on the directory's primary addresses, whatever case was typed. A typo'd manager used
to be accepted and half-offboard the account (failure-log 2026-09-23).

`build_offboard_steps` returns 6 ordered `OffboardStep`s (`password`, `delegate`, `vacation`,
`transfer`, `calacls`, `reminder`), each a `lambda conn: conn.<method>(...)` plus `commands`, the
argv(s) that method runs, built from the same `GAMCommands` builders and values. It is pure and
testable; the route runs it. The **preview** lists every step with its exact command
(`command_line`: one `gam …` line per argv, each element shell-quoted so a subject with spaces is
visibly one argument; a value after a sensitive key masked as the audit log masks it, GAM's own
`password random` shown as is — offboarding carries no secret). Display only: nothing is ever run
through a shell. `test_offboard_preview_commands_are_what_runs` holds the preview to what the mock
receives, in order. `offboard_run` first refuses a POST without `confirmed=1`
(`guard.enforce`, the leaver declared `DESTRUCTIVE`: the preview's Run button posts it, and
`hx-disabled-elt` stops a double-click starting a second run — a bare POST once ran the whole
routine, failure-log 2026-09-23). **Run executes exactly what was previewed**: the preview holds the
steps it built (`_Preview`, on `AppState.offboard_previews`, at most 8) under a single-use token that
the Run button posts (`hx-vals`), for `PREVIEW_TTL` (15 min). Run refuses a missing, used or expired
token, and a live form (`hx-include`) that no longer matches the previewed one (`_form_key`) — edit a
field after Preview and you must preview again. It then re-checks the previewed addresses against the
directory and hands the held steps, never rebuilt ones, to `start_job` and `_run_offboard` (once it
rebuilt them from the live form, failure-log 2026-09-23). An emptied subject/message runs the default
text, as the auto-reply block shows. `_run_offboard` runs each step in order and appends a `✓/✗`
line to `job.log`; **a step whose `requires` did not all succeed is not run** (a `–` line, listed in
`job.skipped`, and the panel says "Offboarding stopped"). The "timer" is the last step: a calendar
reminder event on the manager's calendar `days` out — there is no app-side scheduler. The final
account **delete** is a distinct guarded action on the user detail page (`delete_user`,
`RiskLevel.DESTRUCTIVE`).

### When a step fails
`lifecycle.REQUIRES`, pinned by `test_offboard_step_dependencies_are_the_documented_ones`. Until
2026-09-23 no failure stopped anything, so a failed reset or a bad manager still ran every later
step (failure-log).

| Step | If it fails | Why |
|---|---|---|
| Reset password (+ sign-out) | **stop** — nothing else runs | The lock is the point. Nothing may announce the departure or move data while the account can still sign in, and a first-step failure (wrong credentials, a missing user) usually fails every step. The sign-out is best-effort: its failure doesn't fail the reset. |
| Set delegate | **stop** — nothing else runs | The first write to the manager, who also receives the transfer and the reminder. |
| Auto-reply | continue | Nothing depends on it; senders get no auto-reply until it's re-run. |
| Transfer Drive & Calendar | continue, **but no reminder** | The reminder asks the manager to approve deletion, and deleting before the transfer loses the files for good. A transfer that was never created leaves nothing for the delete screen's pending-transfer warning to find. |
| Remove from everyone's calendars | continue | The account is locked, so a leftover share grants nothing; the reminder doesn't depend on it. |
| Manager reminder | (last) | — |

## Grammar check of every step (GAM 7.48.11, re-verified 2026-09-23)
Each argv our builder emits, next to its line in the vendored `gamgui/resources/gam7/GamCommands.txt`
(line numbers at this pin). Where the grammar was ambiguous the vendored GAM build's own argument
parser was read statically (its bytecode, never run). No mismatch found.

| Step | argv we emit | Grammar | Verdict |
|---|---|---|---|
| Reset password | `update user <leaver> password random changepassword off` | `gam update user <UserItem> [ignorenullpassword] <UserAttribute>*` (5942); `<UserBasicAttribute>` (5823): `(password (random [<Integer>])\|…)`, `(changepassword\|changepasswordatnextlogin <Boolean>)` | Match. GAM generates the password; we never see it. |
| …then sign-out (only if the reset worked) | `user <leaver> signout` | `gam <UserTypeEntity> signout` (8938) | Match; the command takes no options. |
| Delegate | `user <leaver> add delegate <manager>` | `gam <UserTypeEntity> create\|add delegate\|delegates [convertalias] <UserEntity>` (7910) | Match; `convertalias` optional, unused. |
| Auto-reply | `user <leaver> vacation on subject <S> message <M> html` | `gam <UserTypeEntity> vacation [<Boolean>] [subject <String>] [<VacationMessageContent> …] [html [<Boolean>]] [contactsonly …] [domainonly …] [start …] [end …]` (8283); `<VacationMessageContent>` ::= `(message\|textmessage\|htmlmessage <String>)\|…` | Match, in grammar order; no `formatjson`. `html`: see Gotchas. |
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
  primary <email>` and passes `tolerate_kinds=(NOT_FOUND, PERMISSION_DENIED)` to `_run_write`.
  NOT_FOUND = that user never shared with the leaver; PERMISSION_DENIED = the leaver's OWN primary
  calendar (`cannotChangeOwnAcl`, exit 50) — both expected, so the step still counts as success and
  is audited `ok=True, tolerated=True`. A real SCOPE_MISSING/AUTH_EXPIRED still fails it. The sweep
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
- Same commit fixed the sweep misclassification: `classify_stderr` mapped "Cannot change your own
  access level" to UNKNOWN (no 403 token), so the already-present PERMISSION_DENIED tolerance never
  fired. `errors.py` now maps `cannotChangeOwnAcl` → `PERMISSION_DENIED`.

## Gotchas / mock-lies traps
- **`set_vacation` rejects `formatjson`** (see MEMORY / the formatjson gotcha) — the mock can't catch
  that; verify against the vendored grammar `gamgui/resources/gam7/GamCommands.txt`.
- The mock only 409s when the old-owner email contains the literal `CONFLICT409`. The `all users
  delete calendaracls` sweep succeeds by default; `OWNACL` in the address emits the exact own-ACL
  stderr (exit 50, tolerated) and `SWEEPFAIL` a scope error (not tolerated); `SWEEPBENIGN` a
  multi-user stderr where every line is tolerable and `SWEEPMIXED` the same plus one real per-user
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
argv, the second-same-user 409 still failing hard, sweep tolerance (own-ACL and not-found) vs. real
auth errors, a mixed multi-user stderr (all-tolerable vs. one real failure), the sweep's long
timeout and a timeout as a clear step failure (`test_offboard_sweep_timeout_is_a_clear_step_failure`),
auto-reply substitution, reminder invitee, `incomplete_transfers_for` filtering, the directory check
(unknown/alias/same-account blocked on preview and run, admin/suspended warnings), the frozen preview
(Run's writes = the previewed lines, `test_offboard_run_executes_exactly_the_previewed_commands`; an
edited form, a used/expired token and a directory change are refused), the dependency rules (a failed
reset / delegate / transfer against the mock's `missing` and `CONFLICT409` triggers), and the preview's
commands: each step's exact `gam` line in the page, and the previewed argv = what the mock received
(`test_offboard_preview_commands_are_what_runs`).
**Not proven offline** (the mock lies): a live DTS transfer of a real user's Drive+Calendar, the
actual per-user calendar sweep at domain scale, and `delete_user` itself. Per CLAUDE.md, these must
be run against a **throwaway** account before being trusted.

## To do common tasks here
- **Add/reorder an offboard step:** edit `build_offboard_steps` in `core/lifecycle.py` (add an
  `OffboardStep` whose lambda calls a connector method), add the connector method + its
  `GAMCommands` argv builder if new, then update `tests/test_lifecycle.py` (order + right-method
  assertions).
- **Change what the sweep tolerates:** edit `remove_from_all_calendars`'s `tolerate_kinds` and, if a
  new stderr phrase is involved, the regex table in `core/gam/errors.py`; extend
  `tests/test_offboard_safety.py`.
- **Change the delete gate:** edit `delete_confirm`/`delete_apply` in `web/routes/users.py` and
  `incomplete_transfers_for` in the connector. Keep the type-the-email confirm and never let a
  read-error hard-block deletion.
- **Auto-reply wording/placeholders:** `DEFAULT_SUBJECT` / `DEFAULT_MESSAGE` and `fill_autoreply`
  (`{employee}`, `{manager}`, `{contact}`) in `core/lifecycle.py`.
