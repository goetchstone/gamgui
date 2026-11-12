"""The roster reader and its store (plan 2026-11-11 C2.1, C2.3, C2.5). An upload is hostile until read, so most of this is
what gets refused, and that each refusal says how to fix the export."""
from __future__ import annotations

import csv
import io
import os
import stat
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from gamgui.core import roster
from gamgui.core.roster import PackageError, read_upload
from gamgui.core.roster_store import KEEP_DAYS, RosterStore, StoreError

MANIFEST = [("propertyName", "value"), ("manifest.version", "1.0"), ("oneroster.version", "1.1"),
            ("file.academicSessions", "bulk"), ("file.classes", "bulk"), ("file.courses", "bulk"), ("file.orgs", "bulk"),
            ("file.users", "bulk"), ("file.enrollments", "bulk")]
ORGS = [("sourcedId", "status", "dateLastModified", "name", "type"), ("S1", "", "", "North High", "school")]
TERMS = [("sourcedId", "status", "title", "type", "startDate", "endDate"), ("T1", "", "Fall 2026", "term", "2026-08-20", "2026-12-20")]
COURSES = [("sourcedId", "status", "title", "courseCode", "orgSourcedId"), ("CO1", "", "Biology", "BIO", "S1")]
CLASSES = [("sourcedId", "status", "title", "courseSourcedId", "classCode", "schoolSourcedId", "termSourcedIds", "location"),
           ("C1", "", "Biology P2", "CO1", "BIO-2", "S1", "T1", "B12")]
USERS = [("sourcedId", "status", "role", "username", "givenName", "familyName", "email"),
         ("U1", "", "teacher", "ann", "Ann", "Lee", "Ann.Lee@example.com"),
         ("U2", "", "student", "bo", "Bo", "Ng", "bo@example.com"),
         ("U3", "", "aide", "cy", "Cy", "Oh", "cy@example.com")]
ENROLLMENTS = [("sourcedId", "status", "classSourcedId", "schoolSourcedId", "userSourcedId", "role", "primary"),
               ("E1", "", "C1", "S1", "U1", "teacher", "true"), ("E2", "", "C1", "S1", "U2", "student", "false")]


def _csv(rows) -> bytes:
    """Rows as CSV, every field quoted, written by hand: Python 3.10's csv writer refuses a NUL ("need to escape"), and
    these tests send one on purpose."""
    return "".join(",".join('"' + str(v).replace('"', '""') + '"' for v in row) + "\r\n" for row in rows).encode()


def _files(**override) -> dict:
    files = {"manifest.csv": MANIFEST, "orgs.csv": ORGS, "academicSessions.csv": TERMS, "courses.csv": COURSES,
             "classes.csv": CLASSES, "users.csv": USERS, "enrollments.csv": ENROLLMENTS}
    files.update(override)
    return {name: rows if isinstance(rows, bytes) else _csv(rows) for name, rows in files.items() if rows is not None}


def _zip(files: dict, prefix: str = "", extra=()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(prefix + name, body)
        for info, body in extra:
            z.writestr(info, body)
    return out.getvalue()


def _refused(uploads, says: str) -> None:
    with pytest.raises(PackageError) as err:
        read_upload(uploads)
    assert says in str(err.value), str(err.value)


# --- a good package ----------------------------------------------------------------------------------------------------
def test_a_oneroster_zip_reads_into_one_snapshot():
    snap = read_upload([("export.zip", _zip(_files()))])
    assert snap.counts == {"schools": 1, "terms": 1, "courses": 1, "classes": 1, "teachers": 1, "students": 1, "enrollments": 2}
    assert snap.people["U1"].email == "ann.lee@example.com"          # lowercased
    assert snap.classes["C1"].term_ids == ("T1",) and snap.others == 1  # the aide is counted, not rostered
    assert not snap.issue_counts


def test_finders_one_enclosing_folder_and_its_litter_are_fine():
    data = _zip(_files(), prefix="Export/", extra=[("__MACOSX/Export/._users.csv", b"x"), ("Export/.DS_Store", b"x")])
    assert read_upload([("Export.zip", data)]).counts["classes"] == 1


def test_a_folders_csv_files_read_like_its_zip():
    snap = read_upload(list(_files().items()))
    assert snap.counts["enrollments"] == 2


def test_a_byte_order_mark_is_dropped():
    assert read_upload(list(_files(**{"users.csv": b"\xef\xbb\xbf" + _csv(USERS)}).items())).people["U2"].email == "bo@example.com"


# --- what the package-level refusals say -------------------------------------------------------------------------------
@pytest.mark.parametrize(("uploads", "says"), [
    ([], "Choose the roster export"),
    ([("roster.zip", b"not a zip at all")], "That isn't a ZIP file"),
    ([("a.zip", _zip(_files())), ("users.csv", b"x")], "one ZIP on its own"),
    ([("notes.txt", b"x")], "isn't an export GamGUI knows"),
    ([("a.csv", b"x"), ("b.csv", b"y")], "isn't an export GamGUI knows"),
    ([("users.csv", b"x"), ("sections.csv", b"y")], "mix a OneRoster export with a Clever one"),
])
def test_the_wrong_kind_of_file_is_refused_with_its_fix(uploads, says):
    _refused(uploads, says)


# OneRoster 1.2: users.csv has no role; roles.csv says it (1EdTech 1.2 CSV binding, Appendix A).
USERS_12 = [("sourcedId", "status", "dateLastModified", "enabledUser", "username", "userIds", "givenName", "familyName",
             "middleName", "identifier", "email", "primaryOrgSourcedId"),
            ("U1", "", "", "true", "ann", "", "Ann", "Lee", "", "", "Ann.Lee@example.com", "S1"),
            ("U2", "", "", "true", "bo", "", "Bo", "Ng", "", "", "bo@example.com", "S1"),
            ("U3", "", "", "true", "cy", "", "Cy", "Oh", "", "", "cy@example.com", "S1"),
            ("U4", "", "", "true", "di", "", "Di", "Ho", "", "", "di@example.com", "S1")]
ROLES_12 = [("sourcedId", "status", "dateLastModified", "userSourcedId", "roleType", "role", "beginDate", "endDate",
             "orgSourcedId", "userProfileSourcedId"),
            ("R1", "", "", "U1", "primary", "teacher", "", "", "S1", ""),
            ("R2", "", "", "U2", "primary", "student", "", "", "S1", ""),
            ("R3", "", "", "U3", "primary", "aide", "", "", "S1", ""),
            ("R4", "", "", "U4", "primary", "ext:counselor", "", "", "S1", ""),
            ("R5", "", "", "U4", "secondary", "teacher", "", "", "S2", "")]
MANIFEST_12 = [r if r[0] != "oneroster.version" else ("oneroster.version", "1.2") for r in MANIFEST] + [("file.roles", "bulk")]


def _files_12(**override) -> dict:
    return _files(**{"manifest.csv": MANIFEST_12, "users.csv": USERS_12, "roles.csv": ROLES_12, **override})


@pytest.mark.parametrize("version", ["1.2", "1.2.1"])
def test_oneroster_1_2_reads_each_role_from_roles_csv(version):
    manifest = [r if r[0] != "oneroster.version" else ("oneroster.version", version) for r in MANIFEST_12]
    enrol = ENROLLMENTS + [("E3", "", "C1", "S1", "U4", "teacher", "false")]
    snap = read_upload([("export.zip", _zip(_files_12(**{"manifest.csv": manifest, "enrollments.csv": enrol})))])
    assert snap.source == "OneRoster 1.2 CSV"
    assert {p.id: p.role for p in snap.people.values()} == {"U1": "teacher", "U2": "student", "U4": "teacher"}
    assert snap.others == 1                                   # the aide: counted, not rostered
    assert [(e.person_id, e.role, e.primary) for e in snap.enrollments] == [
        ("U1", "teacher", True), ("U2", "student", False), ("U4", "teacher", False)]
    assert not snap.issue_counts


def test_oneroster_1_2_needs_roles_csv_and_holds_enrolments_to_it():
    _refused(list(_files_12(**{"roles.csv": None}).items()), "no roles.csv")
    enrol = ENROLLMENTS + [("E3", "", "C1", "S1", "U2", "teacher", "false")]      # bo is only a student
    snap = read_upload(list(_files_12(**{"enrollments.csv": enrol}).items()))
    assert snap.issue_counts == {"role_mismatch": 1} and "in roles" in snap.issue_sample[0].detail


def test_oneroster_1_2_user_profiles_are_named_but_never_unpacked(monkeypatch):
    # userProfiles.csv holds per-app passwords: never read, whatever it holds.
    data = _zip(_files_12(**{"userProfiles.csv": b"sourcedId,password\r\nP1,hunter2\r\n"}))
    opened = []
    real = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, info, *a, **k: opened.append(getattr(info, "filename", info))
                        or real(self, info, *a, **k))
    read_upload([("export.zip", data)])
    assert "userProfiles.csv" not in opened and "users.csv" in opened


def test_a_1_1_users_csv_in_a_1_2_package_is_still_read_by_name():
    # 1.2 reads columns by name: a users.csv that still has a role column reads the same (roles.csv decides).
    assert read_upload([("export.zip", _zip(_files_12(**{"users.csv": USERS})))]).people["U1"].role == "teacher"


def test_a_delta_export_says_to_export_bulk():
    manifest = [r if r[0] != "file.users" else ("file.users", "delta") for r in MANIFEST]
    _refused(list(_files(**{"manifest.csv": manifest}).items()), "full (bulk) export")


@pytest.mark.parametrize("missing", ["manifest.csv", "users.csv", "enrollments.csv"])
def test_a_missing_file_is_named(missing):
    _refused(list(_files(**{missing: None}).items()), missing if missing != "manifest.csv" else "no manifest.csv")


def test_wrong_columns_say_its_not_oneroster_1_1():
    _refused(list(_files(**{"users.csv": [("id", "mail"), ("U1", "a@b.com")]}).items()), "missing the columns")


def test_text_that_isnt_utf8_is_refused_by_file():
    _refused(list(_files(**{"orgs.csv": "sourcedId,status,name,type\nS1,,Zürich,school\n".encode("latin-1")}).items()),
             "orgs.csv isn't UTF-8")


# --- what the ZIP may hold ---------------------------------------------------------------------------------------------
def _with(name: str, body: bytes = b"x", info=None) -> bytes:
    return _zip(_files(), extra=[(info or zipfile.ZipInfo(name), body)])


@pytest.mark.parametrize("name", ["../users.csv", "/users.csv", "a\\users.csv", "a/b/users.csv", "./users.csv",
                                  "C:/users.csv", "users.csv\x00.txt"])
def test_a_member_name_that_could_be_a_path_refuses_the_package(name):
    with pytest.raises(PackageError):
        roster.files_from_zip(_with(name))


def test_a_symlink_member_is_refused():
    info = zipfile.ZipInfo("Export/users.csv")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    _refused([("x.zip", _zip({k: v for k, v in _files().items() if k != "users.csv"}, extra=[(info, b"/etc/passwd")]))],
             "is a link or a special file")


def test_an_encrypted_member_is_refused():
    data = bytearray(_zip(_files()))
    # flag bit 0 (encrypted) on the first local header and its central directory entry
    data[6] |= 1
    central = data.find(b"PK\x01\x02")
    data[central + 8] |= 1
    _refused([("x.zip", bytes(data))], "password-protected")


def test_two_members_with_one_name_in_any_case_are_refused():
    _refused([("x.zip", _with("USERS.csv", _csv(USERS)))], "users.csv twice")


def test_an_unknown_member_is_refused_not_ignored():
    _refused([("x.zip", _with("payload.sh", b"#!/bin/sh"))], "isn't a OneRoster file")


def test_two_folders_are_refused():
    _refused([("x.zip", _zip(_files(), prefix="A/", extra=[("B/resources.csv", b"x")]))], "more than one folder")


def test_too_many_entries_are_refused(monkeypatch):
    monkeypatch.setattr(roster, "MEMBERS_CAP", 5)
    _refused([("x.zip", _zip(_files()))], "entries")


def test_a_decompression_bomb_is_refused_by_its_ratio():
    bomb = _files(**{"users.csv": _csv(USERS) + b"0" * (8 * 1024 * 1024)})
    _refused([("x.zip", _zip(bomb))], "unpacks to far more")


def test_an_optional_file_is_named_but_never_unpacked(monkeypatch):
    # The reader doesn't use them, and each could hold its whole cap: 3.7 MB of ZIP once pinned 620 MB (adversarial run).
    monkeypatch.setattr(roster, "MEMBER_CAP", 64 * 1024)
    big = _files(**{"demographics.csv": b"x" * (1024 * 1024)})
    assert read_upload([("x.zip", _zip(big))]).counts["classes"] == 1


# --- found by the adversarial run (2026-11-11), each proved against the code before the fix -----------------------------
def test_an_unknown_zip_version_is_not_a_zip_not_a_crash():
    data = bytearray(_zip(_files()))
    central = data.find(b"PK\x01\x02")
    data[central + 6:central + 8] = (64).to_bytes(2, "little")      # "version needed to extract" 6.4
    _refused([("x.zip", bytes(data))], "That isn't a ZIP file")


def test_a_huge_central_directory_is_refused_before_zipfile_walks_it(monkeypatch):
    data = _zip(_files())                    # built before ZipFile is replaced: the module is shared
    monkeypatch.setattr(roster, "CENTRAL_DIRECTORY_CAP", 200)
    called = []
    monkeypatch.setattr(roster.zipfile, "ZipFile", lambda *a, **k: called.append(1))
    _refused([("x.zip", data)], "far more entries")
    assert not called


def _directory_only(entries: int, mode: str) -> bytes:
    """A ZIP that is only central-directory entries, the shape that cost gigabytes: as the recheck crafted it, with the end
    record's 32-bit size lying behind a ZIP64 record that gives the real one (``zip64``), or with the end-record signature
    planted inside the size field itself (``fieldsig``)."""
    import struct
    name = b"x"
    entry = struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0, 0, 0, 0, 0, 0) + name
    cd = entry * entries
    if mode == "zip64":
        z64 = struct.pack("<4sQ2H2I2Q2Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, len(cd), 0)
        loc = struct.pack("<4sIQI", b"PK\x06\x07", 0, len(cd), 1)
        return cd + z64 + loc + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 46, 0, 0)
    size = struct.unpack("<I", b"PK\x05\x06")[0]
    return entry * 4 + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 4, 4, size, 0, 0)


@pytest.mark.parametrize("mode", ["zip64", "fieldsig"])
def test_a_directory_whose_size_lies_is_refused_before_zipfile_walks_it(monkeypatch, mode):
    data = _directory_only(2000, mode)
    called = []
    monkeypatch.setattr(roster.zipfile, "ZipFile", lambda *a, **k: called.append(1))
    with pytest.raises(PackageError):
        read_upload([("r.zip", data)])
    assert not called


def test_this_python_still_has_the_end_record_reader_the_zip_check_needs():
    # Read without it, the directory's size couldn't be checked before zipfile walks it, so a ZIP would be refused outright.
    assert callable(getattr(roster.zipfile, "_EndRecData", None))


def test_a_field_spanning_many_lines_is_refused_whatever_the_process_wide_csv_limit(monkeypatch):
    monkeypatch.setattr(roster, "LINE_CAP", 4000)
    before = csv.field_size_limit()
    csv.field_size_limit(10**9)          # as another thread's GAM read may have it (parser.field_limit_at_least)
    try:
        spread = b'U9,,student,"' + b"x" * 900 + (b"\n" + b"x" * 900) * 10 + b'",A,B\n'
        _refused(list(_files(**{"users.csv": _csv(USERS) + spread}).items()), "record longer than")
    finally:
        csv.field_size_limit(before)


def test_a_wide_header_is_refused_and_rows_cost_only_the_columns_used():
    wide = [USERS[0] + tuple(f"x{i}" for i in range(300))] + list(USERS[1:])
    _refused(list(_files(**{"users.csv": wide}).items()), "columns")
    ok = [USERS[0] + tuple(f"x{i}" for i in range(100))] + list(USERS[1:])
    assert read_upload(list(_files(**{"users.csv": ok}).items())).counts["teachers"] == 1


def test_a_column_named_twice_is_refused_not_silently_overridden():
    users = [USERS[0] + ("email",), ("U1", "", "teacher", "ann", "Ann", "Lee", "ann@example.com", "evil@x.com")]
    _refused(list(_files(**{"users.csv": users}).items()), "column email twice")


def test_a_manifest_past_its_cap_is_refused_unread():
    _refused(list(_files(**{"manifest.csv": _csv(MANIFEST) + b"\n" * (128 * 1024)}).items()), "far larger than a OneRoster manifest")


def test_a_line_of_millions_of_fields_is_refused_before_it_is_split(monkeypatch):
    monkeypatch.setattr(roster, "LINE_CAP", 1000)
    _refused(list(_files(**{"users.csv": _csv(USERS) + b"U9" + b"," * 5000 + b"\n"}).items()), "line longer than")


def test_a_row_wider_than_its_header_is_refused():
    _refused(list(_files(**{"users.csv": _csv(USERS) + b"U9,,student,x,A,B,c@example.com,1,2,3\n"}).items()), "fields; its header")


@pytest.mark.parametrize("email", ["b\x1b[31m@example.com", "a\u200b@example.com", "a\x07@example.com",
                                   "é@example.com"])
def test_an_address_with_a_control_or_invisible_character_is_refused_not_kept(email):
    users = USERS[:3] + [("U9", "", "student", "", "X", "Y", email)]
    snap = read_upload(list(_files(**{"users.csv": users}).items()))
    assert "U9" not in snap.people and snap.issue_counts == {"bad_email": 1}


def test_control_characters_are_dropped_from_names():
    users = USERS[:3] + [("U9", "", "student", "", "Al\x07ex", "Ki\x1bm", "u9@example.com")]
    person = read_upload(list(_files(**{"users.csv": users}).items())).people["U9"]
    assert (person.given, person.family) == ("Alex", "Kim")


@pytest.mark.parametrize("where", ["email", "name"])
def test_a_nul_refuses_the_package_on_every_python(where):
    # Python 3.10's csv module refuses a NUL and later ones read it: the reader refuses it first, the same on all of them.
    row = ("U9", "", "student", "", "X", "Y", "a\x00@example.com") if where == "email" else \
          ("U9", "", "student", "", "Al\x00ex", "Y", "u9@example.com")
    _refused(list(_files(**{"users.csv": USERS[:3] + [row]}).items()), "holds a NUL character")


def test_an_issue_names_the_line_its_record_starts_on():
    users = USERS[:3] + [("U9", "", "student", "", "Multi\nLine\nName", "Y", ""), ("U8", "", "student", "", "A", "B", "")]
    snap = read_upload(list(_files(**{"users.csv": users}).items()))
    assert [(i.source_id, i.row) for i in snap.issue_sample if i.code == "no_email"] == [("U9", 4), ("U8", 7)]


def test_a_member_is_capped_by_what_it_unpacks_to_not_what_it_says(monkeypatch):
    monkeypatch.setattr(roster, "MEMBER_CAP", 1024)
    monkeypatch.setattr(roster, "RATIO_CAP", 10**9)
    _refused([("x.zip", _zip(_files(**{"users.csv": _csv(USERS) + b"x" * 4096})))], "larger than")


def test_a_field_past_its_cap_is_refused_and_the_csv_limit_is_left_alone(monkeypatch):
    before = csv.field_size_limit()
    monkeypatch.setattr(roster, "FIELD_CAP", 100)
    _refused(list(_files(**{"orgs.csv": [ORGS[0], ("S1", "", "", "x" * 200, "school")]}).items()), "longer than")
    assert csv.field_size_limit() == before


def test_rows_past_the_cap_are_refused(monkeypatch):
    monkeypatch.setattr(roster, "ROWS_CAP", 1)
    _refused(list(_files().items()), "more than 1 rows")


def test_the_upload_cap_is_checked_before_anything_is_read(monkeypatch):
    monkeypatch.setattr(roster, "UPLOAD_CAP", 10)
    _refused([("x.zip", _zip(_files()))], "more than 0 MB")


# --- row-level issues, for the SIS team -------------------------------------------------------------------------------
def test_row_problems_are_counted_by_kind_and_sampled():
    users = USERS + [("U4", "", "student", "", "No", "Mail", ""), ("U5", "", "student", "", "Bad", "Mail", "a b@x.com"),
                     ("U6", "", "student", "", "Dup", "Mail", "BO@example.com"), ("U7", "tobedeleted", "student", "", "", "", "z@x.com")]
    enrolls = ENROLLMENTS + [("E3", "", "C9", "S1", "U2", "student", ""), ("E4", "", "C1", "S1", "U9", "student", ""),
                             ("E5", "", "C1", "S1", "U2", "teacher", "")]
    classes = CLASSES + [("C2", "", "", "CO9", "", "S9", "T9", "")]
    snap = read_upload(list(_files(**{"users.csv": users, "enrollments.csv": enrolls, "classes.csv": classes}).items()))
    assert snap.issue_counts == {"no_email": 1, "bad_email": 1, "duplicate_email": 1, "skipped": 1, "unknown_class": 1,
                                 "unknown_person": 1, "role_mismatch": 1, "no_title": 1, "unknown_course": 1,
                                 "unknown_school": 1, "unknown_term": 1}
    assert all(code in roster.ISSUE_WORDS for code in snap.issue_counts)
    no_email = next(i for i in snap.issue_sample if i.code == "no_email")
    assert (no_email.file, no_email.row, no_email.source_id) == ("users.csv", 5, "U4")


def test_the_sample_is_capped_but_the_count_is_whole(monkeypatch):
    monkeypatch.setattr(roster, "ISSUE_SAMPLE", 2)
    users = [USERS[0]] + [(f"U{i}", "", "student", "", "", "", "") for i in range(10)]
    snap = read_upload(list(_files(**{"users.csv": users, "enrollments.csv": [ENROLLMENTS[0]]}).items()))
    assert snap.issue_counts["no_email"] == 10 and len(snap.issue_sample) == 2


# --- the store ---------------------------------------------------------------------------------------------------------
def test_a_snapshot_is_kept_owner_only_and_read_back_whole(tmp_path):
    store = RosterStore(tmp_path / "roster")
    snap = read_upload([("x.zip", _zip(_files()))])
    meta = store.save("Example.com", snap, "export.zip", "abc")
    assert meta["counts"] == snap.counts and meta["domain"] == "example.com"
    assert stat.S_IMODE(os.stat(tmp_path / "roster").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path("example.com")).st_mode) == 0o600
    assert "example" not in store.path("example.com").name           # a domain's spelling never becomes a path
    back = store.load("example.com")
    assert back is not None and back.people == snap.people and back.classes == snap.classes
    assert back.enrollments == snap.enrollments and back.counts == snap.counts
    assert not [p for p in (tmp_path / "roster").iterdir() if p.name.startswith(".saving-")]


def test_a_snapshot_past_its_days_is_deleted_on_the_next_look(tmp_path):
    clock = [datetime(2026, 11, 11, tzinfo=timezone.utc)]
    store = RosterStore(tmp_path / "roster", now=lambda: clock[0])
    store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    clock[0] += timedelta(days=KEEP_DAYS)
    assert store.meta("example.com") is None and not store.path("example.com").exists()


def test_purge_deletes_it_at_once(tmp_path):
    store = RosterStore(tmp_path / "roster")
    store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    assert store.purge("example.com") and store.load("example.com") is None and not store.purge("example.com")


def test_a_save_a_quit_cut_off_is_swept_by_the_next_save_and_by_purge(tmp_path):
    store = RosterStore(tmp_path / "roster")
    snap = read_upload([("x.zip", _zip(_files()))])
    store.save("example.com", snap, "x", "")
    final = store.path("example.com")
    stale = final.with_name(f".saving-99999-{final.name}")         # another process's, holding student data
    stale.write_bytes(b"ann.lee@example.com")
    (final.parent / f"{stale.name}-journal").write_bytes(b"x")
    assert store.purge("example.com")
    assert sorted(p.name for p in final.parent.iterdir()) == []
    stale.write_bytes(b"ann.lee@example.com")
    store.save("example.com", snap, "x", "")
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name]


@pytest.mark.parametrize("expires", ['"garbage"', '"2099-01-01T00:00:00+00:00"', "5", '"9999-01-01T00:00:00Z"'])
def test_an_expiry_it_cant_trust_deletes_the_snapshot(tmp_path, expires):
    import sqlite3
    store = RosterStore(tmp_path / "roster")
    store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    con = sqlite3.connect(store.path("example.com"))
    con.execute("UPDATE meta SET value = ? WHERE key = 'expires_at'", (expires,))
    con.commit()
    con.close()
    if expires.startswith('"9999'):          # far future: capped at imported + KEEP_DAYS
        store.now = lambda: datetime.now(timezone.utc) + timedelta(days=KEEP_DAYS + 1)
    assert store.meta("example.com") is None and not store.path("example.com").exists()


def test_anything_that_stops_a_save_is_a_store_error_and_leaves_nothing(tmp_path):
    store = RosterStore(tmp_path / "roster")
    snap = read_upload([("x.zip", _zip(_files()))])
    snap.people["U2"] = roster.Person("U2", "student", "bo@example.com", "Bo\ud800", "Ng")   # can't be encoded
    with pytest.raises(StoreError):
        store.save("example.com", snap, "x", "")
    assert not list((tmp_path / "roster").iterdir())
    final = store.path("example.com")
    (final.parent / f".saving-{os.getpid()}-{final.name}").mkdir()                 # a folder where the temp goes
    with pytest.raises(StoreError):
        store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")


def test_a_folder_named_like_a_leftover_never_blocks_a_purge(tmp_path):
    store = RosterStore(tmp_path / "roster")
    store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    final = store.path("example.com")
    (final.parent / f".saving-8-{final.name}").mkdir()
    assert store.purge("example.com") and not final.exists()


@pytest.mark.parametrize("damage", ["drop meta", "not a database"])
def test_a_snapshot_that_cant_be_read_is_deleted_and_said(tmp_path, damage):
    import sqlite3
    store = RosterStore(tmp_path / "roster")
    store.save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    path = store.path("example.com")
    if damage == "drop meta":
        con = sqlite3.connect(path)
        con.execute("DROP TABLE meta")
        con.commit()
        con.close()
    else:
        path.write_bytes(b"garbage")
    with pytest.raises(StoreError, match="so it was deleted"):
        store.meta("example.com")
    assert not path.exists()


def test_a_label_that_isnt_text_is_kept_as_text(tmp_path):
    meta = RosterStore(tmp_path / "roster").save("example.com", read_upload([("x.zip", _zip(_files()))]), None, None)
    assert meta["label"] == "" and meta["sha256"] == ""


def test_a_linked_store_folder_is_refused(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "roster").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(StoreError):
        RosterStore(tmp_path / "roster").save("example.com", read_upload([("x.zip", _zip(_files()))]), "x", "")
    assert not list((tmp_path / "elsewhere").iterdir())


# --- found by the adversarial run of C5 (2026-11-12) -----------------------------------------------------------------
def test_one_csv_is_a_spreadsheet_to_map_whatever_its_name():
    for name in ("students.csv", "users.csv", "sections.csv", "schedule.csv"):
        with pytest.raises(roster.NeedsMap):
            read_upload([(name, b"a,b\r\n1,2\r\n")])


def test_a_header_that_is_a_row_or_holds_a_huge_cell_is_refused():
    with pytest.raises(PackageError, match="it's a row, not a header"):
        roster.header_of("s.csv", b"C1,Math,ann@x.edu,teacher\r\n")
    with pytest.raises(PackageError, match="column name longer than"):
        roster.header_of("s.csv", b"a," + b"x" * 201 + b"\r\n")


def test_a_kelvin_sign_address_is_refused_in_oneroster_not_lowercased_into_ascii():
    users = USERS[:1] + [("U1", "", "teacher", "ann", "Ann", "Lee", "\u212aim@example.com")] + USERS[2:]
    snap = read_upload(list(_files(**{"users.csv": users}).items()))
    assert "U1" not in snap.people and snap.issue_counts.get("bad_email") == 1


def test_a_1_2_enrolment_of_someone_without_a_roster_role_points_at_roles_csv():
    roles = [r for r in ROLES_12 if r[3] != "U2"]                      # bo has no row in roles.csv
    snap = read_upload(list(_files_12(**{"roles.csv": roles}).items()))
    assert snap.issue_counts.get("role_mismatch") == 1 and "unknown_person" not in snap.issue_counts
    assert "roles.csv" in next(i.detail for i in snap.issue_sample if i.code == "role_mismatch")
