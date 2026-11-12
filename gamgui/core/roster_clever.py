"""Read Clever's CSV roster export into the snapshot (plan 2026-11-11, C5.2).

Clever's SFTP CSV (spec v2.2.0) is five files with Capitalized_snake_case headers, matched ignoring case. It has no
manifest, no status column and no delta: every row is live. It differs from OneRoster in ways that each need a rule here:

- **Students and teachers are separate ID spaces** that may collide, so a person's ID is ``student:`` or ``teacher:``
  plus Clever's.
- **A student repeats once per guardian contact**, and a teacher once per school. A repeat is normal: the first row is
  kept, and a repeat is a ``conflict`` only when its address differs.
- **A class's teachers are on its section row** (``Teacher_id`` the primary, ``Teacher_2_id`` to ``Teacher_10_id``
  co-teachers); enrollments.csv holds students only.
- **Courses and terms aren't files**: they are columns of a section, and are built from them.

An upload is hostile until read, and Clever's files hold far more than a roster needs (passwords, birth dates, guardian
contacts, IEP status). Every row goes through ``roster._rows``, which builds a row from the columns named here alone, so
``Username``, ``Password``, ``Contact_email`` and the rest are never read into anything. staff.csv and administrators.csv
are named and never unpacked (``SKIP``).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Set, Tuple

from .roster import (Class, Course, Enrollment, Layout, PackageError, Person, School, Snapshot, Term, _one_address,
                     _rows)

REQUIRED = ("schools.csv", "students.csv", "teachers.csv", "sections.csv", "enrollments.csv")
SKIP = ("staff.csv", "administrators.csv")     # not on a class's roster; staff.csv and the others hold passwords
LAYOUT = Layout("Clever", {n.lower(): n for n in (*REQUIRED, *SKIP)}, frozenset(SKIP))

SOURCE = "Clever CSV"
_LAYOUT = "That isn't Clever's CSV layout: export Clever's schools, students, teachers, sections and enrollments files."
_CO_TEACHERS = tuple(f"Teacher_{n}_id" for n in range(2, 11))

# The columns each file must have for this reader, and the ones its rows are read for. Never Username, Password,
# Contact_email or any other column Clever puts beside them: a row is built from these alone.
COLUMNS: Dict[str, Tuple[str, ...]] = {
    "schools.csv": ("School_id", "School_name"),
    "teachers.csv": ("Teacher_id", "Teacher_email", "First_name", "Last_name"),
    "students.csv": ("Student_id", "Student_email", "First_name", "Last_name"),
    "sections.csv": ("School_id", "Section_id", "Teacher_id"),
    "enrollments.csv": ("Section_id", "Student_id"),
}
USED: Dict[str, Tuple[str, ...]] = {
    **COLUMNS,
    "sections.csv": ("School_id", "Section_id", "Teacher_id", *_CO_TEACHERS, "Name", "Section_number", "Course_name",
                     "Course_number", "Period", "Term_name", "Term_start", "Term_end"),
}
_ADDRESS = {"teachers.csv": "Teacher_email", "students.csv": "Student_email"}

_US_DATE = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")


def _iso(text: str) -> str:
    """Clever's MM/DD/YYYY as YYYY-MM-DD, when it is exactly that and a real date; anything else is kept as given, so the
    operator sees what the export said rather than a guess."""
    match = _US_DATE.fullmatch(text)
    if not match:
        return text
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return text


def _people(snap: Snapshot, name: str, body: bytes, role: str, emails: Dict[str, str]) -> None:
    """teachers.csv or students.csv into people, ``role:`` + Clever's ID. A repeated ID (a student per guardian, a teacher
    per school) keeps its first row; a repeat whose address differs from the first row's is a conflict."""
    id_column, email_column = f"{role.capitalize()}_id", _ADDRESS[name]
    first: Dict[str, str] = {}               # Clever ID -> the first row's address, lowercased (kept or not)
    for line, row in _rows(name, body, COLUMNS[name], USED[name], fold_case=True, layout=_LAYOUT, raw=(email_column,)):
        sid = row.get(id_column, "")
        if not sid:
            snap.issue("no_id", name, line)
            continue
        given = row.get(email_column, "")
        email = given.lower()
        if sid in first:
            if email and email != first[sid]:
                snap.issue("conflict", name, line, sid, f"{email} here, {first[sid] or 'no address'} on its first row")
            continue
        first[sid] = email
        pid = f"{role}:{sid}"
        if not email:
            snap.issue("no_email", name, line, sid)
            continue
        if not _one_address(given):            # as given: lowercasing first turns a non-ASCII K (U+212A) into an ASCII k
            snap.issue("bad_email", name, line, sid, given)
            continue
        if email in emails:
            snap.issue("duplicate_email", name, line, sid, email)
            continue
        emails[email] = pid
        snap.people[pid] = Person(pid, role, email, row.get("First_name", ""), row.get("Last_name", ""))


def _title(row: Dict[str, str]) -> str:
    """A section's name, else its course's name with its period, else its course's name, else ""."""
    if row.get("Name"):
        return row["Name"]
    course, period = row.get("Course_name", ""), row.get("Period", "")
    if course and period:
        return f"{course} - Period {period}"
    return course


def _term(snap: Snapshot, row: Dict[str, str]) -> Tuple[str, ...]:
    """The section's term, built from its Term_ columns (Clever has no terms file), or () when it names none. Sections
    with the same three values share one term."""
    name, start, end = row.get("Term_name", ""), row.get("Term_start", ""), row.get("Term_end", "")
    if not (name or start or end):
        return ()
    tid = f"term:{name}|{start}|{end}"
    if tid not in snap.terms:
        start_iso, end_iso = _iso(start), _iso(end)
        snap.terms[tid] = Term(tid, name or f"{start_iso} to {end_iso}", "term", start_iso, end_iso)
    return (tid,)


def _sections(snap: Snapshot, body: bytes) -> None:
    """sections.csv into classes, the courses they name, and each class's teachers (the primary, then co-teachers)."""
    name = "sections.csv"
    for line, row in _rows(name, body, COLUMNS[name], USED[name], fold_case=True, layout=_LAYOUT, raw=()):
        sid = row.get("Section_id", "")
        if not sid:
            snap.issue("no_id", name, line)
            continue
        if sid in snap.classes:
            snap.issue("duplicate_id", name, line, sid)
            continue
        school, title = row.get("School_id", ""), _title(row)
        if not title:
            snap.issue("no_title", name, line, sid)
        if school not in snap.schools:
            snap.issue("unknown_school", name, line, sid, school)
        course_id = ""
        if row.get("Course_number"):
            course_id = f"course:{row['Course_number']}"
            if course_id not in snap.courses:
                snap.courses[course_id] = Course(course_id, row.get("Course_name", ""), row["Course_number"], school)
        snap.classes[sid] = Class(sid, title, course_id, school, _term(snap, row), row.get("Section_number", ""))
        seen: Set[str] = set()
        for column in ("Teacher_id", *_CO_TEACHERS):
            tid = row.get(column, "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            pid = f"teacher:{tid}"
            if pid not in snap.people:
                snap.issue("unknown_person", name, line, sid, tid)
                continue
            snap.enrollments.append(Enrollment(sid, pid, "teacher", column == "Teacher_id"))


def _enrollments(snap: Snapshot, body: bytes) -> None:
    """enrollments.csv: students only (a section's teachers are on its row), never primary."""
    name = "enrollments.csv"
    seen: Set[Tuple[str, str]] = set()
    for line, row in _rows(name, body, COLUMNS[name], USED[name], fold_case=True, layout=_LAYOUT, raw=()):
        section, student = row.get("Section_id", ""), row.get("Student_id", "")
        if section not in snap.classes:
            snap.issue("unknown_class", name, line, student, section)
            continue
        pid = f"student:{student}"
        if pid not in snap.people:
            snap.issue("unknown_person", name, line, section, student)
            continue
        if (section, pid) in seen:
            snap.issue("duplicate_row", name, line, section, student)
            continue
        seen.add((section, pid))
        snap.enrollments.append(Enrollment(section, pid, "student", False))


def read_clever(files: Dict[str, bytes]) -> Snapshot:
    """A Clever CSV export (``files``: its files by canonical name, from ``roster.files_from_zip`` or
    ``roster.files_from_csvs`` with ``LAYOUT``) as a snapshot. Each file is taken out of ``files`` as it's read, so its
    bytes go once its rows are in. A file missing, or one missing a column this reader needs, refuses the export."""
    missing: List[str] = [n for n in REQUIRED if n not in files]
    if missing:
        raise PackageError(f"The export has no {missing[0]}, which a roster needs. " + _LAYOUT)
    snap = Snapshot(source=SOURCE)
    schools = files.pop("schools.csv")
    for line, row in _rows("schools.csv", schools, COLUMNS["schools.csv"], fold_case=True, layout=_LAYOUT, raw=()):
        sid = row.get("School_id", "")
        if not sid:
            snap.issue("no_id", "schools.csv", line)
            continue
        if sid in snap.schools:
            snap.issue("duplicate_id", "schools.csv", line, sid)
            continue
        snap.schools[sid] = School(sid, row.get("School_name", ""))
    emails: Dict[str, str] = {}      # address -> person ID, across teachers and students: one address is one person
    _people(snap, "teachers.csv", files.pop("teachers.csv"), "teacher", emails)
    _people(snap, "students.csv", files.pop("students.csv"), "student", emails)
    _sections(snap, files.pop("sections.csv"))
    _enrollments(snap, files.pop("enrollments.csv"))
    return snap

