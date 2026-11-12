"""The column-mapped spreadsheet reader (plan 2026-11-11, C5) and the saved maps it's read with."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from gamgui.core import roster
from gamgui.core.roster import Class, Enrollment, PackageError, Person, Term
from gamgui.core.roster_mapped import FIELDS, ColumnMap, guess, preview, read_mapped
from gamgui.core.roster_maps import MAPS_CAP, MapStore

HEADER = "Section ID,Course Title,Term,Email Address,User Type,Lead\n"
CMAP = ColumnMap(class_id="Section ID", class_name="Course Title", section="Term", email="Email Address",
                 role="User Type", primary="Lead", teacher_words=("teacher", "staff"), student_words=("student",))


def sheet(*rows: str, header: str = HEADER) -> bytes:
    return (header + "".join(r + "\n" for r in rows)).encode()


def codes(snap: roster.Snapshot) -> list:
    return [(i.code, i.row) for i in snap.issue_sample]


# --- guess and problem -----------------------------------------------------------------------------------------------
def test_guess_on_a_realistic_header():
    header = ["Student_Number", "Section ID", "Course Title", "TERM", "e-mail", "User Type", "Is Primary", "Grade"]
    m = guess(header)
    assert (m.class_id, m.class_name, m.section, m.email, m.role, m.primary) == (
        "Section ID", "Course Title", "TERM", "e-mail", "User Type", "Is Primary")
    assert m.problem(header) == ""


def test_guess_leaves_unknown_fields_empty_and_never_maps_one_column_twice():
    m = guess(["Class", "Mail", "Grade"])
    assert (m.class_id, m.class_name, m.email, m.role, m.section, m.primary) == ("", "Class", "Mail", "", "", "")
    assert "class id" in m.problem(["Class", "Mail", "Grade"])


def test_fields_are_the_screens_order():
    assert [f for f, _, _ in FIELDS] == ["class_id", "class_name", "section", "email", "role", "primary"]
    assert [f for f, _, req in FIELDS if req] == ["class_id", "class_name", "email", "role"]


@pytest.mark.parametrize("cmap, words", [
    (ColumnMap(class_name="A", email="B", role="C"), "class id"),
    (ColumnMap(class_id="A", class_name="A", email="Nope", role="C"), "“Nope”"),
    (ColumnMap(class_id="A", class_name="A", email="B", role="C", primary="Zed"), "primary teacher"),
    (ColumnMap(class_id="A", class_name="A", email="B", role="C", teacher_words=()), "for a teacher"),
    (ColumnMap(class_id="A", class_name="A", email="B", role="C", student_words=(" ",)), "for a student"),
    (ColumnMap(class_id="A", class_name="A", email="B", role="C", teacher_words=("staff", "Aide"),
               student_words=("aide",)), "“aide”"),
])
def test_problem_sentences(cmap, words):
    problem = cmap.problem(["A", "B", "C"])
    assert words in problem and problem.endswith(".")


def test_from_json_never_raises_on_junk():
    for junk in (None, 3, "x", [], {"class_id": 5, "email": ["a"], "teacher_words": "teacher", "student_words": {"a": 1}},
                 {"teacher_words": [1, None, "", "  "], "student_words": []}):
        m = ColumnMap.from_json(junk)
        assert (m.class_id, m.email) == ("", "")
        assert m.teacher_words == ("teacher",) and m.student_words == ("student",)
    m = ColumnMap.from_json({"class_id": " " + "x" * 500, "teacher_words": [" Teacher ", "TEACHER", 7, "Lead"],
                             "student_words": [f"w{i}" for i in range(40)]})
    assert m.class_id == "x" * 200
    assert m.teacher_words == ("teacher", "lead") and len(m.student_words) == 20
    assert ColumnMap.from_json(CMAP.as_json()) == CMAP


# --- preview ---------------------------------------------------------------------------------------------------------
def test_preview_reads_only_n_rows_and_says_what_the_role_means():
    body = sheet("C1,Math,Fall,T@X.EDU,Staff,yes", "", "C1,Math,Fall,s@x.edu,student,", "C2,Art,,a@x.edu,aide,")
    body += b"C9,Filler,,f@x.edu,student,\n" * 4000 + b"\x00\xff\xfe not UTF-8\n"    # far past row 3: refused if read
    rows = preview("dir/sheet.csv", body, CMAP)
    assert [r["as"] for r in rows] == ["teacher", "student", ""]
    assert rows[0] == {"class_id": "C1", "class_name": "Math", "section": "Fall", "email": "t@x.edu", "role": "Staff",
                       "primary": "yes", "as": "teacher"}
    with pytest.raises(PackageError):                          # the same file read far enough reaches the bad line
        preview("sheet.csv", body, CMAP, n=5000)


def test_preview_works_with_a_map_still_being_filled():
    rows = preview("s.csv", sheet("C1,Math,Fall,t@x.edu,teacher,"), ColumnMap(email="Email Address", role="Missing"))
    assert rows == [{"class_id": "", "class_name": "", "section": "", "email": "t@x.edu", "role": "", "primary": "",
                     "as": ""}]


# --- read_mapped -----------------------------------------------------------------------------------------------------
def test_read_mapped_on_a_realistic_sheet():
    body = sheet(
        "M101,Algebra I,Fall 2026,Lee@School.org,Teacher,Y",
        "M101,Algebra I,Fall 2026,co@school.org,staff,",
        "M101,Algebra I,Fall 2026,ann@school.org,Student,",
        "M101,Algebra I,Fall 2026,bo@school.org,student,yes",
        "A200,Art,,lee@school.org,teacher,x",
        "A200,Art,,ann@school.org,student,",
    )
    snap = read_mapped("exports/Spring Sheet.csv", body, CMAP)
    assert snap.source == "Spreadsheet (column map)"
    assert snap.classes == {"M101": Class("M101", "Algebra I", "", "", ("section:Fall 2026",)),
                            "A200": Class("A200", "Art", "", "", ())}
    assert snap.terms == {"section:Fall 2026": Term("section:Fall 2026", "Fall 2026")}
    assert snap.people == {e: Person(e, r, email=e) for e, r in (("lee@school.org", "teacher"), ("co@school.org", "teacher"),
                                                                  ("ann@school.org", "student"), ("bo@school.org", "student"))}
    assert snap.enrollments == [Enrollment("M101", "lee@school.org", "teacher", True),
                                Enrollment("M101", "co@school.org", "teacher", False),
                                Enrollment("M101", "ann@school.org", "student", False),
                                Enrollment("M101", "bo@school.org", "student", False),     # primary means nothing for a student
                                Enrollment("A200", "lee@school.org", "teacher", True),
                                Enrollment("A200", "ann@school.org", "student", False)]
    assert snap.issue_counts == {} and snap.schools == {} and snap.courses == {}
    assert snap.counts == {"schools": 0, "terms": 1, "courses": 0, "classes": 2, "teachers": 2, "students": 2,
                           "enrollments": 6}


def test_every_row_issue():
    body = sheet(
        ",Algebra,Fall,a@x.edu,student,",              # 2 no_id
        "C1,Algebra,Fall,,student,",                   # 3 no_email (the class is still kept)
        "C1,Algebra,Fall,not an address,student,",     # 4 bad_email
        "C1,Algebra,Fall,b@x.edu,aide,",               # 5 unknown_role
        "C1,Geometry,Fall,c@x.edu,student,",           # 6 conflict (name), row kept
        "C1,Algebra,Spring,d@x.edu,student,",          # 7 conflict (section), row kept
        "C1,Algebra,Fall,c@x.edu,teacher,yes",         # 8 duplicate_row, the first (student) kept
        "C2,,,e@x.edu,teacher,",                       # 9 no_title
        "C2,,,f@x.edu,student,",                       # 10 no second no_title
        "C1,Algebra,Fall,\"g\x01@x.edu\",student,",    # 11 bad_email: a control character is refused, not dropped
    )
    snap = read_mapped("s.csv", body, CMAP)
    assert codes(snap) == [("no_id", 2), ("no_email", 3), ("bad_email", 4), ("unknown_role", 5), ("conflict", 6),
                           ("conflict", 7), ("duplicate_row", 8), ("no_title", 9), ("bad_email", 11)]
    assert all(i.file == "s.csv" and i.code in roster.ISSUE_WORDS for i in snap.issue_sample)
    assert snap.classes["C1"] == Class("C1", "Algebra", "", "", ("section:Fall",))
    assert snap.classes["C2"].title == ""
    assert set(snap.terms) == {"section:Fall"}
    assert snap.people["c@x.edu"].role == "student"
    assert [(e.class_id, e.person_id, e.role) for e in snap.enrollments] == [
        ("C1", "c@x.edu", "student"), ("C1", "d@x.edu", "student"), ("C2", "e@x.edu", "teacher"), ("C2", "f@x.edu", "student")]


def test_a_person_keeps_their_first_role():
    snap = read_mapped("s.csv", sheet("C1,A,,p@x.edu,teacher,", "C2,B,,p@x.edu,student,"), CMAP)
    assert snap.people["p@x.edu"].role == "teacher"
    assert [e.role for e in snap.enrollments] == ["teacher", "student"]


def test_one_column_mapped_to_two_fields():
    cmap = ColumnMap(class_id="Class", class_name="Class", email="Email", role="Role")
    snap = read_mapped("s.csv", sheet("Algebra 1,t@x.edu,teacher", "Algebra 1,s@x.edu,student", header="Class,Email,Role\n"),
                       cmap)
    assert snap.classes == {"Algebra 1": Class("Algebra 1", "Algebra 1", "", "", ())}
    assert len(snap.enrollments) == 2 and not snap.issue_counts
    assert preview("s.csv", sheet("Algebra 1,t@x.edu,teacher", header="Class,Email,Role\n"), cmap)[0]["class_name"] == "Algebra 1"


def test_a_map_that_doesnt_fit_the_header_refuses_the_file():
    with pytest.raises(PackageError, match="isn't in the spreadsheet's header"):
        read_mapped("s.csv", sheet("C1,A,,t@x.edu,teacher,", header="Class,Name,Term,Email,Role,Lead\n"), CMAP)
    with pytest.raises(PackageError, match="class id"):
        read_mapped("s.csv", sheet("C1,A,,t@x.edu,teacher,"), ColumnMap())


def test_the_bounded_reader_still_refuses():
    with pytest.raises(PackageError, match="NUL"):
        read_mapped("s.csv", sheet("C1,A,,t\x00@x.edu,teacher,"), CMAP)
    with pytest.raises(PackageError, match="UTF-8"):
        read_mapped("s.csv", sheet("C1,A,,t@x.edu,teacher,") + b"C1,\xff,,s@x.edu,student,\n", CMAP)
    with pytest.raises(PackageError, match="fields"):
        read_mapped("s.csv", sheet("C1,A,,t@x.edu,teacher," + "," * 20), CMAP)
    with pytest.raises(PackageError, match="field longer"):
        read_mapped("s.csv", sheet("C1," + "a" * (roster.FIELD_CAP + 1) + ",,t@x.edu,teacher,"), CMAP)


def test_needs_map_for_one_unknown_csv_only():
    body = sheet("C1,A,,t@x.edu,teacher,")
    with pytest.raises(roster.NeedsMap) as caught:
        roster.read_upload([("folder/My Export.csv", body)])
    assert (caught.value.name, caught.value.body) == ("My Export.csv", body)
    with pytest.raises(PackageError, match="isn't an export GamGUI knows"):
        roster.read_upload([("a.csv", body), ("b.csv", body)])


# --- saved maps ------------------------------------------------------------------------------------------------------
@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "roster-maps.json"


def test_store_round_trip_and_modes(store_path):
    store = MapStore(store_path)
    assert store.names() == []
    store.save("  Aeries  ", CMAP)
    store.save("beta", ColumnMap(email="Mail"))
    store.save("ALPHA", ColumnMap(class_id="x"))
    assert store.names() == ["Aeries", "ALPHA", "beta"]
    again = MapStore(store_path)
    assert again.get("Aeries") == CMAP and again.get("beta") == ColumnMap(email="Mail")
    assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store_path.parent).st_mode) == 0o700
    data = json.loads(store_path.read_text())
    assert data["version"] == 1 and set(data["maps"]) == {"ALPHA", "Aeries", "beta"}
    again.save("aeries", ColumnMap(email="E"))                 # the same name in another case replaces it
    assert again.names() == ["aeries", "ALPHA", "beta"]
    again.delete("beta")
    again.delete("never saved")
    assert MapStore(store_path).names() == ["aeries", "ALPHA"]
    assert [p.name for p in store_path.parent.iterdir()] == ["roster-maps.json"]     # no temp file left


def test_store_save_is_atomic(store_path, monkeypatch):
    store = MapStore(store_path)
    store.save("one", CMAP)
    before = store_path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr("gamgui.core.roster_maps.os.replace", boom)
    with pytest.raises(OSError):
        store.save("two", CMAP)
    assert store_path.read_bytes() == before
    assert [p.name for p in store_path.parent.iterdir()] == ["roster-maps.json"]


@pytest.mark.parametrize("junk", [b"not json", b"[]", b'{"version": 2, "maps": {}}', b'{"version": 1, "maps": []}',
                                  b'{"version": 1, "maps": {"a": 3}}', b'{"version": 1, "maps": {"a\\u0001": {}}}',
                                  b"\xff\xfe"])
def test_an_unreadable_file_is_moved_aside_never_saved_over(store_path, junk):
    store_path.parent.mkdir(parents=True)
    store_path.write_bytes(junk)
    store = MapStore(store_path)
    assert store.names() == [] and store.set_aside is not None
    assert store.set_aside.read_bytes() == junk and not store_path.exists()
    store.save("fresh", CMAP)
    assert store.set_aside.read_bytes() == junk
    assert MapStore(store_path).names() == ["fresh"]


def test_a_stored_map_with_junk_fields_reads_without_raising(store_path):
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({"version": 1, "maps": {"m": {"class_id": 9, "email": "Mail", "teacher_words": "x"}}}))
    m = MapStore(store_path).get("m")
    assert m == ColumnMap(email="Mail")


@pytest.mark.parametrize("name, words", [("", "Give the map a name"), ("   ", "Give the map a name"),
                                         ("x" * 61, "at most 60"), ("tab\there", "control"), ("zero\u200bwidth", "control")])
def test_store_name_rules(store_path, name, words):
    with pytest.raises(ValueError, match=words):
        MapStore(store_path).save(name, CMAP)
    assert not store_path.exists()


def test_store_holds_at_most_fifty(store_path):
    store = MapStore(store_path)
    for i in range(MAPS_CAP):
        store.save(f"map {i}", CMAP)
    with pytest.raises(ValueError, match="50 saved maps"):
        store.save("one more", CMAP)
    store.save("MAP 3", ColumnMap(email="E"))           # replacing one is fine
    assert len(MapStore(store_path).names()) == MAPS_CAP


def test_the_store_keeps_no_row_data(store_path):
    store = MapStore(store_path)
    store.save("m", guess(["Section ID", "Course Title", "Email", "Role"]))
    text = store_path.read_text()
    assert "@" not in text


# --- found by the adversarial run of C5 (2026-11-12) -----------------------------------------------------------------
def test_a_column_mapped_as_the_address_and_a_class_is_still_cleaned_for_the_class():
    body = b"who,role\r\n\"x\u202e\x07y@x.edu\",teacher\r\n".decode("unicode_escape").encode()
    cmap = ColumnMap(class_id="who", class_name="who", email="who", role="role")
    snap = read_mapped("s.csv", body, cmap)
    assert all(c.isprintable() and c != "\u202e" for cid in snap.classes for c in cid)
    assert snap.issue_counts.get("bad_email") == 1                       # the address itself is refused, not cleaned


def test_an_address_is_checked_before_it_is_lowercased():
    # U+212A KELVIN SIGN lowercases to an ASCII k: lowercasing first would accept a different, valid address.
    body = "class_id,class_name,email,role\r\nC1,Math,\u212aim@x.edu,teacher\r\n".encode()
    snap = read_mapped("s.csv", body, ColumnMap(class_id="class_id", class_name="class_name", email="email", role="role"))
    assert snap.people == {} and snap.issue_counts == {"bad_email": 1}


def test_someone_who_teaches_one_class_and_studies_in_another_counts_as_a_teacher():
    body = b"class_id,class_name,email,role\r\nC1,Math,s@x.edu,student\r\nC2,Art,s@x.edu,teacher\r\n"
    snap = read_mapped("s.csv", body, ColumnMap(class_id="class_id", class_name="class_name", email="email", role="role"))
    assert snap.people["s@x.edu"].role == "teacher" and snap.counts["teachers"] == 1
    assert sorted((e.class_id, e.role) for e in snap.enrollments) == [("C1", "student"), ("C2", "teacher")]
