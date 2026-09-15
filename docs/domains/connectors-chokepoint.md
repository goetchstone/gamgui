# Domain: Connectors — the write chokepoint

**One line:** The `GAMConnector` translates high-level operations into `GAMCommands` argv, and every mutation the app makes funnels through its single `_run_write` helper, which serializes, redacts, runs, and audits the call.

**Owns invariant(s):** #2 (every mutation goes through the chokepoint; no second write path; `audit_argv` redaction). Leans on #1 (argv-only builders) and #4 (serialization protects the ephemeral GAMCFGDIR).
**Enforcement home:** `tests/test_gam_connector.py`, `tests/test_audit.py` (redaction), `tests/test_lifecycle.py` (tolerate/serialize), `tests/test_runner.py` (serialize + token write-back). No dedicated "grep for a second write path" drift guard exists yet.

## Files
- `gamgui/core/connectors/gam_connector.py` — the Google Workspace connector; all read/mutation methods + the `_run_write` chokepoint and `apply`/`plan_suspend`.
- `gamgui/core/connectors/base.py` — `Connector` base, `ChangePreview`/`ChangeResult`, `RiskLevel`, `ConnectorID`, `Capability`, `LifecycleAction`.
- `gamgui/core/connectors/abapit_connector.py` — optional ABM/Mosyle wrapper (lazy import; separate system, not GAM).
- `gamgui/core/connectors/person.py` — `Person` / `ConnectorAccount` identity DTOs.
- `gamgui/core/guard.py` — `evaluate()` → `GuardDecision`; the confirmation-policy gate (see flow note).
- `gamgui/core/audit.py` — `AuditLog.record`, `redact_argv`, rolled generations.
- `gamgui/core/gam/runner.py` — `GAMRunner.run_authenticated(..., serialize=True)`, the only subprocess.

## How it works
A mutation is: connector method (e.g. `set_signature`, `add_group_member`, `delete_user`) builds argv via a `GAMCommands` static method, then calls `await self._run_write(action, target, argv, RiskLevel, ...)`. `_run_write` wraps it in a `ChangePreview`, runs `self.runner.run_authenticated(self.domain, argv, serialize=True)`, and on both success and failure calls `self.audit.record(...)`, returning a `ChangeResult`. Planned changes (`plan_suspend` → `apply`) route through the same `_run_write`. `guard.evaluate()` is NOT called inside `_run_write` — it runs earlier, in the web routes (`web/routes/users.py`, `web/routes/builder.py`), to decide whether the UI must confirm before `apply`/run is invoked; the chokepoint is the enforcement point, the guard is the confirmation policy.

## Invariants & the failure history
- **Single write path (#2).** All writes go through `_run_write`; `apply()` delegates to it, and the Builder's `run` route calls `conn.apply([preview])` for mutations. The only direct `run_authenticated` calls are reads or the two audited serialized exceptions below — do not add a fourth caller that mutates.
- **Redaction is two-layer.** `_run_write(audit_argv=...)` swaps a secret out *before* it reaches the log (create_user passes a `"********"` password argv as `audit_argv`; the real password argv still runs). Independently, `audit.redact_argv` masks the value after any of `password, signature, recoveryemail, recoveryphone, alternateemail`. Both must hold — the docstring: the shown argv is "never the raw secret".
- **`tolerate_kinds` for best-effort sweeps.** `remove_from_all_calendars` passes `(NOT_FOUND, PERMISSION_DENIED)` so per-user "never shared" and the departing user's own-primary `cannotChangeOwnAcl` (GAM exit 50) count as success and are audited `ok, tolerated=True`. Regression fixed in b26da7e / test `test_offboard_calendar_sweep_tolerates_own_acl`.
- **Serialization (#4).** `serialize=True` takes the runner's `_write_lock` so two writes can't race the same ephemeral GAMCFGDIR; it also gives the oauth-token write-back a stable dir (`test_oauth_token_write_back_through_a_real_run`).
- **Serialized non-`_run_write` write paths.** Two mutations run via a direct `runner.run_authenticated(..., serialize=True)` instead of `_run_write`. They differ, and the difference matters: `create_onboarding_runbook` (tasklist + per-task loop) **is** audited — it calls `self.audit.record("onboard_runbook", ...)` itself. But `reset_password`'s best-effort follow-up sign-out (`GAMCommands.signout_user`, in a `try/except: pass`) is serialized and **not** audited — it is fire-and-forget; only the reset itself is audited (through `_run_write`). So the audit log is not a complete record of every session-ending call. Don't treat either as a licence to add more un-audited writes.

## Gotchas / mock-lies traps
- `RiskLevel` on each `_run_write` call must match the real GAM verb's blast radius, and the catalog's declared risk must match it (see `core/catalog/catalog.py`). `delete_calendar` uses GAM's `remove calendars` (= Calendars.delete, permanent) not `delete calendars` (unsubscribe) — verified against GAM7 source, comment in `delete_calendar`.
- `formatjson` traps (MEMORY: gamgui-gam-formatjson): `show signature`/`show vacation` are parsed as *text* here (`_parse_signature`, `Vacation.from_show_text`), not JSON — do not "modernize" them to formatjson. The mock cannot catch a formatjson rejection; check `gamgui/resources/gam7/GamCommands.txt`.
- `mock_gam.sh` returned members for any address once, making a user look like a group — a permissive mock converts a live break into a green test. Anything mutating (create/delete/transfer/ACL) is only *proven* against a real throwaway tenant; passing tests do not prove a write works.

## Testing / live-verification status
`.venv/bin/python -m pytest -q` runs fully offline: the `connector` fixture (`tests/conftest.py`) wires a `GAMRunner` at `mock_gam.sh` via `GAMGUI_GAM_BINARY` + an in-memory vault, audit to a tmp file. Covered: read parsing, `set_signature` redaction, `plan_suspend` risk, `apply`, vacation set/clear, redaction masks (`test_audit.py`), tolerate-own-acl and the merged-transfer rule (Drive+Calendar go out as one `create datatransfer` to avoid a same-user 409 — `test_lifecycle.py`), serialize + token write-back (`test_runner.py`). Still needs a real tenant to trust: every DESTRUCTIVE op (`delete_user`, `delete_event`, `delete_calendar`, suspend), `transfer_data`, and the calendar-ACL sweeps — verify on a throwaway user/event/calendar per CLAUDE.md rules of engagement.

## To do common tasks here
- **Add a new mutation:** add the argv builder in `core/gam/commands.py` (argv-only, #1) → add an `async def` on `GAMConnector` that calls `self._run_write(action, target, argv, RiskLevel.X, ...)` → pick the honest `RiskLevel` → if it carries a secret, pass a redacted `audit_argv` and confirm the sensitive key is in `_SENSITIVE_KEYS` → wire the route (usually via `conn.apply([preview])` after `guard.evaluate`). Add a connector test + make `mock_gam.sh` fail the way real GAM fails.
- **Make a bulk sweep tolerate expected per-entity errors:** map the GAM exit to a `GAMErrorKind` in `core/gam/errors.py`, then pass `tolerate_kinds=(...)` to `_run_write`.
- **Redact a new secret field:** add the gam key to `_SENSITIVE_KEYS` in `audit.py` and add a `test_audit.py` case; for a positional secret with no key token before it, pass an explicit `audit_argv` instead.
