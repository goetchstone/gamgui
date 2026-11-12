"""Clever's CSV export into the roster snapshot (plan 2026-11-11 C5.2). An upload is hostile until read, and Clever's
files carry passwords and student PII beside the roster, so much of this is what is never read."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from gamgui.core.roster import Class, Course, Enrollment, Issue, PackageError, Person, School, Term, read_upload
from gamgui.core.roster_clever import read_clever

FIXTURE = Path(__file__).parent / "fixtures" / "roster" / "clever"
NAMES = ("schools.csv", "teachers.csv", "students.csv", "sections.csv", "enrollments.csv")

SCHOOLS = [("School_id", "School_name"), ("S1", "North High")]
TEACHERS = [("School_id", "Teacher_id", "Teacher_email", "First_name", "Last_name", "Username", "Password"),
            ("S1", "T1", "ann@example.com", "Ann", "Lee", "alee", "hunter2-teacher"),
            ("S1", "T2", "ben@example.com", "Ben", "Ortiz", "bortiz", "hunter2-teacher")]
STUDENTS = [("School_id", "Student_id", "Student_email", "First_name", "Last_name", "Contact_email", "Username", "Password"),
            ("S1", "P1", "bo@example.com", "Bo", "Ng", "parent@home.example.com", "bng", "hunter2-student")]
SECTIONS = [("School_id", "Section_id", "Teacher_id", "Name", "Course_name", "Course_number", "Period"),
            ("S1", "C1", "T1", "Biology", "Biology", "BIO", "2")]
ENROLLMENTS = [("School_id", "Section_id", "Student_id"), ("S1", "C1", "P1")]


def _csv(rows) -> bytes:
    return "".join(",".join('"' + str(v).replace('"', '""') + '"' for v in row) + "\r\n" for row in rows).encode()


def _files(**override) -> dict:
    files = {"schools.csv": SCHOOLS, "teachers.csv": TEACHERS, "students.csv": STUDENTS, "sections.csv": SECTIONS,
             "enrollments.csv": ENROLLMENTS}
    files.update(override)
    return {name: rows if isinstance(rows, bytes) else _csv(rows) for name, rows in files.items() if rows is not None}


def _fixture() -> dict:
    return {name: (FIXTURE / name).read_bytes() for name in NAMES}


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return out.getvalue()


# --- the fixture, whole (the Swift port reads the same files) --------------------------------------------------------
def test_the_fixture_reads_into_this_whole_snapshot():
    snap = read_clever(_fixture())
    lincoln, roosevelt = "5f2a0c1e9d3b4a0012ab0001", "5f2a0c1e9d3b4a0012ab0002"
    a1, a2, a3 = (f"teacher:6a10000000000000000000a{n}" for n in (1, 2, 3))
    b1, b2, b3 = (f"student:7b20000000000000000000b{n}" for n in (1, 2, 3))
    c1, c2, c3 = (f"8c3000000000000000000c0{n}" for n in (1, 2, 3))
    fall = "term:Fall 2026|08/20/2026|12/18/2026"
    assert snap.source == "Clever CSV"
    assert snap.schools == {lincoln: School(lincoln, "Lincoln High School"),
                            roosevelt: School(roosevelt, "Roosevelt Middle School")}
    assert snap.terms == {fall: Term(fall, "Fall 2026", "term", "2026-08-20", "2026-12-18")}
    assert snap.courses == {"course:BIO101": Course("course:BIO101", "Biology", "BIO101", lincoln),
                            "course:MATH201": Course("course:MATH201", "Algebra I", "MATH201", lincoln)}
    assert snap.classes == {
        c1: Class(c1, "Biology - P2", "course:BIO101", lincoln, (fall,), "BIO-2"),
        c2: Class(c2, "Algebra I - Period 3", "course:MATH201", lincoln, (fall,), "ALG-3"),
        c3: Class(c3, "Art", "", roosevelt, (), "ART-1"),
    }
    assert snap.people == {
        a1: Person(a1, "teacher", "ann.lee@example.edu", "Ann", "Lee"),
        a2: Person(a2, "teacher", "ben.ortiz@example.edu", "Ben", "Ortiz"),
        a3: Person(a3, "teacher", "cara.diaz@example.edu", "Cara", "Diaz"),
        b1: Person(b1, "student", "bo.ng@students.example.edu", "Bo", "Ng"),
        b2: Person(b2, "student", "cy.oh@students.example.edu", "Cy", "Oh"),
        b3: Person(b3, "student", "di.park@students.example.edu", "Di", "Park"),
    }
    assert snap.enrollments == [
        Enrollment(c1, a1, "teacher", True), Enrollment(c1, a2, "teacher", False),
        Enrollment(c2, a2, "teacher", True), Enrollment(c3, a3, "teacher", True),
        Enrollment(c1, b1, "student", False), Enrollment(c1, b2, "student", False),
        Enrollment(c2, b1, "student", False), Enrollment(c2, b3, "student", False),
    ]
    assert snap.issue_counts == {"conflict": 1, "no_email": 1, "unknown_person": 2, "unknown_class": 1, "duplicate_row": 1}
    assert snap.issue_sample == [
        Issue("conflict", "students.csv", 6, "7b20000000000000000000b3",
              "dpark@students.example.edu here, di.park@students.example.edu on its first row"),
        Issue("no_email", "students.csv", 7, "7b20000000000000000000b4"),
        Issue("unknown_person", "sections.csv", 3, c2, "6a10000000000000000000ff"),
        Issue("unknown_person", "enrollments.csv", 6, c3, "7b20000000000000000000b4"),
        Issue("unknown_class", "enrollments.csv", 7, "7b20000000000000000000b1", "8c3000000000000000000c09"),
        Issue("duplicate_row", "enrollments.csv", 8, c1, "7b20000000000000000000b1"),
    ]
    assert snap.counts == {"schools": 2, "terms": 1, "courses": 2, "classes": 3, "teachers": 3, "students": 3,
                           "enrollments": 8}


def test_the_fixtures_passwords_and_pii_are_never_read():
    snap = repr(read_clever(_fixture()))
    for secret in ("CleverTeacherPw", "CleverStudentPw", "alee", "home.example.com", "04/02/2011", "Mia Ng", "555-01"):
        assert secret not in snap


# --- the layout --------------------------------------------------------------------------------------------------------
def test_headers_match_ignoring_case():
    lower = {name: _csv([tuple(h.lower() for h in rows[0]), *rows[1:]])
             for name, rows in (("schools.csv", SCHOOLS), ("teachers.csv", TEACHERS), ("students.csv", STUDENTS),
                                ("sections.csv", SECTIONS), ("enrollments.csv", ENROLLMENTS))}
    upper = {name: _csv([tuple(h.upper() for h in rows[0]), *rows[1:]])
             for name, rows in (("schools.csv", SCHOOLS), ("teachers.csv", TEACHERS), ("students.csv", STUDENTS),
                                ("sections.csv", SECTIONS), ("enrollments.csv", ENROLLMENTS))}
    expected = read_clever(_files())
    for files in (lower, upper):
        snap = read_clever(files)
        assert (snap.people, snap.classes, snap.enrollments, snap.courses) == (
            expected.people, expected.classes, expected.enrollments, expected.courses)
    assert expected.people["teacher:T1"].given == "Ann" and len(expected.enrollments) == 2


@pytest.mark.parametrize("name, column", [("schools.csv", "School_name"), ("teachers.csv", "Teacher_email"),
                                          ("students.csv", "Student_id"), ("sections.csv", "Teacher_id"),
                                          ("enrollments.csv", "Section_id")])
def test_a_missing_column_is_refused_with_the_layout(name, column):
    rows = {"schools.csv": SCHOOLS, "teachers.csv": TEACHERS, "students.csv": STUDENTS, "sections.csv": SECTIONS,
            "enrollments.csv": ENROLLMENTS}[name]
    keep = [i for i, h in enumerate(rows[0]) if h != column]
    files = _files(**{name: [tuple(r[i] for i in keep) for r in rows]})
    with pytest.raises(PackageError) as err:
        read_clever(files)
    assert column in str(err.value) and "That isn't Clever's CSV layout" in str(err.value)


def test_a_missing_file_is_refused():
    with pytest.raises(PackageError, match="no sections.csv"):
        read_clever(_files(**{"sections.csv": None}))


def test_each_file_is_taken_out_as_it_is_read():
    files = _files()
    read_clever(files)
    assert files == {}


# --- people ------------------------------------------------------------------------------------------------------------
def test_a_repeated_student_keeps_its_first_row_and_flags_a_different_address():
    students = STUDENTS + [("S1", "P1", "BO@example.com", "Bo", "Ng", "dad@home.example.com", "bng", "x"),   # same
                           ("S1", "P1", "", "Bo", "Ng", "aunt@home.example.com", "bng", "x"),                 # blank
                           ("S1", "P1", "bo.ng@example.com", "Bobby", "Ng", "", "bng", "x")]                  # differs
    snap = read_clever(_files(**{"students.csv": students}))
    assert snap.people["student:P1"] == Person("student:P1", "student", "bo@example.com", "Bo", "Ng")
    assert snap.issue_counts == {"conflict": 1}
    assert snap.issue_sample[0].row == 5 and snap.issue_sample[0].source_id == "P1"


def test_a_repeated_teacher_follows_the_same_rule():
    teachers = TEACHERS + [("S2", "T1", "ann@example.com", "Ann", "Lee", "", ""),
                           ("S2", "T1", "ann.lee@example.com", "Ann", "Lee", "", "")]
    snap = read_clever(_files(**{"teachers.csv": teachers}))
    assert snap.people["teacher:T1"].email == "ann@example.com" and snap.issue_counts == {"conflict": 1}


def test_student_and_teacher_ids_are_separate():
    students = STUDENTS + [("S1", "T1", "cy@example.com", "Cy", "Oh", "", "", "")]
    snap = read_clever(_files(**{"students.csv": students}))
    assert snap.people["teacher:T1"].given == "Ann" and snap.people["student:T1"].given == "Cy"


def test_addresses_are_checked_and_one_address_is_one_person():
    students = STUDENTS + [("S1", "P2", "", "No", "Mail", "", "", ""), ("S1", "P3", "not an address", "Bad", "Mail", "", "", ""),
                           ("S1", "P4", "ANN@example.com", "Dup", "Mail", "", "", ""), ("S1", "", "x@example.com", "No", "Id",
                                                                                        "", "", "")]
    snap = read_clever(_files(**{"students.csv": students}))
    assert snap.issue_counts == {"no_email": 1, "bad_email": 1, "duplicate_email": 1, "no_id": 1}
    assert set(snap.people) == {"teacher:T1", "teacher:T2", "student:P1"}


def test_secret_columns_never_reach_the_snapshot():
    snap = read_clever(_files())
    assert "hunter2" not in repr(snap) and "parent@home" not in repr(snap) and "alee" not in repr(snap)


# --- sections ----------------------------------------------------------------------------------------------------------
def test_co_teachers_and_the_primary_flag():
    sections = [("School_id", "Section_id", "Teacher_id", "Teacher_2_id", "Teacher_10_id", "Name"),
                ("S1", "C1", "T1", "T1", "T2", "Biology"),        # a repeat of the primary is dropped
                ("S1", "C2", "", "T2", "", "Chemistry")]          # a co-teacher with no primary is still a co-teacher
    snap = read_clever(_files(**{"sections.csv": sections, "enrollments.csv": ENROLLMENTS[:1]}))
    assert snap.enrollments == [Enrollment("C1", "teacher:T1", "teacher", True),
                                Enrollment("C1", "teacher:T2", "teacher", False),
                                Enrollment("C2", "teacher:T2", "teacher", False)]
    assert snap.issue_counts == {}


def test_unknown_teacher_student_section_and_school():
    teachers = TEACHERS + [("S1", "T3", "", "No", "Mail", "", "")]
    sections = [("School_id", "Section_id", "Teacher_id", "Teacher_2_id", "Name"),
                ("S1", "C1", "T9", "T3", "Biology"), ("S9", "C2", "T1", "", "Art"), ("S1", "C2", "T2", "", "Again"),
                ("S1", "", "T1", "", "No id")]
    enrollments = ENROLLMENTS + [("S1", "C9", "P1"), ("S1", "C1", "P9"), ("S1", "C1", "T1")]
    snap = read_clever(_files(**{"teachers.csv": teachers, "sections.csv": sections, "enrollments.csv": enrollments}))
    assert snap.issue_counts == {"no_email": 1, "unknown_person": 4, "unknown_school": 1, "duplicate_id": 1, "no_id": 1,
                                 "unknown_class": 1}
    assert snap.classes["C2"] == Class("C2", "Art", "", "S9")          # an unknown school is flagged, still kept
    unknown = [(i.file, i.detail) for i in snap.issue_sample if i.code == "unknown_person"]
    assert unknown == [("sections.csv", "T9"), ("sections.csv", "T3"), ("enrollments.csv", "P9"), ("enrollments.csv", "T1")]


def test_terms_are_built_from_section_columns():
    sections = [("School_id", "Section_id", "Teacher_id", "Name", "Term_name", "Term_start", "Term_end"),
                ("S1", "C1", "T1", "A", "Fall", "08/20/2026", "12/18/2026"),
                ("S1", "C2", "T1", "B", "Fall", "08/20/2026", "12/18/2026"),       # the same term
                ("S1", "C3", "T1", "C", "", "01/05/2027", "2027-05-30"),           # no name; an ISO end kept as given
                ("S1", "C4", "T1", "D", "Odd", "13/01/2026", "8/20/2026"),         # not a date; not two digits
                ("S1", "C5", "T1", "E", "", "", "")]                                # no term
    snap = read_clever(_files(**{"sections.csv": sections}))
    fall, spring, odd = ("term:Fall|08/20/2026|12/18/2026", "term:|01/05/2027|2027-05-30", "term:Odd|13/01/2026|8/20/2026")
    assert snap.terms == {fall: Term(fall, "Fall", "term", "2026-08-20", "2026-12-18"),
                          spring: Term(spring, "2027-01-05 to 2027-05-30", "term", "2027-01-05", "2027-05-30"),
                          odd: Term(odd, "Odd", "term", "13/01/2026", "8/20/2026")}
    assert [snap.classes[c].term_ids for c in ("C1", "C2", "C3", "C4", "C5")] == [(fall,), (fall,), (spring,), (odd,), ()]


def test_title_fallbacks_and_courses():
    sections = [("School_id", "Section_id", "Teacher_id", "Name", "Course_name", "Course_number", "Period", "Section_number"),
                ("S1", "C1", "T1", "Bio P2", "Biology", "BIO", "2", "B-2"),
                ("S1", "C2", "T1", "", "Biology", "BIO", "3", ""),
                ("S1", "C3", "T1", "", "Biology Honors", "BIO", "", ""),     # the first course row wins
                ("S1", "C4", "T1", "", "", "", "4", "")]
    snap = read_clever(_files(**{"sections.csv": sections, "enrollments.csv": ENROLLMENTS[:1]}))
    assert [snap.classes[c].title for c in ("C1", "C2", "C3", "C4")] == ["Bio P2", "Biology - Period 3", "Biology Honors", ""]
    assert snap.courses == {"course:BIO": Course("course:BIO", "Biology", "BIO", "S1")}
    assert snap.classes["C1"].code == "B-2" and snap.classes["C4"].course_id == ""
    assert snap.issue_counts == {"no_title": 1}


# --- through the upload -----------------------------------------------------------------------------------------------
def test_read_upload_detects_clever_as_a_zip_and_as_picked_csvs():
    expected = read_clever(_fixture())
    from_zip = read_upload([("clever.zip", _zip(_fixture()))])
    picked = read_upload([(f"export/{name}", body) for name, body in _fixture().items()])
    for snap in (from_zip, picked):
        assert snap.source == "Clever CSV"
        assert (snap.people, snap.classes, snap.enrollments, snap.issue_counts) == (
            expected.people, expected.classes, expected.enrollments, expected.issue_counts)


def test_staff_csv_is_named_but_never_unpacked(monkeypatch):
    data = _zip({**_fixture(), "staff.csv": b"Staff_id,Staff_email,Password\r\nX1,x@example.com,StaffPw-secret\r\n"})
    opened = []
    real = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, info, *a, **k: opened.append(getattr(info, "filename", info))
                        or real(self, info, *a, **k))
    snap = read_upload([("clever.zip", data)])
    assert "staff.csv" not in opened and "students.csv" in opened and "StaffPw" not in repr(snap)


def test_picked_staff_csv_is_dropped_unread():
    snap = read_upload([*_fixture().items(), ("staff.csv", b"\xff not even utf-8")])
    assert snap.source == "Clever CSV"


def test_a_file_that_isnt_clevers_is_refused():
    with pytest.raises(PackageError, match="isn't a Clever file"):
        read_upload([*_fixture().items(), ("grades.csv", b"x")])


def test_a_nul_in_a_clever_file_refuses_it():
    with pytest.raises(PackageError, match="NUL"):
        read_clever(_files(**{"students.csv": _csv(STUDENTS) + b'"S1","P2","a\x00@example.com","A","B","","",""\r\n'}))



def test_a_kelvin_sign_address_is_refused_not_lowercased_into_ascii():
    # Found by the adversarial run of C5: U+212A lowercases to an ASCII k, so it's checked as given first.
    teachers = TEACHERS[:2] + [("S1", "T2", "Ken@example.com", "Ben", "Ortiz", "bortiz", "x")]
    snap = read_clever(_files(**{"teachers.csv": teachers}))
    assert "teacher:T2" not in snap.people and snap.issue_counts.get("bad_email") == 1
