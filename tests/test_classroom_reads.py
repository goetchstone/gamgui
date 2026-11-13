"""Google Classroom reads (plan 2026-11-11 C1.1–C1.2): the argv, the mock answering as the vendored build prints, the parse,
and the admin token's Classroom scopes. Read only: GamGUI never writes to Classroom (the plan's E2)."""
from __future__ import annotations

import pytest

from gamgui.core.classroom import CLASSROOM_SCOPES, classroom_scopes_missing, roster_from
from gamgui.core.gam.commands import COURSE_FIELDS, GAMCommands as C, is_course_id
from gamgui.core.gam.errors import GAMError, GAMErrorKind, classify_stderr
from gamgui.core.phishing import Incomplete

CHEM = "500000000002"


# --- argv ---------------------------------------------------------------------------------------
def test_print_courses_by_teacher_and_states_is_the_grammars_shape():
    assert C.print_courses(teacher="ann@example.com", states=["active", "provisioned"]) == [
        "print", "courses", "teacher", "ann@example.com", "states", "active,provisioned",
        "owneremail", "fields", COURSE_FIELDS, "formatjson"]


def test_print_courses_by_id_and_the_roster_read():
    assert C.print_courses(course_ids=[CHEM]) == ["print", "courses", "course", CHEM, "owneremail", "fields",
                                                  COURSE_FIELDS, "formatjson"]
    assert C.print_course_participants(CHEM) == ["print", "course-participants", "course", CHEM, "show", "all",
                                                 "formatjson"]


def test_a_class_is_never_read_with_its_join_code():
    assert "enrollmentcode" not in COURSE_FIELDS


@pytest.mark.parametrize("bad", ["", "1,2", "d:alias", "12 34", "-1", "１２", "1" * 31])
def test_a_course_id_is_digits_only(bad):
    assert not is_course_id(bad)
    with pytest.raises(ValueError):
        C.print_course_participants(bad)
    with pytest.raises(ValueError):
        C.print_courses(course_ids=[bad])


@pytest.mark.parametrize("bad", ["a@x.com,b@x.com", "a@x.com b@x.com", "Ann <a@x.com>", "id:123", "ann"])
def test_a_teacher_or_student_is_one_address(bad):
    with pytest.raises(ValueError):
        C.print_courses(teacher=bad)
    with pytest.raises(ValueError):
        C.print_courses(student=bad)


def test_a_state_is_one_of_the_grammars_and_an_id_read_takes_no_filter():
    with pytest.raises(ValueError):
        C.print_courses(states=["active", "all,archived"])
    with pytest.raises(ValueError):
        C.print_courses(course_ids=[CHEM], teacher="ann@example.com")


# --- through the strict mock --------------------------------------------------------------------
async def test_a_teachers_classes_in_the_states_asked(connector):
    courses = await connector.list_courses(teacher="alice@example.com", states=["active", "provisioned"])
    assert [(c.name, c.state) for c in courses] == [
        ("Algebra", "PROVISIONED"), ("Biology", "ACTIVE"), ('Chemistry, "Honors"', "ACTIVE")]
    assert {c.owner_email for c in courses} == {"alice@example.com"}


async def test_a_students_classes_include_an_archived_one_when_no_state_is_asked(connector):
    names = [c.name for c in await connector.list_courses(student="carol@example.com")]
    assert names == ["Biology", 'Chemistry, "Honors"', "World History 2025"]


async def test_one_class_by_id_reads_its_quoted_name_back(connector):
    course = await connector.get_course(CHEM)
    assert course is not None
    assert (course.name, course.section, course.room, course.state_words) == (
        'Chemistry, "Honors"', "Period 4", "C3", "Active")


async def test_a_class_or_teacher_classroom_does_not_know_is_not_found(connector):
    for read in (lambda: connector.get_course("999999"), lambda: connector.course_roster("999999"),
                 lambda: connector.list_courses(teacher="nobody@example.com")):
        with pytest.raises(GAMError) as err:
            await read()
        assert err.value.kind is GAMErrorKind.NOT_FOUND
        assert err.value.exit_code == 56


async def test_an_admin_token_without_classroom_scopes_says_so(connector, gam_state):
    (gam_state / "noclassroom").touch()
    with pytest.raises(GAMError) as err:
        await connector.list_courses(teacher="alice@example.com")
    assert err.value.kind is GAMErrorKind.SCOPE_MISSING
    assert err.value.exit_code == 15


async def test_a_roster_has_its_teachers_and_students(connector):
    roster = await connector.course_roster(CHEM)
    assert roster.course_name == 'Chemistry, "Honors"'
    assert [(p.email, p.name) for p in roster.teachers] == [("alice@example.com", "Alice Anders")]
    assert [p.email for p in roster.students] == ["bob@example.com", "carol@example.com"]
    assert not roster.unreadable


async def test_an_empty_class_reads_as_empty_not_unreadable(connector):
    roster = await connector.course_roster("500000000003")
    assert roster.students == [] and len(roster.teachers) == 1 and not roster.unreadable


async def test_gam_answering_nothing_is_never_no_classes(connector, gam_state):
    (gam_state / "classroom_silent").touch()
    for read in (lambda: connector.list_courses(teacher="alice@example.com"), lambda: connector.get_course(CHEM),
                 lambda: connector.course_roster(CHEM)):
        with pytest.raises(Incomplete):
            await read()


async def test_the_mock_never_answers_a_short_course_id_with_a_longer_ones_roster(connector):
    # "1" once matched "500000000001\t" and came back as Biology's roster: GAM says the class doesn't exist.
    with pytest.raises(GAMError) as err:
        await connector.course_roster("1")
    assert err.value.exit_code == 56


@pytest.mark.hand_built_argv   # on purpose: argv no builder sends, to hold the mock to the grammar's refusal
@pytest.mark.parametrize("states", ["all,active", "active,bogus"])
async def test_the_mock_takes_all_only_on_its_own_as_the_grammar_does(runner, domain, states):
    argv = ["print", "courses", "states", states, "formatjson"]
    with pytest.raises(GAMError) as err:
        await runner.run_authenticated(domain, argv)
    assert err.value.exit_code == 2      # GAM's usage error
    assert await runner.run_authenticated(domain, ["print", "courses", "states", "all", "formatjson"])


async def test_without_owner_email_gam_names_no_owner_address(connector):
    assert "owneremail" not in C.print_courses(owner_email=False)
    courses = await connector.list_courses(teacher="alice@example.com", owner_email=False)
    assert courses and {c.owner_email for c in courses} == {""} and {c.owner_id for c in courses} == {"100000000000000000001"}


def test_a_missing_roster_column_is_unreadable_not_an_empty_side():
    roster = roster_from([{"courseId": "1", "courseName": "X", "JSON-teachers": "[]"}], "1")
    assert roster.students == [] and roster.unreadable == ["JSON-students"]


def test_a_bare_string_is_never_split_into_one_class_a_character():
    with pytest.raises(TypeError):
        C.print_courses(course_ids="123")
    with pytest.raises(TypeError):
        C.print_courses(states="active")


# --- the parse never reads a shortfall as complete ----------------------------------------------
def test_a_person_listed_without_an_address_says_addresses_are_hidden():
    cell = '[{"userId": "9", "profile": {"id": "9", "name": {"fullName": "No Mail"}}}]'
    roster = roster_from([{"courseId": "1", "courseName": "X", "JSON-teachers": cell, "JSON-students": "[]"}], "1")
    assert roster.emails_hidden and roster.teachers[0].name == "No Mail"
    assert not roster_from([{"courseId": "1", "courseName": "X", "JSON-teachers": "[]", "JSON-students": "[]"}],
                           "1").emails_hidden


def test_a_roster_cell_that_isnt_a_list_is_unreadable_not_empty():
    roster = roster_from([{"courseId": "1", "courseName": "X", "JSON-teachers": "[]", "JSON-students": "{oops"}], "1")
    assert roster.unreadable == ["JSON-students"]


def test_a_missing_row_is_unreadable_not_an_empty_class():
    assert roster_from([], "1").unreadable == ["the class's row"]


# --- the admin token's scopes -------------------------------------------------------------------
def test_the_classroom_scopes_are_gams_client_scopes_and_read_only_will_do():
    full = [scope for scope, _, _ in CLASSROOM_SCOPES]
    assert classroom_scopes_missing(full) == []
    assert classroom_scopes_missing([f"{s}.readonly" if "profile" not in s else s for s in full]) == []
    assert classroom_scopes_missing([]) == [name for _, name, _ in CLASSROOM_SCOPES]
    assert classroom_scopes_missing(None) is None


def test_gams_no_scopes_message_is_a_missing_scope():
    assert classify_stderr("ERROR: There are no scopes authorized for the API(s): Classroom API") is GAMErrorKind.SCOPE_MISSING


# --- an owner GAM can't look up (failure log 2026-11-11; found by the SwiftGamGui port's review) ------------------------
ADMIN_ID = "100000000000000000009"     # owns World History 2025 in the fixtures


async def test_a_deleted_owner_never_hides_the_classes(connector, gam_state):
    # GAM prints every class, the owner's address as "Unknown user", warns and exits 56: one owner hid every class.
    (gam_state / "ownergone").mkdir()
    (gam_state / "ownergone" / ADMIN_ID).touch()
    courses = await connector.list_courses(student="carol@example.com")
    assert [c.name for c in courses] == ["Biology", 'Chemistry, "Honors"', "World History 2025"]
    history = courses[-1]
    assert history.owner_email == "" and history.owner_id == ADMIN_ID          # never "unknown user" as an address


async def test_a_deleted_owner_never_reads_as_the_leaver_owning_nothing(connector, gam_state):
    # courses_owned_by read NOT_FOUND as "owns none", and exit 56 classified as NOT_FOUND.
    (gam_state / "ownergone").mkdir()
    (gam_state / "ownergone" / ADMIN_ID).touch()
    assert {c.name for c in await connector.courses_owned_by("alice@example.com")} == {
        "Algebra", "Biology", 'Chemistry, "Honors"'}
    assert (await connector.get_course("500000000004")).owner_id == ADMIN_ID


async def test_unknown_owners_with_no_table_never_read_as_owning_none(connector, monkeypatch):
    # Exit 56 with only unknown-owner warnings means GAM found classes; no table then is a broken read. It raised as a plain
    # GAM error classed NOT_FOUND, which courses_owned_by took as "owns none" at offboarding (found by the SwiftGamGui port).
    from gamgui.core.gam.runner import RunResult

    async def warned_but_no_table(domain, argv, timeout=None):
        return RunResult(stdout="", stderr=f"Course: 500000000004, Owner ID: {ADMIN_ID}, Does not exist\n", returncode=56)

    monkeypatch.setattr(connector.runner, "run_any_exit", warned_but_no_table)
    with pytest.raises(Incomplete):
        await connector.courses_owned_by("alice@example.com")
    with pytest.raises(Incomplete):
        await connector.list_courses(teacher="alice@example.com")


def test_only_an_unknown_owner_warning_lets_the_table_stand():
    from gamgui.core.gam.errors import only_unknown_owners
    warn = "Course: 500000000004, Owner ID: 100000000000000000009, Does not exist"
    assert only_unknown_owners(warn) and only_unknown_owners(f"Getting all Courses that match query ()\n{warn} (1/2)\n")
    assert not only_unknown_owners("")
    assert not only_unknown_owners(f"{warn}\nTeacher: nobody@example.com, Does not exist")
    assert not only_unknown_owners("Course: 999, Does not exist")
