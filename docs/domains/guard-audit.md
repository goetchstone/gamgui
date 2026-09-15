# Domain: Guard & Audit

**One line:** The confirmation gate that decides how hard a mutation must be confirmed, and the append-only redacted JSONL log that durably records every mutation the tool ran.

**Owns invariant(s):** #2 (guard.evaluate is the mandatory middle of the mutation chokepoint); #4 in part (redact_argv keeps Keychain-class secrets out of the log); the audit retention bound is the same "bound anything durable/polled" discipline as #9.
**Enforcement home:** `tests/test_guard.py` (gating rules), `tests/test_audit.py` (redaction + 0600 + tail), `tests/test_audit_web.py` (rotation, generation spanning, mid-read snapshot). No drift guard — these are behavioral tests, not contract tests.

## Files
- `gamgui/core/guard.py` — `evaluate(previews) -> GuardDecision`; pure, no GAM call. Decides confirm / typed-confirm / hard-cap warning from a list of `ChangePreview`.
- `gamgui/core/audit.py` — `redact_argv`, `AuditLog.record`, rolling (`_roll_if_large`, `generation_paths`), and reverse-streaming readers (`iter_records`, `read_records`, `tail`).
- `gamgui/core/connectors/base.py` — defines `RiskLevel` (IntEnum READ_ONLY=0, LOW=1, DESTRUCTIVE=2) and `ChangePreview(target, risk, argv, …)` that guard consumes.
- `gamgui/core/connectors/gam_connector.py` — `_run_write(...)` audits (and serializes) almost every mutation. **One method skips it:** `create_onboarding_runbook` calls `self.audit.record("onboard_runbook", …)` directly (gam_connector.py:359), because it's a two-step tasklist build run straight through `runner.run_authenticated`. So `_run_write` is the *main* audit chokepoint, not the only `record()` caller — grep `audit.record` before assuming it.
- `gamgui/web/routes/builder.py` — the server-side guard enforcement: `/preview`/`/run` re-check `requires_confirmation`, and `/builder/sequence/run` re-checks `requires_typed_confirmation` (typed "confirm"), mirroring the template rather than trusting it.
- `gamgui/web/routes/users.py` — uses `guard.evaluate` to render the suspend preview; single-user actions are never bulk so typed confirmation never triggers here, and account **delete** uses its own type-the-exact-email gate (`/delete/apply`), not guard's typed confirmation.
- `gamgui/web/templates/_sequence_preview.html` — the template that renders `decision.requires_typed_confirmation` / `requires_confirmation` (client side; builder.py backs it server-side).

## How it works
`evaluate` takes `max_risk = max(p.risk for p in previews)` and `count = len(previews)`. Rules: destructive → always `requires_confirmation`; bulk (`count >= DEFAULT_BULK_THRESHOLD` = 10) of any non-read risk → confirm; destructive **and** bulk → `requires_typed_confirmation` (operator must type "confirm"); `count > DEFAULT_HARD_CAP` (200) → an `over_hard_cap` warning but not a hard block. It returns a `GuardDecision`; `builder.py` re-enforces the same booleans server-side, not just in the template. Separately, `_run_write` calls `self.audit.record(...)` on both the success and exception paths, writing one JSON line per mutation; `record` masks the argv through `redact_argv` and appends 0600 under a `threading.Lock`, rolling the file when it passes `max_bytes`.

## Invariants & the failure history
- **Chokepoint (#2):** guard sits between `ChangePreview` and `_run_write`, and `_run_write` audits every write that flows through it. The one live exception is `create_onboarding_runbook`, which runs its GAM calls via `runner.run_authenticated` and audits by hand — additive/low-risk, but a reminder that "everything is audited" holds only because that one path calls `record()` itself. Any *new* mutation must funnel through `_run_write` (or, like the runbook, record explicitly) so nothing mutates un-audited.
- **Two layers keep secrets out of the log (#4).** `redact_argv` masks the value *after* any of `_SENSITIVE_KEYS = {password, signature, recoveryemail, recoveryphone, alternateemail}` (positional — it masks the next token, matching GAM's `key value` argv shape). Belt-and-braces: `create_user` also pre-builds a `redacted` argv (`audit_argv=`) so the raw temp password never even reaches `record`. `alternateemail`/recovery fields were added by the "widen audit redaction" hardening pass.
- **Rolling never loses in-window history.** `_roll_if_large` shifts generations oldest-first (`.9`→`.10` … `.1`→`.2`, then live→`.1`) so a roll only displaces the oldest of `RETENTION_GENERATIONS` (10); readers span all generations. Retention bound ≈ 10 × 16 MiB.
- **Readers snapshot generations against a mid-read roll.** `_open_generations` opens every generation up front and dedupes by `(st_dev, st_ino)`, so a roll landing mid-export can't make a long CSV export re-emit a drained file or skip one. `iter_records` streams newest-first and never materializes the whole log.
- **A filter must not pass `limit`.** `read_records`' docstring warns: capping before filtering would only ever show the newest slice and silently hide older matches.

## Gotchas / mock-lies traps
- Guard and `redact_argv` are pure Python over argv/previews — no GAM call — so `tests/fixtures/mock_gam.sh` and the vendored grammar are irrelevant to *this* domain; these tests are trustworthy as-is.
- But the domain is only as safe as the `RiskLevel` each mutation declares (`DESTRUCTIVE`: `delete_user`, `delete_event`, `delete_calendar`, and `plan_suspend(..., suspend=True)`; almost everything else `LOW`; unsuspend is `LOW`). A mislabeled risk silently downgrades the confirmation the operator sees; that's a curation decision the tests here do NOT check — they assume the label is right.
- `redact_argv` is key-driven: a *new* secret-bearing GAM flag not in `_SENSITIVE_KEYS` would be logged in the clear. Adding a mutation that carries a secret means adding its key here (or passing `audit_argv=`).

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_guard.py tests/test_audit.py tests/test_audit_web.py` — fully offline, no tenant needed (pure functions + temp-file rotation with `max_bytes` shrunk to a few records via `_rotating_log`). Nothing in this domain touches a live domain, so nothing here needs live verification; the live risk lives one layer out, in whether each connector method passes the correct `RiskLevel` and a fully-redacted argv.

## To do common tasks here
- **New secret-bearing flag:** add its lowercase key to `_SENSITIVE_KEYS` in `audit.py`; add a `test_redact_masks_*` case in `tests/test_audit.py`. Prefer also passing `audit_argv=` from the connector when the secret must never transit `record` at all.
- **Change a confirmation threshold/rule:** edit `DEFAULT_BULK_THRESHOLD` / `DEFAULT_HARD_CAP` or the boolean rules in `guard.evaluate`; update the assertions in `tests/test_guard.py`; verify `web/routes/builder.py` (`/run` and `/builder/sequence/run`, backing `_sequence_preview.html`) still enforces the same booleans server-side, and that the `users.py` suspend/delete gates still read as intended.
- **Change retention/rotation:** edit `MAX_LOG_BYTES` / `RETENTION_GENERATIONS` in `audit.py`; re-check `tests/test_audit_web.py` rotation and generation-spanning tests.
