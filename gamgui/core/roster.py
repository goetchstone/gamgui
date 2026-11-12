"""Read a student information system's roster export into one snapshot (plan 2026-11-11, C2.1 and C2.3).

The snapshot is one model every reader fills (schools, terms, courses, classes, people, enrolments), so nothing after it
knows which SIS or format it came from. This module reads an upload (a ZIP, or loose CSV files), tells which export it
is by its file names, and reads OneRoster 1.1 and 1.2 CSV itself. Clever's CSV is `roster_clever`; a spreadsheet of any
other shape is read with the operator's column map by `roster_mapped` (plan C5). Every reader fills the same `Snapshot`,
through the same bounded row reader (`_rows`).

An upload is hostile until read: it comes from outside the app and lands in student data. So:

- **Nothing is extracted and no name is ever a path.** A member is matched by its whole base name against the files
  OneRoster names. A symlink, an encrypted member, `..`, an absolute name, a backslash, more than one level of folder,
  or two members with one name (in any case) refuses the package. (A NUL can't be refused: `zipfile` cuts a name at
  it, so `users.csv\\0../x` arrives as `users.csv`. That's safe only because no name is ever a path.)
- **Sizes are counted, never trusted.** Each member is decompressed into a bounded buffer that stops one byte past its cap,
  whatever its header says; the members together, the ratio of a member's size to its compressed size, the number of
  members, rows, fields and line lengths are all capped.
- **Text is strict UTF-8** (a leading byte-order mark is dropped). Anything else refuses the file by name.
- **A package-level mistake refuses the whole package with its fix** (`PackageError`): not a ZIP, an unknown export, a
  delta export, a file missing or with the wrong columns. Row-level problems are kept as `Issue`s for the SIS team.
- **A file the reader doesn't use is named, never unpacked** (OneRoster 1.2's `userProfiles.csv` and Clever's files hold
  passwords): only the columns a reader uses are ever built from a row.

The reader holds no state and writes nothing: `roster_store` keeps a snapshot, `0600`, for a stated number of days.
"""
from __future__ import annotations

import csv
import io
import itertools
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Collection, Dict, FrozenSet, Generator, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

# --- caps (#9) -------------------------------------------------------------------------------------------------------
UPLOAD_CAP = 200 * 1024 * 1024       # the bytes picked, a ZIP or the CSVs together
MEMBER_CAP = 256 * 1024 * 1024       # one file, decompressed
TOTAL_CAP = 768 * 1024 * 1024        # every file, decompressed
RATIO_CAP = 200                      # a member's size over its compressed size: past it, a decompression bomb
MEMBERS_CAP = 64                     # entries in a ZIP, counting the ones ignored
CENTRAL_DIRECTORY_CAP = 64 * 1024    # a ZIP's list of entries, in bytes: MEMBERS_CAP entries with long names fit easily
MANIFEST_CAP = 64 * 1024             # manifest.csv: a real one is under 2 KB
COLUMNS_CAP = 256                    # columns in a file's header (OneRoster's have about 20)
LINE_CAP = 2 * 1024 * 1024           # one physical line, before the csv module splits it into fields
ROWS_CAP = 2_000_000                 # rows in one file
FIELD_CAP = 64 * 1024                # one field
HEADER_CELL_CAP = 200                # a column name a column map can hold (roster_mapped keeps 200 characters)
ISSUE_SAMPLE = 1000                  # rows kept per kind of issue (the Issues CSV); the count is always whole

# OneRoster CSV, as 1EdTech names its files. A rostering package needs the first six (and the manifest); 1.2 also needs
# roles.csv (its users.csv has no role: 1.2 binding, Appendix A).
REQUIRED = ("orgs.csv", "academicSessions.csv", "courses.csv", "classes.csv", "users.csv", "enrollments.csv")
ROLES = "roles.csv"
OPTIONAL = ("demographics.csv", "lineItems.csv", "categories.csv", "results.csv", "resources.csv", "classResources.csv",
            "courseResources.csv", "userResources.csv", "userProfiles.csv", "scoreScales.csv", "lineItemScoreScales.csv",
            "resultScoreScales.csv", "lineItemLearningObjectiveIds.csv", "resultLearningObjectiveIds.csv")
MANIFEST = "manifest.csv"
KNOWN = {name.lower(): name for name in (MANIFEST, *REQUIRED, ROLES, *OPTIONAL)}
# What macOS adds to a ZIP it makes: never ours, never read.
_MAC_LITTER = ("__MACOSX/", ".DS_Store", "._")

COLUMNS = {     # the columns each OneRoster 1.1 file must have for this reader; others are allowed and ignored
    "orgs.csv": ("sourcedId", "status", "name", "type"),
    "academicSessions.csv": ("sourcedId", "status", "title", "type", "startDate", "endDate"),
    "courses.csv": ("sourcedId", "status", "title", "orgSourcedId"),
    "classes.csv": ("sourcedId", "status", "title", "courseSourcedId", "schoolSourcedId", "termSourcedIds"),
    "users.csv": ("sourcedId", "status", "role", "email", "givenName", "familyName"),
    "enrollments.csv": ("sourcedId", "status", "classSourcedId", "userSourcedId", "role"),
}
# The columns each file's rows are read for: a row is built from these alone, so its cost doesn't grow with the header.
USED = {
    "orgs.csv": ("sourcedId", "status", "name"),
    "academicSessions.csv": ("sourcedId", "status", "title", "type", "startDate", "endDate"),
    "courses.csv": ("sourcedId", "status", "title", "courseCode", "orgSourcedId"),
    "classes.csv": ("sourcedId", "status", "title", "courseSourcedId", "schoolSourcedId", "termSourcedIds", "classCode",
                    "location"),
    "users.csv": ("sourcedId", "status", "role", "email", "givenName", "familyName"),
    "enrollments.csv": ("sourcedId", "status", "classSourcedId", "userSourcedId", "role", "primary"),
}
# OneRoster 1.2: users.csv lost `role` (and `orgSourcedIds`) to roles.csv; every other file read keeps its 1.1 columns.
COLUMNS_12 = {**COLUMNS, "users.csv": ("sourcedId", "status", "email", "givenName", "familyName"),
              ROLES: ("sourcedId", "status", "userSourcedId", "roleType", "role")}
USED_12 = {**USED, "users.csv": ("sourcedId", "status", "email", "givenName", "familyName"),
           ROLES: ("sourcedId", "status", "userSourcedId", "roleType", "role")}
ROSTERED_ROLES = ("teacher", "student")      # the people a class's roster holds; others are counted, not rostered
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")   # a ZIP's first member, or an empty ZIP's end record


class PackageError(Exception):
    """The upload can't be read as a roster at all. ``str()`` says what's wrong and how to fix it, in words."""


class NeedsMap(Exception):
    """One CSV that isn't a known export: a spreadsheet the operator maps by its columns (`roster_mapped`). Holds the file
    as picked; nothing about it has been trusted yet."""

    def __init__(self, name: str, body: bytes) -> None:
        super().__init__("This spreadsheet needs a column map.")
        self.name, self.body = name, body


@dataclass(frozen=True)
class Layout:
    """An export's files: the ones it may hold, by lowercased name, and the ones it holds that no reader uses (named and
    checked, never unpacked: each could hold its whole cap, and some hold passwords)."""
    label: str                          # "OneRoster", in "isn't a OneRoster file"
    known: Dict[str, str]
    skip: FrozenSet[str] = frozenset()


ONEROSTER = Layout("OneRoster", KNOWN, frozenset(OPTIONAL))


# --- the snapshot ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class School:
    id: str
    name: str


@dataclass(frozen=True)
class Term:
    id: str
    title: str
    kind: str = ""
    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    code: str = ""
    school_id: str = ""


@dataclass(frozen=True)
class Class:
    id: str
    title: str
    course_id: str
    school_id: str
    term_ids: Tuple[str, ...] = ()
    code: str = ""
    location: str = ""


@dataclass(frozen=True)
class Person:
    id: str
    role: str            # "teacher" or "student", lowercased
    email: str = ""      # lowercased
    given: str = ""
    family: str = ""


@dataclass(frozen=True)
class Enrollment:
    class_id: str
    person_id: str
    role: str            # "teacher" or "student"
    primary: bool = False


@dataclass(frozen=True)
class Issue:
    code: str
    file: str
    row: int             # the CSV line, counting the header as line 1
    source_id: str = ""
    detail: str = ""


# Each kind of issue: what it means and what to change, in the SIS team's words.
ISSUE_WORDS: Dict[str, Tuple[str, str]] = {
    "skipped": ("Marked to be deleted in the export, so left out.", "Nothing to do, unless it should still be there."),
    "no_id": ("A row has no ID.", "Every row needs the SIS's ID for it (sourcedId, or the file's _id column)."),
    "duplicate_id": ("Two rows share one ID; the first is kept.", "Each ID must be unique in its file."),
    "no_email": ("A teacher or student has no email address.",
                 "Give them their Google Workspace address in the export's email column."),
    "bad_email": ("An email address isn't one address.", "Fix the email column: one plain address, no spaces."),
    "duplicate_email": ("Two people share one email address; the first is kept.", "Each person needs their own address."),
    "unknown_class": ("An enrolment names a class that isn't in the export.",
                      "Include the class, or drop the enrolment."),
    "unknown_person": ("An enrolment names a person who isn't in the export (or has no address).",
                       "Include the person with their address, or drop the enrolment."),
    "role_mismatch": ("An enrolment's role isn't a role the person has in the export.", "Make the roles agree."),
    "unknown_course": ("A class names a course that isn't in the export.", "Include the course."),
    "unknown_school": ("A class names a school that isn't in the export.", "Include the school."),
    "unknown_term": ("A class names a term that isn't in the export.", "Include the term."),
    "no_title": ("A class or course has no title.", "Give it a title; it becomes the class's name in Classroom."),
    "conflict": ("Rows for one ID disagree (a name, a title or an address); the first is kept.", "Make the rows agree."),
    "duplicate_row": ("The same person is listed in the same class twice; the first is kept.",
                      "Nothing to do, unless the two rows should differ."),
    "unknown_role": ("A row's role isn't one of the words the column map gives for a teacher or a student.",
                     "Add the word to the map, or fix the role in the spreadsheet."),
}


@dataclass
class Snapshot:
    source: str                     # the format it came from, e.g. "OneRoster 1.1 CSV"
    schools: Dict[str, School] = field(default_factory=dict)
    terms: Dict[str, Term] = field(default_factory=dict)
    courses: Dict[str, Course] = field(default_factory=dict)
    classes: Dict[str, Class] = field(default_factory=dict)
    people: Dict[str, Person] = field(default_factory=dict)
    enrollments: List[Enrollment] = field(default_factory=list)
    issue_counts: Dict[str, int] = field(default_factory=dict)
    issue_sample: List[Issue] = field(default_factory=list)
    others: int = 0                 # people in roles a class's roster doesn't hold (aides, guardians…), counted only

    def issue(self, code: str, file: str, row: int, source_id: str = "", detail: str = "") -> None:
        """Count one issue and keep it while its kind has fewer than ``ISSUE_SAMPLE`` kept (#9)."""
        self.issue_counts[code] = self.issue_counts.get(code, 0) + 1
        if self.issue_counts[code] <= ISSUE_SAMPLE:
            self.issue_sample.append(Issue(code, file, row, source_id, detail[:200]))

    @property
    def counts(self) -> Dict[str, int]:
        teachers = sum(1 for p in self.people.values() if p.role == "teacher")
        return {"schools": len(self.schools), "terms": len(self.terms), "courses": len(self.courses),
                "classes": len(self.classes), "teachers": teachers, "students": len(self.people) - teachers,
                "enrollments": len(self.enrollments)}


# --- reading the files out of an upload -----------------------------------------------------------------------------
def _ignored(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return name.startswith(_MAC_LITTER[0]) or "/__MACOSX/" in name or base == _MAC_LITTER[1] or base.startswith(_MAC_LITTER[2])


def _member_name(name: str) -> str:
    """A ZIP member's file name, at the root or in one enclosing folder (Finder's Compress makes one). Refuses anything that
    is or could become a path out."""
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or ":" in name.split("/")[0]:
        raise PackageError(f"The ZIP holds a file named “{name[:80]}”, which isn't a plain file name. Export the roster again.")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts[:-1]) or parts[-1] in (".", ".."):
        raise PackageError(f"The ZIP holds a file named “{name[:80]}”, which isn't a plain file name. Export the roster again.")
    if len(parts) > 2:
        raise PackageError("The CSV files are inside folders within folders. Put them at the top of the ZIP, or in one "
                           "folder, and try again.")
    return parts[-1]


def _bounded(stream, cap: int, label: str) -> bytes:
    """Read ``stream`` to its end, refusing at one byte past ``cap`` whatever any header said."""
    out = io.BytesIO()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return out.getvalue()
        out.write(chunk)
        if out.tell() > cap:
            raise PackageError(f"{label} is larger than {cap >> 20} MB once unpacked, more than any roster file should be.")


def _refuse_a_huge_directory(data: bytes) -> None:
    """Refuse a ZIP whose central directory is past ``CENTRAL_DIRECTORY_CAP`` bytes, or lists more than ``MEMBERS_CAP``
    entries, before `zipfile` parses it: `zipfile` builds an object per entry by walking the directory's bytes, whatever the
    count says, so millions of tiny entries cost gigabytes before the entry cap could refuse them.

    The end record is read with `zipfile`'s own reader (`_EndRecData`), which reads only the archive's tail: a hand-written
    one was walked around twice (a ZIP64 record `zipfile` honours whatever the 32-bit field says, and a signature planted
    inside a field), so the size checked is exactly the size `zipfile` will walk. If a Python ever drops that private
    function, the ZIP is refused rather than read unchecked (`tests/test_roster.py` holds the function to this Python)."""
    end_record = getattr(zipfile, "_EndRecData", None)
    size_at, entries_at = getattr(zipfile, "_ECD_SIZE", None), getattr(zipfile, "_ECD_ENTRIES_TOTAL", None)
    if end_record is None or size_at is None or entries_at is None:
        raise PackageError("This Python can't check a ZIP's size before reading it, so the ZIP isn't read. Pick the CSV "
                           "files instead.")
    try:
        record = end_record(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — a malformed tail: zipfile refuses it next, as not a ZIP
        return
    if not record:
        return
    if record[size_at] > CENTRAL_DIRECTORY_CAP or record[entries_at] > MEMBERS_CAP:
        raise PackageError("The ZIP lists far more entries than a roster export holds. Check it's the right file.")


def _open_zip(data: bytes) -> zipfile.ZipFile:
    _refuse_a_huge_directory(data)
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — BadZipFile, ValueError, OSError, NotImplementedError (an unknown version), …
        raise PackageError("That isn't a ZIP file. Choose the ZIP your SIS exported, or its CSV files.") from exc


def zip_names(data: bytes) -> List[str]:
    """The base names of a ZIP's files, lowercased, read from its directory alone (nothing unpacked): enough to tell which
    export it is (`detect`). Every name is checked as `files_from_zip` checks it."""
    with _open_zip(data) as archive:
        infos = archive.infolist()
        if len(infos) > MEMBERS_CAP:
            raise PackageError(f"The ZIP holds {len(infos)} entries; a roster export holds a dozen or so. Check it's the "
                               "right file.")
        return [_member_name(i.filename).lower() for i in infos if not _ignored(i.filename) and not i.filename.endswith("/")]


def files_from_zip(data: bytes, layout: Layout = ONEROSTER) -> Dict[str, bytes]:
    """The roster files in a ZIP, by their name in ``layout``. Unknown files are refused, never ignored: a roster package
    holds only roster files, and a stray one usually means the wrong export. The files no reader uses (``layout.skip``) are
    name-checked but never unpacked: each could hold its whole cap, and some hold passwords."""
    archive = _open_zip(data)
    files: Dict[str, bytes] = {}
    skipped: set = set()
    folders: set = set()
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MEMBERS_CAP:
            raise PackageError(f"The ZIP holds {len(infos)} entries; a roster export holds a dozen or so. Check it's the "
                               "right file.")
        for info in infos:
            name = info.filename
            if _ignored(name):
                continue
            if name.endswith("/"):           # a folder entry: only the one enclosing folder is allowed (checked below)
                _member_name(name.rstrip("/") + "/x")
                folders.add(name.rstrip("/"))
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if info.create_system == 3 and mode and not stat.S_ISREG(mode):
                raise PackageError(f"“{name[:80]}” in the ZIP is a link or a special file, not a file. Export the roster again.")
            if info.flag_bits & 0x1:
                raise PackageError("The ZIP is password-protected. Export it without a password.")
            base = _member_name(name)
            if "/" in name:
                folders.add(name.split("/")[0])
            canonical = layout.known.get(base.lower())
            if canonical is None:
                raise PackageError(f"The ZIP holds “{base[:80]}”, which isn't a {layout.label} file. Choose the "
                                   f"{layout.label} export itself.")
            if canonical in files or canonical in skipped:
                raise PackageError(f"The ZIP holds {canonical} twice. Export the roster again.")
            if canonical in layout.skip:     # a file no reader uses: named, never unpacked
                skipped.add(canonical)
                continue
            if info.compress_size and info.file_size / info.compress_size > RATIO_CAP:
                raise PackageError(f"{canonical} unpacks to far more than its size in the ZIP. That isn't a normal export.")
            try:
                with archive.open(info) as member:
                    body = _bounded(member, MEMBER_CAP, canonical)
            except PackageError:
                raise
            except Exception as exc:  # noqa: BLE001 — zlib.error, a CRC failure, an unsupported method
                raise PackageError(f"{canonical} couldn't be unpacked from the ZIP. Export the roster again.") from exc
            if info.compress_size and len(body) / max(info.compress_size, 1) > RATIO_CAP:
                raise PackageError(f"{canonical} unpacks to far more than its size in the ZIP. That isn't a normal export.")
            total += len(body)
            if total > TOTAL_CAP:
                raise PackageError(f"The roster is larger than {TOTAL_CAP >> 20} MB unpacked, more than any roster should be.")
            files[canonical] = body
    if len(folders) > 1:
        raise PackageError("The CSV files are in more than one folder. Put them at the top of the ZIP, or in one folder.")
    return files


def _base(raw_name: str) -> str:
    return (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1]


def files_from_csvs(named: Iterable[Tuple[str, bytes]], layout: Layout = ONEROSTER) -> Dict[str, bytes]:
    """The roster files picked one by one (a folder's CSVs), by their name in ``layout``. A picked file's name is matched,
    never used as a path."""
    files: Dict[str, bytes] = {}
    total = 0
    for raw_name, body in named:
        base = _base(raw_name)
        if _ignored(base):
            continue
        canonical = layout.known.get(base.lower())
        if canonical is None:
            raise PackageError(f"“{base[:80]}” isn't a {layout.label} file. Pick the CSV files from the export, or its ZIP.")
        if canonical in files:
            raise PackageError(f"{canonical} was picked twice.")
        if canonical in layout.skip:         # read already (it was uploaded), but never parsed or kept
            continue
        total += len(body)
        if len(body) > MEMBER_CAP or total > TOTAL_CAP:
            raise PackageError("The files are larger than any roster should be.")
        files[canonical] = body
    return files


# --- the package -----------------------------------------------------------------------------------------------------
class _Lines:
    """A file's physical lines for the csv module, decoded as they're read (strict UTF-8; a byte-order mark dropped), never
    held whole: a 255 MB file as one `str` in a `StringIO` was a gigabyte (four bytes a character). Bounded twice, before
    the csv module builds anything from them: a line past ``LINE_CAP`` (millions of commas were millions of fields), and a
    record past ``LINE_CAP`` across lines (a quoted field spanning many short lines was built whole whenever another read
    had raised the process-wide csv field limit). ``record_done`` starts the next record's count."""

    def __init__(self, name: str, body: bytes) -> None:
        self.name = name
        self.stream = io.TextIOWrapper(io.BytesIO(body), encoding="utf-8-sig", newline="")
        self.since = 0
        self.stream_line = 0

    def __iter__(self) -> "_Lines":
        return self

    def __next__(self) -> str:
        try:
            line = self.stream.readline(LINE_CAP + 1)
        except UnicodeDecodeError as exc:
            raise PackageError(f"{self.name} isn't UTF-8 text. Export the roster as UTF-8 CSV.") from exc
        if not line:
            raise StopIteration
        if len(line) > LINE_CAP:
            raise PackageError(f"{self.name} has a line longer than {LINE_CAP >> 20} MB. That isn't a normal export.")
        if "\x00" in line:
            # The same answer on every Python: 3.10's csv module refuses a NUL ("line contains NUL") and later ones read it,
            # so a NUL in an address was a whole-package refusal on one and a row issue on another. No export holds one.
            raise PackageError(f"{self.name} holds a NUL character on or after line {self.stream_line + 1}. That isn't "
                               "a normal export: export the roster again.")
        self.stream_line += line.count("\n") or 1
        self.since += len(line)
        if self.since > LINE_CAP:
            raise PackageError(f"{self.name} has a record longer than {LINE_CAP >> 20} MB. That isn't a normal export.")
        return line

    def record_done(self) -> None:
        self.since = 0


_CONTROL = {chr(c) for c in range(32)} | {"\x7f"}


def _clean(text: str) -> str:
    """A field as kept: control characters (NUL among them) and invisible format characters dropped, ends trimmed."""
    if not any(c in _CONTROL or unicodedata.category(c) == "Cf" for c in text):
        return text.strip()
    return "".join(c for c in text if c not in _CONTROL and unicodedata.category(c) != "Cf").strip()


_OR11 = "That isn't OneRoster 1.1's layout: export OneRoster 1.1 CSV."


def header_of(name: str, body: bytes) -> List[str]:
    """A CSV's header row, stripped, read through the same bounds as its rows (for a column map's choices)."""
    lines = _Lines(name, body)
    try:
        header = next(csv.reader(lines, strict=True), None)
    except csv.Error as exc:
        raise PackageError(f"{name} isn't readable CSV ({exc}).") from exc
    if not header or not any(h.strip() for h in header):
        raise PackageError(f"{name} is empty, or its first line isn't a header.")
    if len(header) > COLUMNS_CAP:
        raise PackageError(f"{name} has {len(header):,} columns, more than any roster file has.")
    header = [h.strip() for h in header]
    if any(len(h) > HEADER_CELL_CAP for h in header):
        raise PackageError(f"{name} has a column name longer than {HEADER_CELL_CAP} characters, so its first line isn't a "
                           "header. Export it with a header row.")
    if any(_one_address(h.lower()) or "@" in h for h in header):
        # A sheet exported without its header: the first line is a person's row. Mapping it would save their address
        # as a "column name" (in a saved map) and drop them from the roster, so it's refused.
        raise PackageError(f"The first line of {name} holds an email address, so it's a row, not a header. Export the "
                           "spreadsheet with its header row (class ID, class name, email, role…).")
    return header


def _rows(name: str, body: bytes, required: Optional[Sequence[str]] = None, used: Optional[Sequence[str]] = None,
          *, fold_case: bool = False, layout: str = _OR11, raw: Collection[str] = ("email",)
          ) -> Generator[Tuple[int, Dict[str, str]], None, None]:
    """Each row of a roster CSV as a dict of the columns ``used`` (default: OneRoster 1.1's for ``name``), keyed as
    ``used`` spells them, with the line it starts on (the header is line 1). A ``required`` column missing refuses the
    file, followed by the ``layout`` sentence. ``fold_case`` matches the header ignoring case (Clever's rule). Columns in
    ``raw`` (addresses) are kept as given, to be checked whole; every other cell has control characters dropped. Bounded:
    lines, columns, fields, rows."""
    required = COLUMNS.get(name, ()) if required is None else required
    used = USED.get(name, required) if used is None else used
    # The csv module's field limit is process-wide and the GAM parser raises it for other reads (parser.field_limit_at_least),
    # so this reader never sets it: each cell's length is checked here instead, and `_Lines` bounds each record.
    lines = _Lines(name, body)
    try:
        reader = csv.reader(lines, strict=True)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise PackageError(f"{name} is empty. Export the roster again.") from None
        lines.record_done()
        if len(header) > COLUMNS_CAP:
            raise PackageError(f"{name} has {len(header):,} columns, more than any roster file has. That isn't a normal "
                               "export.")
        keys = [h.lower() for h in header] if fold_case else header
        doubled = sorted({h for h in keys if h and keys.count(h) > 1})
        if doubled:
            raise PackageError(f"{name} has the column {doubled[0]} twice. Export the roster again.")
        find = (lambda c: c.lower()) if fold_case else (lambda c: c)
        missing = [c for c in required if find(c) not in keys]
        if missing:
            raise PackageError(f"{name} is missing the column{'s' if len(missing) > 1 else ''} {', '.join(missing)}. "
                               + layout)
        wanted = [(c, keys.index(find(c))) for c in used if find(c) in keys]
        width = len(header) + 1
        count = 0
        while True:
            start = reader.line_num + 1          # the line this record starts on (a quoted field may span lines)
            try:
                row = next(reader)
            except StopIteration:
                return
            lines.record_done()
            count += 1
            if count > ROWS_CAP:
                raise PackageError(f"{name} has more than {ROWS_CAP:,} rows, more than any roster should.")
            if len(row) > width:
                raise PackageError(f"{name} line {start} has {len(row):,} fields; its header has {len(header)}. Export the "
                                   "roster again.")
            if any(len(cell) > FIELD_CAP for cell in row):
                raise PackageError(f"{name} has a field longer than {FIELD_CAP >> 10} KB on line {start}. That isn't a "
                                   "normal export.")
            if not any(cell.strip() for cell in row):
                continue
            # An address is kept as given and checked (`_one_address`); every other field has its control characters dropped.
            yield start, {key: ((row[i].strip() if key in raw else _clean(row[i])) if i < len(row) else "")
                          for key, i in wanted}
    except csv.Error as exc:
        raise PackageError(f"{name} isn't readable CSV ({exc}). Export the roster again.") from exc


def _manifest(files: Dict[str, bytes]) -> Dict[str, str]:
    if MANIFEST not in files:
        raise PackageError("There's no manifest.csv, so this isn't a OneRoster export. Export OneRoster 1.1 or 1.2 CSV from "
                           "your SIS.")
    if len(files[MANIFEST]) > MANIFEST_CAP:
        raise PackageError("manifest.csv is far larger than a OneRoster manifest. Export the roster again.")
    try:
        rows = list(itertools.islice(csv.reader(_Lines(MANIFEST, files[MANIFEST]), strict=True), 256))
    except csv.Error as exc:
        raise PackageError(f"manifest.csv isn't readable CSV ({exc}).") from exc
    if not rows or [c.strip() for c in rows[0][:2]] != ["propertyName", "value"]:
        raise PackageError("manifest.csv isn't a OneRoster manifest. Export OneRoster 1.1 or 1.2 CSV from your SIS.")
    return {r[0].strip(): r[1].strip() for r in rows[1:256] if len(r) >= 2}


def oneroster_version(manifest: Dict[str, str]) -> str:
    """"1.1" or "1.2" (the 1.2 binding says "1.2"; a SIS writing its revision, "1.2.1", is the same layout)."""
    version = manifest.get("oneroster.version", "")
    if version == "1.1":
        return "1.1"
    if version == "1.2" or version.startswith("1.2."):
        return "1.2"
    raise PackageError(f"The manifest says OneRoster version “{version[:20] or 'none'}”. Export OneRoster 1.1 or 1.2 CSV.")


def check_package(files: Dict[str, bytes]) -> Dict[str, str]:
    """Refuse a package this reader can't take, with its fix: the manifest, the version, a delta export, a missing file."""
    manifest = _manifest(files)
    needed = REQUIRED + ((ROLES,) if oneroster_version(manifest) == "1.2" else ())
    for name in needed:
        mode = manifest.get(f"file.{name[:-4]}", "").lower()
        if mode == "delta":
            raise PackageError(f"This is a changes-only (delta) export: {name} holds changes, not the whole roster. Export a "
                               "full (bulk) export.")
        if mode == "absent" or name not in files:
            raise PackageError(f"The export has no {name}, which a roster needs. Include it in the export (as bulk).")
    return manifest


# --- the rows into a snapshot ---------------------------------------------------------------------------------------
def _live(snap: Snapshot, name: str, line: int, row: Dict[str, str]) -> Optional[str]:
    """The row's sourcedId, or None when the row is left out (marked to be deleted, or no ID)."""
    if row.get("status", "").lower() == "tobedeleted":
        snap.issue("skipped", name, line, row.get("sourcedId", ""))
        return None
    sid = row.get("sourcedId", "")
    if not sid:
        snap.issue("no_id", name, line)
        return None
    return sid


def _one_address(text: str) -> bool:
    """One plain address, printable ASCII only: a control or invisible character is refused, never dropped (dropping one
    could make a different, valid-looking address), and a NUL never reaches an argv."""
    local, at, host = text.partition("@")
    return (bool(local and at and "." in host) and "@" not in host and text.isascii()
            and all(c.isprintable() and not c.isspace() and c not in "<>,;:\"'" for c in text))


def _roles_12(snap: Snapshot, body: bytes) -> Dict[str, Tuple[Set[str], Set[str]]]:
    """OneRoster 1.2's roles.csv: each user's roster roles (teacher, student), as (primary, secondary). Other roles (an
    aide, a guardian, an ``ext:`` one) aren't rostered and aren't kept."""
    out: Dict[str, Tuple[Set[str], Set[str]]] = {}
    for line, row in _rows(ROLES, body, COLUMNS_12[ROLES], USED_12[ROLES], layout=_OR12):
        if _live(snap, ROLES, line, row) is None:
            continue
        role = row.get("role", "").lower()
        if role not in ROSTERED_ROLES:
            continue
        primary, secondary = out.setdefault(row.get("userSourcedId", ""), (set(), set()))
        (primary if row.get("roleType", "").lower() == "primary" else secondary).add(role)
    return out


def _role_12(roles: Optional[Tuple[Set[str], Set[str]]]) -> str:
    """The roster role a 1.2 user is counted under: their primary role's, teacher first, else a secondary one's; "" if
    neither is a teacher or a student. Each enrolment still carries its own role."""
    for found in (roles or (set(), set())):
        for role in ROSTERED_ROLES:
            if role in found:
                return role
    return ""


_OR12 = "That isn't OneRoster 1.2's layout: export OneRoster 1.2 CSV."


def read_oneroster(files: Dict[str, bytes], version: str = "1.1") -> Snapshot:
    """A OneRoster 1.1 or 1.2 CSV package (already checked by ``check_package``) as a snapshot. Each file is taken out of
    ``files`` as it's read, so its bytes go once its rows are in the snapshot rather than all staying for the whole read.
    1.2 differs only in where a person's role is: roles.csv, not users.csv."""
    snap = Snapshot(source=f"OneRoster {version} CSV")
    columns, used, layout = (COLUMNS_12, USED_12, _OR12) if version == "1.2" else (COLUMNS, USED, _OR11)

    def rows(name: str) -> Iterator[Tuple[int, Dict[str, str]]]:
        return _rows(name, files.pop(name), columns[name], used[name], layout=layout)

    for line, row in rows("orgs.csv"):
        if (sid := _live(snap, "orgs.csv", line, row)) is None:
            continue
        if sid in snap.schools:
            snap.issue("duplicate_id", "orgs.csv", line, sid)
            continue
        snap.schools[sid] = School(sid, row.get("name", ""))
    for line, row in rows("academicSessions.csv"):
        if (sid := _live(snap, "academicSessions.csv", line, row)) is None:
            continue
        if sid in snap.terms:
            snap.issue("duplicate_id", "academicSessions.csv", line, sid)
            continue
        snap.terms[sid] = Term(sid, row.get("title", ""), row.get("type", "").lower(), row.get("startDate", ""),
                               row.get("endDate", ""))
    for line, row in rows("courses.csv"):
        if (sid := _live(snap, "courses.csv", line, row)) is None:
            continue
        if sid in snap.courses:
            snap.issue("duplicate_id", "courses.csv", line, sid)
            continue
        if not row.get("title"):
            snap.issue("no_title", "courses.csv", line, sid)
        snap.courses[sid] = Course(sid, row.get("title", ""), row.get("courseCode", ""), row.get("orgSourcedId", ""))
    for line, row in rows("classes.csv"):
        if (sid := _live(snap, "classes.csv", line, row)) is None:
            continue
        if sid in snap.classes:
            snap.issue("duplicate_id", "classes.csv", line, sid)
            continue
        title, course, school = row.get("title", ""), row.get("courseSourcedId", ""), row.get("schoolSourcedId", "")
        terms = tuple(t.strip() for t in row.get("termSourcedIds", "").split(",") if t.strip())
        if not title:
            snap.issue("no_title", "classes.csv", line, sid)
        if course not in snap.courses:
            snap.issue("unknown_course", "classes.csv", line, sid, course)
        if school not in snap.schools:
            snap.issue("unknown_school", "classes.csv", line, sid, school)
        for term in terms:
            if term not in snap.terms:
                snap.issue("unknown_term", "classes.csv", line, sid, term)
        snap.classes[sid] = Class(sid, title, course, school, terms, row.get("classCode", ""), row.get("location", ""))
    emails: Dict[str, str] = {}
    roles = _roles_12(snap, files.pop(ROLES)) if version == "1.2" else {}
    allowed: Dict[str, Set[str]] = {}            # each person's roster roles: an enrolment must be one of them
    unrostered: Set[str] = set()                 # in users.csv, but no teacher or student role (an aide, a guardian…)
    files.pop(ROLES, None)                       # a 1.1 package may carry one: never read
    for line, row in rows("users.csv"):
        if (sid := _live(snap, "users.csv", line, row)) is None:
            continue
        role = _role_12(roles.get(sid)) if version == "1.2" else row.get("role", "").lower()
        if role not in ROSTERED_ROLES:
            snap.others += 1
            unrostered.add(sid)
            continue
        if sid in snap.people:
            snap.issue("duplicate_id", "users.csv", line, sid)
            continue
        given = row.get("email", "")
        if not given:
            snap.issue("no_email", "users.csv", line, sid)
            continue
        if not _one_address(given):        # as given: lowercasing first turns a non-ASCII K (U+212A) into an ASCII k
            snap.issue("bad_email", "users.csv", line, sid, given)
            continue
        email = given.lower()
        if email in emails:
            snap.issue("duplicate_email", "users.csv", line, sid, email)
            continue
        emails[email] = sid
        snap.people[sid] = Person(sid, role, email, row.get("givenName", ""), row.get("familyName", ""))
        allowed[sid] = (roles[sid][0] | roles[sid][1]) if version == "1.2" else {role}
    roles.clear()
    for line, row in rows("enrollments.csv"):
        if (sid := _live(snap, "enrollments.csv", line, row)) is None:
            continue
        role = row.get("role", "").lower()
        if role not in ROSTERED_ROLES:
            continue
        klass, person = row.get("classSourcedId", ""), row.get("userSourcedId", "")
        if klass not in snap.classes:
            snap.issue("unknown_class", "enrollments.csv", line, sid, klass)
            continue
        if person in unrostered:
            # In users.csv, but no role a class roster holds: 1.2's roles.csv is what to fix, not a missing person.
            where = "no teacher or student row in roles.csv" if version == "1.2" else "users.csv gives another role"
            snap.issue("role_mismatch", "enrollments.csv", line, sid, f"{role} in enrollments, {where}")
            continue
        if person not in snap.people:
            snap.issue("unknown_person", "enrollments.csv", line, sid, person)
            continue
        if role not in allowed[person]:
            where = "roles" if version == "1.2" else "users"
            snap.issue("role_mismatch", "enrollments.csv", line, sid,
                       f"{role} in enrollments, {'/'.join(sorted(allowed[person]))} in {where}")
            continue
        snap.enrollments.append(Enrollment(klass, person, role, row.get("primary", "").lower() == "true"))
    return snap


_CLEVER_NAMES = ("schools.csv", "students.csv", "teachers.csv", "sections.csv")
_ONEROSTER_ONLY = {n.lower() for n in (MANIFEST, *REQUIRED, ROLES)} - {"enrollments.csv"}


def detect(names: Iterable[str]) -> str:
    """Which export a set of file names is: "oneroster", "clever" or "" (unknown). By name alone, before anything is
    unpacked: OneRoster's camelCase files and manifest, or Clever's schools, students, teachers and sections. enrollments.csv
    is in both and decides nothing. Both kinds at once is neither."""
    lowered = {n.lower() for n in names}
    oneroster, clever = bool(lowered & _ONEROSTER_ONLY), bool(lowered & set(_CLEVER_NAMES))
    if oneroster and clever:
        raise PackageError("These files mix a OneRoster export with a Clever one. Choose one export.")
    return "oneroster" if oneroster else "clever" if clever else ""


def read_upload(uploads: Sequence[Tuple[str, bytes]]) -> Snapshot:
    """What the operator picked (one ZIP, or a folder's CSV files) as a checked snapshot: OneRoster 1.1 or 1.2, or Clever.
    Raises ``PackageError`` with the fix for anything that isn't one, and ``NeedsMap`` for one CSV that is neither (a
    spreadsheet the operator maps by its columns)."""
    from . import roster_clever      # it builds on this module

    if not uploads:
        raise PackageError("Choose the roster export: its ZIP, or the CSV files in it.")
    if sum(len(body) for _, body in uploads) > UPLOAD_CAP:
        raise PackageError(f"That's more than {UPLOAD_CAP >> 20} MB, more than a roster export should be.")
    zips = [body[:4] in _ZIP_MAGIC or name.lower().endswith(".zip") for name, body in uploads]
    if len(uploads) == 1 and zips[0]:
        kind = detect(zip_names(uploads[0][1]))      # a .zip that isn't one says "That isn't a ZIP file"
    elif any(zips):
        raise PackageError("Choose one ZIP on its own, or the CSV files without it.")
    else:
        names = [_base(name) for name, _ in uploads if not _ignored(_base(name))]
        if len(names) == 1 and names[0].lower().endswith(".csv"):
            # One CSV is never a whole OneRoster or Clever export (each is five files or more), whatever it's named: a SIS
            # report called students.csv is a spreadsheet to map, not a Clever export missing four files.
            name, body = next((n, b) for n, b in uploads if _base(n) == names[0])
            raise NeedsMap(names[0], body)
        kind = detect(names)
    if kind == "clever":
        files = files_from_zip(uploads[0][1], roster_clever.LAYOUT) if zips[0] else files_from_csvs(uploads, roster_clever.LAYOUT)
        return roster_clever.read_clever(files)
    if not kind:
        raise PackageError("This isn't an export GamGUI knows. Choose a OneRoster (1.1 or 1.2) or Clever export: its ZIP, "
                           "or every CSV file in it. Any other SIS: export one spreadsheet (a CSV) with a row per person "
                           "in a class, and map its columns.")
    files = files_from_zip(uploads[0][1]) if zips[0] else files_from_csvs(uploads)
    manifest = check_package(files)
    return read_oneroster(files, oneroster_version(manifest))
