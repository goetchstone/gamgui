# Handoff plan: onboarding code-review fixes

**Status: APPLIED — do not re-apply.** Steps 1–6 landed in `37b2e22`. Of the "Additional
suggestions", #1, #2, #4 and #5 have since landed (see the batch-2 plan) and #3 landed for
`create task` and `sendemail`. Still open: #3 for signature-set (it still falls through the mock's
permissive catch-all) and #6 (consolidate the three job types). Originally written against `38d1fa4` for a reader with no prior session
context.

## Context
A code review of the 2026-09-15 onboarding work (commits `d78a1ef..38d1fa4`: account creation,
per-role groups/calendars, CSV bulk import, searchable pickers) found one real behavior bug and two
small cleanups. Everything is already committed and pushed to `main`; the suite is green (575
passed). This plan makes three contained changes. `CLAUDE.md` is auto-loaded — honor its invariants;
read `docs/domains/onboarding.md` before starting (the domain runbook).

**Repo conventions you must follow (session-specific, not in CLAUDE.md):**
- Test command: `.venv/bin/python -m pytest -q` (must stay green; expect 576 passed after this).
- Commit **directly to `main`** (no PR). A local **pre-commit hook hard-blocks any `fix:`/`fix(`
  commit unless `docs/failure-log.md` was modified within the last hour** — Step 4 below satisfies it.
- Push as the repo owner (the active `gh` account may lack write access).
- End the commit message with the `Co-Authored-By:` line your session's attribution reminder gives.
- Docs follow code in the same commit (Step 5).

## Findings being fixed
1. **Bug — bulk applies the role signature to EXISTING accounts; the single flow doesn't.**
   `gamgui/web/routes/onboarding.py`: single `run()` calls `_apply_signature` only inside
   `if make_account:`; bulk `_provision_hire` calls it under `if email:` for every row. A bulk run
   silently overwrites an existing user's customized signature, and a row with no name renders a
   blank `{name}`. Fix: apply only when this run created the account (match the single flow).
2. **Cleanup — `start_job` imported but unused; job pruning duplicated inline in `bulk_run`.**
   Factor a shared `register_job` into `gamgui/web/jobs.py`.
3. **Tidy — two `<input name="q">` sit inside the role save form** and get posted on every role
   save (ignored, harmless). Detach them with `form="ob-picker-noop"`.

Dropped: "invalidate the group-picker cache on group create" — no web route creates groups, so
there's no call site; the 5-minute TTL is fine. Deferred (not this pass): flag single-word names
that become "Ada Ada"; unify the single `run()` onto `_provision_hire`.

## Steps (do in order)

### Step 1 — Fix #1 in `gamgui/web/routes/onboarding.py` (`_provision_hire`)
Edit — replace this exact block:
```python
    if email:
        res["signature"] = await _apply_signature(conn, sig_store, cfg, email, first, last)
        if cfg.groups:
```
with:
```python
    if email:
        # Signature only for an account THIS run created — matches the single /run flow and never
        # clobbers an existing user's customized signature (or renders a blank {name} for an
        # existing-account row that has no name in the CSV).
        if res["account_created"]:
            res["signature"] = await _apply_signature(conn, sig_store, cfg, email, first, last)
        if cfg.groups:
```

### Step 2 — Fix #2: shared `register_job`
**2a.** In `gamgui/web/jobs.py`, replace the whole `start_job` function (this exact text):
```python
def start_job(jobs: dict, total: int, keep: int = 10) -> BatchJob:
    """Register a fresh job, pruning the oldest finished ones so the registry can't grow forever."""
    finished = [jid for jid, j in jobs.items() if getattr(j, "finished", False)]
    for jid in finished[:-keep] if len(finished) > keep else []:
        jobs.pop(jid, None)
    job = BatchJob(id=secrets.token_urlsafe(8), total=total)
    jobs[job.id] = job
    return job
```
with:
```python
def register_job(jobs: dict, job, keep: int = 10):
    """Register ``job`` (anything with ``.id`` and ``.finished``), pruning the oldest finished jobs
    first so the registry can't grow forever. Shared by ``start_job`` and feature-specific job
    types (e.g. onboarding's ``OnboardJob``)."""
    finished = [jid for jid, j in jobs.items() if getattr(j, "finished", False)]
    for jid in finished[:-keep] if len(finished) > keep else []:
        jobs.pop(jid, None)
    jobs[job.id] = job
    return job


def start_job(jobs: dict, total: int, keep: int = 10) -> BatchJob:
    """Register a fresh job, pruning the oldest finished ones so the registry can't grow forever."""
    return register_job(jobs, BatchJob(id=secrets.token_urlsafe(8), total=total), keep=keep)
```
**2b.** In `gamgui/web/routes/onboarding.py`, change the import line
`from ..jobs import start_job` → `from ..jobs import register_job`.
**2c.** In the same file, in `bulk_run`, replace this exact block:
```python
    # Register the job, pruning finished ones so the registry can't grow (mirrors jobs.start_job).
    finished = [jid for jid, j in st.jobs.items() if getattr(j, "finished", False)]
    for jid in finished[:-10] if len(finished) > 10 else []:
        st.jobs.pop(jid, None)
    job = OnboardJob(id=secrets.token_urlsafe(8), total=len(valid))
    st.jobs[job.id] = job
```
with:
```python
    job = register_job(st.jobs, OnboardJob(id=secrets.token_urlsafe(8), total=len(valid)))
```
(`secrets` is already imported in that file; keep it.)

### Step 3 — Fix #3 in `gamgui/web/templates/onboarding.html`
Two edits — add `form="ob-picker-noop"` (a non-existent form id detaches the input from the
enclosing save form; HTMX still sends the input's own `q` value on its `hx-get`):
- `<input type="search" name="q" autocomplete="off" placeholder="Search your groups…"`
  → `<input type="search" name="q" form="ob-picker-noop" autocomplete="off" placeholder="Search your groups…"`
- `<input type="search" name="q" autocomplete="off" placeholder="Search shared calendars…"`
  → `<input type="search" name="q" form="ob-picker-noop" autocomplete="off" placeholder="Search shared calendars…"`

### Step 4 — Add the test for #1 and a failure-log entry
**4a.** Append to `tests/test_onboarding.py` (the helpers `_hire(**over)`, the `connector` fixture,
`RunbookStore`, `SignatureStore`, and `pytest` already exist in that file):
```python
@pytest.mark.asyncio
async def test_provision_hire_skips_signature_for_existing_account(connector, tmp_path):
    # Bulk must match the single flow: the role signature is applied only to an account this run
    # created — never clobbering an existing user's signature (create_account=False).
    from gamgui.web.routes.onboarding import _provision_hire
    store = RunbookStore(tmp_path / "ob.json")
    store.set_role("Sales", ["Set up POS"], signature="Classic")
    cfg = store.role("Sales")
    r = await _provision_hire(connector, SignatureStore(tmp_path / "sig.json"), store, cfg,
                              _hire(name="Ada Byte", email="ada@example.com", create_account=False))
    assert r["ok"] and r["account_created"] is False and r["signature"] is None
```
**4b.** Add this entry to `docs/failure-log.md` directly **below the `---` line** (newest first):
```markdown
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
```

### Step 5 — Docs follow code (`docs/domains/onboarding.md`)
In "How it works", replace the exact phrase
`then best-effort `_apply_signature`; then — whenever an email is set`
with
`then best-effort `_apply_signature` (**only when this run created the account** — never for an existing hire); then — whenever an email is set`.

### Step 6 — Verify, commit, push
1. `.venv/bin/python -m pytest -q` → expect **576 passed, 1 skipped** (575 + the new test).
   If `test_provision_hire_skips_signature_for_existing_account` fails, Step 1 was not applied.
2. Existing job-start tests must still pass (they prove `start_job` behavior is unchanged):
   `tests/test_users_web.py::test_bulk_store_apply_runs_as_job`,
   `tests/test_onboarding.py::test_bulk_run_starts_a_job`.
3. Existing `tests/test_onboarding.py::test_add_and_delete_role` must still pass (role save works
   with the detached `q` inputs).
4. Stage exactly: `gamgui/web/routes/onboarding.py gamgui/web/jobs.py
   gamgui/web/templates/onboarding.html tests/test_onboarding.py docs/failure-log.md
   docs/domains/onboarding.md` (plus this plan file if you want it in history). Do **not** stage
   anything under `.claude/hooks/` or `.claude/settings.json` (gitignored local wiring).
5. Commit to `main` with a message starting `fix(onboarding): apply the role signature only to
   accounts the run created` (the failure-log touch in Step 4b clears the pre-commit hook), then
   push with the owner-account command in the conventions above.

## Optional visual check (not required)
`.claude/launch.json` has an `onboard-preview` config (mock-backed). Start it, open
`http://127.0.0.1:8766/?token=t` then `/onboard` → Role templates → type in the Groups search box
and confirm results still appear (proves the `form="ob-picker-noop"` detach didn't break HTMX).

## Additional suggestions — worthwhile follow-ups, each its own small commit (NOT this pass)
Ranked by value. Each was verified against the code; none is speculative.

1. **Tripwire: every `audit.record` call goes through `_run_write` except an explicit allowlist.**
   `docs/RULE-FEEDBACK.md` already logs that `create_onboarding_runbook` audits outside the
   chokepoint (invariant 2's "no second write path" reads stricter than reality). Operationalize it:
   a source-text test that scans `gamgui/core/connectors/gam_connector.py` for `self.audit.record(`,
   asserts every occurrence is inside `_run_write` OR a named allowlist
   (`{"create_onboarding_runbook"}`), and fails on any new one. Cheap, catches a genuinely rogue
   new write path, and is the exact "tripwire" home the framework prescribes.
2. **Redact secrets out of the audit record's `extra.error`.** An adversarial review found
   `_run_write`'s failure path records `extra={"error": str(exc)}` un-redacted (only `argv` is
   masked). Safe today (GAM doesn't echo a submitted password), but it's the one audited field
   outside the redactor. Precise fix in `_run_write`: compute the secret tokens as the values
   present in `argv` but masked in `audit_argv`, and replace any literal occurrence of them in
   `str(exc)` before recording. Add a test that raises a `GAMError` whose stderr contains the
   password and asserts it never lands in `audit.jsonl`. Also logged in `docs/RULE-FEEDBACK.md`.
3. **Make the mock fail like GAM for `create task`, `sendemail`, and signature-set.**
   `docs/domains/onboarding.md` warns these three still fall through to the permissive catch-all in
   `tests/fixtures/mock_gam.sh`, so a bad flag would pass green and only break live — the repo's #1
   failure class. Add handlers that reject malformed syntax, checked against the vendored grammar
   (`gamgui/resources/gam7/GamCommands.txt`: `create task` ~L8824, `create tasklist` ~L8855,
   `sendemail` ~L5001).
4. **Unify the single `/run` onto `_provision_hire`.** `run()` still keeps a parallel
   orchestration; Finding #1 is precisely the drift that duplication produces. Have `run()` build a
   `hire` dict from the form, call `_provision_hire`, and render `_onboard_run.html` from the result
   → one code path, one set of tests. Do it after #1 lands so the behaviors are already aligned.
5. **Warn on single-word names in the previews.** `_split_name` turns "Ada" into first="Ada",
   last="Ada" (GAM needs a lastname), so an account is silently created as "Ada Ada". In both the
   single preview and `_bulk_summary`, flag rows where `last` defaulted to `first`
   ("set first/last explicitly"), or require both when `create_account` and the name is one word.
6. **Consolidate the three job types.** `BatchJob` (`web/jobs.py`), signatures' `ApplyJob`, and
   onboarding's `OnboardJob` each re-implement bounded feeds. A generic bounded job (recent window +
   capped failed sample + counts) would let all three share one tested implementation. Larger; only
   worth it if another bulk feature arrives.
