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
  `OffboardStep` dataclass, `DEFAULT_SUBJECT` / `DEFAULT_MESSAGE`. No scheduler, no persisted state.
- `gamgui/web/routes/lifecycle.py` — `/lifecycle` page + `/offboard/{preview,autoreply,run,status}`.
  Executes the steps as a progress-tracked `BatchJob` (`_run_offboard`); name-resolution helpers.
- `gamgui/web/routes/users.py` (lines ~416-447) — the **delete** flow: `delete_zone`,
  `delete_confirm` (warns on pending transfers), `delete_apply` (type-the-exact-email confirm).
- `gamgui/core/connectors/gam_connector.py` — the real mutations: `transfer_data`,
  `remove_from_all_calendars`, `reset_password`, `delete_user`, `add_delegate`, `set_vacation`,
  `add_calendar_event`; all via `_run_write` (`tolerate_kinds`). `incomplete_transfers_for` is a
  **read** (direct `run_authenticated`, not `_run_write`) that the delete gate calls.
- `gamgui/core/gam/commands.py` (~268-315) — argv builders (`create_datatransfer`,
  `remove_all_calendar_acls`, `print_datatransfers`, `delete_user`, `reset_password`, …).

## How it works
`build_offboard_steps` returns 6 ordered `OffboardStep`s (`password`, `delegate`, `vacation`,
`transfer`, `calacls`, `reminder`), each a `lambda conn: conn.<method>(...)`. It is pure and
testable; the route runs it. `offboard_run` first refuses a POST without `confirmed=1`
(`guard.enforce`, the leaver declared `DESTRUCTIVE`: the preview's Run button posts it, and
`hx-disabled-elt` stops a double-click starting a second run — a bare POST once ran the whole
routine, failure-log 2026-09-23), then builds the steps, calls `start_job`, and hands them to
`_run_offboard`, which runs each step in order, appends a `✓/✗` line to `job.log`, and **never aborts
on a failed step** (every failure is reported). The "timer" is the last step: a calendar reminder
event on the manager's calendar `days` out — there is no app-side scheduler. The final account
**delete** is a distinct guarded action on the user detail page (`delete_user`, `RiskLevel.DESTRUCTIVE`).

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
  failure (not tolerated). Every step's write has a strict handler that fails a malformed argv. It
  does **not** model real DTS async timing, partial multi-app transfer failures, or per-user
  calendar iteration — a green sweep/transfer test proves classification/argv, not that a live
  tenant transfers cleanly.
- `incomplete_transfers_for` reads `overallTransferStatusCode` (falling back to `status`) and treats
  anything not `"completed"` as pending; a real tenant's status vocabulary is the source of truth.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_lifecycle.py tests/test_offboard_safety.py` (offline: mock
gam + in-memory Keychain). Covered: step order/keys, the single combined-service transfer + audit
argv, the second-same-user 409 still failing hard, sweep tolerance (own-ACL and not-found) vs. real
auth errors, a mixed multi-user stderr (all-tolerable vs. one real failure), auto-reply
substitution, reminder invitee, and `incomplete_transfers_for` filtering.
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
