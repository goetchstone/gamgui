# Plan: Google Classroom and SIS rostering, in both apps

**Status: NOT STARTED, 2026-11-11. Nothing here is built.** Written at GamGUI `0a4dd66` for a reader with no session
context. `CLAUDE.md` is auto-loaded. Before any item, read that app's runbook for the area (`docs/domains/` in each repo).
When a PR closes an item, mark it under "Status by item" in the same PR.

## The goal

The operator, 2026-11-11, after reading a community fork (github.com/Sykezzz/gamgui, branch `district-main`): bring over its
Google Classroom administration and its roster importer, not its Windows port, make the importer work with more than one
student information system (SIS), make it better for the operator than the fork's version, and do it in SwiftGamGui too.

This reverses one line of `2026-11-07-finish-the-roadmap.md` D1, which kept Classroom out. That plan says so.

## What the fork has, and why it is rebuilt rather than merged

Checked 2026-11-11 by reading the fork with git only. None of its code was run. The fork is MIT, like this repo; credit
"the Sykezzz/gamgui fork" in each PR that adapts its code.

- **Size.** About 25,000 lines of app code plus 19,000 of tests, across `core/classroom`, `core/classroom_access`,
  `components/oneroster` and three route modules (`oneroster.py` alone is 3,094 lines).
- **It split off on 2026-07-25** (`564a8f5`). Since then it has 209 commits and we have 310. The shared files it needs
  (`gam_connector.py`, `server.py`, `setup.py`, `commands.py`, `base.html`, `mock_gam.sh`) changed by thousands of lines
  on both sides. A cherry-pick is not viable. Its Classroom commits are also tangled with its Windows and updater work.
- **Its writes break invariant 2.** No Classroom or importer route calls the guard or uses a single-use preview. Create
  class, edit class, add and remove a roster member all write on a bare POST. Teacher access applies a fresh plan, not the
  approved one.
- **Its import is a second write path.** `run_classroom_batch` writes a `gam batch` file and runs it outside `_run_write`.
  It audits one `<private-batch>` record per 50 changes, so the log never says which class changed. It also turns argv into
  text that GAM parses again (invariant 1).
- **It writes unattended.** A launchd job reconciles teacher access on a timer, and a 30-second server loop runs an "armed"
  student import when due. This repo has no unattended write and this plan adds none.
- **Its mock lies.** `mock_gam.sh` has no Classroom handler, so every Classroom command, `gam batch` included, falls to
  "echo ok; exit 0". Its fakes accept any removal and make anyone owner.
- **Nothing is proven live.** The fork's README confirms no Classroom or import write.
- **Worth keeping:** the Classroom argv builders (grammar-correct at 7.51.00), the per-class connector writes (already on
  `_run_write`), the course index and roster diff, the upload hardening in `ingest.py` (bounded, never extracts, rejects
  symlinks, traversal, encrypted and nested members, strict UTF-8), the threshold logic, and the planner's diff of the SIS
  snapshot against live Classroom.
- **Left behind:** the component/profile installer, the updater, canary and activation leases, Windows ACLs, the
  scheduler and armed gate, the `gam batch` path, the hard-coded `America/Chicago`, the dashboard and tour, the separate
  Monitoring and Recovery pages, and the custom class-name template language.

## Decide first (the operator)

Each has a default. Work follows the default unless the operator says otherwise.

- **E1. A Workspace for Education tenant.** Classroom exists only there. **The operator has none and cannot live-check
  Classroom (2026-11-11).** So every item is built and tested offline only: each README row stays "not yet", and the
  screens say plainly that Classroom changes have not been confirmed on a real domain. The mock handlers (C1.2) are the
  main defence, so each must refuse what GAM refuses, checked against the grammar and GAM's source at the pin. The live
  checks below can only be run by someone else who has an Education domain; until then they stay listed, not run.
- **E2. Import speed.** Each change through `_run_write`, as a BatchJob, is the default: per-write audit, Stop and retry
  for free. A district's first import can be tens of thousands of changes. If it measures too slow live, a faster path is
  its own design (every argv held in the preview, audited per line), never the fork's batch file.
- **E3. No unattended writes.** Teacher access is a group the operator manages, and an import runs when the operator
  confirms it. Default: no scheduler, no armed gate.
- **E4. Which SIS exports.** Default order: OneRoster 1.1 CSV, then OneRoster 1.2 CSV, then Clever's CSV, then any
  spreadsheet with a column map. Pulling from a SIS's API (OneRoster REST, Clever, ClassLink) is later: it adds a SIS
  credential, which goes in the Keychain under invariant 4.
- **E5. The district's timezone.** Default: the Mac's, shown on the screen, changeable per import.

## How each item runs

As in the finish-the-roadmap plan: **one feature, two PRs, GamGUI first.** GamGUI gets the builder from the grammar, a mock
handler that fails as GAM fails, tests, the screen, the runbook and a README row "not yet". Then the Swift session ports it,
regenerating its fixtures from GamGUI at the merge commit, porting the mock handler and test cases, then ChangeCore and the
SwiftUI screen. Live, once, in either app; an identical argv carries the proof.

**Rules every item keeps:** the invariants in both `CLAUDE.md`s. Every new POST is in `tests/test_write_routes_guarded.py`.
Polled feeds keep the ~12-row window and a capped failure list. Uploads get adversarial verification (path handling and file
uploads are on the list in `CLAUDE.md`). Each Classroom GAM line is checked against `GamCommands.txt` at the pin.

## Phase C1: read Classroom (both apps)

- **C1.1 Scopes in Setup.** Add the Classroom scopes to Setup's check, optional like `DEVICES_SCOPE` so no install is
  forced to re-authorize: courses, rosters, profile emails. Check which token each command uses (GAM's own scope list) rather
  than assuming delegation. Each scope carries a plain reason, "Create classes and set their rosters".
- **C1.2 Mock handlers first.** `print courses` (3328), `info course` (3320), `print course-participants` (3339): JSON only
  where GAM gives it, an unknown course ID refused as GAM refuses it, `teacher <email>` filtering honoured.
- **C1.3 A Classroom screen with two tabs, Classes and Import.** One nav entry. Classes: one search box that takes a class
  name or a teacher's email. An email runs `print courses teacher <email> states active,provisioned owneremail`; text
  searches a cached index. Paginated, capped, server-side. A class card shows owner, teachers, students, state.
- **C1.4 "Classes" on a person's page.** Loaded when asked, each linking to its class card. Answers "what is this student in?"

## Phase C2: change one class (both apps)

Every write is a GAMCommands builder, a ChangePreview, a single-use preview token, `guard.enforce()` in the apply route and
`_run_write`.

- **C2.1 Add or remove one student or teacher** (3293 to 3295), through the same exact-diff preview as a roster change.
- **C2.2 Transfer a class's owner** (`update course <id> owner <email>`, 3235). The preview shows from and to; the standard
  confirm, not the fork's retyped ID.
- **C2.3 Archive** (`update courses <entity> state archived`, 3253) as a curated Builder command with a count and a cap, so
  year-end cleanup needs no screen. **Create** and **edit details** (3217, 3235) are curated Builder commands too.
- **C2.4 Offboarding: hand over the classes this person owns.** One more addable step in `ADDABLE_STEPS`, off by default,
  to the same recipient as Drive. The preview reads the classes they own and lists them.
- **C2.5 Who can create classes.** A short panel naming the Classroom Teachers group and its count, with "Add a staff group"
  through the Groups screen's existing add. Replaces the fork's reconciler.

## Phase C3: import a roster, read side (both apps)

- **C3.1 Upload.** Port `ingest.py`'s hardening: bounded copy to a `0600` file in a `0700` dir, at most 32 members, size and
  compression-ratio caps, no extraction, no symlinks, traversal, encrypted or nested members, strict UTF-8 with the BOM
  stripped, bounded lines, fields and rows. One control takes a ZIP or a folder (Finder's Compress nests a folder).
- **C3.2 Say what to export.** Beside the picker: which format, which files it must hold, where SIS products usually keep
  that export. A `docs/guide.md` section, "Import Classroom rosters from your SIS".
- **C3.3 Reject a wrong file at the form.** Not a ZIP, the wrong version, a nested folder, a missing file: each answered
  under the picker with its fix ("This is OneRoster 1.2: choose 1.1 CSV, or wait for C5.1"). Only a package that parses is kept.
- **C3.4 Issues the SIS team can fix.** Grouped by kind with a count, a plain cause and "fix it in your SIS" line, a capped
  sample with file and row, and an Issues CSV download. **Every CSV this feature writes neutralises formula prefixes**
  (`= + - @`), a gap in the fork. Downloads go through `tests/test_app_window_downloads.py`.
- **C3.5 The snapshot store.** The SIS data normalised into one model every reader fills: schools, terms, courses, classes,
  people, enrolments. On disk it is student data: `0700`/`0600` in the data dir, kept for a stated number of days, purged on
  a button, and named in `SECURITY.md`. Not the Directory: the user cache stays in memory.

## Phase C4: import a roster, the change (both apps)

- **C4.1 One review, not three manifests.** On/off sections, as offboarding is an on/off list: create and update classes,
  teachers, students, owner transfers, archives. Removals and archives are off by default when a limit holds them. Search by
  class, teacher or student, filter by action with counts, paginated, inside 1100x760.
- **C4.2 The blast radius in one sentence, above the button,** destructive parts first: "Archives 41 classes, removes 1,203
  students; creates 340 classes and adds 9,800 students." The confirm types that count, as the guard asks for any large run.
- **C4.3 Limits with defaults.** Hold only removals or archives beyond a built-in count or share. Each hold says why in a
  sentence ("removes 1,203 students; the limit is 500"). Releasing a hold is a reason recorded in the audit log. No
  nine-row threshold table, no blackout form.
- **C4.4 Apply as a BatchJob over `_run_write`.** One audit record per change with its class and person. Stop, "Retry the N
  that failed", a bounded live feed, a failure list with plain reasons. Before the run, re-read Classroom and hold what
  that read showed; the confirm runs exactly the previewed changes.
- **C4.5 What the run leaves.** "Download results (CSV)": one row per change with class, person, action, before, after,
  status and reason. An interrupted run (the app quit) says so on launch and offers to compare again.
- **C4.6 Plain words.** Approved change list, not manifest. "Changed in Classroom since you reviewed", not drift. "Skipped
  rows", not quarantine. "Add only, no removals", not Limited Import. IDs and hashes go under "Details".

## Phase C5: more than one SIS (both apps)

Each reader is a function from a file to the C3.5 snapshot, with its own fixtures, its own refusals, and nothing after
the snapshot changes.

- **C5.1 OneRoster 1.2 CSV.** The fork refuses it. Same files plus `roles.csv` and changed columns.
- **C5.2 Clever's CSV** (schools, students, teachers, sections, enrollments).
- **C5.3 Any spreadsheet, with a column map.** For a SIS that exports neither: one CSV per kind, the operator maps columns
  once, saved by name like offboarding templates. The preview shows three mapped rows before anything else runs.
- **C5.4 Later: pulling from an API** (OneRoster REST, Clever, ClassLink). A SIS credential in the Keychain, read-only, and
  its own short design first.

## Swift notes

The Swift session decides its own mechanics; these are the differences already visible.

- **ZIP reading.** Foundation has no ZIP reader. Whatever reads it must keep C3.1's rules: no extraction, member caps,
  ratio caps, no symlinks or nesting. A shelled `ditto` or `unzip` extracts, so it doesn't qualify.
- **The snapshot store** goes in Application Support with data protection, and shares GamGUI's snapshot fixtures so the
  two apps parse a package identically.
- **Picking a file or folder** is a `fileImporter` that accepts both; security-scoped access ends after the copy.
- **Long imports** survive closing the sheet, as Swift's signature-for-many does (finish-the-roadmap 2.2). Its batch
  mechanism (finish-the-roadmap 2.1) comes first.

## Live checks this adds

Each on a throwaway class in an Education tenant, each write with the operator's yes for that one action.

- **Reads:** the scope check, find by name and by teacher, a class card, a person's classes.
- **Writes:** add and remove one student and one teacher, transfer an owner, archive one class, create one, the offboarding
  step, and a small real import (three classes) end to end, including Stop and retry.
- **E2's measure:** a timed import of a few hundred changes, to decide whether a faster path is needed.

## Size

About 30 PRs: 1 for this plan, then roughly 12 GamGUI and 12 Swift across C1 to C5, plus fixtures. C1 and C2 are useful
alone. After C4 a district can roster from OneRoster 1.1. After C5.3 almost any SIS works.

## Status by item

| Item | GamGUI | Swift |
|---|---|---|
| E1 | no Education tenant, no live checks possible: offline only | same |
| E2 to E5 | defaults stand | defaults stand |
| C1.1 to C1.4 | open | open |
| C2.1 to C2.5 | open | open |
| C3.1 to C3.5 | open | open |
| C4.1 to C4.6 | open | open |
| C5.1 to C5.3 | open | open |
| C5.4 | later | later |
