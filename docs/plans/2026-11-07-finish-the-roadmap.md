# Plan: finish the roadmap, in both apps

**Status: IN PROGRESS — Phase 0 under way (0.1 done); no feature built yet.** Written at GamGUI `6f7d96c` and SwiftGamGui `f4c7c5b`, for a
reader with no session context. `CLAUDE.md` is auto-loaded. Before any item, read that app's runbook for the area
(`docs/domains/` in each repo). When a PR closes half of an item, mark it under "Status by item" in the same PR.

## The goal

The operator, 2026-11-07: finish the roadmap and add all the features to SwiftGamGui too. That is two jobs:

1. **Every open item in `ROADMAP.md`, built in both apps.**
2. **Everything GamGUI has that SwiftGamGui doesn't, built there.** GamGUI reached parity going one way on 2026-11-06
   (`archive/2026-10-30-backport-swiftgamgui.md`). This is the other way. Swift's own rule 11 already calls GamGUI parity a floor.

## Where things stand (checked 2026-11-07)

### The roadmap

| Roadmap item | GamGUI | SwiftGamGui |
|---|---|---|
| Live checks for the parity work | Owed | Mostly its own list, `docs/live-verification.md` |
| Report buckets actionable | Not built: `/reports` has no write | Not built |
| Offboarding options | Steps and templates shipped. No recipient, services, end date, roles or groups | Same. Recipient and roles are "not planned unless asked" |
| Back up signatures | **None, not even for one person** | Put Back for one person. Nothing for many |
| Bulk apply from CSV | Hires only | Nothing |
| Org units | Displays them, can't move anyone | Same |
| Licenses | Builder read only | Same. "Removing the licence" is not planned unless asked |
| Group settings | Membership only | Same |
| **Compromised-account playbook** | **Shipped (#91, #97)** | **Shipped** |
| Delegate and forwarding review | One person at a time | Same. Planned as gaps slice 2 |
| Shared drives | Builder reads only | Same. Planned as gaps slice 1 |
| Grow the buildable writes | 26 curated | Same list, two sent elsewhere (below) |
| Sequencer dataflow; export to `gam batch` | Not built | No sequences at all |
| Native fast path for signatures | Not built | Not built |
| Notarization | Not built | Slice R, waiting on the Developer account |

### Only in GamGUI today

From Swift's README and gaps plan, then checked against GamGUI's routes:

- Many at once: a department for many (`/users/bulk`), a signature for many (`/signatures/apply`), hires from a CSV
  (`/onboarding/bulk`).
- The account delete (`/users/delete/*`). Swift's Builder message names a Close Out screen that doesn't exist yet.
- Builder: sequences with Stop, the 14 sensitive reads, message bodies in Search a Mailbox, Sheet export (`todrive`).
- Calendars: find by name (the index), share with a group, delete a calendar, list and delete events.
- Home's Recent failures card.

Comparing builders by name finds only `todrive_args` missing in Swift. The rest of the gap is screens and flows, not argv.

### Three things this check found

- **GamGUI's parity claim has a hole.** Swift's Put Back on Signatures was never ported. Item 4.1 closes it.
- **The two apps disagree on Close Out.** GamGUI's offboarding plan rules out "a separate finish offboarding screen". Swift's
  plans one, for a reason GamGUI's plan didn't have: the transfer runs 10 to 25 minutes after its command, and groups,
  org unit and suspend can switch off what it needs. See D2.
- **A backup can be walked around.** Swift's Builder refuses `build.set_signature` and `build.delete_user` and sends them
  to their screens (`CuratedCommand.elsewhere`). GamGUI's Builder runs both. Once 4.1 and 2.9 land, it would skip the backup
  and the delete's refusals. GamGUI takes Swift's routing in those PRs.

## Decide first (the operator)

Each has a default. Work follows the default unless the operator says otherwise, so nothing here waits to be asked twice.

- **D1. How literally "all the features".** Default: literally. Swift's "not planned unless asked" entries that GamGUI has
  are now asked: the Calendars set, many at once and sequences. **Sheet export reverses a decision** (Swift design doc §1,
  "`todrive` is a write"), so it's named here. Default: port it through ChangeCore as the write it is, as GamGUI does.
  Things neither app has stay out: scheduled runs, other languages, removing limited Drive access, a plain-language
  finder, auto-update, saved reads. Classroom came in on 2026-11-11 (the operator) and has its own plan,
  `2026-11-11-classroom-and-rostering.md`.
- **D2. Close Out, in both apps.** Default: yes, one section of the Offboard screen, not a new sidebar entry: after the
  transfer finishes, groups, org unit, suspend or archive, licence, aliases, then the delete. This changes
  `2026-10-02-configurable-offboarding.md`'s "Not planned", and that plan says so in 4.5's PR. The offboarding routine itself stays
  one fixed list with no delete in it.
- **D3. What each report bucket can do.** Default, one action each, all previewed:
  - Inactive: suspend.
  - No 2SV, No recovery: email them a reminder.
  - Suspended: remove the licence, once 4.3 lands. Delete stays on Close Out.
  - No title, department, phone, location: export a CSV to fill in and feed 4.6.
  - Administrators: no bulk action, on purpose.
- **D4. When "grow the writes" is finished.** It can't mean all 537 inert commands, since each is curated by hand. Default: the
  closed list in 5.1, then one at a time when asked. The roadmap entry stays as that rule.
- **D5. The Apple Developer account.** Not a design choice, but 5.5 waits on it, as does Swift's name change.

**Not in this plan:** a free-text `gam` box and Windows or Linux builds. `ROADMAP.md` lists them as trade-offs, not backlog, and
says to ask. The second connector came off the roadmap (the operator, 2026-11-07), but its code stays: the operator may connect these systems for their own use, so no item here removes `abapit_connector.py` or the connector layer. Also out, by earlier decisions: offboarding from a CSV, a scheduler, device wipes, HR webhooks and 2SV off.

## How a two-app item runs

**One feature, two PRs, GamGUI first.** Swift's parity fixtures are generated from GamGUI and its mock began as GamGUI's, so
that's the direction the proof already flows.

1. **GamGUI session.** Builder from the grammar and GAM's source at the pin, a strict mock handler, tests, the screen, the
   runbook, a README row reading "not yet". One `gam-command-reviewer` pass on a write. Merge.
2. **Swift session, rooted in that repo.** Regenerate `Tests/Fixtures/*.json` from a clone pinned at the merge. Port the mock
   handler and the test cases, then ChangeCore and the SwiftUI screen, the runbook, a "Not run" line in `live-verification.md`.
3. **Live, once, in either app.** Swift is the one in daily use, so its run is the likely proof. An identical argv carries
   that proof to the other app, and both records say which app ran it.

**The exception:** 4.1, where Swift already has the design, ports the other way.

**Disagreement:** if GAM's source shows GamGUI's reading is wrong, fix GamGUI and regenerate. It isn't frozen now, so a Swift
deviation is only for what the platform forces.

**An item is done when both PRs are merged.** Phases 1 and 2 are Swift only and can run alongside Phase 3's GamGUI half, because
they share no files. Two sessions at once at most, for cost.

## Rules every item keeps

- **The invariants in both `CLAUDE.md`s, unchanged.** Each new write goes builder, preview, guard in the apply route, audited
  run. Each new GamGUI POST is listed in `tests/test_write_routes_guarded.py`. Polled feeds stay bounded.
- **The grammar says what GAM accepts; GAM's source says what it prints and exits.** The line numbers below are the 7.50.00 grammar,
  and that's all that has been checked. Nothing in this plan has been run.
- **Both mocks fail as GAM fails.** They've drifted (2,783 and 3,206 lines). Each new handler goes in both, with the same refusals.
- **A read that fell short says so.** An unknown line is unread, never absent. The last plan's reviews kept finding one shape of bug:
  something reads as finished when it isn't. Adversarial review stays on for anything incident-shaped.
- **New scopes come in one go.** `DWD_SCOPES` holds nothing for licensing, group settings or Alert Center, and which token each read
  uses isn't checked yet (GAM's `glapi`). Find them all in 3.1 and offer them together, not required by Setup's verify, as
  `DEVICES_SCOPE` does, so the operator re-authorizes once and no install is forced to.
- **The full process, at the right cost.** `pre-commit` skill, tests that fail without the change, runbooks, a failure-log entry
  for each `fix:`. Adversarial verification only for credentials, path handling and file uploads. No workflows, no fan-out.
- **No write on a real domain without the operator's yes for that one action**, on a throwaway object.

## Phase 0: clear the ground

- **0.1 GamGUI docs PR.** Start from `origin/main`: local `docs/parity-sweep` is stale, merged as #99.
  - `ROADMAP.md`: drop the compromised-account entry and point at this plan.
  - The offboarding plan: close P1.2, which templates replaced.
  - The two untracked `2026-10-08-native-gamgui*.md`: the back-port plan cites them, so commit them after the private-terms
    check, or drop the citations.
- **0.2 Swift docs and fixtures PR.** Owed since the last plan.
  - Regenerate the fixtures from GamGUI at a pinned commit and drop the shims (the widened `_INSTRUCTION_LINE`).
  - Fix `gen_fixtures.py`'s line calling GamGUI frozen.
  - Point `2026-11-05-filling-the-gaps.md` here. Its slices map as: 1 → 3.1, 2 → 3.2, 3 → 2.9 and 4.5, 4 → 2.1 to 2.3 and 3.5,
    5 → 2.4 and 2.5, R → 5.5.
  - Clear the working tree's `project.pbxproj` diff, which only moves one line.
- **0.3 Live reads (the operator's say).** `scripts/acceptance.py` last ran 2026-10-08. Then the reads under "Live checks owed" in
  the back-port plan (`archive/2026-10-30-backport-swiftgamgui.md`). Also owed, from the closed ten-out-of-ten plan: the
  one-time sheet's native print in the built `.app`, the offboarding reminder with an invitee (`sendupdates all`), and
  the new-account signature and calendar retry (about seven minutes). No code.
- **0.4 GamGUI: the last raw GAM errors** (ten-out-of-ten U9). Three write paths still lead with GAM's raw line: the
  Builder's single Run result (`builder.py`), "Couldn't create the account" on onboarding (`onboarding.py`), and the
  offboarding step log (`lifecycle.py`). Give each the friendly message the other write paths use, GAM's words kept
  below it.

## Phase 1: Swift, the bugs the port found

From the back-port plan's "What SwiftGamGui should take back". GamGUI already behaves. None has been reproduced in Swift, so run the
`post-failure` skill on each: reproduce first, then fix, and drop any that doesn't reproduce. A small PR each.

- **1.1** Done reads finished when it isn't: a later Find showing a copy, a delivery after the last look.
- **1.2** A narrow read makes a log read whole (`merged`, `max(throughs)`).
- **1.3** A silent exit 0 on `block deviceuser`, `delete deviceuser` and `print devices`.
- **1.4** The report never says what it rests on: a capped log, 2SV read as "on" for anyone not in the set, failures counted in Steps
  taken, a line break forging a heading.
- **1.5** A long attack pushes the lock out of What was done; Do It Here picks a computer for a phone's to-do item.
- **1.6** The smaller list: a stray row at a read's cap, Read the Rest past 30 days, `listlimit 0` on `print mobile`, a member with
  no address.

## Phase 2: Swift, everything GamGUI has

Swift PRs against GamGUI's behavior, one each, smallest mechanism first. **At the end of this phase Swift is at the floor.**

- **2.1 A department for many.** Proves the mechanism: many steps in one preview, the typed count, the jobs tray, retry what failed. Take
  GamGUI's stop rules with it (`core/bulk.py`: `stop_reason`, `stop_requested`).
- **2.2 A signature for many** (Swift's S2), with its read-before-write as 4.1's starting point. Long enough that it must survive
  closing the sheet.
- **2.3 Hires from a CSV** (O3). A spreadsheet's CSV isn't in GAM's dialect, so `GamOutput`'s reader isn't the one to reuse. The credentials sheet
  stays in memory only.
- **2.4 An audit record for reads.** Then the 14 sensitive reads run, and message bodies in Search a Mailbox. Records the run, never the output.
- **2.5 Sequences, with Stop.** `MAX_SEQUENCE_STEPS` 25, the typed word at ten with a destructive step, plus what `catalog-builder.md`'s
  "Next" lists.
- **2.6 Calendars.** Find by name (a persisted index, built in the background), share with a group (fan-out as a job), delete a calendar, list
  and delete events. The builders exist, so this is screens and rules. Probably two PRs.
- **2.7 Recent failures on Home.** Read from the audit log: the last 3 failed writes within 30 days, as GamGUI's `web/routes/home.py` does.
- **2.8 Sheet export,** per D1. An audited write through ChangeCore; shows the URL GAM prints.
- **2.9 The delete (Off3).** Refused while a transfer runs, a secondary calendar is still owned or a Vault hold covers the person. GamGUI's
  `/users/delete` takes the same refusals, and `build.delete_user` moves off its Builder: **one small GamGUI PR too.**
- **2.10 A last sweep, the other way.** Walk every GamGUI route against Swift's screens. The last sweep compared one direction only and
  missed Put Back. Swift's README loses its "Still only in GamGUI" line.

## Phase 3: both apps, the "Next" items and the reads

- **3.1 Named reports (reads).** One table naming each report and its catalog command, no new parser: licenses (`print licenses`, 4492), shared drives
  (5112), shared drive permissions (5350), organizers (5125), org units (4607), mobile devices, group memberships (4103), data transfers (3611),
  Google's alerts (1604). A refused scope says which. Settles the scope list. Check each by eye on first run.
- **3.2 Domain-wide mail settings (reads).** `all users print forward enabledonly` (8014) and `all users print delegates shownames` (7933). Neither
  takes `formatjson`, the old trap. A background job on the `print tokens` path. Each user GAM lists is accounted for. The screen says it doesn't cover
  filters that forward, because `forward off` doesn't stop those either. Each row opens the person.
- **3.3 Report buckets actionable,** per D3. Traps:
  - The buckets come from a cache: re-read each target at preview, and hold what that read showed.
  - Inactive includes people who have never signed in, so a new hire can land there. The preview flags anyone created inside the window.
  - The connected-admin refusal (A6) already holds at the chokepoint. Make sure it reads as "left out", not as a failure.
  - Also here: the Suspended report's reason, once GAM's source shows the field.
- **3.4 Offboarding options** (P1.3 to P1.5). A recipient, checked at preview and run like the manager. Services (Drive, Calendar, Looker Studio). An auto-reply
  end date held as an absolute date. The manager optional when nothing needs one. `release_resources` only once both mocks refuse it without Calendar. The
  golden default stays byte for byte. Templates' file version moves in both apps, and an old file still loads.
- **3.5 Remove several members from a group.** New to both. The same mechanism as 2.1.

## Phase 4: both apps, the "Then" items

Ordered so each is built once: org units and licences come before the Close Out that uses them, and org units before the CSV column.

- **4.1 Signature backup. Swift to GamGUI first, then many, in both.**
  - GamGUI gets Put Back for one person. Then both get it for many: the previous value of each user, kept before the overwrite under a run's record,
    `0600` in the data dir, last N runs.
  - **A failed read means that person isn't overwritten and is listed as failed**, never overwritten without a backup.
  - An empty signature restores as empty. Restore is a preview through the chokepoint, and says when the signature has changed since.
  - Check in GAM's source which `show signature` mode (8320: `compact|format|html|template`) gives back the exact bytes. A mock that echoes what was set would hide a mangled restore.
  - It doubles the GAM calls in a domain-wide run. Say so on the screen.
  - `build.set_signature` moves off GamGUI's Builder.
- **4.2 Org units.**
  - Read the tree (4607) and move people: `update org <ou> add|move <users>` (4585), or the user attribute, whichever GAM's source shows is right. One person from their page, many through 2.1's mechanism.
  - The preview shows from and to, and says that policy follows the OU. Refuses an unknown path as GAM does.
  - Create, rename and delete (4582, 4583, 4588) are curated Builder commands, not a screen.
  - The operator's tenant has no OUs, so the live check creates one first.
- **4.3 Licenses.**
  - Add (8509), remove (8512), swap (8510, including `archive`). Never `sync`: it can remove what the preview didn't name.
  - **The report that pays for this: seats on suspended accounts,** joining 3.1's read to the directory cache.
  - Refuse a removal while a transfer for that person runs. Preview wording about what a removed licence costs comes from Google's docs, cited.
  - A live check needs a spare seat, so check `print licenses countsonly` first.
- **4.4 Group settings.**
  - Read with `info group` (4051), write with `update group <g> <attribute>` (3933, attributes at 3851).
  - KISS: who can post, join and view members, external members, moderation, archive, web posting. Only changed settings go in the argv. The preview shows old and new.
  - Google refuses some combinations, and that message reaches the screen as its own.
- **4.5 Close Out,** per D2.
  - After the transfer finishes (read `print datatransfers`): remove from groups (8405, memberships recorded in the preview), admin roles (`print admins` 1584, `delete admin` 1579), org unit, suspend or archive, licence, aliases to the manager, then the delete from 2.9.
  - Each off unless picked, except the delete's own refusals.
  - Check Google's Workspace Updates first: a platform change already made one proven routine lose calendars.
- **4.6 Bulk apply from CSV.**
  - An `email` column plus any of title, department, work phone, org unit. The same reader as hires. Bounded, formula prefixes handled, a BOM and Excel's dialect read.
  - The preview shows each row's current and new value, skips what's unchanged, refuses an unknown person.
  - **A blank cell means leave it alone, never clear it.**
  - GAM replaces the whole phones and organizations lists (the signature phone's trap), so read again at the run and ask for a new preview if anything was added.
  - The preview's "before" downloads as a CSV, which is the undo. In GamGUI that download goes through `tests/test_app_window_downloads.py`.
  - An upload, so adversarially verified.
- **4.7 Shared drives.** Membership on 3.1's inventory: add, change role, remove (`create|add drivefileacl <SharedDriveEntityAdmin>`, 5256). The curated "List a shared drive's files" read. **The orphan check:** drives where the leaver is the only organizer (5125), as a warning on the delete and Close Out, offering to add an organizer.
- **4.8 Moving a folder of mixed ownership into a shared drive.** A Builder recipe as a sequence, so Swift needs 2.5. A read counts what will move and what must be copied, then `move drivefile … retainsourcefolders duplicatefolders merge` (7169) and `copy drivefile … recursive copysubfilesownedby others duplicatefolders merge` (7119). GAM has no dry run and a partial run can't be undone, so the preview says both. Live on a throwaway folder holding a file from outside the domain.

## Phase 5: both apps, the "Later" items

- **5.1 The writes, a closed list (D4).** One `add-builder-command` run each: rename a user, delete a group, add an admin role, create and rename a shared drive. Org units and the rest come from Phase 4. Anything further is one at a time.
- **5.2 Sequencer dataflow.** **This runs into invariant 2**: a confirm runs what its preview held, and a later step's argv can't be held if it depends on a write that hasn't run. So only reads bind. Bound reads run at preview, the resolved argv is what's held and shown, each value is one element checked by the slot's own validator, a fan-out counts toward the guard's typed count, and binding to a write's output is refused. Needs its own short design first, in both apps' terms.
- **5.3 Export a sequence to `gam batch` or CSV** (1405, 1417). Quoting has to survive GAM's own parser, so read `doBatch` and test against it. A password is refused, not written. The export is audited, and the screen says the file then runs outside this app's guard and audit. Byte-identical between the apps, held by a fixture.
- **5.4 The GAM-native fast path for signatures.** Opt-in beside the per-user loop. Per-user templates mean `gam csv` with a column (1417), its file in the ephemeral `0700` dir. Requires 4.1's backup first. No live feed and no Stop, and the preview says so. One audit record with the count, and GAM's per-user lines parsed so Retry still works. Don't trust the exit code alone.
- **5.5 Notarization (D5).** Swift first, as its slice R: Developer ID, the bundled GAM signed, a build on Releases, a first run on a clean Mac. GamGUI after, replacing its self-signed certificate. **Test the Keychain before shipping either:** a new team ID may strand stored credentials in Swift's data-protection items, and a new signature changes the prompts in GamGUI. Credentials must be re-importable with a plain explanation.

## Live checks this adds

To the back-port plan's list, each write on a throwaway with the operator's yes for that action. Group them into one sitting a phase, using Swift where it's built.

- **Throwaways to create first (each its own yes):** an org unit, a group, a shared drive, a folder with an outside file, a user holding a spare seat.
- **New to both apps:** move an org unit, add, remove and swap a licence, a group setting, a shared drive member, Close Out's groups and admin roles, the folder recipe, the fast path.
- **New to Swift, proven in GamGUI:** the delete, a bulk department, hires from a CSV, a calendar delete, an event delete.

## Size

About 55 PRs, counting 2 for Phase 0, 6 for Phase 1, 12 for Phase 2, 10 for Phase 3, 16 for Phase 4 and 8 for Phase 5. The parity run (#67 to #99) was 33, from 2026-10-30 to 2026-11-07. Swift's half costs more per item, because it's a new screen rather than a template. A rough count, not a promise.

**Good places to stop, each useful alone:** after Phase 2, the apps match. After 3.2, domain-wide mail is auditable. After 3.3 and 4.3, the worklists act and the seats stop costing. After Phase 4, only speculative work is left.

## Status by item

| Item | GamGUI | Swift |
|---|---|---|
| 0.1 / 0.2 / 0.3 | done (this PR) / – / acceptance done 2026-11-10 (#104); live checks owed | – / open / – |
| 0.4 | open | – |
| 1.1 to 1.6 | – | open |
| 2.1, 2.3 to 2.5, 2.7, 2.8, 2.10 | – | open |
| 2.2 | – | done (SwiftGamGui#161: a group, an OU or the whole company); live check owed |
| 2.6 | – | done but find by name, deferred until asked (SwiftGamGui#161: share with a group, delete a calendar, list and delete events); live checks owed |
| 2.9 | open | open |
| 3.1 to 3.5 | open | open |
| 4.1 to 4.8 | open | open |
| 5.1 to 5.4 | open | open |
| 5.5 | waits on D5 | waits on D5 |
