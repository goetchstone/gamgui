# Plan: back-port SwiftGamGui's features and fixes to GamGUI

**Status: PHASE A IN PROGRESS — A1 applied (branch `fix/gam-csv-escape-char`); A2–A9 open. The operator
lifted the freeze for Phase A only (G1 (a), 2026-10-30). Phases B–F stay PROPOSED, and G2–G5 only matter
if one of them is approved.**
Written 2026-10-30 at GamGUI `9cad029` and SwiftGamGui `3394130`, for a reader with no session context.
`CLAUDE.md` is auto-loaded. Before any item, read the SwiftGamGui runbook and failure-log entries it names
(`../SwiftGamGui/docs/domains/`, `../SwiftGamGui/docs/failure-log/`), as well as GamGUI's own runbook for that area.
When a PR closes an item, mark it here in the same PR.

## Context

SwiftGamGui is a native SwiftUI rewrite of GamGUI. It runs the same `gam` argv and is held to GamGUI's builders by
fixtures generated from this repo (`../SwiftGamGui/scripts/gen_fixtures.py`). Since 2026-10-08, GamGUI has been
**frozen except for GAM updates** (`docs/plans/2026-10-08-native-gamgui.md` §1), and new work has gone to Swift.
In three weeks Swift grew to ~42k lines plus ~21k of tests. Two kinds of work there matter to this repo:

1. **Bugs in logic that GamGUI shares.** Swift found them by reading GAM's source while porting GamGUI's
   readers and mock. Several of its failure-log entries say that frozen GamGUI reads the same way today.
   These are the cheapest and most valuable items here (Phase A).
2. **Features that exist only in Swift.** Ranked from smallest to largest: reports and users additions, the
   jobs tray, more offboarding steps, phishing clean-up, and **incident response**. Incident response is about
   11k Swift lines, roughly the size of all of GamGUI's `core/` plus `web/routes/` today.

Why GamGUI at all: SwiftUI is Apple-only, so GamGUI is the only codebase that could ever run off the Mac.
That reason is a thought experiment, not a plan. See `ROADMAP.md`, "Deliberate trade-offs".

## Decide first (the operator)

- **G1. Lift the freeze, and how far.** (a) Phase A only. These are bug fixes, closest to "maintenance".
  (b) Phases A–D. (c) Everything. Whichever is picked, update the README's banner and Status section in the
  first PR, since they say new work happens in Swift.
  **Decided 2026-10-30: (a), Phase A only.** The README keeps "new work happens in SwiftGamGui". Its
  "GAM updates and dependency fixes" line gains "and fixes for bugs SwiftGamGui found in shared logic".
- **G2. A snapshot, not a sync.** Recommended: this plan ports Swift as of `3394130`. A later Swift feature is
  a new plan, not an obligation. Keeping both apps in step doubles the cost of every incident-rule change.
- **G3. Offboarding templates (C5).** Swift's saved routines conflict with this repo's KISS decision
  (`2026-10-02-configurable-offboarding.md`, "Not planned: playbooks"). Default: don't port them.
- **G4. PDF export (E8).** Swift prints a PDF natively. Here, a download in the app window is a known trap
  (`tests/test_app_window_downloads.py`). Default: Markdown, text and CSV only.
- **G5. The Cloud Identity devices scope (B4, E7).** Reading "computers signed in" needs
  `https://www.googleapis.com/auth/cloud-identity.devices`. Adding it to Setup's DWD list (`core/setup.py`,
  ~l. 362) means every existing install must re-authorize. Default: leave those reads out until asked.

## Rules every item keeps

- **The invariants in CLAUDE.md, unchanged.** Every new write goes through a `GAMCommands` builder, then
  `ChangePreview`, then `guard.enforce()` in the apply route, then `_run_write`. Every new POST is listed in
  `tests/test_write_routes_guarded.py`. Polled feeds stay bounded (invariant 9). Nothing goes through
  `| tojson` in an attribute (invariant 8).
- **Argv identical to Swift's builder.** Each new builder gets a golden test asserting the same list that
  Swift's `GamCommands.swift` function emits for the same inputs, citing the Swift function and its test. This
  is how the live proof carries over: an identical write that Swift confirmed live is "argv-identical" here.
  The status of each Swift write is in `../SwiftGamGui/docs/live-verification.md`. Anything "new to both
  apps" is unproven until `live-verify` runs it on a throwaway account with the operator's per-action yes.
- **Port the tests' cases, not just the code.** Swift's tests encode what real logs and GAM's source taught it.
  Port its mock handlers for each new command to `tests/fixtures/mock_gam.sh`, checked against this repo's
  vendored grammar at 7.49.02. Swift's mock is stricter, so take its refusals too.
- **One PR per item.** Run the `pre-commit` skill. A fix item gets a GamGUI failure-log entry that cites the
  Swift entry it came from (the `fix:` hook requires one).
- **Reviews scale with the stakes.** Every write PR gets one `gam-command-reviewer` pass. Adversarial
  verification is only for credentials and path handling: F5, C1 and E4.
- **Keep Swift's fixtures honest.** After Phase A changes GamGUI's readers, re-run Swift's `gen_fixtures.py`
  and drop the shims it uses to paper over GamGUI's old readings (e.g. its widened `_INSTRUCTION_LINE`). That
  is one Swift PR.

## Items

### Phase A — Fixes Swift found in shared logic (GamGUI has them today)

Each item: reproduce against GamGUI first (the `post-failure` skill), fix, add a tripwire. Most were found
by reading GAM's source and never seen live; say so in the failure-log entry.

- **A1. GAM's CSV escape character.** `core/gam/parser.py:84` reads with the default dialect. GAM writes
  `escapechar='\\'` (its `setDialect`). Rows whose JSON holds a quote, like a name `Ann "Q" Lee` or a group
  description, are damaged or dropped without a word. Swift reproduced GamGUI returning 3 of 4 rows. Swift
  entry: `2026-10-10-gam-csv-escapes`. Re-check GAM's `setDialect` at 7.49.02 before fixing.
  **Applied:** GamGUI failure log 2026-10-30 "GAM's CSV escapes". Re-checked at 7.49.02 (Python 3.15).
  `scripts/gam_rc_table.py` now refuses a build whose dialect moved, and the mock's `print users` prints
  GAM's escaped CSV.
- **A2. Progress read as the error.** `core/gam/errors.py:131` `_PROGRESS_LINE` covers 2 of GAM's 6 progress
  printers. The others become a failure's message. Swift entry: `2026-10-13-gam-progress-read-as-error`.
- **A3. GAM's command echo read as the failure.** `_error_lines` keeps the echo GAM prints before a usage or
  entity-does-not-exist error. Swift entry: `2026-10-13-gam-command-echo-read-as-the-failure`.
- **A4. An auto-reply's text hid the auto-reply.** `core/gam/models.py:100` `Vacation.from_show_text` ends the
  message at any line that looks like a setting. An attacker's reply containing `Enabled: False` reads as off,
  and nothing offers to turn it off. GAM prints `Message:` last. Swift entry:
  `2026-10-21-auto-reply-text-hid-the-auto-reply`. Swift's `Tests/Fixtures/vacation.json` pins the old
  reading; regenerate it.
- **A5. Mock lies (`tests/fixtures/mock_gam.sh`).** Make each one fail as GAM does, then see what goes red here.
  A red test is a real bug the mock was hiding.
  - `create user` for a taken address (`2026-10-12-duplicate-create-mock-lie`)
  - the `add calendars` line shape (`2026-10-13-calendar-subscribe-two-mocks`)
  - offboarding's transfer and deprovision cases M1–M5, including a transfer to the leaver's own alias
    (`2026-10-13-offboarding-mock-lies`)
  - `print forwardingaddresses` answering for anyone, and `delete delegate` for a non-delegate
    (`2026-10-19-mock-gmail-settings-answered-anyone`)
  - found in A1: the `formatjson` reads other than `print users` (groups, group members, cros, resources,
    calendars, calendar ACLs, events, Drive files) print NDJSON, which GAM never does. Port SwiftGamGui's
    approach: GAM's `key,JSON` CSV through `print_formatjson`, with each read's key column checked against
    GAM's source rather than guessed. A read whose shape isn't modelled is refused, not answered as NDJSON.
- **A6. The connected admin, at the chokepoint.** Today only offboarding checks it
  (`core/lifecycle.py:236`). That check compares by email and is skipped when the admin is unknown, the shape
  Swift's `2026-10-13-offboarding-could-offboard-its-own-admin` exploited. Users' suspend, delete and sign-out
  have no check at all. Port Swift's design (guard-audit runbook, "Built: the connected-admin check"):
  - Refuse any write that cuts an account off (delete, suspend, deprovision, password reset or set, sign-out)
    when it targets the admin GamGUI signs in as.
  - Compare by Google account ID: the token's `sub`, kept only once `info user <admin> fields primaryEmail,id`
    agrees.
  - Check at preview, then again just before the run, by reading each target as GAM will be sent it. An
    unconfirmed admin refuses.
  - Adversarially verified, since this is credentials.
- **A7. The usage report's "as of" date** names the day asked for, not the day GAM's rows are from (Swift
  `audit-reports.md`, "Deviations from GamGUI").
- **A8. Audit export ignores the screen's search and filter.** The route takes both, but the page's link
  drops them (same section).
- **A9. A token write-back can undo a re-import.** Swift's `directory-home.md`, "Fixed in PR #8's review": a
  load's refreshed `oauth2.txt` replaced a token re-imported mid-call. GamGUI has the same shape.
  `_write_back_refreshed_token` (`core/secrets/ephemeral.py:313`) compares GAM's file with what it
  materialized, never with what the vault holds now, then sets the vault. Fix it with a compare-and-swap:
  write back only while the vault still holds the materialized token. Reproduce it first.

### Phase B — Reads and small additions

- **B1. Reports: connected apps and AI agents.** Every account's OAuth tokens, as a summary and a list.
  Domain-wide, so cached and bounded. Swift: `GamEngine/ConnectedApps.swift`, `App/ConnectedAppsView.swift`,
  `audit-reports.md`. The on-device model's wording is not ported.
- **B2. Reports: drill-downs.** Each directory count opens the people behind it (`DirectoryReport.swift`), and
  storage and mail usage get their list (`UsageReport.swift`, compared against `/reports/usage`).
- **B3. Jobs tray: mostly here already.** GamGUI's tray (plan U5, `web/routes/jobs.py`) already lists every
  background job from any page, with Stop, and Swift's bounds came from it. Swift's only addition is listing
  one-step changes too. Low value: drop it unless asked. (Corrected 2026-10-30. The first draft called the
  tray Swift-only.)
- **B4. Users: Security reads.** A person's Chrome profiles, mobile devices and, after G5, computers. Swift
  `compromised-account.md` slices 5 and 8. Reads only.

### Phase C — Small writes

- **C1. Temporary password.** Changed at next sign-in, masked in the preview and the audit, and shown once.
  New to both apps, so verify live on a throwaway user. It is in A6's set. Swift: `GamCommands.setPassword`,
  `onboarding.md` D4. Adversarially verified.
- **C2. The phone a signature shows.** GAM replaces the whole list, so write back every other phone unchanged
  and re-read the phones just before the run; one added since means "preview again". Setting is confirmed live
  in Swift (2026-10-24); changing or removing is not yet. Swift: `GamCommands.updatePhones`,
  `directory-home.md`, "Users writes".
- **C3. Offboarding's added steps, off by default:**
  - clear recovery email and phone, hide from the directory, IMAP off, POP off: confirmed live in Swift
    (2026-10-16), so argv-identical here
  - hand over secondary calendars, remove filters that forward: not yet live in either app

  This closes most of `2026-10-02-configurable-offboarding.md` P2 (GAL off, `popimap`, recovery); mark it
  there. Check that eight steps plus the added ones reach the guard's typed `confirm` (≥10 targets with a
  destructive step), as Swift's `offboarding.md`, "Typed confirm", found.
- **C4. Typed address for a destructive step.** GamGUI already asks for the exact address on every account
  delete (`core/guard.py`), and for the word or the count on a large destructive change. Swift also asks for
  it on its other destructive single-person changes. Extend the guard's rule to the incident lock (E1) when E1
  lands. Suspend stays a Confirm click unless the operator asks for more.
- **C5. Offboarding templates.** Only if G3 says yes.

### Phase D — Phishing

- **D1. Find by Message-ID.**
  - Strict RFC 5322 `id-left@id-right`, accepting the bare id, `<id>`, the header line or `rfc822msgid:`.
    Refuse `{ }`, non-ASCII and anything over 998 characters.
  - The query is `rfc822msgid:<id> -in:sent`, so the sender's own copy stays as evidence.
  - Find is a read, not a held write. It is domain-wide, so it runs as a bounded background job.
  - Swift: `PhishingChanges.swift`, `MessageCopies.swift`, `phishing.md` D1–D3.
- **D2. Trash per mailbox.** One guarded step per mailbox that Find showed, not one `all users … doit`, so
  the preview names each mailbox and Stop works between them. Each step is `notquick max_to_trash <count Find
  saw>`, so a run never trashes more than its preview showed. Typed confirm. Argv-identical to Swift's live
  runs (2026-10-19, 2026-10-23). Swift `phishing.md` D4–D6.
- **D3. Spam copies.** Not yet live in either app. Swift: `SpamCopies.swift`.

### Phase E — Incident response (one PR per slice, in this order)

Read `../SwiftGamGui/docs/domains/compromised-account.md` (988 lines) and
`../SwiftGamGui/docs/plans/2026-10-22-incidents.md` first. Put the rules in a new `core/incident/` with no web
imports, and write a GamGUI runbook, `docs/domains/incident.md`, in E1's PR.

- **E1. Check an account and lock it.**
  - Reads: forwarding, filters, delegates, send-as, IMAP/POP, recovery, drafts, scheduled sends.
  - The lock steps (reset, sign-out, forwarding off, IMAP/POP off, clear recovery) are confirmed live in Swift.
    Deleting filters, send-as and drafts, and the scheduled-send clean-up, are not.
  - Swift: `Compromised.swift`, `CompromisedChanges.swift`.
- **E2. Pull the incident logs** (`report <app> user <u>`).
  - Narrow first: the people reported, then the attacker's addresses. A day at a time, oldest first, bounded,
    paging past the 1,000-activity cap, with Read the Rest.
  - Saved as `0600` under `core/paths.py`'s data dir.
  - Runs as a background job, never on the event loop. Swift: `2026-10-23-the-log-reader-stopped-the-app`.
  - A saved pull is added to the open incident in memory, never by re-reading every saved pull
    (`2026-10-23-each-pull-re-read-every-saved-pull`).
  - A read that fails partway must say so (`compromised-account.md`, "Next").
  - Swift: `ActivityLogs.swift`, `IncidentLogs.swift`.
- **E3. The log reader.** Port `Assist/LogReading.swift` (2.1k lines) and its tests as pure Python,
  linear in the events. Carry its failure history:
  - `2026-10-22-a-guessed-device-action-status`
  - `2026-10-22-googles-account-warnings-have-no-actor`
  - `2026-10-23-every-gmail-event-read-as-a-send`
- **E4. A kept incident.** On disk, made by hand or from an export, with evidence copied and fingerprinted
  (SHA-256). It reopens without re-reading. This is file and path handling under the user's data dir, so it is
  adversarially verified, with the same care as invariant 5. Swift: `IncidentStore.swift`.
- **E5. Respond.**
  - Where each account stands, from the admin log's timeline: locked, half-locked, or signed in to again.
  - Still to do, where Do It Here opens a preview with only that fix picked (through the chokepoint, never
    around it).
  - Done from the audit log, and a hand tick for what no log can show, never on a lock, a log read or a
    computer (`2026-10-28-still-to-do-asked-what-was-done`).
- **E6. Who it reached, and where it started.**
  - Every recipient once, with received or bounced from Gmail's own events, and groups read to their members,
    nested too, at most 20 a screen.
  - The warning picked is kept, with a CSV export.
  - Where it started: the incoming phish, marked by the operator, and who else got it.
  - Pull every account from the attacker's address, with a wide read saying up front how far it will get.
- **E7. Devices, Apps Script, Chrome, connected apps for one person.** Block a computer or take the account
  off it (`blockDeviceUser` / `deleteDeviceUser`). Not yet live in either app. The computer reads need G5.
- **E8. The report.**
  - Every number opens its list. Each step taken sits beside the record that shows it.
  - The review: type, severity, the meeting, lessons, the plan going forward.
  - Export Markdown, text and CSVs, with the evidence copied and hashed. PDF only per G4.
  - Swift: `Assist/IncidentReport.swift`, `App/IncidentReportView.swift`.

### Phase F — Docs, in the PR that ships each piece, plus a last sweep

README's Status, including what is only in which app; a "Live verification status" row per write; ROADMAP;
`docs/domains/incident.md` and `docs/domains/phishing.md`.

## Not ported

- **Apple-only:** Siri and App Intents, the on-device model (`Assist/LogExplainer.swift`, the report's
  drafted summary), Touch ID, the native PDF. The findings and Still to do are code; GamGUI shows them unworded.
- **SwiftUI-only failures:** layout loops, table widths, snapshots, the display name, Xcode 27.1.
- **Builder slices 1–2:** GamGUI's Builder is the original.

## Effort (rough, in focused sessions)

| Phase | Sessions | Notes |
|---|---|---|
| A — shared-logic fixes | 2–3 | Highest value per hour; A6 is the largest |
| B — reads and the tray | 2–3 | Reads only, except Stop |
| C — small writes | 3–5 | C1 and the not-yet-live steps need live runs |
| D — phishing | 2–4 | A mass write: typed confirm, throwaway mailboxes |
| E — incident response | 15–25 | E3 and E5 are most of it |
| **Total** | **~25–40** | GamGUI's code roughly doubles |

## Status by item

- **A1** — applied (branch `fix/gam-csv-escape-char`: the parser's escape character, the mock's escaped
  `print users`, the build's dialect check).
- A2–A9 — approved, open.
- B1–B4, C1–C5, D1–D3, E1–E8, F — proposed, not approved (G1 (a)).
