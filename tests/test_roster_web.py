"""Plan a Classroom import (/classroom/import, plan 2026-11-11 C2.2 and C2.4): upload a SIS export, see what's in it and
what the SIS team should fix, and purge it. Nothing here calls GAM: every request is checked against the mock's argv log."""
from __future__ import annotations

import csv
import io
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import roster
from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.roster_store import RosterStore, StoreError
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS
from .test_roster import ENROLLMENTS, MANIFEST, USERS, _files, _zip

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     roster_store=RosterStore(tmp_path / "roster"))
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _upload(client, *files):
    return client.post("/classroom/import/upload", files=[("files", (name, body, "application/octet-stream"))
                                                          for name, body in files])


def _store(client) -> RosterStore:
    return client.app.state.gamgui.roster_store


def _audited(client):
    return [(e["action"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]


def test_the_page_says_what_to_export_and_keeps_nothing_yet(client, gam_calls):
    r = client.get("/classroom/import")
    assert "OneRoster 1.1" in r.text and "full (bulk)" in r.text and "No roster is kept on this Mac" in r.text
    assert 'aria-current="page"' in r.text and "Plan an import" in r.text
    assert not gam_calls()


def test_a_good_export_is_read_kept_and_audited_without_gam(client, gam_calls):
    r = _upload(client, ("export.zip", _zip(_files())))
    assert "Read: export.zip" in r.text and "None. Every class, person and enrolment read cleanly." in r.text
    assert "Purge it now" in r.text
    path = _store(client).path(DOMAIN)
    assert path.exists() and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ("roster_import", True) in _audited(client)
    assert not gam_calls()


def test_a_folders_csv_files_upload_together(client):
    r = _upload(client, *_files().items())
    assert "Read: 7 CSV files" in r.text


def test_a_wrong_file_is_refused_with_its_fix_and_nothing_is_kept(client):
    manifest = [row if row[0] != "oneroster.version" else ("oneroster.version", "2.0") for row in MANIFEST]
    r = _upload(client, ("export.zip", _zip(_files(**{"manifest.csv": manifest}))))
    assert "OneRoster version “2.0”" in r.text and "No roster is kept" in r.text
    assert not _store(client).path(DOMAIN).exists()


def test_a_wrong_file_leaves_the_last_good_one_kept(client):
    _upload(client, ("good.zip", _zip(_files())))
    r = _upload(client, ("notes.txt", b"hello"))
    assert "isn&#39;t an export GamGUI knows" in r.text and "Kept: good.zip" in r.text


def test_more_than_the_upload_cap_is_refused_before_reading(client, monkeypatch):
    monkeypatch.setattr("gamgui.web.routes.roster.UPLOAD_CAP", 100)
    r = _upload(client, ("export.zip", _zip(_files())))
    assert "more than 0 MB" in r.text and not _store(client).path(DOMAIN).exists()


def test_problems_are_grouped_for_the_sis_team_with_an_issues_csv(client):
    users = USERS + [("U4", "", "student", "", "No", "Mail", ""), ("U5", "", "student", "", "Also", "None", "")]
    enrolls = ENROLLMENTS + [("E9", "", "C9", "S1", "U2", "student", "")]
    r = _upload(client, *_files(**{"users.csv": users, "enrollments.csv": enrolls}).items())
    assert "Problems to fix in your SIS" in r.text
    assert "A teacher or student has no email address." in r.text and "Fix:" in r.text
    assert '<a href="/classroom/import/issues.csv" download' in r.text
    sheet = client.get("/classroom/import/issues.csv")
    rows = list(csv.DictReader(io.StringIO(sheet.text)))
    no_email = [row for row in rows if row["kind"] == "no_email"]
    assert [(row["ID"], row["line"], row["kind_total"]) for row in no_email] == [("U4", "5", "2"), ("U5", "6", "2")]
    assert sheet.headers["content-disposition"] == 'attachment; filename="roster-issues.csv"'


def test_purge_deletes_it_now_and_is_audited(client):
    _upload(client, ("export.zip", _zip(_files())))
    r = client.post("/classroom/import/purge")
    assert "The kept roster was deleted from this Mac." in r.text and not _store(client).path(DOMAIN).exists()
    assert ("roster_purge", True) in _audited(client)
    assert "There was no roster kept to delete." in client.post("/classroom/import/purge").text


def test_a_roster_is_kept_per_domain(client):
    _upload(client, ("export.zip", _zip(_files())))
    assert _store(client).meta("other.example.org") is None


def test_the_upload_takes_no_more_files_than_a_zip_may_hold(client, monkeypatch):
    many = [(f"x{i}.csv", b"x") for i in range(roster.MEMBERS_CAP + 5)]
    r = _upload(client, *many)
    assert "couldn&#39;t be read" in r.text or "isn&#39;t a OneRoster file" in r.text
    assert not _store(client).path(DOMAIN).exists()


# --- compare with Classroom (C3), end to end through the strict mock ----------------------------------------------------
# The mock's Classroom (tests/fixtures/courses.json, course_aliases.tsv): Biology (gg-C1: alice teaches, carol), Chemistry
# (gg-C2 and a hand-made alias: alice; bob and carol), Algebra (no GamGUI alias: hand-made), World History (gg-OLD, archived).
SIS_CLASSES = [("sourcedId", "status", "title", "courseSourcedId", "schoolSourcedId", "termSourcedIds"),
               ("C1", "", "Biology", "CO1", "S1", "T1"), ("C2", "", 'Chemistry, "Honors"', "CO1", "S1", "T1"),
               ("C3", "", "Physics", "CO1", "S1", "T1")]
SIS_USERS = [("sourcedId", "status", "role", "email", "givenName", "familyName"),
             ("U1", "", "teacher", "alice@example.com", "Alice", "Anders"),
             ("U2", "", "student", "carol@example.com", "Carol", "Clark")]
SIS_ENROLL = [("sourcedId", "status", "classSourcedId", "userSourcedId", "role", "primary"),
              ("E1", "", "C1", "U1", "teacher", "true"), ("E2", "", "C1", "U2", "student", ""),
              ("E3", "", "C2", "U1", "teacher", "true"), ("E4", "", "C2", "U2", "student", ""),
              ("E5", "", "C3", "U1", "teacher", "true"), ("E6", "", "C3", "U2", "student", "")]


def _compared(client):
    from .helpers import wait_for_job
    _upload(client, *_files(**{"classes.csv": SIS_CLASSES, "users.csv": SIS_USERS, "enrollments.csv": SIS_ENROLL}).items())
    r = client.post("/classroom/import/compare")
    assert "Reading every class in Classroom" in r.text
    wait_for_job(client, client.app.state.gamgui.jobs[client.app.state.gamgui.roster_compare_job])
    return client.get("/classroom/import/compare/status", params={"job": client.app.state.gamgui.roster_compare_job})


def test_compare_reads_classroom_and_plans_only_gamgui_classes(client, gam_calls):
    r = _compared(client)
    plan = client.app.state.gamgui.roster_compare["plan"]
    kinds = sorted((c.kind, c.title, c.person) for c in plan.changes)
    assert ("create", "Physics", "alice@example.com") in kinds and ("add_student", "Physics", "carol@example.com") in kinds
    assert ("remove_student", 'Chemistry, "Honors"', "bob@example.com") in kinds
    assert not [c for c in plan.changes if c.title == "Algebra"]              # hand-made: never compared
    assert not [c for c in plan.changes if c.kind == "archive"]               # gg-OLD is already archived
    assert plan.unmanaged_ignored == 1 and not plan.incomplete
    assert "Would remove 1 person; would create 1 class, add 1 person and rename 2 classes." in r.text and "Change list" in r.text
    writes = [a for a in gam_calls() if a[:2] not in (["print", "courses"], ["print", "course-participants"], ["print", "users"])]
    assert writes == [], writes                                               # reads only


def test_the_change_list_filters_and_searches_server_side(client):
    _compared(client)
    r = client.get("/classroom/import/changes", params={"kind": "remove_student"})
    assert "bob@example.com" in r.text and "Physics" not in r.text
    r = client.get("/classroom/import/changes", params={"q": "physics"})
    assert "Physics" in r.text and "bob@example.com" not in r.text


def test_a_new_roster_drops_the_old_compare(client):
    _compared(client)
    _upload(client, ("export.zip", _zip(_files())))
    assert "Compare with Classroom" in client.get("/classroom/import/compare/status").text
    assert client.get("/classroom/import/changes").text.count("Compare again") == 1


def test_a_compare_that_cant_read_classroom_says_so_in_fixed_words(client, gam_state):
    from .helpers import wait_for_job
    _upload(client, ("export.zip", _zip(_files())))
    (gam_state / "noclassroom").touch()
    client.post("/classroom/import/compare")
    st = client.app.state.gamgui
    wait_for_job(client, st.jobs[st.roster_compare_job])
    r = client.get("/classroom/import/compare/status", params={"job": st.roster_compare_job})
    assert "Couldn&#39;t compare with Classroom" in r.text and "can&#39;t read Classroom" in r.text and "Try again" in r.text


# --- hand it over (C4) ---------------------------------------------------------------------------------------------------
def test_the_hand_over_is_a_zip_of_files_audited_without_rows(client, gam_calls):
    import zipfile
    _compared(client)
    page = client.get("/classroom/import/compare/status", params={"job": client.app.state.gamgui.roster_compare_job})
    assert "outside GamGUI&#39;s preview, confirmation and audit log" in page.text or \
        "outside GamGUI's preview, confirmation and audit log" in page.text
    assert '<a href="/classroom/import/handover.zip" download' in page.text
    before = len([a for a in gam_calls()])
    r = client.get("/classroom/import/handover.zip")
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith('attachment; filename="classroom-changes-example.com-')
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert {"README.txt", "review.csv", "03-create-classes.csv", "06-add-students.csv", "10-remove-students.csv"} <= set(names)
    assert len(gam_calls()) == before                                   # no GAM: it hands over, it never runs
    record = [e for e in client.app.state.gamgui.connector.audit.tail() if e["action"] == "roster_handover"][-1]
    assert record["extra"]["counts"]["create"] == 1 and "bob@example.com" not in str(record)


def test_the_same_compare_hands_over_the_same_bytes(client):
    _compared(client)
    assert client.get("/classroom/import/handover.zip").content == client.get("/classroom/import/handover.zip").content


def test_a_flagged_kind_needs_its_own_link(client, monkeypatch):
    monkeypatch.setattr("gamgui.core.roster_plan.LIMITS", {"remove_student": (0, 0.0), "remove_teacher": (99, 1.0),
                                                           "archive": (99, 1.0)})
    import zipfile
    _compared(client)
    left = zipfile.ZipFile(io.BytesIO(client.get("/classroom/import/handover.zip").content)).namelist()
    assert "10-remove-students.csv" not in left
    with_it = client.get("/classroom/import/handover.zip", params={"include": "remove_student"}).content
    assert "10-remove-students.csv" in zipfile.ZipFile(io.BytesIO(with_it)).namelist()
    bogus = client.get("/classroom/import/handover.zip", params={"include": "archive"}).content   # not flagged: no effect
    assert zipfile.ZipFile(io.BytesIO(bogus)).namelist() == left


def test_no_compare_or_an_incomplete_one_hands_nothing_over(client):
    assert client.get("/classroom/import/handover.zip").status_code == 400
    _compared(client)
    client.app.state.gamgui.roster_compare["plan"].incomplete = True
    r = client.get("/classroom/import/handover.zip")
    assert r.status_code == 400 and "incomplete" in r.text
    client.app.state.gamgui.roster_compare["plan"].incomplete = False
    client.app.state.gamgui.roster_compare["plan"].changes = []
    r = client.get("/classroom/import/handover.zip")
    assert r.status_code == 400 and r.text == "There's nothing to hand over.\n"


# --- a spreadsheet mapped by its columns (plan C5.3) ---------------------------------------------------------------------
SHEET = (b"Section ID,Class Title,Term,Email Address,User Type,Lead\r\n"
         b"BIO-1,Biology,Fall 2026,ann@example.com,Teacher,yes\r\n"
         b"BIO-1,Biology,Fall 2026,bo@example.com,Student,\r\n"
         b"BIO-1,Biology,Fall 2026,cy@example.com,Student,\r\n"
         b"ART-2,Art,Fall 2026,di@example.com,Pupil,\r\n")
MAP_FORM = {"col_class_id": "Section ID", "col_class_name": "Class Title", "col_section": "Term",
            "col_email": "Email Address", "col_role": "User Type", "col_primary": "Lead",
            "teacher_words": "teacher", "student_words": "student, pupil"}


@pytest.fixture
def maps(client, tmp_path):
    from gamgui.core.roster_maps import MapStore
    client.app.state.gamgui.roster_maps = MapStore(tmp_path / "maps.json")
    return client.app.state.gamgui.roster_maps


def test_one_unknown_spreadsheet_asks_for_its_map_and_keeps_nothing_yet(client, maps, gam_calls):
    r = _upload(client, ("schedule.csv", SHEET))
    assert "Map the columns of schedule.csv" in r.text and "Read with this map" in r.text
    assert '<option value="Email Address" selected>' in r.text           # guessed from the header
    assert not _store(client).path(DOMAIN).exists() and ("roster_import", True) not in _audited(client)
    held = client.app.state.gamgui.roster_pending
    assert held["name"] == "schedule.csv" and held["domain"] == DOMAIN
    assert not gam_calls()


def test_the_map_shows_three_rows_read_with_it_before_anything_is_read(client, maps):
    _upload(client, ("schedule.csv", SHEET))
    r = client.post("/classroom/import/map/preview", data=MAP_FORM)
    assert "The first 3 rows, read with this map" in r.text and "cy@example.com" in r.text
    assert "di@example.com" not in r.text                     # the fourth row isn't shown
    r = client.post("/classroom/import/map/preview", data={**MAP_FORM, "col_email": ""})
    assert "Read with this map" in r.text and "disabled" in r.text     # a needed field unmapped: no read


def test_reading_with_a_map_keeps_the_roster_audits_the_map_and_can_save_it(client, maps, gam_calls):
    _upload(client, ("schedule.csv", SHEET))
    r = client.post("/classroom/import/map/read", data={**MAP_FORM, "save_as": "District schedule"})
    assert "Read: schedule.csv" in r.text and "Spreadsheet" in r.text
    snap = _store(client).load(DOMAIN)
    assert {p.email for p in snap.people.values()} == {"ann@example.com", "bo@example.com", "cy@example.com",
                                                        "di@example.com"}
    assert client.app.state.gamgui.roster_pending is None
    assert maps.names() == ["District schedule"]
    entry = next(e for e in client.app.state.gamgui.connector.audit.tail() if e["action"] == "roster_import")
    assert entry["extra"]["mapped_fields"] == ["class_id", "class_name", "email", "primary", "role", "section"]
    assert "ann@example.com" not in str(entry) and "Email Address" not in str(entry)     # fields, never the sheet's text
    assert not gam_calls()


def test_a_saved_map_that_fits_is_used_next_time_and_can_be_deleted(client, maps):
    from gamgui.core.roster_mapped import ColumnMap
    maps.save("Mine", ColumnMap.from_json({k[4:]: v for k, v in MAP_FORM.items() if k.startswith("col_")}
                                          | {"teacher_words": ["teacher"], "student_words": ["student", "pupil"]}))
    r = _upload(client, ("schedule.csv", SHEET))
    assert '<option value="Mine" selected>' in r.text and "student, pupil" in r.text   # the saved words, not the defaults
    r = client.post("/classroom/import/map/delete", data={**MAP_FORM, "delete": "Mine"})
    assert maps.names() == [] and "Map the columns of schedule.csv" in r.text


def test_cancel_purge_and_a_new_upload_drop_the_held_spreadsheet(client, maps):
    for leave in (lambda: client.post("/classroom/import/map/cancel"), lambda: client.post("/classroom/import/purge"),
                  lambda: _upload(client, ("export.zip", _zip(_files())))):
        _upload(client, ("schedule.csv", SHEET))
        assert client.app.state.gamgui.roster_pending is not None
        leave()
        assert client.app.state.gamgui.roster_pending is None
    r = client.post("/classroom/import/map/read", data=MAP_FORM)
    assert "Pick the spreadsheet again" in r.text


def test_a_held_spreadsheet_from_another_domain_is_never_read(client, maps):
    _upload(client, ("schedule.csv", SHEET))
    client.app.state.gamgui.roster_pending["domain"] = "other.example"
    r = client.post("/classroom/import/map/read", data=MAP_FORM)
    assert "Pick the spreadsheet again" in r.text and not _store(client).path(DOMAIN).exists()


def test_a_spreadsheet_whose_header_cant_be_read_is_refused_at_once(client, maps):
    r = _upload(client, ("schedule.csv", b"\xff\xfe not utf-8"))
    assert "isn&#39;t UTF-8" in r.text and client.app.state.gamgui.roster_pending is None


# --- found by the adversarial run of C5 (2026-11-12) -----------------------------------------------------------------
def test_a_column_change_after_picking_a_saved_map_is_kept(client, maps):
    from gamgui.core.roster_mapped import ColumnMap
    maps.save("Mine", ColumnMap.from_json({k[4:]: v for k, v in MAP_FORM.items() if k.startswith("col_")}
                                          | {"student_words": ["student", "pupil"]}))
    _upload(client, ("schedule.csv", SHEET))
    changed = {**MAP_FORM, "use": "Mine", "col_section": ""}
    r = client.post("/classroom/import/map/preview", data=changed, headers={"HX-Trigger-Name": "col_section"})
    assert '<option value="Term" selected>' not in r.text            # the change stands; `use` rode along unasked
    r = client.post("/classroom/import/map/preview", data=changed, headers={"HX-Trigger-Name": "use"})
    assert '<option value="Term" selected>' in r.text                # picking the saved map loads it


def test_a_refused_upload_drops_the_held_spreadsheet(client, maps):
    _upload(client, ("schedule.csv", SHEET))
    _upload(client, ("roster.zip", b"not a zip"))
    assert client.app.state.gamgui.roster_pending is None


def test_a_tenant_switch_during_a_mapped_read_keeps_nothing(client, maps, monkeypatch):
    import gamgui.web.routes.roster as routes
    _upload(client, ("schedule.csv", SHEET))
    real = routes.read_mapped

    def switch_mid_read(*a):
        client.app.state.gamgui.audit_domain = "other.example"
        return real(*a)
    monkeypatch.setattr(routes, "read_mapped", switch_mid_read)
    r = client.post("/classroom/import/map/read", data=MAP_FORM)
    assert "switched to another domain" in r.text
    assert not _store(client).path(DOMAIN).exists() and not _store(client).path("other.example").exists()


def test_a_sheet_without_its_header_row_is_refused_before_any_map(client, maps):
    r = _upload(client, ("schedule.csv", b"BIO-1,Biology,ann@example.com,teacher\r\nBIO-1,Biology,bo@example.com,student\r\n"))
    assert "it&#39;s a row, not a header" in r.text and client.app.state.gamgui.roster_pending is None
    assert maps.names() == []


def test_one_csv_named_like_a_clever_or_oneroster_file_is_still_mapped(client, maps):
    r = _upload(client, ("students.csv", SHEET))
    assert "Map the columns of students.csv" in r.text


def test_a_maps_file_moved_aside_is_said_on_the_map_step(client, maps):
    maps.path.write_text("{not json")
    from gamgui.core.roster_maps import MapStore
    client.app.state.gamgui.roster_maps = MapStore(maps.path)
    r = _upload(client, ("schedule.csv", SHEET))
    assert "The saved maps file couldn't be read" in r.text


def test_a_kept_roster_that_cant_be_read_just_now_still_shows_purge(client, monkeypatch):
    _upload(client, ("export.zip", _zip(_files())))
    store = _store(client)

    def fails(_domain):
        raise StoreError("The kept roster couldn't be read just now. Try again; Purge still deletes it.")

    monkeypatch.setattr(store, "meta", fails)
    r = client.get("/classroom/import")
    assert "couldn&#39;t be read just now" in r.text and "Purge it now" in r.text
    monkeypatch.undo()
    store.purge(DOMAIN)
    monkeypatch.setattr(store, "meta", fails)
    assert "Purge it now" not in client.get("/classroom/import").text     # nothing kept: nothing to purge


def test_a_roster_that_cant_be_read_during_compare_is_said_as_the_roster_not_gam(client, monkeypatch):
    from .helpers import wait_for_job

    def fails(_domain):
        raise StoreError("The kept roster couldn't be read just now. Try again; Purge still deletes it.")

    _upload(client, *_files(**{"classes.csv": SIS_CLASSES, "users.csv": SIS_USERS, "enrollments.csv": SIS_ENROLL}).items())
    monkeypatch.setattr(_store(client), "load", fails)
    client.post("/classroom/import/compare")
    job = client.app.state.gamgui.jobs[client.app.state.gamgui.roster_compare_job]
    wait_for_job(client, job)
    assert job.error == "The kept roster couldn't be read, so nothing was compared. Open the import page to see why."


# --- a compare belongs to its domain and its roster; held-back rows say so beyond colour --------------------------------
def _old_job(client, sha, error=None):
    """A compare job as one started earlier would leave it: still running, or failed."""
    from gamgui.web.jobs import start_job
    st = client.app.state.gamgui
    job = start_job(st.jobs, 0, kind="roster_compare", title="Compare the roster with Classroom")
    if error:
        job.error = error
        job.finish()
    st.roster_compare_job, st.roster_compare_sha = job.id, sha
    return job


def test_a_tenant_switch_drops_the_old_domains_compare(client):
    _upload(client, ("export.zip", _zip(_files())))
    st = client.app.state.gamgui
    old = _old_job(client, _store(client).meta(DOMAIN)["sha256"])               # still running on the old tenant
    other = GAMConnector(runner=st.runner, domain="other.example.org", audit=st.connector.audit)
    st.activate(other)
    assert st.roster_compare_job == "" and st.roster_compare_sha == ""
    assert "Reading every class" not in client.get("/classroom/import/compare/status", params={"job": old.id}).text
    r = client.post("/classroom/import/compare")                 # never "still reading" the old tenant's job
    assert "No roster is kept to compare" in r.text and "Reading every class" not in r.text


def test_a_compare_of_an_earlier_roster_is_neither_waited_on_nor_its_failure_shown(client):
    from .helpers import wait_for_job
    _upload(client, ("export.zip", _zip(_files())))
    st = client.app.state.gamgui
    running = _old_job(client, _store(client).meta(DOMAIN)["sha256"])
    _upload(client, *_files(**{"classes.csv": SIS_CLASSES, "users.csv": SIS_USERS, "enrollments.csv": SIS_ENROLL}).items())
    client.post("/classroom/import/compare")                                     # a new roster: not the old job
    assert st.roster_compare_job != running.id
    wait_for_job(client, st.jobs[st.roster_compare_job])
    failed = _old_job(client, "an-earlier-roster", error="Something went wrong talking to GAM.")
    r = client.get("/classroom/import/compare/status", params={"job": failed.id})
    assert "Couldn&#39;t compare" not in r.text and "Something went wrong" not in r.text


def test_a_compare_that_lost_its_place_keeps_no_result(client, monkeypatch):
    """A compare overtaken by a newer one (or a switch) finishes without replacing the domain's change list."""
    from .helpers import wait_for_job
    st = client.app.state.gamgui
    _compared(client)
    kept = st.roster_compare
    real = st.connector.classroom_state

    async def overtaken():
        st.roster_compare_job = "a-newer-compare"
        return await real()

    monkeypatch.setattr(st.connector, "classroom_state", overtaken)
    client.post("/classroom/import/compare")
    job = next(j for j in st.jobs.values() if j.kind == "roster_compare" and not j.finished)
    wait_for_job(client, job)
    assert st.roster_compare is kept


def test_held_back_rows_say_so_with_a_mark_and_to_a_screen_reader(client, monkeypatch):
    from gamgui.core import roster_plan
    monkeypatch.setattr(roster_plan, "LIMITS", {"remove_student": (0, 0.0)})
    _compared(client)
    plan = client.get("/classroom/import/compare/status").text
    rows = client.get("/classroom/import/changes", params={"kind": "remove_student"}).text
    for page in (plan, rows):
        assert '<span aria-hidden="true">⚠ </span>' in page and '<span class="sr-only">, held back</span>' in page
    assert rows.count(", held back") == rows.count("<tr>") - 1 >= 1                # every row, past the header


def test_purge_drops_the_change_list_so_the_same_bytes_never_bring_it_back(client):
    _compared(client)
    st = client.app.state.gamgui
    client.post("/classroom/import/purge")
    assert st.roster_compare is None and st.roster_compare_job == "" and st.roster_compare_sha == ""
    _upload(client, *_files(**{"classes.csv": SIS_CLASSES, "users.csv": SIS_USERS, "enrollments.csv": SIS_ENROLL}).items())
    assert client.get("/classroom/import/handover.zip").status_code == 400


def test_a_purge_during_a_compare_keeps_nothing_from_it(client, monkeypatch):
    from .helpers import wait_for_job
    st = client.app.state.gamgui
    _upload(client, *_files(**{"classes.csv": SIS_CLASSES, "users.csv": SIS_USERS, "enrollments.csv": SIS_ENROLL}).items())
    real = st.connector.classroom_state

    async def purged_meanwhile():
        st.roster_compare_sha = ""                       # what POST /purge does while the read runs
        return await real()

    monkeypatch.setattr(st.connector, "classroom_state", purged_meanwhile)
    client.post("/classroom/import/compare")
    wait_for_job(client, next(j for j in st.jobs.values() if j.kind == "roster_compare"))
    assert st.roster_compare is None
