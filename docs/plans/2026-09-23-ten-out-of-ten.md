# Plan: GamGUI to 10/10 — security, quality, usability, value

**Status: NOT STARTED** (written 2026-09-23 at `d5d775b`; 597 passed, 1 skipped). Self-contained for
a reader with no session context. `CLAUDE.md` is auto-loaded — honor its invariants. Before each
item, read the `docs/domains/` runbook for its area (index: `docs/domains/README.md`).

## How this was produced
A three-lens review on 2026-09-23: an adversarial **security** reviewer that proved findings with
offline PoCs (TestClient, a recording mock, a browser pane; never a real tenant); a **code-quality /
test / mock-fidelity** reviewer (coverage run, a 97-request error fuzz across every route, a
builder→grammar check, a strict-catch-all experiment); a **usability / accessibility / value**
reviewer (template + route read, WCAG contrast math, web research). The top findings were then
re-checked against the code by the orchestrating session. **CONFIRMED** = demonstrated by running
code or read directly in the source; **PLAUSIBLE** = reasoned, not demonstrated. PoC scripts were
session-scratch and are not kept — each item says how to reproduce.

## Scorecard

| Dimension | Now | Realistic target | What caps it below 10 |
|---|---|---|---|
| Security | 7 | 9.5 | 10 needs Developer ID signing + notarization (a paid Apple account — decision D1) |
| Code quality | 7 | 9 | — |
| Test quality | 7 | 9 | — |
| Mock fidelity | **4** | 8 offline, 10 with live fixtures | Only real GAM output from a throwaway tenant (Phase 8) proves the mock |
| Tooling / CI | 6 | 9.5 | — |
| Usability | 6 | 9 | — |
| Accessibility | **3** | 9 | — |
| Docs / first run | 5 | 9 | 10 needs a downloadable release (Phase 7) |
| Product value | 6 | 8–9 | Depends on scope decisions D4–D6 |
| Agent framework (CLAUDE.md, skills, hooks, runbooks) | 6 → **8.5** (fixed 2026-09-23) | 9.5 | Remaining items in Phase 0 |

What already holds up, and was actively attacked without success: argv-only execution (all 512
auto-promoted reads enumerated, each slot exactly one element, none emits a write verb); the
Origin-before-token web gate (cross-port / `Origin: null` / `Sec-Fetch-Site: same-site` all 403,
constant-time compare); the inode-bounded, `O_NOFOLLOW` credentials import; the fail-closed GAM pin;
template escaping (no `|safe`, every `tojson` in a single-quoted attribute, signature HTML only in a
sandboxed iframe); `csv_safe` on every export cell; zero 5xx across 97 error-path requests; 88% line
coverage.

## Does it serve value? — yes, for a specific admin
**Who:** a solo Google Workspace admin at a 50–5,000-person org, on a Mac, who knows GAM and cares
about credential hygiene. **Differentiators:** Keychain-only credentials materialized per call and
wiped (raw GAM leaves plaintext files); a server-side guard with a preview and an audit log of every
write; a scoped signature designer with variables (the Admin console has no native per-user
signature management); onboarding/offboarding runbooks; calendar sharing that actually subscribes
members; a published list of which writes are live-proven; free and MIT. **Demand signal:** 3 stars
and an active fork (`Sykezzz/gamgui`, pushed 2026-09-20, adding Classroom/Drive — schools).
**Competition (reported by the reviewer, not re-verified):** *GAM & Google Workspace Companion*
(gam-gui.com) — a native, **notarized**, paid macOS GAM GUI with dry-runs, audit workflows, CSV
export, scheduling and multi-domain; GuruGabe's GAM-GUI-Overlay (Windows, 317 tasks); commercial
suites (GAT Labs, CloudM). **Gaps that send admins elsewhere:** no downloadable app; no CSV bulk
for users outside onboarding; no licence/OU management; no group creation/settings; offboarding
lacks sign-out, group removal, licence release; one tenant only; reports you can't act on.

## Operator decisions (2026-09-23)
- **D1** No Apple Developer account for now → hardened runtime only (S1c); V1 ships self-signed or
  not at all.
- **D2** Keep browser mode. It is only the fallback when pywebview is missing (`app.py` `main()`),
  so the `.app` never uses it; S2 reduces to a printed warning + a SECURITY.md note.
- **D3** Keep the sensitive auto-promoted reads **and audit them**.
- **D4** Remove the "store" wording from the UI (neutral "Department").
- **D5** Leave `abapit_connector.py` as is (it may need a rewrite later); only scrub the local path.
- **D6** No new scope. Priority is making what's in use solid: **onboarding, offboarding,
  signatures, delegation**.
- **D7** Dark mode: not now.
- **D8** No throwaway account yet. The operator will offboard a real user or two soon — so the first
  real offboarding *is* the live test, and offboarding is hardened first.

## Decisions the operator needed to make (items that depend on them say so)
- **D1 Apple Developer Program ($99/yr)?** Needed for Developer-ID signing + notarization (security
  10, a releasable `.app`). Hardened runtime (S1c) does *not* need it and can ship now.
- **D2 Browser mode:** keep it and fix the cookie disclosure properly (S2, M), or drop browser mode
  from the packaged `.app` (S) and keep it for development only. Native WKWebView mode is unaffected.
- **D3 Sensitive auto-promoted reads** (`show/print backupcodes`, `print browsertokens`,
  `get drivefile`/`document`): exclude them from auto-promotion (curate them instead) or keep them
  and audit them. This revisits the documented "reads are open" trade-off.
- **D4 "Store" vocabulary in the public UI** ("Department (store)", "Bulk: set store"): neutral
  labels for everyone (recommended — the tenant's department=store model is unaffected), or a
  configurable label.
- **D5 `abapit_connector.py`:** it's 0% covered and its docstring names a local path, but it is
  the seed of the multi-connector vision. Keep it (give it a real test, scrub the path) or park it
  on a branch until the second connector is actually built.
- **D6 Scope expansions** from Phase 7 (bulk act-on-report, fuller lifecycle, licences/OUs,
  multi-tenant): which, if any. Each is additive; none is needed for the other scores.
- **D7 Dark mode** (accessibility A8, M effort): worth it?
- **D8 Live verification** (Phase 8): a throwaway user/group/calendar on the real tenant, with
  per-action permission — the only thing that moves mock fidelity and "does the write work" to 10.

## Conventions for executing this plan
One item (or a tight group) per commit, straight to `main` when the operator asks for commits.
`.venv/bin/python -m pytest -q` green before each commit. A `fix:` commit needs a fresh
`docs/failure-log.md` entry (the local hook blocks it otherwise) — every CONFIRMED defect below is a
real incident and gets one. Docs follow code in the same commit (the area's runbook). For the
security items, one adversarial verification pass on the finished diff earns its cost (CLAUDE.md
"Working here economically"); for everything else, none. Mutations are never run against the real
tenant without per-action permission.

---

## Phase 0 — Agent framework follow-ups (S each)
- **F1 Run `/improve-rules`.** 7 `fix:` commits since 2026-08-12 have never had an observer pass —
  the nudge fired but the hook's output was invisible until 2026-09-23 (failure-log entry). The
  RULE-FEEDBACK #2-wording item and the "guard is UI-only on five routes" finding (S4) are the
  obvious evidence.
- **F2 Commit a generic mock-backed preview script** (`scripts/preview_mock.py`: in-memory vault,
  `tests/fixtures/mock_gam.sh`, seeded example.com data, `127.0.0.1:8766`) and point
  `.claude/launch.json` at it. Today launch.json points at one session's scratch file, which no
  other session has, so future sessions can't visually verify a UI change.
- **F3 Private-term tripwire in the local pre-commit hook** (gitignored, so the terms themselves
  never enter the public repo): refuse a commit whose diff adds the operator's company name/domain.
  That leak happened (U3) despite a de-branding rule that lived only in memory.

## Phase 1 — Exposure and safety (mostly S; do first)
- **S3 `gam-watch.yml` expression injection — CONFIRMED.** `.github/workflows/gam-watch.yml:62`
  expands `${{ steps.check.outputs.latest }}` (an upstream release tag) into a `run:` script, in a job
  with `contents: write` + `pull-requests: write`, before attestation runs. A legal tag like
  ``v7.49.0$(curl${IFS}…|sh)`` executes. **Fix:** pass it via `env: LATEST:` and use `"v$LATEST"`;
  in the check step reject anything not matching `^[0-9]+(\.[0-9]+){2,3}$`. Grep every workflow for
  other `${{ … }}` inside `run:`.
- **S1 `gam` binary chosen by env var, full env inherited — CONFIRMED.** `core/gam/runner.py:52-54`
  honors `GAMGUI_GAM_BINARY` even when frozen; `_build_env` (`:89`) copies all of `os.environ`
  (incl. `DYLD_*`); `scripts/build_app.sh:44-45` signs without hardened runtime. A same-user
  `launchctl setenv GAMGUI_GAM_BINARY ~/x` makes the next launch hand all three plaintext
  credentials to an attacker binary with no Keychain prompt. **Fix:** (a) ignore the override when
  `sys.frozen`; (b) give `gam` an allowlisted env (`PATH HOME LANG LC_ALL TMPDIR GAMCFGDIR
  GAM_NO_UPDATE_CHECK`); (c) `codesign --options runtime`. **Test:** a tripwire asserting the
  subprocess env keys ⊆ the allowlist and that the override is ignored under a faked `sys.frozen`.
- **S4 Guard enforced only in the UI on five write routes — CONFIRMED.** No server-side
  `confirmed`/typed check at apply time in `users.py` `/suspend/apply` (~:521) and `/bulk/apply`
  (~:326), `calendars.py` `/event/delete` (~:505), `lifecycle.py` `/offboard/run` (~:156),
  `signatures.py` `/apply` (~:171); bare POSTs ran a suspend, an event delete, a company-wide
  signature overwrite and a full offboarding. Builder, account delete and calendar delete *do*
  check. **Fix:** one helper, e.g. `enforce_guard(previews, form) -> Optional[error]`, called by every
  mutating route before any GAM call (destructive → posted `confirmed=1`; bulk-destructive → typed
  confirm), and update the templates to post the field. **Tripwire:** a parametrized test that
  enumerates every mutating POST route, posts without confirmation, and asserts the recording mock
  saw zero `gam` calls. Then fix `guard.py`'s docstring claim.
- **S7 Redaction is positional — CONFIRMED (against an echoing mock).** `core/gam/errors.py:19-23`
  and `core/audit.py:41-48` mask "the value after a sensitive key"; a hire whose surname is
  `Password` (or `Signature`) shifts the mask and the temp password reaches `audit.jsonl`
  `extra.error` and the UI. **Fix:** redact by value — `_run_write(..., secrets=[pw])` replaces every
  occurrence in argv, stderr and `str(exc)`; keep the positional pass as a second layer. **Test:**
  the surname-`Password` case end-to-end into `audit.jsonl`.
- **S6 Stale-config sweep follows symlinks — CONFIRMED.** `core/secrets/ephemeral.py:69, 136-149`: a
  `gamcfg-*` symlink in the runtime dir makes the shutdown sweep zero every file in its target.
  **Fix:** skip `is_symlink()` entries, open files `O_NOFOLLOW`, `rmtree` only real dirs. **Test:**
  symlink to a temp dir with a sentinel file; sweep; sentinel intact.
- **S5 FIFO import hangs the app — CONFIRMED.** `core/setup.py:242` opens before the `S_ISREG` check
  and a FIFO blocks; `web/routes/setup.py:58` runs the sync import on the event loop. **Fix:**
  `O_NONBLOCK` on the open, and `run_in_threadpool(import_dir, …)`.
- **S8 Gate edges — CONFIRMED.** `web/server.py:143,154`: any `Host` accepted (a DNS-rebound page can
  use `/healthz` as a port oracle); `path.startswith("/static")` would exempt a future `/staticfoo`
  route from the token. **Fix:** Host allowlist `127.0.0.1:<port>` / `localhost:<port>`; match
  `path == "/static" or path.startswith("/static/")`.
- **S2 Launch-token cookie readable by other loopback ports (browser mode) — CONFIRMED.**
  `web/server.py:164`: cookies aren't port-scoped, so a page on another `127.0.0.1` port receives
  the token via a same-site fetch after a navigation, then drives GamGUI with curl (no Origin →
  allowed). Depends on **D2**: drop browser mode from the `.app` (S), or serve browser mode on a
  random per-launch `<rand>.localhost` host (cookie becomes host-only) plus S8's Host allowlist (M;
  verify WebKit/Chrome resolve `*.localhost` first).
- **S9 Sensitive auto-promoted reads — CONFIRMED buildable.** Depends on **D3**. Either a small
  denylist in `core/catalog/readbuilder.py` (the `_make_reads_buildable` gate) for
  `backupcodes`/`browsertokens`/`get drivefile`/`get document`, or route them through an audited
  read. `get drivefile` also writes files into the process CWD.
- **Q6 Chokepoint tripwire can't see unaudited writes.** Extend
  `test_audit_record_only_in_run_write_or_allowlist` to every `run_authenticated(` call site; route
  the Builder's `todrive` export (`web/routes/builder.py` ~:277, creates a Sheet, possibly in another
  user's Drive via `tduser`) and `reset_password`'s follow-up `signout_user` (`gam_connector.py`
  ~:335) through audited paths.
- **Q8 Error classification can report a partial failure as success — PLAUSIBLE.**
  `core/gam/errors.py:57-77` classifies by the first matching pattern anywhere in stderr; one
  tolerable "not found" line can mask a real error in the all-users calendar-ACL sweep. **Fix:**
  classify per line; tolerate only when every error line is tolerable. **Test:** a mixed-stderr mock
  case.
- **Q7 Calendar role unvalidated on one path.** `web/routes/users.py` ~:399 passes any role;
  `calendars.py` ~:361 checks. **Fix:** validate against the grammar's `CalendarACLRole` inside the
  `GAMCommands` builder so every path is covered.
- **B1 Bulk-onboarding CSV 500 — CONFIRMED.** `core/onboarding.py:165` iterates the reader outside
  the `try`; a field over 131,072 chars raises `csv.Error`, uncaught by `/onboard/bulk/preview`
  (`routes/onboarding.py:444`). **Fix:** move the loop inside the `try`, catch `csv.Error`; cap the
  upload size (e.g. 1 MB) before decoding. (This is batch-2 plan item F#21.)
- **B2 `BatchJob.failed` is unbounded** (`users.py` ~:290, `builder.py` ~:370, `lifecycle.py` ~:149)
  — invariant #9. Cap like `ApplyJob`/`OnboardJob` (`_FAILED_SAMPLE_CAP`).
- **U2 Offboarding runs on unvalidated, un-previewed input.** `lifecycle.py:44-53` falls back to the
  raw address for an unknown user; `_offboard_preview.html:13` submits the live form, so editing
  after Preview runs against an account nobody previewed; a failed step doesn't stop the routine, so
  a typo'd manager yields a half-offboarded account. **Fix:** validate both addresses against the
  cached directory and block unknowns; warn on a suspended manager or an admin leaver; freeze the
  previewed values in hidden fields; stop after the password reset if the delegate target is
  invalid.
- **U6 Signature designer defaults to "Whole company".** `signatures.html:36`; overwrite confirmed
  only by a browser `confirm()` (`_sig_preview.html:13`), though the README promises typed
  confirmation for bulk. **Fix:** default scope "Specific user (test)"; typed confirmation (the
  count) above a threshold; covered server-side by S4. (Backing up existing signatures first: M,
  optional.)
- **U3 Company branding leaked back into the public repo — CONFIRMED.** Placeholders
  `person@<company domain>` (`bulk_store.html:35`), the company's town (`_org_form.html:8`,
  `bulk_store.html:24`), its vendor names in the seed role (`core/onboarding.py:51-52`,
  `onboarding.html:70`), a calendar name (`calendars.html:23`, `gam_connector.py:235`), and a local
  home path in `abapit_connector.py`'s docstring. **Fix:** generic example.com placeholders and a
  neutral seed role; apply D4. Add F3's tripwire so it can't recur. (Git history keeps the old
  strings; rewriting history is not proposed.)

## Phase 2 — Make the tests honest (mock fidelity 4 → 8)
- **T1 The mock's catch-all succeeds — CONFIRMED.** `tests/fixtures/mock_gam.sh:299-304` exits 0 for
  any unhandled argv, including `delete user`, `remove calendars` and `delete events` without
  `doit`. 23 of 33 used write builders have no handler; making the catch-all fail broke 20 tests
  whose "success" rested on it. **Fix:** catch-all → `exit 2` with `ERROR: mock: unhandled argv`,
  then a strict handler per builder that checks the required keywords (from the grammar) and fails
  like GAM otherwise. Missing today: `update_organization set_suspended reset_password
  add_calendar_acl delete_calendar_acl remove_calendar delete_event delete_user undelete_user
  signout_user add_calendar_event set_signature add_delegate remove_delegate set_vacation
  vacation_off add_forwarding_address set_forward forward_off create_user_alias delete_alias
  create_group remove_group_member`. Permissive (own handler, always ok): `create_tasklist
  add_calendar_acl_cal delete_calendar_acl_cal`. `remove_all_calendar_acls` always fails (success
  path never exercised).
- **T2 `info user <any>` returns Alice** (`mock_gam.sh:35-42`) — the same lie as the group-members
  bug already in the failure log. Key on the address; 404 unknowns. Also implement the header's
  claimed-but-missing check that `$GAMCFGDIR/oauth2service.json` exists on authenticated calls.
- **T3 Nine tests pass when the write fails:** `test_users_web.py` —
  `test_vacation_set_and_off`, `test_user_groups_view_add_remove`,
  `test_groups_board_members_view_and_mutate`, `test_signatures_apply`,
  `test_bulk_store_apply_runs_as_job`, `test_lifecycle_offboard_run_starts`,
  `test_offboard_executor_runs_every_step` (checks `job.done`, not applied/failed);
  `test_builder.py` — `test_run_mutation_goes_through_guard_and_audit`,
  `test_sequence_add_remove_and_run`. Routes return 200 on failure, so status codes prove nothing.
  **Fix:** a shared `assert_ok_partial(r)` that rejects the amber error partial, plus assertions on
  the audit record (`ok=True`) and on the argv the mock received.
- **T4 Contract test is coarse.** `tests/test_command_contract.py:27-67` checks bare words anywhere
  in a 391 KB grammar; 60 of 99 builder keywords (incl. `doit`, `eventid`, `returnidonly`,
  `notifypassword`, `sendupdates`) aren't tracked. **Fix:** generate tokens by calling each builder
  with placeholders; assert whole-word presence and that the leading words match a grammar line.
- **T5 Untested failure paths:** the runner timeout/kill path (`runner.py` ~:107-110 — add a mock
  handler that sleeps) and the ephemeral-wipe error paths (`ephemeral.py` ~:75-78, ~:151-154 —
  invariant #4).
- **T6 Coverage gate** in CI at the current 88%, ratcheting to 90%+ with the error branches of
  `users.py` (79%, `/delegate/remove` untested), `reports.py`, `calendars.py`, `groups.py`.
- **T7 (highest ceiling, M–L) A Python mock that validates argv against `GamCommands.txt`** using the
  existing `core/catalog/parser.py`, replacing hand-written syntax checks; the shell mock keeps only
  canned outputs.

## Phase 3 — Tooling and supply chain
- **C1 Ruff** (lint + format) with a CI step; fix or `noqa` the initial findings in one commit.
- **C2 mypy** on `core/` (start non-strict, ratchet), CI step.
- **C3 Hash-locked dependencies** (`pip-compile --generate-hashes` or `uv lock`) used by CI *and*
  `scripts/build_app.sh` (`--require-hashes`). Today CI installs flexible ranges, the pinned
  `requirements.txt` is unused and lacks pytest-timeout/pytest-cov, and the `.app` freezes whatever
  the dev venv resolved.
- **C4 SHA-pin GitHub Actions** and add the `github-actions` ecosystem to `.github/dependabot.yml`.
- **C5 CI job that builds the unsigned `.app`** and smoke-imports the frozen modules.
- **C6 Attestation hardening:** add `--signer-workflow`/`--source-ref` to `gh attestation verify` in
  `scripts/bump_gam.py`.
- **C7 Stale config:** `sonar-project.properties` Python `3.9,3.12`; the 3.9 note in
  `pyproject.toml`'s `filterwarnings`; the Starlette/httpx TestClient deprecation warning.

## Phase 4 — Accessibility (3 → 9)
- **A1 Contrast (S, global):** `brand-gray` #B2B4BB text is 2.07:1 on white (195 uses incl. headings
  and helper text); `blueink/80` 3.86:1, `/70` 3.14:1; white on `brand-blue` 3.97:1. Adjust the
  Tailwind tokens in `base.html` to ≥4.5:1 for text (keep the light gray for borders only).
- **A2** ARIA tabs (`tablist`/`tab`/`aria-selected`/arrow keys) on user detail and onboarding.
- **A3** Stable `aria-live="polite"` containers for polled progress (swap the inner content, not
  the region); text status alongside ✓/✗ (they're `aria-hidden` today, `_sig_apply.html:20-21`);
  risk shown as text, not only a coloured dot (`_command_row.html:2`).
- **A4** Visible or `aria-label` labels on placeholder-only inputs (Users/Audit/Builder/Calendars
  search, delete-confirm).
- **A5** Focus management: after an HTMX swap into a confirm panel, move focus to its heading or
  primary button (`htmx:afterSwap`).
- **A6** Keyboard way to add a group member (see U7).
- **A7** Vendor the two Google Fonts locally — also makes the README's "nothing leaves your machine"
  true (`base.html:10-12` loads them from a CDN).
- **A8** Dark mode (M) — depends on **D7**.
- **A9** An automated axe-core check over the main screens in CI (M; needs a headless browser).

## Phase 5 — Usability
- **U1 Builder filter sees only 100 rows (S–M).** `_records_table.html:16` renders `records[:100]`
  and the "Filter rows" / "External only" controls filter only those — an external-sharing audit on
  5,000 rows can falsely show nothing. Filter server-side over the retained result, or ship all rows
  to the existing client pager; always show "N of M".
- **U4 Home page is stale** (`index.html:52-58` says the shipped screens "come next"): correct it
  (S), or make it a dashboard — report counts, recent audit failures, running jobs, active tenant (M).
- **U5 Jobs tray (M):** a list of `st.jobs` in `base.html` so a job survives navigation, with Stop
  (the ROADMAP's cancel flag) and "retry failures only".
- **U7 Groups board (M):** type-ahead add (reuse the onboarding picker) instead of drag-only; use
  the cached `st.groups()` (`groups.py:30` calls GAM live each visit); a member-role column; confirm
  on remove.
- **U8 Users list (S–M):** `hx-push-url` for search/page and the tab in the hash (Back currently
  loses all state); search department and OU too; larger pages; sort.
- **U9 Friendly write errors (S):** `gam_connector.py` ~:482 returns raw `str(exc)`; show the
  `remediation` text (as reads already do) with the raw error expandable; keep a capped reason per
  failed user in bulk feeds (`signatures.py` ~:70-81 lists emails only).
- **U10 Setup (S–M):** infer the domain from the admin email; list the DWD scopes and the
  pre-filled authorization link up front (`_dwd.html:40`); show the active tenant in the header;
  `AppState.create` silently picks `list_domains()[0]` (`server.py:83`) — make it explicit.
- **U11 Print in the native window — PLAUSIBLE broken.** `_onboard_run.html` prints a dynamic
  iframe; pywebview only overrides top-level `window.print`. Test in the built `.app`; add a Copy
  button; print from a top-level page if needed. The temp password is shown once.
- **U12 Cache at scale (M):** a single title save invalidates the whole directory
  (`users.py` ~:259, ~:297; `lifecycle.py` ~:176) so the next page re-runs `gam print users`; update
  the one record instead; serve stale-while-refreshing with an "as of" label (`age_seconds` exists,
  unused); give domain-wide commands (the all-users calendar-ACL sweep) their own timeout (default
  120s, `runner.py:24`).
- **U13 Small (S):** current-page indicator in the nav (`base.html:71-82`); `hx-disabled-elt` on
  Apply/Run (double-click starts two jobs); load user-detail tabs on first open, not all five on
  page load (`user_detail.html:73-111`).
- **U14 Precompiled CSS (M):** `base.html:16` runs Tailwind's in-browser JIT (`tailwind-play.js`).
  Build the CSS at `make app` time — faster first paint, and a prerequisite for Q13's CSP.

## Phase 6 — Architecture and code health
- **Q9** Move business logic out of routes: `_provision_hire` and helpers (`routes/onboarding.py`
  ~:79-203) → `core/onboarding.py`; `_run_offboard` → `core/lifecycle.py`; bulk store
  (`users.py` ~:277) → core. Tests then import core, not private route functions. (Supersedes
  batch-2 F#11.)
- **Q10** Shared `web/routes/_common.py` for `_friendly` (7 copies), `_err` (5), `_conn` (3); one
  bounded job base class replacing `BatchJob`/`ApplyJob`/`OnboardJob` (their `_RECENT_WINDOW` /
  `_FAILED_SAMPLE_CAP` are defined twice). (Supersedes the 09-18 plan's suggestion #6.)
- **Q11** Delete the six never-called builders (`update_user unsubscribe_calendar
  delete_forwarding_address create_project oauth_create create_svcacct`); `create_project` also
  mixes two grammar forms. `abapit_connector.py` per **D5**.
- **Q12** Small: `builder.py` ~:370 does work inside a ternary; the audit log stores `target_extra`
  under the key `"group"` even for event ids and ACL scopes (`gam_connector.py` ~:476, ~:485);
  Builder writes are audited as action `"apply"`, losing the command id (~:448).
- **Q13 Real script CSP (M–L, after U14):** move inline handlers into same-origin JS files and set
  `default-src 'self'; script-src 'self'` (with `img-src https:` for signature images — the
  declined `img-src` clamp stays declined). Any future template XSS becomes inert.
- **Batch-2 leftovers:** F#22 (`_bulk_summary` split), F#23 (one print-sheet helper).

## Phase 7 — Distribution and value (depends on D1, D6)
- **V1 GitHub Release `.app`** built by CI from the hash-locked deps (C3), hardened runtime (S1c);
  Developer-ID signed + notarized if **D1**. Two-line install at the top of the README. This is the
  single biggest gap versus the paid competitor.
- **V2 README/ROADMAP truth pass (S):** a top section — who it's for, install, first read in 5
  minutes; fix stale counts (README "~1,040"; ROADMAP 533/1,067 → 538/1,075) and ROADMAP items that
  have shipped (onboarding account creation, screenshots). Consider a drift test like
  `test_polish.py`'s CLAUDE.md count check for README/ROADMAP.
- **V3 Bulk act-on-results (M–L, D6):** select users from a report bucket or CSV → preview →
  guarded job with Stop and retry-failures, reusing the guard + job machinery.
- **V4 Fuller lifecycle (M–L, D6):** offboarding adds sign-out/deprovision, group removal, licence
  release, move to a "departed" OU; basic OU and licence management.
- **V5 Multi-tenant switching (M, D6)** — the caches are already tenant-bust-aware.
- **V6 A short task-based user guide** (`docs/guide.md`): first setup, onboard a hire, offboard,
  roll out a signature, audit external sharing.

## Phase 8 — Live verification (gates every "10" involving GAM writes; needs D8)
Walk README "Live verification status" on a throwaway user/group/calendar, one write at a time, with
the operator's per-action permission: account create/delete/undelete, suspend, reset password,
datatransfer, calendar ACL add/remove, event delete, signature set, delegate add/remove, group
add/remove, task list/tasks, welcome email. Capture real stdout/stderr (redacted) into
`tests/fixtures/` so the strict mock (T1/T7) replays real GAM, and update the README table.

## Recommended order
Phase 0 F1–F3 and Phase 1 first (about two focused days; S3, S1, S4, S7, U3 are the most important
items in this plan). Then Phases 2 and 3 together — they make every later change safer. Phases 4 and
5 interleave by screen. Phase 6 opportunistically as screens are touched. Phase 7 per D1/D6. Phase 8
whenever the operator has a throwaway account ready.
