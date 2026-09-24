# Plan: onboarding review — batch 2 (24 verified findings)

**Status: MOSTLY APPLIED — do not re-apply.** Batches A–E and Batch F #4/#8 landed in
`ce8640d..7e8b49e`; the "replace this exact block" snippets no longer match. Batch F #21
(`parse_hire_csv`'s `csv.Error`, `5a3298b`) and #23 (one print-sheet helper, `a32acb1`) landed on
`harden-2026-09-23`. **Still open:** Batch F #11 (move the per-hire helpers into `core/` — the 10/10
plan's Q9 supersedes it) and #22 (`_bulk_summary` split). Originally written against `37b2e22` for a
reader with no session context.

## Context
A five-lens adversarial code review of the onboarding feature (commits `d78a1ef..37b2e22`) produced
56 raw findings; **24 were confirmed** by a skeptic that reproduced or ran each one (0 refuted; 32
low-severity dropped at the verification cap and listed at the end). This plan groups them into
batches you can ship independently, highest-value first. `CLAUDE.md` is auto-loaded — honor its
invariants; read `docs/domains/onboarding.md` first.

**Repo conventions (session-specific, not in CLAUDE.md):**
- Tests: `.venv/bin/python -m pytest -q` (stay green).
- Commit **directly to `main`** (no PR). A local **pre-commit hook hard-blocks a `fix:`/`fix(` commit
  unless `docs/failure-log.md` was touched in the last hour** — Batch A adds an entry, satisfying it.
- Push as the repo owner (the active `gh` account may lack write access).
- End commit messages with the `Co-Authored-By:` line from your session's attribution reminder.
- Docs follow code in the same commit. After code changes that a running app would show, rebuild:
  `make app` (PyInstaller; verify frozen modules via the PYZ, not by grepping `.py` in the bundle).

**Recommended order & scope:** Batch A (the credential-loss bug) is the must-do. Then B (real bugs),
C (security hardening), D (test hardening — closes the "mock lies" gap that hid Batch A). E and F are
optional cleanup. Each batch is one commit.

---

## BATCH A — CRITICAL: single `/run` strands a created account's one-time password  (findings #1, #2)

**The bug.** `run()` creates the account, applies the signature, adds groups/calendars — *then*
validates the assignee and creates the task list. Both later failures `return _err(...)`, a template
with **no credentials slot**, so the temp password (which by design exists nowhere else — never
audited, never emailed) is lost. The error doesn't say the account was created, so the operator
retries and 409s. Reproduced by the reviewer: make `create_onboarding_runbook` raise a 403 (the
realistic first-run case — Tasks scope missing from DWD) → 200 with only the error box, `create_user`
`ok=true` in the audit, password absent. The **bulk path already handles this correctly**; `run()`
diverged. Also fixes: `(assignee or email).strip()` treats a single space as truthy — a stray space
in the assignee box overrides the email fallback.

### A1 — `gamgui/web/routes/onboarding.py`: hoist validation before any write
Replace this exact block:
```python
    if make_account:
        # Creating a real account is gated behind the preview: its Run button posts confirm=1.
        if confirm != "1":
            return _err(request, "Preview first — creating an account needs confirmation.")
        if not email:
            return _err(request, "Enter the new hire's email to create the account.")
        f, l = _split_name(name, first, last)
        if not f or not l:
            return _err(request, "Enter the new hire's first and last name to create the account.")
        temp = onboarding.generate_temp_password()
```
with:
```python
    # Validate everything that does NOT write BEFORE any mutation, so a bad assignee (or any later
    # step) can't strand a just-created account's one-time password (it exists nowhere else).
    f = l = ""
    if make_account:
        # Creating a real account is gated behind the preview: its Run button posts confirm=1.
        if confirm != "1":
            return _err(request, "Preview first — creating an account needs confirmation.")
        if not email:
            return _err(request, "Enter the new hire's email to create the account.")
        f, l = _split_name(name, first, last)
        if not f or not l:
            return _err(request, "Enter the new hire's first and last name to create the account.")
    assignee = assignee.strip() or email
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")

    if make_account:
        temp = onboarding.generate_temp_password()
```

### A2 — same file: drop the now-duplicate assignee block; make the tasklist failure non-fatal
Replace this exact block:
```python
    assignee = (assignee or email).strip()
    if not assignee:
        return _err(request, "Enter the assignee (who does the setup) or the new hire's email.")
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    try:
        result = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
    except Exception as exc:  # noqa: BLE001
        return _err(request, "Couldn't create the task list: " + str(getattr(exc, "remediation", exc)))
```
with:
```python
    title = "Onboard {} — {}".format(name or email or "new hire", role)
    try:
        result = await conn.create_onboarding_runbook(assignee, title, cfg.steps)
    except Exception as exc:  # noqa: BLE001
        # An account or membership already succeeded — never bare-_err here (that strands the
        # one-time password). Surface the failure in the result panel beside the credentials sheet.
        if credentials is None and memberships is None:
            return _err(request, "Couldn't create the task list: " + str(getattr(exc, "remediation", exc)))
        result = {"tasklist_id": "", "created": 0, "failed": list(cfg.steps),
                  "total": len(cfg.steps), "error": str(getattr(exc, "remediation", exc))}
```

### A3 — `gamgui/web/templates/_onboard_run.html`: surface `result.error`
Replace:
```html
  {% else %}
    Couldn't create the task list — no tasklist id came back. Check the assignee email and that the Tasks API scope is granted in Domain-Wide Delegation.
  {% endif %}
```
with:
```html
  {% else %}
    Couldn't create the task list{% if result.error %}: {{ result.error }}{% else %} — no tasklist id came back. Check the assignee email and that the Tasks API scope is granted in Domain-Wide Delegation.{% endif %}
  {% endif %}
```

### A4 — regression test (append to `tests/test_onboarding.py`)
```python
@pytest.mark.asyncio
async def test_run_keeps_credentials_when_tasklist_fails(client, monkeypatch):
    # After the account is created, a task-list failure must NOT strand the one-time password.
    monkeypatch.setattr(onboarding, "generate_temp_password", lambda: "KEEPpw-1234-5678")
    async def boom(self, assignee, title, steps):
        raise RuntimeError("insufficientPermissions: Tasks scope not granted")
    monkeypatch.setattr(
        "gamgui.core.connectors.gam_connector.GAMConnector.create_onboarding_runbook", boom)
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})
    r = client.post("/onboard/run", data={"role": "Sales", "name": "Ada Byte", "email": "ada@example.com",
                                          "assignee": "it@example.com", "create_account": "1", "confirm": "1"})
    assert r.status_code == 200
    assert "KEEPpw-1234-5678" in r.text                 # temp password still shown
    assert "Couldn't create the task list" in r.text    # failure surfaced, not swallowed
```

### A5 — failure-log entry (prepend below the `---` in `docs/failure-log.md`, newest first)
```markdown
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
  can `return _err` and discard the one-shot secret it produced — see Batch D (make the mock fail).
```

**Verify A:** `.venv/bin/python -m pytest -q tests/test_onboarding.py` → all pass incl. the new test.
Prove it's a real tripwire: temporarily revert A1+A2, run just the new test → it FAILS; restore.
Commit message starts `fix(onboarding): never strand a created account's temp password on a later failure`.

---

## BATCH B — real bugs (MEDIUM), one commit

### B1 — Cross-tenant cache leak: group picker serves the old tenant's groups after a domain switch (#7)
`AppState.group_cache` (and `user_cache`) have no domain tag, and `setup.py` swaps the connector on
verify without busting them, so `/onboard/search/groups` serves tenant A's groups for up to the
5-minute TTL after switching to B. In `gamgui/web/routes/setup.py`, replace:
```python
    if result.ok:
        st.connector = GAMConnector(runner=st.runner, domain=domain)
        st.audit_domain = domain
```
with:
```python
    if result.ok:
        st.connector = GAMConnector(runner=st.runner, domain=domain)
        st.audit_domain = domain
        st.invalidate_users()   # caches are not domain-tagged; a tenant switch must bust them
        st.invalidate_groups()  # (the calendar index self-checks its stored domain, so it's already safe)
```
Test (append to `tests/test_onboarding.py` or `tests/test_setup*.py`): seed the vault for two domains,
GET `/onboard/search/groups` under A, POST `/setup/verify` for B (stub B's `connector.list_groups` to
return a distinct group), assert the next GET does not contain A's group email.

### B2 — Bulk feed shows ✓ for hires whose sub-steps failed; failures vanish from the panel (#3, #5)
`_provision_hire` only records tasklist failures in `res["errors"]`; group/calendar/signature/welcome
failures are dropped, and `res["ok"]` ignores them, so the final panel shows a green ✓. In
`gamgui/web/routes/onboarding.py`, in the `if email:` block, capture failures, and set `ok` from
`errors` before `return res`:
- After `res["groups"] = {...}` add: `if g_fail: res["errors"].append("groups: " + ", ".join(g_fail))`
- After `res["calendars"] = {...}` add: `if c_fail: res["errors"].append("calendars: " + ", ".join(c_fail))`
- After the signature line, add: `if cfg.signature and res["account_created"] and not res["signature"]: res["errors"].append("signature: not applied")`
- In the welcome block, in the `except`/`False` path add: `res["errors"].append("welcome email: failed")`
- Immediately before `return res` (last line of the function) add: `res["ok"] = not res["errors"]`

Existing executor tests still pass (their sub-steps succeed under the mock → no errors → ok=True).
Add a test: a role with a `missing-group@example.com` group, `create_account=False`, one hire →
assert the executor counts it in `job.failed_total` and the joined reason is in `job.failed`.

### B3 — `create_onboarding_runbook` leaves no audit record when the tasklist create fails (#6)
The first `run_authenticated` (create tasklist) is unwrapped, so a failure raises with no audit line —
a mutation attempt that vanishes. In `gamgui/core/connectors/gam_connector.py`, replace:
```python
        out = await self.runner.run_authenticated(
            self.domain, GAMCommands.create_tasklist(assignee, title), serialize=True)
```
with:
```python
        try:
            out = await self.runner.run_authenticated(
                self.domain, GAMCommands.create_tasklist(assignee, title), serialize=True)
        except Exception as exc:  # noqa: BLE001 — record the attempt before it propagates
            self.audit.record("onboard_runbook", target=assignee,
                              argv=GAMCommands.create_tasklist(assignee, title), ok=False,
                              extra={"title": title, "error": str(exc)})
            raise
```
Test: patch the runner to raise for a `create tasklist` argv, call `create_onboarding_runbook`, assert
it raises AND `audit.jsonl` has one `onboard_runbook` line with `ok:false`.

### B4 — `_split_name` fabricates a surname for a single-word name (#9)
"Ada" → first "Ada", last "Ada", which silently defeats the first/last guard. In
`gamgui/web/routes/onboarding.py:71`, change:
```python
        last = " ".join(parts[1:]) or parts[0]
```
to:
```python
        last = " ".join(parts[1:])
```
Now a one-word name returns `('Ada', '')`, so the "need a first & last name" guards at run() and
`_provision_hire` fire. Add a test: `_split_name("Ada", "", "") == ("Ada", "")`, and a `/onboard/run`
with `create_account=1` and a single-word name asserts the "first and last name" error (no `create_user`
in the audit). (No existing test uses a single-word create-account name.)

### B5 — `bulk_status` 500s on a job id that belongs to another feature's `BatchJob` (#24)
`st.jobs` is shared; a non-`OnboardJob` reaches `_onboard_bulk_status.html` and crashes on missing
attrs. In `gamgui/web/routes/onboarding.py` `bulk_status`, after `j = _st(request).jobs.get(job) if job else None` add:
```python
    if not isinstance(j, OnboardJob):
        j = None
```
Test: register a `BatchJob` in `st.jobs`, GET `/onboard/bulk/status?job=<id>`, assert 200 and
"no longer available" in the body.

**Verify B:** full suite green + the new tests. Commit: `fix(onboarding): bust caches on tenant switch; surface bulk sub-step failures; audit failed tasklist; single-word names; hardened bulk status`. (Touches failure-log via Batch A's window, or add a short entry.)

---

## BATCH C — security hardening (LOW/MEDIUM), one commit

### C1 — GAM *does* echo the password on a usage error; redact at the chokepoint (#20)
`errors.py`'s docstring claims "GAM does not echo secrets" — false. GAM prints the full command line
(incl. `password <pw>`) on a usage error; onboarding is safe today only because `GAMError.message`
takes the *last* stderr line. Harden it so `exc.stderr`/`exc.argv` are safe for any consumer:
- Fix the false docstring at `gamgui/core/gam/errors.py:79`.
- In `GAMError.from_run` (or in `runner.run_authenticated` just before it raises), store
  `argv = redact_argv(argv)` and scrub stderr by masking values after `password`/`notifypassword`
  (regex `(?i)\b(password|notifypassword)\s+\S+` → `\1 ***redacted***`, and drop a leading
  `Command:` line). **Verify no circular import** between `core/gam/errors.py` and `core/audit.py`
  (where `redact_argv` lives, `core/audit.py:35`); if audit imports errors, inline a tiny masker in
  errors.py instead.
- Validate `email`/`assignee`/`notify` as `local@domain` in `parse_hire_csv` and the single-hire form
  so the usage error is unreachable from operator input in the first place.
- Tripwire test: onboard a row/form with `email="@example.com"`; assert the generated temp password
  appears in **none** of `audit.jsonl`, the `/onboard/run` HTML, `/onboard/bulk/status` HTML, or
  `exc.stderr`. (Also make the mock's `create user` fail like GAM for a `$3` starting with `@` — see
  Batch D.)
- **Update `docs/RULE-FEEDBACK.md`:** the existing entry saying "GAM doesn't echo a submitted password"
  is wrong — correct it to "GAM echoes the command line on usage errors; redacted at construction".

### C2 — Duplicate emails in a bulk CSV are neither flagged nor tested (#18)
The stateless mock lets two `create user` calls for the same email both "succeed", hiding the live
409. In `parse_hire_csv` (`gamgui/core/onboarding.py`), inside the row loop keep
`seen: Dict[str, int] = {}` on `email.lower()`; on a repeat append
`"Row {}: duplicate email {} (first on row {}).".format(i, email, seen[key])` and `continue`, else
record `seen[key] = i`. Add to `test_parse_hire_csv`.

**Verify C:** full suite green. Commit: `fix(security): redact GAM stderr/argv at the chokepoint; flag duplicate CSV emails`.

---

## BATCH D — test hardening: make the mock fail like GAM (MEDIUM), one commit
This closes the "mock lies" gap (CLAUDE.md's #1 failure class) that hid Batch A. Add handlers to
`tests/fixtures/mock_gam.sh` **before the catch-all** (line ~277), each checked against the vendored
grammar `gamgui/resources/gam7/GamCommands.txt`, then de-vacuous the tests.
- **`create task`** (#12): fail `404 notFound` unless the tasklist id is `MockTasklist_abc123`;
  else echo a GAM-style success. De-vacuum `test_run_creates_google_tasks_list` to assert a task count.
- **`sendemail`** (#13): a `SENDFAIL` recipient → real-shaped `400 invalidArgument`, exit 1; else
  success. Add tests exercising both `email_sent` branches (currently dead).
- **`user … signature … html`** set: allow a `SIGFAIL` marker to fail, so `_apply_signature`'s failure
  branch is reachable.
- **`create user`** with `$3` starting `@` → GAM usage error (supports C1's tripwire).
- **Notify redaction (#14):** add `test_provision_hire_notify_never_audits_password` — patch
  `generate_temp_password` to a sentinel, run a notify hire, assert the sentinel is absent from
  `audit.jsonl` (covers the `notifypassword` position specifically).
- **Single create+groups+calendars together (#15):** one `/onboard/run` test with a role carrying a
  signature, OU, groups and calendars; assert "Account created", "Classic applied", "2 of 2 groups",
  "1 of 1 shared calendar", and the created task count all appear.
- **Bulk one-shot guard (#16):** `test_bulk_status_does_not_drain_credentials_before_finish` — an
  unfinished `OnboardJob` with credentials, GET status, assert the sheet is NOT served and the creds
  remain (the `j.finished` guard is currently unprotected by a test).
- **Excel BOM (#17):** `test_bulk_preview_accepts_excel_utf8_bom` — a CSV prefixed with `﻿`;
  tripwire for the route's `utf-8-sig` decode (`str.strip()` does not remove U+FEFF).
- **Failed-sample cap (#19):** `test_bulk_job_failed_sample_is_capped` — > `_FAILED_SAMPLE_CAP`
  failing hires; assert `len(job.failed) == _FAILED_SAMPLE_CAP` while `failed_total` is the full count
  (invariant #9).

**Verify D:** full suite green; each new/de-vacuumed test fails if its handler is reverted.
Commit: `test(onboarding): make the mock fail like GAM (tasklist/sendemail/signature); cover notify redaction, BOM, dup emails, #9 cap`.

---

## BATCH E — bulk credential lifecycle (MEDIUM, UX/data-loss) (#10) — optional, its own commit
The one-shot sheet is consumed by the first finished GET, so a refresh, window close, or a second
Preview click loses **every** temp password at once. Replace drop-on-first-render with a short TTL +
explicit acknowledgement:
- Add `finished_at: float` to `OnboardJob`; set `time.monotonic()` where `job.finished = True`
  (`_run_bulk_onboard`, line 243).
- In `bulk_status`: serve `j.credentials` on any GET while
  `j.finished and j.credentials and time.monotonic() - j.finished_at < CREDS_TTL` (15 min); clear once
  past the TTL (and in `register_job` pruning).
- Add `POST /onboard/bulk/done` (form `job`) that sets `j.credentials = []` and returns the status
  partial without the sheet.
- In `_onboard_bulk_status.html`: a "Done — I've saved this" button (hx-post to `/bulk/done`); change
  the copy from "this page won't show it again" to "kept for 15 minutes or until you click Done; if
  lost, reset the password in the Admin console". Keep `Cache-Control: no-store`.
- Tests: the sheet survives a second GET within the window; `/bulk/done` clears it; it's gone after TTL.

Commit: `feat(onboarding): keep the bulk credentials sheet for 15 min with an explicit Done, not one GET`.

---

## BATCH F — reuse / cleanup (LOW) — optional
Do **after** A (behaviors are then aligned). Each its own small, behavior-preserving commit:
- **#4/#8 — unify `run()` onto `_provision_hire`.** Keep route-only validation (not-connected, role
  has steps, confirm gate, create-account preconditions, assignee-or-email), build a `hire` dict,
  call `_provision_hire`, and render `_onboard_run.html` from its result. One per-hire code path; the
  divergence that caused Batch A can't recur. (Requires mapping `res` → the template's
  `credentials`/`memberships`/`result` shape — do it carefully with the Batch D tests green.)
- **#11** — move `_split_name`, `_apply_signature`, a single `apply_each(items, call)` (replacing the
  twin `_apply_groups`/`_apply_calendars`), and `provision_hire` into `core/` (pure/testable, no
  route deps).
- **#21** — `parse_hire_csv`: move the `for raw in reader:` loop inside the `try` (the `csv.Error` it
  can raise during iteration currently escapes as a 500); narrow `except Exception` → `except csv.Error`.
- **#22** — split `_bulk_summary` into `_resolve_hires(rows, store) -> (pairs, errors)` and
  `_tally(pairs) -> summary`; `bulk_run` calls only the former (its tally is currently computed and
  thrown away, and it re-filters roles the route already validated).
- **#23** — one `obPrintSheet(sheetId, …)` JS helper instead of the duplicated `obPrintCreds` /
  `obPrintBulkCreds`; the two printable-sheet partials share one macro.

---

## Deferred / dropped at the verification cap (32, unverified — triage before acting)
Notable ones worth a look: single-flow `confirm=1` isn't bound to the previewed payload; `job.recent`
still holds plaintext passwords after the one-shot clear (bounded to 12, per-viewer); an email cell
equal to the GAM keyword `oauthuser` could target the authorizing admin in `update group` (validate
per C1); `RunbookStore` silently replaces templates with the seed on a corrupt file. The rest are
low-value dedupe/dead-code/test-tidy items — see the review run `wf_5fccc9d7-406` journal if needed.
