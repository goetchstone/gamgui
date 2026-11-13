# Plan: Google Classroom and SIS rostering, in both apps — reads and planning only

**Status: IN PROGRESS — Phases C1 to C5.3 built in GamGUI (read only, merged by #115); C5.4 later; the Swift port under way (briefed 2026-11-13; C1.1 to C1.3 in SwiftGamGui#158).** Written at GamGUI `0a4dd66` for a reader with no session
context. `CLAUDE.md` is auto-loaded. Before any item, read that app's runbook for the area (`docs/domains/` in each repo).
When a PR closes an item, mark it under "Status by item" in the same PR.

## The goal

The operator, 2026-11-11, after reading a community fork (github.com/Sykezzz/gamgui, branch `district-main`): bring over its
Google Classroom administration and its roster importer, not its Windows port, make the importer work with more than one
student information system (SIS), make it better for the operator than the fork's version, and do it in SwiftGamGui too.

**Then, the same day: reads and planning only.** The operator has no Workspace for Education tenant and cannot live-check
Classroom, and this repo relies on no write that hasn't run live. So **neither app writes to Classroom.** They read it,
compare it with a SIS export, and hand the operator the change list as files GAM can run, outside the app and said so.
Classroom writes in the app are a later, separate decision, and only once someone with an Education domain can run the
live checks.

This reverses one line of `2026-11-07-finish-the-roadmap.md` D1, which kept Classroom out. That plan says so.

## What the fork has, and what comes over

Checked 2026-11-11 by reading the fork with git only. None of its code was run. The fork is MIT, like this repo; credit
"the Sykezzz/gamgui fork" in each PR that adapts its code.

- **Size.** About 25,000 lines of app code plus 19,000 of tests, across `core/classroom`, `core/classroom_access`,
  `components/oneroster` and three route modules (`oneroster.py` alone is 3,094 lines).
- **It split off on 2026-07-25** (`564a8f5`). Since then it has 209 commits and we have 310. The shared files it needs
  changed by thousands of lines on both sides, and its Classroom commits are tangled with its Windows and updater work.
  A cherry-pick is not viable: new files are adapted, and shared-file changes are rewritten on main.
- **Why its write side stays behind,** beyond the missing live proof: no Classroom or importer route calls the guard or
  uses a single-use preview, and several write on a bare POST. Its import runs a GAM batch file outside `_run_write` and
  audits one redacted record per 50 changes. A launchd job and a 30-second loop write unattended. Its mock has no
  Classroom handler, so every Classroom command "succeeds".
- **Comes over:** the Classroom read builders (`print courses`, `info course`, `print course-participants`,
  grammar-correct at 7.51.00), the course index, the upload hardening in `ingest.py`, the SIS snapshot model, the planner's
  diff of a snapshot against live Classroom, and the threshold logic, which now labels risk in a change list rather than
  gating a run.
- **Left behind:** every Classroom write and its connector methods, the component/profile installer, the updater, canary
  and activation leases, Windows ACLs, the scheduler and armed gate, the batch-file runner, the hard-coded
  `America/Chicago`, the dashboard and tour, the Monitoring and Recovery pages, and the class-name template language.

## Decide first (the operator)

- **E1. Decided: no Education tenant, no live checks.** Everything is built and tested offline. The reads are safe to ship
  that way: a read can't change a domain, and the catalog already runs 515 reads it inferred. The mock handlers (C1.2) still
  have to answer as GAM answers, held to the grammar and GAM's source at the pin, because a wrong parse shows a wrong class
  list or a wrong change list. The screens say once, plainly, that Classroom support hasn't been checked on a real domain.
- **E2. Decided: the app never writes to Classroom.** The change list leaves as files (C4.3). Nothing here adds a POST that
  mutates the tenant. Upload, compare and export are POSTs, so each goes in `tests/test_write_routes_guarded.py`'s
  `LOCAL_ONLY` or `PREVIEWS` set with its reason.
- **E3. Which SIS exports.** Default order: OneRoster 1.1 CSV, then OneRoster 1.2 CSV, then Clever's CSV, then any
  spreadsheet with a column map. Pulling from a SIS's API (OneRoster REST, Clever, ClassLink) is later: it adds a SIS
  credential, which goes in the Keychain under invariant 4.
- **E4. The district's timezone.** Default: the Mac's, shown on the screen, changeable per import.

## How each item runs

As in the finish-the-roadmap plan: **one feature, two PRs, GamGUI first.** GamGUI gets the builder from the grammar, a mock
handler that answers as GAM does, tests, the screen, the runbook and a README row. Then the Swift session ports it,
regenerating its fixtures from GamGUI at the merge commit, porting the mock handler and test cases, then the SwiftUI screen.

**Rules every item keeps:** the invariants in both `CLAUDE.md`s, argv-only reads included. Polled feeds keep the ~12-row
window. Uploads get adversarial verification (file uploads and path handling are on `CLAUDE.md`'s list). Each Classroom
GAM line, read or exported, is checked against `GamCommands.txt` at the pin.

## Phase C1: read Classroom (both apps)

- **C1.1 Scopes in Setup.** Add the read-only Classroom scopes to Setup's check, optional like `DEVICES_SCOPE`, so no install
  is forced to re-authorize. Check which token each command uses (GAM's own scope list) rather than assuming delegation.
  Each scope carries a plain reason, "See classes and their rosters".
- **C1.2 Mock handlers first.** `print courses` (3328), `info course` (3320), `print course-participants` (3339): JSON only
  where GAM gives it, an unknown course ID refused as GAM refuses it, `teacher <email>` and `states` filtering honoured.
- **C1.3 A Classroom screen with two tabs, Classes and Plan an import.** One nav entry. Classes: one search box that takes a
  class name or a teacher's email. An email runs `print courses teacher <email> states active,provisioned owneremail`; text
  searches a cached index. Paginated, capped, server-side. A class card shows owner, teachers, students, state.
- **C1.4 "Classes" on a person's page.** Loaded when asked, each linking to its class card. Answers "what is this student in?"
- **C1.5 Offboarding: a warning, not a step.** When the person owns classes, the offboarding preview lists them with a
  line saying they need a new owner before the account is deleted, and a CSV of them (C4.3's format) to hand to whoever
  can run it.
- **C1.6 Who can create classes.** A short read-only panel: the Classroom Teachers group and its member count, linking to
  the Groups screen, where adding a staff group is an existing, proven write. Replaces the fork's reconciler.

## Phase C2: import a roster, read side (both apps)

- **C2.1 Upload.** Port `ingest.py`'s hardening: bounded copy to a `0600` file in a `0700` dir, at most 32 members, size and
  compression-ratio caps, no extraction, no symlinks, traversal, encrypted or nested members, strict UTF-8 with the BOM
  stripped, bounded lines, fields and rows. One control takes a ZIP or a folder (Finder's Compress nests a folder).
- **C2.2 Say what to export.** Beside the picker: which format, which files it must hold, where SIS products usually keep
  that export. A `docs/guide.md` section, "Plan a Classroom roster import from your SIS".
- **C2.3 Reject a wrong file at the form.** Not a ZIP, the wrong version, a nested folder, a missing file: each answered
  under the picker with its fix. Only a package that parses is kept.
- **C2.4 Issues the SIS team can fix.** Grouped by kind with a count, a plain cause and a "fix it in your SIS" line, a capped
  sample with file and row, and an Issues CSV download.
- **C2.5 The snapshot store.** The SIS data normalised into one model every reader fills: schools, terms, courses, classes,
  people, enrolments. On disk it is student data: `0700`/`0600` in the data dir, kept for a stated number of days, purged on
  a button, and named in `SECURITY.md`. Not the Directory: the user cache stays in memory.

## Phase C3: compare with Classroom (both apps)

- **C3.1 One change list.** A read of live Classroom against the snapshot, grouped into sections: classes to create or
  update, teachers and students to add or remove, owners to change, classes to archive. Search by class, teacher or student,
  filter by section with counts, paginated, inside 1100x760.
- **C3.2 The size in one sentence,** destructive parts first: "Would archive 41 classes and remove 1,203 students; would
  create 340 classes and add 9,800 students."
- **C3.3 Risk labels with defaults.** Removals or archives beyond a built-in count or share are flagged, with the reason in
  a sentence ("would remove 1,203 students; more than 5% of the roster"). Flagged sections are left out of the export unless
  ticked. No nine-row threshold table, no blackout form.
- **C3.4 Plain words.** Change list, not manifest. "Changed in Classroom since you compared" when a re-read differs.
  "Skipped rows", not quarantine. IDs and hashes under "Details".

## Phase C4: hand the change list over (both apps)

- **C4.1 A review CSV** for people: one row per change with class, person, action, current value, new value and why.
  **Formula prefixes (`= + - @`) are neutralised**, a gap in the fork, because this file is meant to be opened in a
  spreadsheet. Downloads go through `tests/test_app_window_downloads.py`.
- **C4.2 GAM files,** one CSV per kind of change, with exact values (no neutralising, so the file says not to open and
  re-save it in a spreadsheet), plus a short text file giving the `gam csv … gam course ~… add students ~…` line for each.
  Never a batch file: `gam csv` (1417) replaces an argument that is exactly `~field` with that field's value (1430), so
  each value reaches GAM as one argument and nothing is re-parsed. Write the lines so every substitution is a whole
  argument, never `~~field~~` inside a larger one. Each command's grammar line
  is cited in a test, and the files are held byte-identical between the apps by a fixture.
- **C4.3 The screen says what this is,** before the button: the files run outside GamGUI's preview, guard and audit log,
  and Classroom support hasn't been checked on a real domain. The export itself is an audited event (what was exported,
  how many rows, never the rows), like the sensitive reads.
- **C4.4 Compare again after.** Once the operator (or whoever ran the files) is done, "Compare again" re-reads Classroom and
  shows what's still different. That is the app's check that the files did what they said.

## Phase C5: more than one SIS (both apps)

Each reader is a function from a file to the C2.5 snapshot, with its own fixtures, its own refusals, and nothing after the
snapshot changes.

- **C5.1 OneRoster 1.2 CSV.** The fork refuses it. Same files plus `roles.csv` and changed columns.
- **C5.2 Clever's CSV** (schools, students, teachers, sections, enrollments).
- **C5.3 Any spreadsheet, with a column map.** For a SIS that exports neither: **one flat CSV, a row per person in a class**
  (the operator chose this over one CSV per kind, 2026-11-11: it's what schedule reports and hand-made sheets look like,
  and six fields map in one step). The operator maps columns once, saved by name like offboarding templates. The screen
  shows three mapped rows before anything else runs.
- **C5.4 Later: pulling from an API** (OneRoster REST, Clever, ClassLink). A SIS credential in the Keychain, read-only, and
  its own short design first.

## Swift notes

The Swift session decides its own mechanics; these are the differences already visible.

- **ZIP reading.** Foundation has no ZIP reader. Whatever reads it must keep C2.1's rules: no extraction, member caps,
  ratio caps, no symlinks or nesting. A shelled `ditto` or `unzip` extracts, so it doesn't qualify.
- **The snapshot store** goes in Application Support with data protection, and shares GamGUI's snapshot fixtures so the
  two apps parse a package identically.
- **Picking a file or folder** is a `fileImporter` that accepts both; security-scoped access ends after the copy.
- **The exported files** match GamGUI's byte for byte (C4.2's fixture).

## If Classroom writes are ever wanted

Not in this plan. It would need someone with an Education domain to run each write live on a throwaway class, then the
usual path: builder, ChangePreview, single-use preview token, `guard.enforce()` in the apply route, `_run_write`, one audit
record per change, a BatchJob with Stop and retry. Never the fork's batch file or timer. The candidates, in order: add or
remove one roster member (3293 to 3295), transfer an owner (3235), archive (3253), create (3217), the offboarding handover,
then applying a whole change list.

## Size

About 22 PRs: 1 for this plan, then roughly 10 GamGUI and 10 Swift across C1 to C5, plus fixtures. C1 is useful alone.
After C4 a district can plan an import from OneRoster 1.1 and run it with GAM. After C5.3 almost any SIS works.

## Status by item

| Item | GamGUI | Swift |
|---|---|---|
| E1, E2 | decided: offline, no writes | same |
| E3, E4 | defaults stand | defaults stand |
| C1.1, C1.2 | done (scopes in Setup, reads and mock) | open |
| C1.3 | done (the Classroom screen) | open |
| C1.4 to C1.6 | done (a person's classes, the offboarding warning, who can create classes) | open |
| C2.1 to C2.5 | done (the reader, the store, and the Plan an import screen) | open |
| C3.1 to C3.4 | done (the planner, the Classroom read, the change list; C3.4's "changed since" moves to C4's re-read) | open |
| C4.1 to C4.4 | done (review sheet, GAM files in run order, the screen's statement, Compare again) | open |
| C5.1 to C5.3 | done (OneRoster 1.2 via roles.csv, Clever CSV, one flat spreadsheet with a saved column map) | open |
| C5.4 | later | later |
