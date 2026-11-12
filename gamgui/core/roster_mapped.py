"""Read one spreadsheet of any shape into the roster snapshot, by the operator's column map (plan 2026-11-11, C5).

For a SIS that exports neither OneRoster nor Clever: one flat CSV, a row per person in a class, mapped by its columns
(which column holds the class's ID, its name, the person's address, their role…). ``roster.read_upload`` raises
``NeedsMap`` for one unknown CSV; the screen shows its header, ``guess`` fills the map it can, ``preview`` shows the first
rows as the map reads them, and ``read_mapped`` reads the whole file into the same ``Snapshot`` every reader fills.

The file is hostile until read, as every roster upload is: each row goes through ``roster._rows``, so the same caps (lines,
fields, rows, columns), the NUL refusal and strict UTF-8 apply, and only the mapped columns are ever built from a row. A
person's ID is their address (a flat sheet has no person ID), so an address is checked whole (``roster._one_address``),
never cleaned into a different one. The map itself holds column names and words only, never a row's data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import roster
from .roster import Class, Enrollment, PackageError, Person, Snapshot, Term

# (field, label, required): the map's fields, in the order the screen shows them.
FIELDS: Tuple[Tuple[str, str, bool], ...] = (
    ("class_id", "Class ID", True),
    ("class_name", "Class name", True),
    ("section", "Section or term", False),
    ("email", "Email address", True),
    ("role", "Role", True),
    ("primary", "Primary teacher", False),
)
_FIELD_NAMES = tuple(f for f, _, _ in FIELDS)
_LABELS = {f: label for f, label, _ in FIELDS}

SOURCE = "Spreadsheet (column map)"
_LAYOUT = "Check the column map against the spreadsheet's header."
_TEXT_CAP = 200          # a column name or a word, as kept in a map
_WORDS_CAP = 20          # words per role
_TRUTHY = frozenset({"true", "yes", "y", "1", "x", "primary"})

# Header spellings each field is guessed from, best first. Compared with case, spaces, underscores and hyphens ignored.
SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "class_id": ("class id", "section id", "course section id", "class code", "classid", "class sourcedid"),
    "class_name": ("class name", "class", "course name", "course title", "section name", "title", "class title"),
    "section": ("section", "term", "period", "semester"),
    "email": ("email", "email address", "e-mail", "student email", "user email", "mail", "person email"),
    "role": ("role", "type", "user type", "person type", "user role"),
    "primary": ("primary", "primary teacher", "lead", "is primary", "lead teacher"),
}


def _norm(text: str) -> str:
    return "".join(c for c in text.lower() if c not in " _-\t")


def _words(raw: Any, default: Tuple[str, ...]) -> Tuple[str, ...]:
    """A role's words from stored or posted JSON: strings only, lowercased and stripped, deduplicated, at most
    ``_WORDS_CAP``; anything else (or nothing left) is the default, so a junk record never raises."""
    if not isinstance(raw, (list, tuple)):
        return default
    out: List[str] = []
    for word in raw:
        if isinstance(word, str):
            word = word.strip().lower()[:_TEXT_CAP]
            if word and word not in out:
                out.append(word)
        if len(out) >= _WORDS_CAP:
            break
    return tuple(out) or default


@dataclass(frozen=True)
class ColumnMap:
    """Which header column holds each field ("" = not used), and the role column's words for a teacher and a student."""
    class_id: str = ""
    class_name: str = ""
    section: str = ""
    email: str = ""
    role: str = ""
    primary: str = ""
    teacher_words: Tuple[str, ...] = ("teacher",)
    student_words: Tuple[str, ...] = ("student",)

    def column(self, name: str) -> str:
        return str(getattr(self, name))

    @property
    def columns(self) -> List[str]:
        """The mapped columns, each once (one column may hold two fields), in field order."""
        return list(dict.fromkeys(c for c in (self.column(f) for f in _FIELD_NAMES) if c))

    def role_of(self, word: str) -> str:
        """What a role cell means under this map: "teacher", "student" or "" (neither)."""
        word = word.strip().lower()
        if word and word in {w.lower() for w in self.teacher_words}:
            return "teacher"
        if word and word in {w.lower() for w in self.student_words}:
            return "student"
        return ""

    def problem(self, header: Sequence[str]) -> str:
        """Why this map can't read a file with ``header``, in one sentence; "" when it can."""
        present = {h.strip() for h in header}
        for name, label, required in FIELDS:
            if required and not self.column(name):
                return f"Choose the column that holds the {label.lower()}."
        for name, label, _ in FIELDS:
            col = self.column(name)
            if col and col not in present:
                return f"The column “{col[:80]}” (for the {label.lower()}) isn't in the spreadsheet's header."
        teachers = {w.strip().lower() for w in self.teacher_words if w.strip()}
        students = {w.strip().lower() for w in self.student_words if w.strip()}
        if not teachers:
            return "Give at least one word the role column uses for a teacher."
        if not students:
            return "Give at least one word the role column uses for a student."
        both = sorted(teachers & students)
        if both:
            return f"“{both[0][:80]}” can't mean both a teacher and a student."
        return ""

    def as_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {name: self.column(name) for name in _FIELD_NAMES}
        out["teacher_words"] = list(self.teacher_words)
        out["student_words"] = list(self.student_words)
        return out

    @classmethod
    def from_json(cls, data: Any) -> "ColumnMap":
        """A map from stored or posted JSON. Never raises: a field that isn't a string is "" (not used), a word list that
        isn't one is the default, and every string is capped."""
        if not isinstance(data, Mapping):
            data = {}

        def text(key: str) -> str:
            value = data.get(key, "")
            return value.strip()[:_TEXT_CAP] if isinstance(value, str) else ""

        return _build({name: text(name) for name in _FIELD_NAMES}, _words(data.get("teacher_words"), ("teacher",)),
                      _words(data.get("student_words"), ("student",)))


def _build(cols: Dict[str, str], teacher_words: Tuple[str, ...] = ("teacher",),
           student_words: Tuple[str, ...] = ("student",)) -> ColumnMap:
    get = cols.get
    return ColumnMap(get("class_id", ""), get("class_name", ""), get("section", ""), get("email", ""), get("role", ""),
                     get("primary", ""), teacher_words, student_words)


def guess(header: Sequence[str]) -> ColumnMap:
    """The map a header's own names suggest. A column is guessed for one field at most; a field with no column named for
    it is left "" for the operator to choose, never filled with a column another field already took."""
    by_norm: Dict[str, str] = {}
    for h in header:
        h = h.strip()
        if h and _norm(h) not in by_norm:
            by_norm[_norm(h)] = h
    taken: Set[str] = set()
    chosen: Dict[str, str] = {}
    for name in _FIELD_NAMES:
        for synonym in SYNONYMS[name]:
            col = by_norm.get(_norm(synonym))
            if col and col not in taken:
                chosen[name] = col
                taken.add(col)
                break
    return _build(chosen)


def _mapped(cmap: ColumnMap, row: Dict[str, str]) -> Dict[str, str]:
    """A row by field. ``_rows`` keeps the email column raw (an address is checked whole, never cleaned), so every other
    field is cleaned here, by field: one column may be mapped to two fields (the email column as a class ID too), and a
    control or invisible character must never reach a class's ID or title through it."""
    out = {name: row.get(cmap.column(name), "") if cmap.column(name) else "" for name in _FIELD_NAMES}
    return {name: value if name == "email" else roster._clean(value) for name, value in out.items()}


def preview(name: str, body: bytes, cmap: ColumnMap, n: int = 3) -> List[Dict[str, str]]:
    """The first ``n`` non-blank rows as ``cmap`` reads them, with "as" (what the role word means: "teacher", "student" or
    ""). Works on a map still being filled in (an unmapped or absent column reads as ""), and stops after ``n`` rows:
    the rest of the file isn't read."""
    base = roster._base(name)
    out: List[Dict[str, str]] = []
    if n <= 0:
        return out
    rows = roster._rows(base, body, required=(), used=cmap.columns, layout=_LAYOUT, raw=(cmap.email,))
    try:
        for _, row in rows:
            mapped = _mapped(cmap, row)
            mapped["email"] = mapped["email"].strip().lower()      # shown only; read_mapped checks it as given
            mapped["as"] = cmap.role_of(mapped["role"])
            out.append(mapped)
            if len(out) >= n:
                break
    finally:
        rows.close()
    return out


def read_mapped(name: str, body: bytes, cmap: ColumnMap) -> Snapshot:
    """The whole spreadsheet as a snapshot. ``PackageError`` with the map's problem when it can't read this header; every
    row-level problem is an ``Issue`` (see ``roster.ISSUE_WORDS``), the row left out or its first reading kept."""
    base = roster._base(name)
    header = roster.header_of(base, body)
    problem = cmap.problem(header)
    if problem:
        raise PackageError(problem)
    snap = Snapshot(source=SOURCE)
    sections: Dict[str, str] = {}             # class ID -> the section its first row gave ("" for none)
    seen: Set[Tuple[str, str]] = set()        # (class ID, address) already enrolled
    cols = cmap.columns
    for line, row in roster._rows(base, body, required=cols, used=cols, layout=_LAYOUT, raw=(cmap.email,)):
        r = _mapped(cmap, row)
        class_id = r["class_id"]
        if not class_id:
            snap.issue("no_id", base, line)
            continue
        _class(snap, base, line, class_id, r["class_name"], r["section"], sections)
        given = r["email"]
        if not given:
            snap.issue("no_email", base, line, class_id)
            continue
        if not roster._one_address(given):        # as given: lowercasing first turns a non-ASCII K into an ASCII k
            snap.issue("bad_email", base, line, class_id, given)
            continue
        email = given.lower()
        role = cmap.role_of(r["role"])
        if not role:
            snap.issue("unknown_role", base, line, email, r["role"])
            continue
        if (class_id, email) in seen:
            snap.issue("duplicate_row", base, line, email, class_id)
            continue
        seen.add((class_id, email))
        if email not in snap.people:
            snap.people[email] = Person(email, role, email=email)
        primary = role == "teacher" and r["primary"].strip().lower() in _TRUTHY
        snap.enrollments.append(Enrollment(class_id, email, role, primary))
    # Someone who teaches one class and is a student in another is counted as a teacher (each enrolment keeps its role).
    teaching = {e.person_id for e in snap.enrollments if e.role == "teacher"}
    for pid in teaching:
        if snap.people[pid].role != "teacher":
            snap.people[pid] = Person(pid, "teacher", email=pid)
    return snap


def _class(snap: Snapshot, base: str, line: int, class_id: str, title: str, section: str, sections: Dict[str, str]) -> None:
    """Keep a class from its first row; a later row that names it differently, or gives another section, is a conflict
    (the first kept). A class with no name is said once."""
    known: Optional[Class] = snap.classes.get(class_id)
    if known is None:
        term_ids: Tuple[str, ...] = ()
        if section:
            term_id = "section:" + section
            snap.terms.setdefault(term_id, Term(term_id, section))
            term_ids = (term_id,)
        snap.classes[class_id] = Class(class_id, title, "", "", term_ids)
        sections[class_id] = section
        if not title:
            snap.issue("no_title", base, line, class_id)
        return
    if title and title != known.title:
        snap.issue("conflict", base, line, class_id, f"name “{title}”, first “{known.title}”")
    if section and section != sections[class_id]:
        snap.issue("conflict", base, line, class_id, f"section “{section}”, first “{sections[class_id]}”")
