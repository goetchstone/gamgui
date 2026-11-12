"""Read a student information system's roster export into one snapshot (plan 2026-11-11, C2.1 and C2.3).

The snapshot is one model every reader fills (schools, terms, courses, classes, people, enrolments), so nothing after it
knows which SIS or format it came from. This module reads OneRoster 1.1 CSV, as a ZIP or as its loose CSV files; plan C5
adds other readers that fill the same `Snapshot`.

An upload is hostile until read: it comes from outside the app and lands in student data. So:

- **Nothing is extracted and no name is ever a path.** A member is matched by its whole base name against the files
  OneRoster names. A symlink, an encrypted member, `..`, an absolute name, a backslash, more than one level of folder,
  or two members with one name (in any case) refuses the package. (A NUL can't be refused: `zipfile` cuts a name at
  it, so `users.csv\\0../x` arrives as `users.csv`. That's safe only because no name is ever a path.)
- **Sizes are counted, never trusted.** Each member is decompressed into a bounded buffer that stops one byte past its cap,
  whatever its header says; the members together, the ratio of a member's size to its compressed size, the number of
  members, rows, fields and line lengths are all capped.
- **Text is strict UTF-8** (a leading byte-order mark is dropped). Anything else refuses the file by name.
- **A package-level mistake refuses the whole package with its fix** (`PackageError`): not a ZIP, OneRoster 1.2, a delta
  export, a file missing or with the wrong columns. Row-level problems are kept as `Issue`s for the SIS team.

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
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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
ISSUE_SAMPLE = 1000                  # rows kept per kind of issue (the Issues CSV); the count is always whole

# OneRoster 1.1 CSV, as 1EdTech names its files. A rostering package needs the first six (and the manifest).
REQUIRED = ("orgs.csv", "academicSessions.csv", "courses.csv", "classes.csv", "users.csv", "enrollments.csv")
OPTIONAL = ("demographics.csv", "lineItems.csv", "categories.csv", "results.csv", "resources.csv", "classResources.csv",
            "courseResources.csv", "userResources.csv")
MANIFEST = "manifest.csv"
KNOWN = {name.lower(): name for name in (MANIFEST, *REQUIRED, *OPTIONAL)}
# What macOS adds to a ZIP it makes: never ours, never read.
_MAC_LITTER = ("__MACOSX/", ".DS_Store", "._")

COLUMNS = {     # the columns each file must have for this reader; others are allowed and ignored
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
ROSTERED_ROLES = ("teacher", "student")      # the people a class's roster holds; others are counted, not rostered
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")   # a ZIP's first member, or an empty ZIP's end record


class PackageError(Exception):
    """The upload can't be read as a roster at all. ``str()`` says what's wrong and how to fix it, in words."""


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
    "no_id": ("A row has no sourcedId.", "Every row needs the SIS's ID in sourcedId."),
    "duplicate_id": ("Two rows share one sourcedId; the first is kept.", "Each sourcedId must be unique in its file."),
    "no_email": ("A teacher or student has no email address.",
                 "Give them their Google Workspace address in users.csv's email column."),
    "bad_email": ("An email address isn't one address.", "Fix the email column: one plain address, no spaces."),
    "duplicate_email": ("Two people share one email address; the first is kept.", "Each person needs their own address."),
    "unknown_class": ("An enrolment names a class that isn't in classes.csv.",
                      "Include the class, or drop the enrolment."),
    "unknown_person": ("An enrolment names a person who isn't in users.csv (or has no address).",
                       "Include the person with their address, or drop the enrolment."),
    "role_mismatch": ("An enrolment's role isn't the person's role in users.csv.", "Make the two roles agree."),
    "unknown_course": ("A class names a course that isn't in courses.csv.", "Include the course."),
    "unknown_school": ("A class names a school that isn't in orgs.csv.", "Include the school."),
    "unknown_term": ("A class names a term that isn't in academicSessions.csv.", "Include the term."),
    "no_title": ("A class or course has no title.", "Give it a title; it becomes the class's name in Classroom."),
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
        raise PackageError("The ZIP lists far more entries than a OneRoster export holds. Check it's the right file.")


def files_from_zip(data: bytes) -> Dict[str, bytes]:
    """The roster files in a ZIP, by their OneRoster name. Unknown files are refused, never ignored: a roster package holds
    only roster files, and a stray one usually means the wrong export. Optional OneRoster files are named-checked but never
    unpacked: this reader doesn't use them, and each could hold its whole cap."""
    _refuse_a_huge_directory(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — BadZipFile, ValueError, OSError, NotImplementedError (an unknown version), …
        raise PackageError("That isn't a ZIP file. Choose the ZIP your SIS exported, or its CSV files.") from exc
    files: Dict[str, bytes] = {}
    skipped: set = set()
    folders: set = set()
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MEMBERS_CAP:
            raise PackageError(f"The ZIP holds {len(infos)} entries; a OneRoster export holds a dozen or so. Check it's the "
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
            canonical = KNOWN.get(base.lower())
            if canonical is None:
                raise PackageError(f"The ZIP holds “{base[:80]}”, which isn't a OneRoster file. Choose the OneRoster export "
                                   "itself.")
            if canonical in files or canonical in skipped:
                raise PackageError(f"The ZIP holds {canonical} twice. Export the roster again.")
            if canonical in OPTIONAL:        # a OneRoster file this reader doesn't use: named, never unpacked
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


def files_from_csvs(named: Iterable[Tuple[str, bytes]]) -> Dict[str, bytes]:
    """The roster files picked one by one (a folder's CSVs), by their OneRoster name. A picked file's name is matched, never
    used as a path."""
    files: Dict[str, bytes] = {}
    total = 0
    for raw_name, body in named:
        base = (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1]
        if _ignored(base):
            continue
        canonical = KNOWN.get(base.lower())
        if canonical is None:
            raise PackageError(f"“{base[:80]}” isn't a OneRoster file. Pick the CSV files from the export, or its ZIP.")
        if canonical in files:
            raise PackageError(f"{canonical} was picked twice.")
        if canonical in OPTIONAL:            # read already (it was uploaded), but never parsed or kept
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


def _rows(name: str, body: bytes) -> Iterator[Tuple[int, Dict[str, str]]]:
    """Each row of a roster CSV as a dict of the columns this reader uses, with the line it starts on (the header is line
    1). Bounded: lines, columns, fields, rows."""
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
            raise PackageError(f"{name} has {len(header):,} columns; OneRoster's have about twenty. That isn't a normal export.")
        doubled = sorted({h for h in header if h and header.count(h) > 1})
        if doubled:
            raise PackageError(f"{name} has the column {doubled[0]} twice. Export the roster again.")
        missing = [c for c in COLUMNS.get(name, ()) if c not in header]
        if missing:
            raise PackageError(f"{name} is missing the column{'s' if len(missing) > 1 else ''} {', '.join(missing)}. That isn't "
                               "OneRoster 1.1's layout: export OneRoster 1.1 CSV.")
        wanted = [(c, header.index(c)) for c in USED.get(name, COLUMNS.get(name, ())) if c in header]
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
            yield start, {key: ((row[i].strip() if key == "email" else _clean(row[i])) if i < len(row) else "")
                          for key, i in wanted}
    except csv.Error as exc:
        raise PackageError(f"{name} isn't readable CSV ({exc}). Export the roster again.") from exc


def _manifest(files: Dict[str, bytes]) -> Dict[str, str]:
    if MANIFEST not in files:
        raise PackageError("There's no manifest.csv, so this isn't a OneRoster export. Export OneRoster 1.1 CSV from your SIS.")
    if len(files[MANIFEST]) > MANIFEST_CAP:
        raise PackageError("manifest.csv is far larger than a OneRoster manifest. Export the roster again.")
    try:
        rows = list(itertools.islice(csv.reader(_Lines(MANIFEST, files[MANIFEST]), strict=True), 256))
    except csv.Error as exc:
        raise PackageError(f"manifest.csv isn't readable CSV ({exc}).") from exc
    if not rows or [c.strip() for c in rows[0][:2]] != ["propertyName", "value"]:
        raise PackageError("manifest.csv isn't a OneRoster manifest. Export OneRoster 1.1 CSV from your SIS.")
    return {r[0].strip(): r[1].strip() for r in rows[1:256] if len(r) >= 2}


def check_package(files: Dict[str, bytes]) -> Dict[str, str]:
    """Refuse a package this reader can't take, with its fix: the manifest, the version, a delta export, a missing file."""
    manifest = _manifest(files)
    version = manifest.get("oneroster.version", "")
    if version.startswith("1.2"):
        raise PackageError("This is OneRoster 1.2. GamGUI reads OneRoster 1.1 for now: export 1.1 CSV from your SIS.")
    if version != "1.1":
        raise PackageError(f"The manifest says OneRoster version “{version[:20] or 'none'}”. Export OneRoster 1.1 CSV.")
    for name in REQUIRED:
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


def read_oneroster(files: Dict[str, bytes]) -> Snapshot:
    """A OneRoster 1.1 CSV package (already checked by ``check_package``) as a snapshot. Each file is taken out of ``files``
    as it's read, so its bytes go once its rows are in the snapshot rather than all staying for the whole read."""
    snap = Snapshot(source="OneRoster 1.1 CSV")
    for line, row in _rows("orgs.csv", files.pop("orgs.csv")):
        if (sid := _live(snap, "orgs.csv", line, row)) is None:
            continue
        if sid in snap.schools:
            snap.issue("duplicate_id", "orgs.csv", line, sid)
            continue
        snap.schools[sid] = School(sid, row.get("name", ""))
    for line, row in _rows("academicSessions.csv", files.pop("academicSessions.csv")):
        if (sid := _live(snap, "academicSessions.csv", line, row)) is None:
            continue
        if sid in snap.terms:
            snap.issue("duplicate_id", "academicSessions.csv", line, sid)
            continue
        snap.terms[sid] = Term(sid, row.get("title", ""), row.get("type", "").lower(), row.get("startDate", ""),
                               row.get("endDate", ""))
    for line, row in _rows("courses.csv", files.pop("courses.csv")):
        if (sid := _live(snap, "courses.csv", line, row)) is None:
            continue
        if sid in snap.courses:
            snap.issue("duplicate_id", "courses.csv", line, sid)
            continue
        if not row.get("title"):
            snap.issue("no_title", "courses.csv", line, sid)
        snap.courses[sid] = Course(sid, row.get("title", ""), row.get("courseCode", ""), row.get("orgSourcedId", ""))
    for line, row in _rows("classes.csv", files.pop("classes.csv")):
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
    for line, row in _rows("users.csv", files.pop("users.csv")):
        if (sid := _live(snap, "users.csv", line, row)) is None:
            continue
        role = row.get("role", "").lower()
        if role not in ROSTERED_ROLES:
            snap.others += 1
            continue
        if sid in snap.people:
            snap.issue("duplicate_id", "users.csv", line, sid)
            continue
        email = row.get("email", "").lower()
        if not email:
            snap.issue("no_email", "users.csv", line, sid)
            continue
        if not _one_address(email):
            snap.issue("bad_email", "users.csv", line, sid, email)
            continue
        if email in emails:
            snap.issue("duplicate_email", "users.csv", line, sid, email)
            continue
        emails[email] = sid
        snap.people[sid] = Person(sid, role, email, row.get("givenName", ""), row.get("familyName", ""))
    for line, row in _rows("enrollments.csv", files.pop("enrollments.csv")):
        if (sid := _live(snap, "enrollments.csv", line, row)) is None:
            continue
        role = row.get("role", "").lower()
        if role not in ROSTERED_ROLES:
            continue
        klass, person = row.get("classSourcedId", ""), row.get("userSourcedId", "")
        if klass not in snap.classes:
            snap.issue("unknown_class", "enrollments.csv", line, sid, klass)
            continue
        if person not in snap.people:
            snap.issue("unknown_person", "enrollments.csv", line, sid, person)
            continue
        if snap.people[person].role != role:
            snap.issue("role_mismatch", "enrollments.csv", line, sid, f"{role} in enrollments, {snap.people[person].role} in users")
            continue
        snap.enrollments.append(Enrollment(klass, person, role, row.get("primary", "").lower() == "true"))
    return snap


def read_upload(uploads: Sequence[Tuple[str, bytes]]) -> Snapshot:
    """What the operator picked (one ZIP, or a folder's CSV files) as a checked snapshot. Raises ``PackageError`` with the
    fix for anything that isn't a OneRoster 1.1 roster."""
    if not uploads:
        raise PackageError("Choose the roster export: its ZIP, or the CSV files in it.")
    if sum(len(body) for _, body in uploads) > UPLOAD_CAP:
        raise PackageError(f"That's more than {UPLOAD_CAP >> 20} MB, more than a roster export should be.")
    zips = [body[:4] in _ZIP_MAGIC or name.lower().endswith(".zip") for name, body in uploads]
    if len(uploads) == 1 and zips[0]:
        files = files_from_zip(uploads[0][1])        # a .zip that isn't one says "That isn't a ZIP file"
    elif any(zips):
        raise PackageError("Choose one ZIP on its own, or the CSV files without it.")
    else:
        files = files_from_csvs(uploads)
    check_package(files)
    return read_oneroster(files)
