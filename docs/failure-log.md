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
