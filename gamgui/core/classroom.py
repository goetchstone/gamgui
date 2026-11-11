"""Google Classroom, read only: classes and who is in them (plan 2026-11-11, C1).

GamGUI never writes to Classroom. The operator has no Workspace for Education domain to prove a Classroom write on,
and this repo relies on no write that hasn't run live, so the app reads Classroom and hands any change over as
files GAM runs outside it (plan C4). The reads below have only run against the mock: their shapes were read from
the vendored GAM build (doPrintCourses, doPrintCourseParticipants), not captured from a tenant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# The admin token's Classroom scopes, as GAM's own table names them (gamlib glapi _CLIENT_SCOPES, 7.51.00). GAM reads
# Classroom with the admin token, so these are picked in `gam oauth create`, not delegated. Either the scope or its
# `.readonly` form will do for a read.
CLASSROOM_SCOPES = (
    ("https://www.googleapis.com/auth/classroom.courses", "Classroom API - Courses", "See classes"),
    ("https://www.googleapis.com/auth/classroom.rosters", "Classroom API - Rosters", "See who is in each class"),
    ("https://www.googleapis.com/auth/classroom.profile.emails", "Classroom API - Profile Emails",
     "See each person's address"),
)

# The Classroom API's course states, as it spells them; the screen says them in words.
STATE_WORDS = {"ACTIVE": "Active", "ARCHIVED": "Archived", "PROVISIONED": "Not yet accepted", "DECLINED": "Declined",
               "SUSPENDED": "Suspended", "COURSE_STATE_UNSPECIFIED": "Unknown"}


def classroom_scopes_missing(granted: object) -> Optional[List[str]]:
    """The Classroom scopes the admin token lacks, by GAM's name for each, or ``None`` when its scopes can't be read."""
    if not isinstance(granted, list):
        return None
    have = {str(s) for s in granted}
    return [name for scope, name, _ in CLASSROOM_SCOPES if scope not in have and f"{scope}.readonly" not in have]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    section: str = ""
    room: str = ""
    state: str = ""
    owner_id: str = ""
    owner_email: str = ""
    created: str = ""
    updated: str = ""
    link: str = ""

    @property
    def state_words(self) -> str:
        return STATE_WORDS.get(self.state, self.state.title() if self.state else "Unknown")

    @classmethod
    def from_json(cls, rec: Dict[str, Any]) -> Optional["Course"]:
        """A course record from `print courses … formatjson`, or ``None`` for one with no ID."""
        course_id = _text(rec.get("id"))
        if not course_id:
            return None
        return cls(id=course_id, name=_text(rec.get("name")), section=_text(rec.get("section")),
                   room=_text(rec.get("room")), state=_text(rec.get("courseState")),
                   owner_id=_text(rec.get("ownerId")), owner_email=_text(rec.get("ownerEmail")).lower(),
                   created=_text(rec.get("creationTime")), updated=_text(rec.get("updateTime")),
                   link=_text(rec.get("alternateLink")))


def courses_from(records: Iterable[Dict[str, Any]]) -> List[Course]:
    """Courses from a `print courses` read, each once, by name."""
    seen: Dict[str, Course] = {}
    for rec in records:
        course = Course.from_json(rec)
        if course is not None:
            seen.setdefault(course.id, course)
    return sorted(seen.values(), key=lambda c: (c.name.lower(), c.section.lower(), c.id))


@dataclass(frozen=True)
class Participant:
    role: str            # "TEACHER" or "STUDENT"
    user_id: str
    email: str = ""
    name: str = ""


@dataclass
class Roster:
    course_id: str
    course_name: str
    teachers: List[Participant] = field(default_factory=list)
    students: List[Participant] = field(default_factory=list)
    # A cell GAM printed that couldn't be read: the roster says so rather than reading as complete.
    unreadable: List[str] = field(default_factory=list)
    # Someone listed without an address: Classroom leaves addresses out without the admin token's "Profile Emails" scope.
    emails_hidden: bool = False


def _people(cell: Any, role: str, column: str, roster: Roster) -> List[Participant]:
    if cell is None:            # the column itself is missing: GAM printed something else, so this side is unknown
        roster.unreadable.append(column)
        return []
    if cell == "":
        return []
    try:
        items = json.loads(cell) if isinstance(cell, str) else cell
    except ValueError:
        roster.unreadable.append(column)
        return []
    if not isinstance(items, list):
        roster.unreadable.append(column)
        return []
    people: List[Participant] = []
    for item in items:
        if not isinstance(item, dict):
            roster.unreadable.append(column)
            continue
        raw_profile = item.get("profile")
        profile: Dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
        raw_name = profile.get("name")
        name: Dict[str, Any] = raw_name if isinstance(raw_name, dict) else {}
        user_id = _text(item.get("userId")) or _text(profile.get("id"))
        email = _text(profile.get("emailAddress")).lower()
        if not email:
            roster.emails_hidden = True
        people.append(Participant(role=role, user_id=user_id, email=email, name=_text(name.get("fullName"))))
    return sorted(people, key=lambda p: (p.name.lower() or p.email, p.email))


def roster_from(records: Iterable[Dict[str, Any]], course_id: str) -> Roster:
    """One class's roster from `print course-participants course <id> show all formatjson`: its one row, `JSON-teachers` and
    `JSON-students` each a list of people. GAM prints the row even for an empty class, so a missing row is a read that fell
    short, never an empty class."""
    for rec in records:
        if _text(rec.get("courseId")) != course_id:
            continue
        roster = Roster(course_id=course_id, course_name=_text(rec.get("courseName")))
        roster.teachers = _people(rec.get("JSON-teachers"), "TEACHER", "JSON-teachers", roster)
        roster.students = _people(rec.get("JSON-students"), "STUDENT", "JSON-students", roster)
        return roster
    return Roster(course_id=course_id, course_name="", unreadable=["the class's row"])
