# Plan: back-port SwiftGamGui's features and fixes to GamGUI

**Status: APPLIED, 2026-11-06. GamGUI has every SwiftGamGui feature but the Apple-only ones under "Not ported". Don't re-apply any
item: "Status by item" at the end says where each landed. Still owed: the live checks and the list for SwiftGamGui, both below.**
Written 2026-10-30 at GamGUI `9cad029` and SwiftGamGui `3394130`, for a reader with no session context.
Re-baselined 2026-11-05 at SwiftGamGui `fef14cc`: its five PRs since are fixes, a small jobs tray change and the GAM
bump, so no item changed. `CLAUDE.md` is auto-loaded. Before any item, read the SwiftGamGui runbook and failure-log entries it names
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
  **Changed 2026-11-05: (c), everything.** The operator: "lets get gamgui feature pairity with swiftgamgui". The
  README's banner and Status say so.
- **G2. A snapshot, not a sync.** Recommended: this plan ports Swift as of `3394130`. A later Swift feature is
  a new plan, not an obligation. Keeping both apps in step doubles the cost of every incident-rule change.
  **Still a snapshot, moved to `fef14cc` (2026-11-05).** A later Swift feature is a new item here.
- **G3. Offboarding templates (C5).** Swift's saved routines conflict with this repo's KISS decision
  (`2026-10-02-configurable-offboarding.md`, "Not planned: playbooks"). Default: don't port them.
  **Yes, port them (2026-11-05),** read from the operator's "I just want gamgui to have the same features we built into
  swiftgamgui that includes the incident shit". They didn't name G3, G4, G5 or B3, and asked not to be asked further.
- **G4. PDF export (E8).** Swift prints a PDF natively. Here, a download in the app window is a known trap
  (`tests/test_app_window_downloads.py`). Default: Markdown, text and CSV only.
  **Yes, a PDF too (2026-11-05),** made in a way that works in the app window. E8 settles how.
- **G5. The Cloud Identity devices scope (B4, E7).** Reading "computers signed in" needs
  `https://www.googleapis.com/auth/cloud-identity.devices`. Adding it to Setup's DWD list (`core/setup.py`,
  ~l. 362) means every existing install must re-authorize. Default: leave those reads out until asked.
  **Yes, add it (2026-11-05),** in the PR that first needs it, whose description says to re-authorize.

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
  **Applied:** GamGUI failure log 2026-10-30 "GAM's progress read as the error". `_PROGRESS_LINE` is
  `(Getting |Got \d+ )`; `scripts/gam_rc_table.py` lists the build's Getting/Got messages and
  `tests/test_gam_progress.py` holds them. The mock's `report users` and `deprovision` print GAM's progress
  on stderr.
- **A3. GAM's command echo read as the failure.** `_error_lines` keeps the echo GAM prints before a usage or
  entity-does-not-exist error. Swift entry: `2026-10-13-gam-command-echo-read-as-the-failure`.
  **Applied:** GamGUI failure log 2026-10-31. `_FRAMING_LINE` skips the echo and also usageErrorExit's two
  help lines, which made every usage error's message the wiki's address (in SwiftGamGui too; not in its
  entry). `tests/test_gam_framing.py` holds the words to the build.
- **A4. An auto-reply's text hid the auto-reply.** `core/gam/models.py:100` `Vacation.from_show_text` ends the
  message at any line that looks like a setting. An attacker's reply containing `Enabled: False` reads as off,
  and nothing offers to turn it off. GAM prints `Message:` last. Swift entry:
  `2026-10-21-auto-reply-text-hid-the-auto-reply`. Swift's `Tests/Fixtures/vacation.json` pins the old
  reading; regenerate it.
  **Applied:** GamGUI failure log 2026-11-04. Nothing after `Message:` sets a field; only GAM's config banner
  ends the message. Offline only. Regenerating Swift's fixture belongs to its one PR, under "Keep Swift's
  fixtures honest".
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

  **Applied:** GamGUI failure log 2026-11-04, "The mock's reads and writes". 26 tests went red, all fixture
  addresses or the mock's own words; no product bug. Offline only. `tests/test_mock_gam_shapes.py` holds it.
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

  **Applied:** GamGUI failure log 2026-11-04, "A write could cut off the admin". Asked in `_run_write`, so every caller
  is held. Offline only here; the read is live-confirmed in SwiftGamGui.
- **A7. The usage report's "as of" date** names the day asked for, not the day GAM's rows are from (Swift
  `audit-reports.md`, "Deviations from GamGUI").

  **Applied:** GamGUI failure log 2026-11-04. The label is the rows' own `date`. Offline only.
- **A8. Audit export ignores the screen's search and filter.** The route takes both, but the page's link
  drops them (same section).

  **Applied:** GamGUI failure log 2026-11-04. The link is in the rows partial, with both.
- **A9. A token write-back can undo a re-import.** Swift's `directory-home.md`, "Fixed in PR #8's review": a
  load's refreshed `oauth2.txt` replaced a token re-imported mid-call. GamGUI has the same shape.
  `_write_back_refreshed_token` (`core/secrets/ephemeral.py:313`) compares GAM's file with what it
  materialized, never with what the vault holds now, then sets the vault. Fix it with a compare-and-swap:
  write back only while the vault still holds the materialized token. Reproduce it first.

  **Applied:** GamGUI failure log 2026-11-04. `SecretsVault.replace_if_current`. Offline only.

### Phase B — Reads and small additions

- **B1. Reports: connected apps and AI agents.** Every account's OAuth tokens, as a summary and a list.
  Domain-wide, so cached and bounded. Swift: `GamEngine/ConnectedApps.swift`, `App/ConnectedAppsView.swift`,
  `audit-reports.md`. The on-device model's wording is not ported.

  **Applied:** `core/connected_apps.py`, Reports' panel, the read as a background job kept in memory. Not yet live in
  either app; `scripts/acceptance.py` runs it. The CSV's shape and the default entity were read in GAM's source at 7.50.00.
- **B2. Reports: drill-downs.** Each directory count opens the people behind it (`DirectoryReport.swift`), and
  storage and mail usage get their list (`UsageReport.swift`, compared against `/reports/usage`).
  **Nothing to port (corrected 2026-11-05):** GamGUI already has both, with the same nine reports and the top 25. Swift's
  came from here (`reports.json`, `usage_report.json` and its runbook say so), and it adds only native table UI.
- **B3. Jobs tray: mostly here already.** GamGUI's tray (plan U5, `web/routes/jobs.py`) already lists every
  background job from any page, with Stop, and Swift's bounds came from it. Swift's only addition is listing
  one-step changes too. Low value: drop it unless asked. (Corrected 2026-10-30. The first draft called the
  tray Swift-only.) **Yes (2026-11-05):** a one-step change lists there too.

  **Applied:** `GAMConnector.on_write` tells `web/jobs.py`'s `watch_writes`, which lists a write made outside any job.
- **B4. Users: Security reads.** A person's Chrome profiles, mobile devices and, after G5, computers. Swift
  `compromised-account.md` slices 5 and 8. Reads only.

  **Applied:** a Security tab on a person's page, `core/person_reads.py`. The devices scope is offered in Setup's delegation
  step but not required by its verify, so no install has to re-authorize unless it wants that list. Not yet live in either app.
  The phones read adds `listlimit 0`: Swift's `printMobileDevices` lacks it, so its list can miss a shared device's second owner.

### Phase C — Small writes

- **C1. Temporary password.** Changed at next sign-in, masked in the preview and the audit, and shown once.
  New to both apps, so verify live on a throwaway user. It is in A6's set. Swift: `GamCommands.setPassword`,
  `onboarding.md` D4. Adversarially verified.

  **Applied:** the Security tab. Offline only: the live check on a throwaway user is still owed.
- **C2. The phone a signature shows.** GAM replaces the whole list, so write back every other phone unchanged
  and re-read the phones just before the run; one added since means "preview again". Setting is confirmed live
  in Swift (2026-10-24); changing or removing is not yet. Swift: `GamCommands.updatePhones`,
  `directory-home.md`, "Users writes".

  **Applied:** the person's Overview, `core/phones.py`. Offline only here. Setting is argv-identical to Swift's live run.
- **C3. Offboarding's added steps, off by default:**
  - clear recovery email and phone, hide from the directory, IMAP off, POP off: confirmed live in Swift
    (2026-10-16), so argv-identical here
  - hand over secondary calendars, remove filters that forward: not yet live in either app

  This closes most of `2026-10-02-configurable-offboarding.md` P2 (GAL off, `popimap`, recovery); mark it
  there. Check that eight steps plus the added ones reach the guard's typed `confirm` (≥10 targets with a
  destructive step), as Swift's `offboarding.md`, "Typed confirm", found.

  **Applied:** `lifecycle.ADDABLE_STEPS`, with the secondary-calendar and Vault-hold warnings. Offline only here.
- **C4. Typed address for a destructive step.** GamGUI already asks for the exact address on every account
  delete (`core/guard.py`), and for the word or the count on a large destructive change. Swift also asks for
  it on its other destructive single-person changes. Extend the guard's rule to the incident lock (E1) when E1
  lands. Suspend stays a Confirm click unless the operator asks for more.
- **C5. Offboarding templates.** Only if G3 says yes.

  **Applied:** `core/offboard_templates.py`. The form still opens as all eight steps until the operator picks a default.

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

  **Applied, D1-D3 together:** `core/phishing.py`, the Phishing screen, `docs/domains/phishing.md`. Offline only here.

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

- **A1** — applied (#67, `add6308`: the parser's escape character, the mock's escaped `print users`, the
  build's dialect check).
- **A2** — applied (#68, `9c1bb72`: every progress printer skipped, the build's progress words held, the
  mock's progress on stderr).
- **A3** — applied (#69, `8d14022`: the command echo and usage help lines skipped, the
  build's framing words held, the mock's usage error and transfer framed as GAM frames them).
- **A4** — applied (#75, `d1bee56`: nothing after `Message:` sets a field).
- **A5** — applied (#76, `7f8cf18`: every `formatjson` read prints GAM's key columns; the listed writes fail
  as GAM does).
- **A6** — applied (#77, `a85f435`: `cut_off_refusal` in `_run_write`, by account ID, an unconfirmed admin
  refusing).
- **A7** — applied (#78, `3525a63`: the label is the rows' own `date`).
- **A8** — applied (#79, `ec6ea27`: the export link carries the screen's search and filter).
- **A9** — applied (#80, `ae5e050`: `replace_if_current`, under a lock that covers one process).
- Swift's fixtures — not done: one Swift PR (see "Keep Swift's fixtures honest").
- **B1** — applied (`feat/connected-apps-report`: Reports' connected apps and agents, offline only).
- **B2** — nothing to port: GamGUI already has it.
- **B3** — applied (`feat/jobs-tray-one-step-changes`: one-step changes in the tray, offline only).
- **B4** — applied (`feat/person-security-reads`: Chrome profiles, phones and computers, offline only).
- **C1** — applied (`feat/temporary-password`: offline only, the live check on a throwaway user owed).
- **C2** — applied (`feat/signature-phone`: offline only, setting argv-identical to Swift's live run).
- **C3** — applied (`feat/offboarding-added-steps`: offline only, four steps argv-identical to Swift's live runs).
- **C4** — the typed address on the incident lock lands with E1; a long offboarding's typed word landed with C3.
- **C5** — applied (`feat/offboarding-templates`: local files only).
- **D1–D3** — applied (`feat/phishing`: offline only; trash by query argv-identical to Swift's live runs, Spam not live in either app).
- **E1** — applied (`feat/incident-check`: offline only), with C4's typed address. The check's devices, scripts and connected apps land with E7.
- **E2** — applied (`feat/incident-logs`: offline only, reads). Saved to disk with E4's kept incident, which is where a pull has a place to live.
- **E3** — applied (`feat/incident-reader`: pure, with Swift's tests ported; the on-device model's are Apple-only and stay out).
- **E4** — applied (`feat/incident-kept`: reads and this Mac only; adversarially verified). E2's "saved as 0600" is here. An export comes in as Export all's zip, since a page has no folder picker.
- **E5** — applied (`feat/incident-respond`: reads and this Mac only). The screens are Incidents, Investigate and Respond; Report comes with E8.
- **E6** — applied (`feat/incident-reach`: reads and this Mac only). The groups are read in the background on Respond, as SwiftGamGui does.
- **E7** — applied (`feat/incident-devices`: offline only; both writes unproven live in either app).
- **E8** — applied (`feat/incident-report`: reads and this Mac only). G4: a PDF from a small writer with no dependency, in the export zip. The summary is typed: the on-device model's draft is Apple-only.
- **F** — applied (`docs/parity-sweep`). A final check against SwiftGamGui's README at `f4c7c5b` found nothing left but the Apple-only pieces.

## Live checks owed

All of Phases B to E has run offline only. Each write needs the operator's yes for that one action, on a throwaway account.

- **Writes:** a temporary password; changing or removing a signature's phone; handing over secondary calendars and removing forwarding filters at offboarding; trashing a copy in Spam; deleting filters, send-as addresses, drafts and scheduled emails from an account, including whether Gmail honours `in:scheduled` and whether a delete stops the send; blocking a computer, and taking an account off one.
- **Reads, the operator's say:** `scripts/acceptance.py` (last run 2026-10-08); each person's Security tab and Check Account against the Admin console; a real incident's logs, Read the logs, each account's status, Who it reached against a list made by hand, Where it started's sender fields; `RESOURCE_ID` as Google's device ID; a Receive row's actor and IP; a group it went to read by its members; a domain alias in `nonEditableAliases`.
- **On this Mac:** the report's PDF and Markdown read in order; `shasum -a 256` of an evidence file against `evidence-sha256.txt`; an export saved from the app window, through its two steps; an incident open again after a relaunch.
- **Setup:** the computers need the devices scope, and blocking needs its full form, so an existing install re-authorizes the delegation.

## What SwiftGamGui should take back

Found while porting, by review or by reading GAM's source. Each is a gap in its code or its spec, not checked there beyond reading.

- **Done can read finished when it isn't.** A later Find showing a copy doesn't take back "nobody has it" or an earlier run, and a delivery the log shows after the last look doesn't put the email back on the list.
- **A narrow read makes a log read whole.** `merged` clears a log's cap on any uncapped read, including one for a few accounts, and `max(throughs)` lets a narrow read's stand for a wide one's. Here a log stays short until each account it was for is covered.
- **A silent exit 0 on `block deviceuser`, `delete deviceuser` and `print devices`.** GAM builds the service first, and exits 0 with no word on a refreshed token it refuses; and it exits 0 while Google's operation is still going. A block can read as done, and computers as none.
- **The report never says what it rests on**: a capped or unread log and a group not read can give a clean-looking report. It also reads 2-Step Verification as "on" for anyone not in the set, counts failures and notes in Steps taken, and a line break in a subject can forge a heading in the text.
- **A long attack pushes the lock out of What was done**, through the timeline's cap.
- **Do It Here can pick a computer for a phone's to-do item**, and `firstSyncTime` as the fallback makes an old computer look new.
- **Smaller:** a stray row at a read's cap; Read the Rest on Gmail skipping what's past 30 days; `listlimit 0` on `print mobile`; a member with no address and a group listing no one, both dropped without a word; re-running `gen_fixtures.py` and dropping its shims.
