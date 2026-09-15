# Domain: Onboarding

**One line:** The `/onboard` screen turns editable per-role runbooks into real actions for a new hire — create the Google account (with a printed one-time temp password), apply the role's signature + OU, push a setup checklist to an assignee's Google Tasks, and send a welcome email.

**Owns invariant(s):** the temp-password redaction rule (a specialization of invariant 2's audit chokepoint): the create-user password is never audited, never surfaced in a preview, and only ever leaves the app on the printable credentials sheet. Also touches invariant 1 (argv-only, injection-safe steps/names).
**Enforcement home:** `tests/test_onboarding.py` — `test_create_user_redacts_password`, `test_run_creates_account_and_returns_sheet` (asserts the sentinel pw is on the sheet but absent from `audit.jsonl`), plus the argv-injection tests. No drift guard beyond the shared command-contract tests.

## Files
- `gamgui/core/onboarding.py` — `RunbookStore` (plain-JSON template persistence, 0600), `RoleTemplate`, `generate_temp_password`, `render` (whitelist token substitution). No GAM calls live here.
- `gamgui/web/routes/onboarding.py` — the `/onboard` router: page, save/delete role, save welcome, `/preview`, `/run`. Orchestrates the connector calls.
- `gamgui/core/connectors/gam_connector.py` — `create_user` (redacted audit), `set_signature`, `create_onboarding_runbook`, `send_welcome_email`, and the `_run_write` chokepoint with its `audit_argv` param.
- `gamgui/core/gam/commands.py` — argv builders: `create_user`, `create_tasklist`, `create_task`, `send_email`, `set_signature`.
- `gamgui/web/templates/onboarding.html` + `_onboard_{roles,welcome,preview,run}.html` — the screen and its htmx partials; `_onboard_run.html` renders + prints the credentials sheet.
- `tests/test_onboarding.py` — store round-trip/migration, argv, temp-pw shape, and the full web flow.

## How it works
Templates are the only local state: a role is `{steps, signature, org_unit}` in `onboarding.json`; `RunbookStore._load` migrates an older bare-list-of-steps form via `_as_role`. `/preview` renders the plan (and gates real account creation). `/run` (POST) does the work in order: if `create_account` it requires `confirm=1`, generates a temp password, calls `conn.create_user(...)`, then best-effort `conn.set_signature(...)`; then always `conn.create_onboarding_runbook(assignee, title, steps)`; then optionally `conn.send_welcome_email(...)`. `create_onboarding_runbook` creates the tasklist with `returnidonly`, reads the id off the last stdout line, and loops one `create task` per step — per-step failures are collected, not fatal.

## Invariants & the failure history
- **Temp password never audited or surfaced.** `create_user` builds the real argv but passes a `"********"` copy as `audit_argv` to `_run_write`; `_run_write` records and previews `shown = audit_argv` and only feeds the true argv to the runner. The plaintext appears exactly once, in `_onboard_run.html`'s credentials sheet, and the account is created `changepassword on` so Google forces a reset at first login (`commit d78a1ef`).
- **argv-only (inv. 1):** every operator string — a runbook step, a name — is one argv element. Tests assert `"evil; rm -rf /"` and `"A; rm -rf /"` land intact as single elements.
- **Runbook write is serialized + audited but not via `_run_write`:** `create_onboarding_runbook` calls the runner directly (it needs the returned tasklist id and loops), then calls `self.audit.record` itself with the tasklist argv. It is still `serialize=True` and audited — not a rogue second write path, just a shaped one.
- **`render` substitutes only `WELCOME_VARS`** (`name/role/email/manager`); an unknown `{token}` is left literal, never crashes.

## Gotchas / mock-lies traps
- **The mock only special-cases some of these commands.** `tests/fixtures/mock_gam.sh` has explicit handlers for `create user` (mirrors the real 409 on an `*exists*` email) and `create tasklist` (echoes `MockTasklist_abc123`), but **`create task`, `sendemail`, and `signature`-set fall through to the catch-all that succeeds for anything.** So a bad attribute keyword or a `formatjson`-style rejection on those three would pass green and only break live. Verify their syntax against `gamgui/resources/gam7/GamCommands.txt` (`create task` ~L8824, `create tasklist` ~L8855, `sendemail`/`<MessageContent>` ~L5001) — the source of truth.
- **`create task` takes the tasklist id positionally** (`<TasklistEntity>`), then `title` as a `<TaskAttribute>`; the id must come from the prior tasklist's `returnidonly` stdout. Passing a title instead of an id would also parse — the mock won't tell you which you sent.
- Temp-password alphabet excludes `0/O/1/l/I` on purpose (read off a printed sheet); don't "simplify" it back to a full alphabet.

## Testing / live-verification status
`tests/test_onboarding.py` runs fully offline (mock gam + in-memory Keychain, isolated `onboarding.json`/`signatures.json` under `tmp_path`). It proves store/argv/redaction/flow-plumbing. **What still needs a real tenant:** that `create user`, `user … create tasklist/task`, `sendemail to … html`, and `user … signature … html` actually apply against live GAM — the mock cannot prove any of those, and per the house rule passing tests do not mean a GAM write works. Verify on a throwaway user before relying on the account-creation path.

## To do common tasks here
- **Add a field to a role** (e.g. a second signature): extend `RoleTemplate` + `_as_role` + `set_role`/`role` in `core/onboarding.py`, add the form field in `onboarding.html`/`_onboard_roles.html`, thread it through `save_role`, `/preview`, `/run` in `routes/onboarding.py`, and update `test_role_config_persists_and_migrates`.
- **Add a new onboarding action** (a real GAM mutation): add an argv builder in `core/gam/commands.py`, a connector method routing through `_run_write` (use `audit_argv` if it carries a secret), call it from `run(...)`, render it in `_onboard_run.html`, and — critically — give `mock_gam.sh` a handler that fails the way real GAM fails, then verify live.
- **Change the welcome tokens:** edit `WELCOME_VARS` in `core/onboarding.py`; `render` and the preview follow automatically.
