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

## Gotchas / mock-lies traps
- **A shortfall never reads as complete.** GAM prints a class's row even when it's empty, so a missing row, or a `JSON-`
  cell that isn't a list, is `Roster.unreadable`, never an empty class. GAM prints its CSV header even when nothing
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
