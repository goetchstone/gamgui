# Domain: Google Classroom (read only)

**One line:** Reads Google Classroom for school domains (classes, who teaches and who is in each) and, per
[the plan](../plans/2026-11-11-classroom-and-rostering.md), plans roster changes from a student information system that
leave the app as files. **GamGUI never writes to Classroom.**

**Owns invariant(s):** none of its own. It leans on #1 (course IDs and addresses are validated, one argv element each)
and #2 by having no write at all.
**Enforcement home:** `tests/test_classroom_reads.py` (argv, the strict mock, the parse, the admin token's scopes), plus
the registries every read joins: `READS` in `tests/test_mock_gam.py`, `HEADERS` in `tests/test_mock_gam_shapes.py`,
`ENUM_ARGS` in `tests/test_command_contract.py`.

## Why read only

The operator has no Workspace for Education domain and can't check a Classroom write live, and this repo relies on no
write that hasn't run live (CLAUDE.md, "the mock lies"). So the plan's E2 is a decision, not a gap: a Classroom write in
either app needs someone else's Education domain for the live checks and a fresh decision. The plan's last section lists
what that would take. The work started from a community fork's Classroom code (Sykezzz/gamgui), whose writes skipped the
guard; only its read side is adapted here.

## Files
- `gamgui/core/classroom.py`: `Course`, `Roster`, `Participant`; `courses_from`, `roster_from`; `CLASSROOM_SCOPES` and
  `classroom_scopes_missing` (the admin token's Classroom scopes).
- `gamgui/core/gam/commands.py`: `print_courses` (by teacher, student, states, or course IDs) and
  `print_course_participants`; `COURSE_FIELDS`, `COURSE_STATES`, `is_course_id`.
- `gamgui/core/connectors/gam_connector.py`: `list_courses`, `get_course`, `course_roster`.
- `gamgui/web/routes/classroom.py`, `classroom.html`, `_classroom_results.html`, `_classroom_card.html`: the screen
  (C1.3). GET routes only: `test_the_screen_posts_nothing` reads the flattened OpenAPI paths and fails on a POST.
- `AppState.courses` / `course_cache` (`web/server.py`): the index, every active and not-yet-accepted class from one
  read without `owneremail`; a tenant switch drops it.
- `tests/fixtures/courses.json`, `tests/fixtures/course_participants.tsv`: four classes around the directory fixture's
  people (Alice teaches, Carol is a student, suspended Bob is in one class).

## How GAM reads Classroom (read from the 7.51.00 build, not seen live)
- **With the admin token.** `doPrintCourses` and `doPrintCourseParticipants` call `buildGAPIObject(CLASSROOM)`, the
  `oauth2.txt` client. The service account is used only with `owneraccess`, which GamGUI never sends. So the scopes are
  GAM's client-access "Classroom API - Courses", "- Rosters" and "- Profile Emails", picked in `gam oauth create` (read
  only is enough), not delegation. Setup's delegation step names them (`dwd_details()["classroom_missing"]`), optional
  like the devices scope, and never adds them to the delegation link.
- **Without them:** `ERROR: There are no scopes authorized for the API(s): Classroom API`, exit `NO_SCOPES_FOR_API_RC`
  (15). `errors.py` reads that as `SCOPE_MISSING`, for any API.
- **Output.** `print courses … formatjson` is `id,JSON`, the course's keys sorted. `print course-participants …
  formatjson` is one row per class, `courseId,courseName` then `JSON-teachers` and `JSON-students` cells, each a JSON list
  of people (`userId`, `profile.emailAddress`, `profile.name.fullName`). `parse_records` decodes only a column named
  `JSON`, so `roster_from` decodes the two lists itself.
- **Not found.** A teacher, student or course Classroom doesn't know is a per-entity warning, `<Entity>: <x>, Does not
  exist`, and exit `ENTITY_DOES_NOT_EXIST_RC` (56): `NOT_FOUND`.
- **Never the join code.** `COURSE_FIELDS` leaves out `enrollmentcode`, which lets anyone with it join the class.
- **`owneremail` costs a Directory read per owner.** `print_courses(owner_email=False)` leaves it off; the course index
  (C1.3) does, and names owners from the directory cache by `ownerId`.

## The screen (C1.3)
- **One search box.** Text searches the cached index (name, section, room), paged server-side (`PAGE_SIZE`). An address
  is a live read of that teacher's classes, archived ones too (`TEACHER_STATES`), and only when asked: typing sends
  `live=0` and the box says "Press Enter"; Enter, Find or a pager click reads. A read per typing pause would have been
  a GAM call per keystroke pause.
- **Owners come from the directory as cached** (`UserCache.cached`, never a fetch): a cold cache leaves the owner
  column blank rather than costing a full `print users`. One class's card reads its owner with `owneremail`.
- **The card** reads the class and its roster side by side; a roster that failed or came back short says so above the
  lists, and each side lists at most `ROSTER_SHOWN` with the count past it (#9).
- **The list loads after the page** (`hx-trigger="load"`): a cold index is a domain-wide read, so `/classroom` itself
  makes no `gam` call. A deep link's card (`?course=`) is still read with the page.
- **A failed index read is said again, not re-run,** for `INDEX_RETRY` (60 s; `AppState.course_index_failed`): on a
  domain without Classroom each typing pause once ran a read that could only fail. Try again (Refresh) reads at once,
  and a tenant switch clears it.
- **The open class stays marked** (`aria-current`): `classroom.js` moves it on a click and keeps `#class-selected`,
  which every search and pager request sends as `selected`.
- **Google's `alternateLink`** is linked only when it starts `https://classroom.google.com/`.
- Errors in words (`_why`): a missing scope points at Setup, an address Classroom doesn't know is named, and an
  `Incomplete` says its own sentence.

## Beside the people screens (C1.4–C1.6)
- **`classroom_readable(vault, domain)`** (`core/setup.py`) reads the admin token's recorded scopes: False on a token known
  to lack Classroom's (a domain that doesn't use it), None when they can't be read. Only False skips a read; None tries.
- **A person's classes** (`/classroom/person`): a "Classroom" card on a user's Overview, left off when
  `classroom_readable` is False, reads nothing until **Show their classes** — then what they teach (owned ones marked) and
  what they're in, `PERSON_STATES` (archived too), each linking to its class card. One cause for both reads is said once.
- **Offboarding** (`routes/lifecycle.py` `offboard_preview`, `_offboard_preview.html`): the classes the leaver owns
  (`GAMConnector.courses_owned_by`: every state, since a declined or suspended class is lost too; Classroom not knowing
  them as a teacher, exit 56, is none, not a failure) are named with a note that GamGUI doesn't change Classroom, and **Download the list
  (CSV)** (`/classroom/owned.csv`, `csv_safe`, a `download` link). A failed read is said, never taken as none; a token
  known to lack the scopes reads nothing and says nothing. It's a warning, not a step: no Classroom write exists.
- **Who can create classes** (`/classroom/creators`): the group named "Classroom Teachers" (or `classroom_teachers@…`)
  from the cached group list, linking to Groups; the Admin console's Classroom settings say which group it really is.

## The roster upload (C2.1, C2.3, C2.5; the screen follows)
- **`core/roster.py`** reads an upload into one `Snapshot` (schools, terms, courses, classes, people, enrolments) that
  every SIS reader fills, as a ZIP or as loose CSVs (a folder's files). `read_upload([(name, bytes), …])` is the entry
  point: it tells the export by its file names alone (`detect`, `zip_names`: nothing unpacked) and reads OneRoster 1.1 or
  1.2 itself, Clever with `roster_clever`, and raises `NeedsMap` for one CSV that is neither (C5).
- **Hostile until read** (SECURITY.md): never extracted; a member is matched by base name against `KNOWN`, and a link,
  encrypted member, `..`, absolute name, backslash, NUL, nested folders, an unknown file or a name twice (any case)
  refuses the package. One enclosing folder (Finder's Compress) and macOS's `__MACOSX/`, `.DS_Store` and `._*` are fine.
  Every size is counted while unpacking (`_bounded`), never taken from a header; members, ratio, rows, fields and the total
  are capped (`*_CAP`).
- **What an adversarial run broke, and the rule each fix keeps** (2026-11-11; `tests/test_roster.py`, "found by the
  adversarial run"): `zipfile` walks a central directory's bytes whatever its entry count says, so its size is read from the
  end record first (`CENTRAL_DIRECTORY_CAP`); a constructor error of any type is "not a ZIP"; a row is built from the
  columns used (`USED`) and the header is capped and refused with a name twice, or a wide header made reading quadratic;
  the manifest has a byte cap; a physical line is capped before the csv module splits it (`_lines`); optional files are
  named but never unpacked; an address with a control or invisible character is refused, never cleaned (cleaning could
  make another valid address), while other fields drop theirs (`_clean`); an issue names the line its record starts on.
  `zipfile` cuts a name at a NUL, so a NUL can't be refused, which is safe only because no name is ever a path.
- **Memory.** Each file is decoded as it's read (`_Lines`) and dropped once parsed, which took a crafted 62 MB package
  within every cap from 5.3 GB to 2.4 GB peak. What's left is the snapshot as objects (that package held 1.3M classes and
  1.8M people, far past any district). If that ever matters, write rows straight into SQLite instead of building objects.
- **The csv module's field limit is never set here.** It's process-wide and the GAM parser raises it for other reads
  (`parser.field_limit_at_least`), so lowering it in a thread could break one; each cell's length is checked instead.
- **Package mistakes refuse with their fix** (`PackageError`): not a ZIP, an export it doesn't know, a delta export, a
  missing file,
  the wrong columns, not UTF-8. **Row problems are `Issue`s** counted by kind (`ISSUE_WORDS`: cause and fix) with a sample
  of `ISSUE_SAMPLE` each: a teacher or student without an address, two sharing one, an enrolment naming someone or a class
  that isn't there, rows marked to be deleted.
- **`core/roster_store.py`** keeps one snapshot per domain: SQLite, named by a hash of the domain, `0600` in a `0700`
  folder refused if it's a link, written under `.saving-…` and renamed in. Kept `KEEP_DAYS` (14); the next look past that
  deletes it; `purge` deletes it at once. **Both sweep every half-written `.saving-*` copy of that domain's file**, whichever
  process left it (a quit between commit and rename once left a whole snapshot that Purge never saw). **Expiry fails
  closed** (`_expired`): unreadable dates, an import dated in the future, or a stored expiry past `imported + KEEP_DAYS` all
  delete it. Every failure of a save is a `StoreError`, and its temp file goes.

## More than one SIS (C5.1 to C5.3)
- **Every reader goes through the same bounds.** `Layout` names an export's files and the ones never unpacked; the
  container code (`files_from_zip`, `files_from_csvs`) takes one, and every row is read by `_rows(name, body, required,
  used, fold_case=, layout=, raw=)`, which builds a row from the `used` columns alone. That is what keeps passwords out:
  **Clever's `Username`/`Password`/`Contact_*`, OneRoster 1.2's `userProfiles.csv` (per-app passwords) and `users.csv`
  `password` are never read into anything**; `staff.csv`, `administrators.csv`, `userProfiles.csv` are named, never
  unpacked. Issue words are format-neutral (`ISSUE_WORDS`), since an issue's `file` names the file.
- **Detection by name:** OneRoster's manifest and camelCase files, or Clever's schools, students, teachers and sections;
  `enrollments.csv` is in both and decides nothing; both kinds at once is refused. **One picked CSV is always a spreadsheet
  to map, whatever its name** (neither export is one file, and a SIS report is often called `students.csv`); anything else
  is "isn't an export GamGUI knows", with what to export.
- **OneRoster 1.2** (`oneroster.version` "1.2", or a revision like "1.2.1"): `users.csv` has no `role`, so `roles.csv` is
  required (the 1.2 binding's Appendix A rostering set) and read first (`_roles_12`). A person is counted under their
  primary role's teacher or student, else a secondary one's; aides, guardians and `ext:` roles aren't rostered. An
  enrolment's role must be one of the person's roster roles, else `role_mismatch`. Everything else is 1.1's reading.
- **Clever** (`core/roster_clever.py`, SFTP CSV spec v2.2.0): headers matched ignoring case only. People are
  `student:<id>` and `teacher:<id>` (separate ID spaces). A student repeats once per guardian contact: the first row is
  kept, a different address is a `conflict`. A section's `Teacher_id` is its primary teacher, `Teacher_2_id`..`Teacher_10_id`
  co-teachers; `enrollments.csv` is students only. Courses (`course:<Course_number>`) and terms
  (`term:<name>|<start>|<end>`, MM/DD/YYYY to ISO when strict) are built from section columns. Title: `Name`, else
  `Course_name` with its period. Fixture: `tests/fixtures/roster/clever/` (the Swift port reads the same files).
- **A spreadsheet of any other shape** (`core/roster_mapped.py`, C5.3, the operator chose **one flat file**: a row per
  person in a class). `ColumnMap` maps six fields (class ID, class name, email and role needed; section, primary teacher
  optional) and the role words; `guess(header)` fills it on first sight; `preview` reads three rows and stops;
  `read_mapped` reads the rest (person ID = address; conflicts first-kept; an unrecognized role word is `unknown_role`).
  Maps are saved by name in `roster-maps.json` (`core/roster_maps.py`: column names and words only, `0600`/`0700`, moved
  aside when unreadable).
- **The map step on the screen:** a spreadsheet isn't read on upload. It's held in memory (`AppState.roster_pending`:
  name, bytes, header, domain; never on disk), and `_roster_map.html` shows the columns, the map (a saved one that fits the
  header, else a guess) and three rows read with it. `POST /map/preview` on every change, `/map/read` reads and keeps it
  like an export (audited with which fields were mapped, never the column names: they're the sheet's text), `/map/delete` deletes a saved map, `/map/cancel` drops the file. A
  new upload (read or refused), Purge, a read, a cancel or a tenant switch drops what's held, and one held for another
  domain is never read; a read is kept under the domain it was picked on, or not at all (`_keep`'s `domain`). A saved map
  is loaded only when its own select fired (`HX-Trigger-Name`), since every control posts the whole form.
- **What the adversarial run of C5 found (2026-11-12), and the rule each fix keeps:** an address is checked as given, then
  lowercased, in every reader (U+212A KELVIN SIGN lowercases to an ASCII `k`); the mapped reader cleans every field but the
  address by field, since one column may be mapped as the address and a class; `header_of` refuses a first line holding an
  address (a sheet without its header would save a student's address as a column name) or a column name past
  `HEADER_CELL_CAP`; a 1.2 enrolment of someone with no roster role in roles.csv is a `role_mismatch` naming roles.csv; a
  person who teaches one class and studies in another counts as a teacher; the Issues CSV's ID column is `ID`.

## The import screen (C2.2, C2.4)
- **`web/routes/roster.py`**, `/classroom/import`, the second tab (`_classroom_tabs.html`: links, not ARIA tabs, since each
  is its own page). One file input takes a ZIP or a folder's CSVs; beside it, what to export. `POST /upload` reads the
  picked files bounded together by `UPLOAD_CAP` (`max_files=MEMBERS_CAP`), runs `read_upload` and `RosterStore.save` off
  the event loop, and audits `roster_import` (label, hash, counts, issue counts; never rows). A `PackageError` is said
  under the picker and nothing is kept; the last good roster stays. `POST /purge` deletes it and audits `roster_purge`.
  Both are `LOCAL_ONLY` (no GAM; `tests/test_roster_web.py` checks the argv log) and named in `AUDITED_OUTSIDE_CONNECTOR`.
- **The page reads only metadata and issues** (`RosterStore.issues`), never the people: loading them all to show counts
  cost seconds on a district. Each kind shows `ISSUES_SHOWN` (20) rows; the **Issues CSV** has every row kept (the reader
  keeps `ISSUE_SAMPLE`, 1,000, per kind; the count is whole), formula-safe.
- **Starlette spools each file part to a private temp file before the route sees it**, so `UPLOAD_CAP` is applied after
  the spool, not before: a huge upload costs temp disk until the request ends. Bounding it earlier needs a multipart
  parser of our own; not done.

## Compare with Classroom (C3)
- **`core/roster_plan.py`** is pure: `plan(snapshot, classes, directory, unread)` → `ChangeList` (changes in run order,
  counts, flags, notes, `summary()`). No GAM, nothing written; GamGUI never applies it.
- **GamGUI's classes are the ones carrying its alias**, `d:gg-<sourcedId>` (`alias_for`: the SIS ID when plain, else a
  hash). Only those are compared, updated or archived; a hand-made class is never touched, and is counted as left alone.
  GAM may print an alias with or without its `d:` scope; both are read.
- **Kinds, in the order GAM must run them:** create (owned by the primary teacher, else the first teacher by address; a
  class with no teacher is noted), rename (name or section), add teacher, add student, owner (after the teacher is added),
  remove teacher (never the owner), remove student, archive (a GamGUI class the SIS no longer has, not already archived).
  A class the SIS has that Classroom archived is noted, never planned. With the directory read, someone without a
  Workspace account is noted, not added.
- **What a review proved, and the rule each fix keeps** (failure-log 2026-11-11): GAM prints `JSON-aliases` as objects,
  `[{"alias": "d:gg-C1"}]`, and the mock once printed strings (`_aliases` reads the objects; an item it can't read makes the
  class unread). GAM prints `[]` when an alias read fails, so a SIS class whose name and section match a class without
  GamGUI's alias is never created (noted). The directory is read fresh and only limits who is *added*: someone the SIS lists
  is never removed for it. A teacher ↔ student swap removes before it adds (`Change.swap`: Classroom refuses to add someone
  already in the class). After an owner change the former owner is removed, and if the SIS now has them as a student their
  add waits for that removal (`Change.late`; Classroom won't remove an owner). A class whose owner wasn't read gets no
  teacher removals and no owner change (noted), and a SIS student who stays a teacher there isn't added (noted). The headline never says "already matches" when the list is incomplete or
  has notes.
- **Risk** (`LIMITS`): a removal or archive kind past both a count and a share of what's there now is flagged with why;
  C4 leaves a flagged kind out of the hand-over unless ticked.
- **The read** (`GAMConnector.classroom_state`): two GAM runs whose shapes were read from the build — `print courses states
  active,provisioned,archived aliases … formatjson` (a `JSON-aliases` column after `JSON`) and `print course-participants
  states … show all formatjson` (one row per class). Archived classes are read so a GamGUI class Classroom archived is never
  planned as new. `managed_from` pairs them by course ID; a class whose aliases or roster can't be read whole is listed as
  unread, and any unread class makes the list **incomplete** (said; C4 hands nothing over).
- **The screen:** **Compare with Classroom** on the import page starts a job (`roster_compare`, unstoppable: two reads),
  one at a time, kept in memory per domain *and* per kept roster (a new upload, a purge or a tenant switch drops it). The
  panel shows the summary sentence (destructive parts first), the flags, the notes, a chip per kind, and the list searched
  and paged server-side (`/classroom/import/changes`). Failures are fixed sentences (`routes/classroom._why`).

## Hand it over (C4)
- **`core/roster_handover.py`** is pure: a `ChangeList` → `README.txt`, `review.csv` and a numbered CSV per kind (`STEPS`),
  each with its `gam csv <file> noescapechar true gam …` line. Nothing runs GAM. **The files are run outside GamGUI**: no
  preview, guard or audit log covers them, and the screen and README say so before the download.
- **Run order** is the file numbers: a role swap's removals (01, 02), create (03, with and without a section), rename (04),
  add teachers (05), add students (06), owners (07), remove teachers (08), add as students the former owners removed in 08
  (09), remove students (10), archive (11). Classroom won't remove a class's owner, so an outgoing owner who becomes a
  student is never a swap: the planner marks their student add `late` (step 09). A class is named by its alias
  (`<CourseID> ::= d:<CourseAlias>`), so a class created in 03 is found by later steps.
- **Values reach GAM whole:** each command uses only `~field` as a whole argument (grammar 1430), never `~~field~~`.
  `noescapechar true` because GAM reads its CSVs with a backslash escape by default; the files are plain CSV (quotes doubled,
  no escape, CRLF). **The GAM files keep values exact; only `review.csv` is formula-safe** (`csv_safe`), so the README says
  never to open and save the numbered files in a spreadsheet. A create with no section gets its own file; a rename always
  sends `section`, because GAM reads it with no minimum length and an empty one clears it (else a term dropped in the SIS
  would be renamed again on every compare). The README's lines are `shlex.join`ed: an unquoted `~field` is a home-folder
  lookup in zsh, macOS's shell, and the line fails before GAM starts.
- **What's handed over:** every change but a flagged kind not ticked, and what needs one (`kept_changes`: an add or owner
  change of someone whose removal from that class is held back would fail; the README counts them); an incomplete list hands nothing
  over (`HandoverError`). `GET /classroom/import/handover.zip[?include=<flagged kind>…]` (a `download` link; a non-flagged
  `include` changes nothing); the ZIP's entries are dated with the compare's time, so one compare gives one set of bytes.
  Audited as `roster_handover` (counts and included flagged kinds; never rows).
- **The fixture is the contract with SwiftGamGui:** `tests/fixtures/handover/` holds the bytes for one list of every kind;
  `test_the_files_match_the_fixture_byte_for_byte` fails on any change (regenerate with `HANDOVER_UPDATE=1`, deliberately),
  and `test_each_command_is_one_the_grammar_has` lines each command up with `GamCommands.txt`.
- **Not checked live, and can't be here:** every command in the hand-over (`create course … state active`, `update course
  … owner`, add/remove, archive). The README tells whoever runs them to try a test class first.

## Gotchas / mock-lies traps
- **A shortfall never reads as complete.** GAM prints a class's row even when it's empty, so a missing row, a missing `JSON-`
  column, or a cell that isn't a list, is `Roster.unreadable`, never an empty class. GAM prints its CSV header even when nothing
  matches, so an answer with no table at all raises `Incomplete` (`_classroom_table`), never "no classes"; `get_course`
  raises it too when the class's record is missing, never "no such class". Someone listed without an address sets
  `Roster.emails_hidden`: Classroom leaves addresses out without the "Profile Emails" scope.
- **A bare string isn't a list.** `print_courses` refuses a `str` for `course_ids` or `states` (`TypeError`): iterating
  `"123"` would have asked for three classes.
- **The mock refuses what it doesn't model.** `print courses` without `formatjson`, by ID and by a filter at once, and
  `print course-participants` by teacher or for every class are `not_modelled`, not answered in a guessed shape. Its
  state and field names are the grammar's (`<CourseState>`, `<CourseFieldName>`).
- **An owner GAM can't look up** (a deleted account) with `owneremail`: GAM prints every class, the address as "Unknown user",
  warns "Course: <id>, Owner ID: <id>, Does not exist" and exits 56. `_courses_read` accepts that exit only when every
  reported line is that warning (`errors.only_unknown_owners`); "Unknown user" is never kept as an address. Failure log
  2026-11-11; found by the SwiftGamGui port. The mock models it with `$GAM_MOCK_STATE/ownergone/<owner id>`.
- **Silent `[]` from GAM:** `_getCourseAliasesMembers` swallows notFound and serviceNotAvailable on a class's aliases *and*
  on a roster side, printing `[]` with exit 0. A failed roster side reads as an empty one; nothing in the output tells them
  apart. The roster compare's duplicate-create guard covers the alias case; the roster case is unguarded (not modelled).
- **An address outside GAM's domain** answers exit 73, "Service not applicable", which neither app's mock models.
- `$GAM_MOCK_STATE/noclassroom` models an admin token without Classroom scopes, `classroom_silent` GAM answering nothing
  (exit 0). A states list takes `all` only on its own, as the grammar's `<CourseStateList>` does.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_classroom_reads.py` runs offline. **Never run live, and can't be here:** no
Education domain. The shapes above are the build's, read with `scripts/gam_rc_table.py`'s module reader under the build's
own Python.
