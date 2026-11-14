"""Compare a kept SIS roster with Classroom into one change list (plan 2026-11-11, C3).

Pure: a `Snapshot` (what the SIS says) and the Classroom classes as read (`Managed`) go in, a `ChangeList` comes out. No GAM
call, nothing written. GamGUI never applies it: the change list is handed over as files GAM runs outside the app (C4).

**Which classes are GamGUI's to plan.** A SIS class is tied to one Classroom class by a domain alias,
`d:gg-<sourcedId>` (``alias_for``): the SIS's own ID when it's plain, a hash of it otherwise. Only classes carrying one of
these aliases are compared, updated or archived; a class someone made by hand in Classroom is never touched or counted.

**What it plans,** in the order GAM must run it:

- *create*: a SIS class with no Classroom class yet, owned by its primary teacher (else its first teacher by address). A
  class with no teacher can't be created (Classroom needs an owner) and is noted instead.
- *rename*: a class whose name or section differs from the SIS.
- *add_teacher* / *add_student*: people the SIS has in a class that Classroom doesn't.
- *owner*: a class whose owner isn't the SIS's primary teacher (after the teacher is added: an owner must teach it).
- *remove_teacher* / *remove_student*: people Classroom has that the SIS doesn't. The owner is never removed.
- *archive*: a class GamGUI manages that the SIS no longer has.

**Who is who.** Classroom names people by their primary address; a SIS may list an alias (a renamed teacher's old
address). Every SIS address is read as its account's primary through the directory (``directory_from``) before anything
is compared, so an alias never plans its own account's removal. Without the directory, a class whose roster names
someone Classroom doesn't list there has no removal and no owner change planned: that may be someone already in it.

**Risk.** Removals and archives past a built-in count *and* share are flagged with the reason in a sentence; a flagged
kind is left out of the hand-over unless ticked (C4). Thresholds are fixed defaults, not a settings table (plan C3.3).
Each share is of what that kind can act on: active classes for archives, their teachers for teacher removals, everyone
in them for student removals. Archived classes never count: nothing is archived or removed from them.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .classroom import Course, Roster, roster_from
from .roster import Snapshot

ALIAS_PREFIX = "gg-"
_PLAIN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")

# The order the kinds run in, and how the screen names each.
KINDS: Tuple[Tuple[str, str], ...] = (
    ("create", "Classes to create"),
    ("rename", "Classes to rename"),
    ("add_teacher", "Teachers to add"),
    ("add_student", "Students to add"),
    ("owner", "Owners to change"),
    ("remove_teacher", "Teachers to remove"),
    ("remove_student", "Students to remove"),
    ("archive", "Classes to archive"),
)
DESTRUCTIVE = ("remove_teacher", "remove_student", "archive")
# Flag a destructive kind past both of these: a count, and a share of what's there now in active managed classes (their
# teachers, everyone in them, or the classes themselves).
LIMITS = {"remove_teacher": (25, 0.05), "remove_student": (500, 0.05), "archive": (25, 0.10)}


def alias_for(source_id: str) -> str:
    """The domain alias that ties a SIS class to its Classroom class: ``d:gg-<id>``, the SIS's ID when it's plain (so the
    SIS team can read it), else a hash of it (an alias is one GAM argument and Classroom limits its characters)."""
    tail = source_id if _PLAIN_ID.fullmatch(source_id) else "h" + hashlib.sha256(source_id.encode()).hexdigest()[:24]
    return f"d:{ALIAS_PREFIX}{tail}"


def _managed_alias(aliases: Iterable[str]) -> Optional[str]:
    """The GamGUI alias among a class's aliases, as ``d:gg-…`` (GAM may print it with or without the ``d:`` scope)."""
    for alias in aliases:
        bare = alias[2:] if alias.startswith("d:") else alias
        if bare.startswith(ALIAS_PREFIX):
            return "d:" + bare
    return None


def directory_from(users: Iterable[object]) -> Dict[str, str]:
    """Every address of every account, lowercased, to the account's primary address: the primary itself, its aliases and
    its non-editable aliases (alias-domain addresses). ``users`` are `GAMUser`s from a `print users` that asked for
    ``aliases`` (GAM then reads both kinds). A primary is always its own account's, whatever an alias says."""
    out: Dict[str, str] = {}
    primaries: Dict[str, str] = {}
    for user in users:
        primary = str(getattr(user, "primary_email", "") or "").strip().lower()
        if not primary:
            continue
        primaries[primary] = primary
        for alias in [*getattr(user, "aliases", ()), *getattr(user, "non_editable_aliases", ())]:
            address = str(alias).strip().lower()
            if address:
                out.setdefault(address, primary)
    out.update(primaries)
    return out


@dataclass(frozen=True)
class Managed:
    """A Classroom class as read: the class, its aliases, and who is in it (addresses, lowercased)."""
    course: Course
    aliases: Tuple[str, ...]
    teachers: Tuple[str, ...]
    students: Tuple[str, ...]

    @classmethod
    def from_read(cls, course: Course, aliases: Sequence[str], roster: Roster) -> "Managed":
        return cls(course, tuple(aliases), tuple(sorted(p.email for p in roster.teachers if p.email)),
                   tuple(sorted(p.email for p in roster.students if p.email)))


def managed_from(course_records: Iterable[Dict[str, object]], participant_records: Iterable[Dict[str, object]]
                 ) -> Tuple[List[Managed], List[str]]:
    """Every class from the two reads (`print courses … aliases` and every class's participants), and the IDs of classes
    whose aliases or roster couldn't be read: those are never compared as if empty (the plan says so instead)."""
    rosters: Dict[str, Roster] = {}
    for rec in participant_records:
        course_id = str(rec.get("courseId") or "").strip()
        if course_id:
            rosters[course_id] = roster_from([rec], course_id)
    out: List[Managed] = []
    unread: List[str] = []
    for rec in course_records:
        course = Course.from_json(rec)
        if course is None:
            continue
        aliases = _aliases(rec.get("JSON-aliases"))
        roster = rosters.get(course.id)
        if aliases is None or roster is None or roster.unreadable or roster.emails_hidden:
            unread.append(course.id)
            continue
        out.append(Managed.from_read(course, aliases, roster))
    return out, unread


def _aliases(raw: object) -> Optional[List[str]]:
    """A class's aliases from its `JSON-aliases` cell, or None when the cell can't be read. GAM dumps the Classroom API's
    items as they come, each an object, ``[{"alias": "d:gg-C1"}]`` (doPrintCourses' formatjson branch, read from the
    7.51.00 build): reading them as strings once made every GamGUI class look hand-made, and the mock had agreed. A bare
    string is read too; any other item makes the cell unread, never an alias."""
    try:
        items = json.loads(raw) if isinstance(raw, str) and raw else []
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    out: List[str] = []
    for item in items:
        value = item.get("alias") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value:
            return None
        out.append(value)
    return out


@dataclass(frozen=True)
class Change:
    kind: str
    alias: str             # the class, by its GamGUI alias (GAM takes ``d:…`` as a course ID)
    title: str             # the class as the screen names it
    person: str = ""       # the address added or removed, or the new owner
    before: str = ""       # what Classroom has now (a name, an owner)
    after: str = ""        # what the SIS says
    course_id: str = ""    # Classroom's ID, when the class exists
    # A removal that must run before the adds: the person changes role in this class (teacher ↔ student), and Classroom
    # refuses to add someone already in it, so add-then-remove would leave them out of the class altogether.
    swap: bool = False
    # A student add that must wait for the teacher removals: the class's outgoing owner becomes a student, and Classroom
    # won't remove an owner until the owner change has run.
    late: bool = False
    name: str = ""         # a create's or rename's new name and section, as their own fields (a name may hold " · ")
    section: str = ""


@dataclass
class ChangeList:
    changes: List[Change] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    flags: Dict[str, str] = field(default_factory=dict)       # kind → why it's held back, in a sentence
    notes: List[str] = field(default_factory=list)            # SIS classes that can't be planned, said once each kind
    managed: int = 0                                          # active Classroom classes GamGUI manages (not archived)
    teachers: int = 0                                         # teachers in them now
    enrolled: int = 0                                         # people in them now
    unmanaged_ignored: int = 0                                # Classroom classes without a GamGUI alias: left alone
    incomplete: bool = False                                  # a class couldn't be read: never handed over

    def summary(self) -> str:
        """The size in one sentence, destructive parts first (plan C3.2)."""
        c = self.counts

        def count(n: int, one: str, many: str) -> str:
            return f"{n:,} {one if n == 1 else many}"

        removed = c.get("remove_teacher", 0) + c.get("remove_student", 0)
        added = c.get("add_teacher", 0) + c.get("add_student", 0)
        destructive = [verb + " " + count(n, one, many) for verb, n, one, many in (
            ("archive", c.get("archive", 0), "class", "classes"), ("remove", removed, "person", "people")) if n]
        rest = [verb + " " + count(n, one, many) for verb, n, one, many in (
            ("create", c.get("create", 0), "class", "classes"), ("add", added, "person", "people"),
            ("rename", c.get("rename", 0), "class", "classes"), ("change the owner of", c.get("owner", 0), "class", "classes"))
            if n]
        if not destructive and not rest:
            if self.incomplete:
                return "Nothing could be planned: part of Classroom couldn't be read. Compare again."
            if self.notes:
                return "Nothing GamGUI can plan; the notes below need attention."
            return "Classroom already matches the roster: nothing to change."

        def listed(items: List[str]) -> str:
            return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

        parts = [listed(p) for p in (destructive, rest) if p]
        return "Would " + "; would ".join(parts) + "."


def _owner_of(teachers: Sequence[Tuple[str, bool]]) -> Optional[str]:
    """The SIS's primary teacher's address, else the first teacher's by address."""
    primary = sorted(email for email, is_primary in teachers if is_primary)
    return primary[0] if primary else (min(email for email, _ in teachers) if teachers else None)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def plan(snap: Snapshot, classroom: Sequence[Managed], directory: Optional[Mapping[str, str]] = None,
         unread: int = 0) -> ChangeList:
    """The change list that would make Classroom match ``snap``. ``directory`` is every address of the domain's accounts
    to its account's primary (``directory_from``), read fresh, when known: a SIS address is planned as its account's
    primary, and a SIS person who isn't a Workspace account can't be *added*, and is noted instead. It never causes a
    removal: someone the SIS lists is never removed from a class because the directory didn't name them. Unknown (None),
    a class whose roster names an address Classroom doesn't list in it gets no removal and no owner change."""
    if directory is not None and not isinstance(directory, Mapping):
        # A set of addresses can't say whose alias an address is; read as a directory, an alias would plan a removal.
        raise TypeError("directory maps each address to its account's primary address (directory_from)")
    out = ChangeList()
    by_alias: Dict[str, Managed] = {}
    unmanaged: Dict[Tuple[str, str], int] = {}
    for managed in classroom:
        alias = _managed_alias(managed.aliases)
        if alias is None:
            out.unmanaged_ignored += 1
            key = (managed.course.name.strip().lower(), managed.course.section.strip().lower())
            unmanaged[key] = unmanaged.get(key, 0) + 1
            continue
        by_alias.setdefault(alias, managed)
    active = [m for m in by_alias.values() if m.course.state != "ARCHIVED"]
    out.managed = len(active)
    out.teachers = sum(len(m.teachers) for m in active)
    out.enrolled = sum(len(m.teachers) + len(m.students) for m in active)

    roster: Dict[str, Dict[str, List[Tuple[str, bool]]]] = {}
    for e in snap.enrollments:
        person = snap.people.get(e.person_id)
        if person is not None:
            roster.setdefault(e.class_id, {"teacher": [], "student": []})[e.role].append((person.email, e.primary))

    no_teacher = no_account_teacher = archived_wanted = maybe_existing = owner_unread = stays_teacher = unmatched = 0
    not_accounts: Set[str] = set()
    aliased: Set[str] = set()

    def accounts(rows: Sequence[Tuple[str, bool]]) -> Tuple[List[Tuple[str, bool]], Set[str]]:
        """The rows as their accounts' primary addresses, and the addresses that are no account (kept as the SIS has
        them: still never removed). Without the directory, every address is taken as it is."""
        found: List[Tuple[str, bool]] = []
        missing: Set[str] = set()
        for email, primary in rows:
            account = email if directory is None else directory.get(email)
            if account is None:
                missing.add(email)
                continue
            if account != email:
                aliased.add(email)
            found.append((account, primary))
        return found, missing
    wanted_aliases: Set[str] = set()
    for class_id in sorted(snap.classes):
        klass = snap.classes[class_id]
        alias = alias_for(class_id)
        wanted_aliases.add(alias)
        members = roster.get(class_id, {"teacher": [], "student": []})
        # Who can be added: a Workspace account, by its primary address, when the directory is known.
        teacher_rows, no_account_t = accounts(members["teacher"])
        student_rows, no_account_s = accounts(members["student"])
        teachers = sorted({email for email, _ in teacher_rows})
        students = sorted({email for email, _ in student_rows} - set(teachers))
        # Who the SIS has: what a removal is measured against, whatever the directory says.
        sis_teachers = set(teachers) | no_account_t
        sis_students = (set(students) | no_account_s) - sis_teachers
        not_accounts.update(no_account_t | no_account_s)
        owner = _owner_of(teacher_rows)
        section = (snap.terms[klass.term_ids[0]].title if klass.term_ids and klass.term_ids[0] in snap.terms else "")
        title = klass.title or class_id
        current = by_alias.get(alias)
        if current is None:
            if owner is None:
                if sis_teachers:
                    no_account_teacher += 1
                else:
                    no_teacher += 1
                continue
            if unmanaged.get((title.strip().lower(), section.strip().lower())):
                # Classroom has a class by this name and section without GamGUI's alias. GAM prints [] for a class whose
                # alias read failed (it swallows the error, exit 0), so this may be the class itself: never a duplicate.
                maybe_existing += 1
                continue
            out.changes.append(Change("create", alias, title, person=owner, after=f"{title} · {section}".rstrip(" ·"),
                                      name=title, section=section))
            out.changes += [Change("add_teacher", alias, title, person=t) for t in teachers if t != owner]
            out.changes += [Change("add_student", alias, title, person=s) for s in students]
            continue
        course = current.course
        if course.state == "ARCHIVED":       # the SIS still has it: reactivating is Classroom's to do, never planned here
            archived_wanted += 1
            continue
        if (course.name, course.section) != (title, section):
            out.changes.append(Change("rename", alias, title, before=f"{course.name} · {course.section}".rstrip(" ·"),
                                      after=f"{title} · {section}".rstrip(" ·"), course_id=course.id, name=title,
                                      section=section))
        have_t, have_s = set(current.teachers), set(current.students)
        gone_t: Set[str] = set()
        # Without the directory, an address Classroom doesn't list in this class may be someone in it by another name.
        hold = directory is None and bool((sis_teachers | sis_students) - have_t - have_s)
        if hold:
            unmatched += 1
        elif not course.owner_email:
            # The owner's address wasn't read: an owner change can't be planned, and a teacher removal might be the owner.
            owner_unread += 1
        else:
            if owner and owner != course.owner_email:
                out.changes.append(Change("owner", alias, title, person=owner, before=course.owner_email, after=owner,
                                          course_id=course.id))
            # The owner stays unless the owner change above hands the class on: then the former one goes, after it.
            keep = {owner} if owner and owner != course.owner_email else {course.owner_email}
            gone_t = have_t - sis_teachers - keep
        # A teacher who becomes a student is removed before the adds (a swap), except the outgoing owner: Classroom won't
        # remove an owner, so that removal waits for the owner change and their student add waits for it (`late`).
        out.changes += [Change("remove_teacher", alias, title, person=t, course_id=course.id,
                               swap=t in set(students) and t != course.owner_email) for t in sorted(gone_t)]
        out.changes += [Change("add_teacher", alias, title, person=t, course_id=course.id) for t in teachers if t not in have_t]
        for s in students:
            if s in have_s:
                continue
            if s in have_t and s not in gone_t:
                stays_teacher += 1          # still a teacher here (the owner, or their removal can't be planned)
                continue
            out.changes.append(Change("add_student", alias, title, person=s, course_id=course.id,
                                      late=s == course.owner_email))
        out.changes += [Change("remove_student", alias, title, person=s, course_id=course.id, swap=s in set(teachers))
                        for s in sorted(have_s - sis_students) if not hold]
    for alias, managed in sorted(by_alias.items()):
        if alias not in wanted_aliases and managed.course.state != "ARCHIVED":
            out.changes.append(Change("archive", alias, managed.course.name or managed.course.id,
                                      before=managed.course.state_words, course_id=managed.course.id))

    # Run order: a role swap's removal first (Classroom refuses to add someone already in the class), then the kinds in order.
    order = {kind: i for i, (kind, _) in enumerate(KINDS)}
    # An outgoing owner's student add (`late`) runs after the teacher removals.
    out.changes.sort(key=lambda c: (-1 if c.swap else order["remove_teacher"] + 0.5 if c.late else order[c.kind],
                                    c.title.lower(), c.alias, c.person))
    for change in out.changes:
        out.counts[change.kind] = out.counts.get(change.kind, 0) + 1
    if no_teacher:
        out.notes.append(f"{_plural(no_teacher, 'SIS class has', 'SIS classes have')} no teacher, so "
                         f"{'it' if no_teacher == 1 else 'they'} can't be created: Classroom needs an owner. Add a teacher in "
                         "the SIS.")
    if no_account_teacher:
        out.notes.append(f"{_plural(no_account_teacher, 'SIS class', 'SIS classes')} can't be created: no teacher of "
                         f"{'it' if no_account_teacher == 1 else 'theirs'} has a Google Workspace account here.")
    if not_accounts:
        n = len(not_accounts)
        out.notes.append(f"{_plural(n, 'person', 'people')} in the roster {'has' if n == 1 else 'have'} no Google Workspace "
                         "account here, so they aren't added (nor removed). Check their addresses in users.csv, or create "
                         "their accounts.")
    if aliased:
        n = len(aliased)
        out.notes.append(f"{_plural(n, 'address', 'addresses')} in the roster {'is' if n == 1 else 'are'} another address "
                         f"(an alias) of a Google Workspace account, so {'it is' if n == 1 else 'they are'} planned as "
                         "that account's primary address. Update them in users.csv.")
    if unmatched:
        out.notes.append(f"{_plural(unmatched, 'class lists', 'classes list')} roster addresses Classroom doesn't have in "
                         f"{'it' if unmatched == 1 else 'them'}, and without the directory GamGUI can't tell whether one is "
                         "another address (an alias) of someone who is, so no one is removed from "
                         f"{'it' if unmatched == 1 else 'them'} and no owner change is planned. Compare again.")
    if maybe_existing:
        out.notes.append(f"{_plural(maybe_existing, 'SIS class matches', 'SIS classes match')} a Classroom class by name "
                         "and section that doesn't carry GamGUI's alias (or whose aliases couldn't be read), so nothing is "
                         "planned for them rather than a second copy. Check them in Classroom.")
    if stays_teacher:
        out.notes.append(f"{_plural(stays_teacher, 'person is a student', 'people are students')} in the SIS but still "
                         f"{'a teacher' if stays_teacher == 1 else 'teachers'} in Classroom (the class's owner, or in a class "
                         "where no teacher removal could be planned), so they aren't added as students there. Change the "
                         "owner in the SIS or in Classroom first.")
    if owner_unread:
        out.notes.append(f"{_plural(owner_unread, 'class', 'classes')}' owner couldn't be read, so no teacher is removed from "
                         f"{'it' if owner_unread == 1 else 'them'} and no owner change is planned.")
    if archived_wanted:
        out.notes.append(f"{_plural(archived_wanted, 'class', 'classes')} the SIS still has "
                         f"{'is' if archived_wanted == 1 else 'are'} archived in Classroom, so nothing is planned for "
                         f"{'it' if archived_wanted == 1 else 'them'}. Restore {'it' if archived_wanted == 1 else 'them'} "
                         "in Classroom if the SIS is right.")
    if unread:
        out.incomplete = True
        out.notes.insert(0, f"{_plural(unread, 'Classroom class', 'Classroom classes')} couldn't be read whole, so this "
                         "change list may be incomplete and isn't handed over. Compare again.")
    out.flags = _flags(out)
    return out


def _flags(out: ChangeList) -> Dict[str, str]:
    """Each destructive kind past both its count and its share of what's there now, with why, in a sentence."""
    flags: Dict[str, str] = {}
    for kind, (count_limit, share_limit) in LIMITS.items():
        n = out.counts.get(kind, 0)
        base, noun, whole = {
            "archive": (out.managed, "classes", "of the active classes GamGUI manages"),
            "remove_teacher": (out.teachers, "teachers", "of the teachers in them"),
            "remove_student": (out.enrolled, "students", "of the people in them"),
        }[kind]
        if n > count_limit and base and n / base > share_limit:
            what = "archive" if kind == "archive" else "remove"
            flags[kind] = (f"Would {what} {n:,} {noun}, {n / base:.1%} {whole} (held back past {count_limit:,} and "
                           f"{share_limit:.0%}). Check the export is complete before including it.")
    return flags
