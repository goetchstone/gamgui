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
  every SIS reader fills; this one reads OneRoster 1.1 CSV, as a ZIP or as loose CSVs (a folder's files).
  `read_upload([(name, bytes), …])` is the entry point.
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
- **Package mistakes refuse with their fix** (`PackageError`): not a ZIP, OneRoster 1.2, a delta export, a missing file,
  the wrong columns, not UTF-8. **Row problems are `Issue`s** counted by kind (`ISSUE_WORDS`: cause and fix) with a sample
  of `ISSUE_SAMPLE` each: a teacher or student without an address, two sharing one, an enrolment naming someone or a class
  that isn't there, rows marked to be deleted.
- **`core/roster_store.py`** keeps one snapshot per domain: SQLite, named by a hash of the domain, `0600` in a `0700`
  folder refused if it's a link, written under `.saving-…` and renamed in. Kept `KEEP_DAYS` (14); the next look past that
  deletes it; `purge` deletes it at once. **Both sweep every half-written `.saving-*` copy of that domain's file**, whichever
  process left it (a quit between commit and rename once left a whole snapshot that Purge never saw). **Expiry fails
  closed** (`_expired`): unreadable dates, an import dated in the future, or a stored expiry past `imported + KEEP_DAYS` all
  delete it. Every failure of a save is a `StoreError`, and its temp file goes.

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
- `$GAM_MOCK_STATE/noclassroom` models an admin token without Classroom scopes, `classroom_silent` GAM answering nothing
  (exit 0). A states list takes `all` only on its own, as the grammar's `<CourseStateList>` does.

## Testing / live-verification status
`.venv/bin/python -m pytest -q tests/test_classroom_reads.py` runs offline. **Never run live, and can't be here:** no
Education domain. The shapes above are the build's, read with `scripts/gam_rc_table.py`'s module reader under the build's
own Python.
