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
from gamgui.core.roster_store import RosterStore
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
    manifest = [row if row[0] != "oneroster.version" else ("oneroster.version", "1.2") for row in MANIFEST]
    r = _upload(client, ("export.zip", _zip(_files(**{"manifest.csv": manifest}))))
    assert "This is OneRoster 1.2" in r.text and "No roster is kept" in r.text
    assert not _store(client).path(DOMAIN).exists()


def test_a_wrong_file_leaves_the_last_good_one_kept(client):
    _upload(client, ("good.zip", _zip(_files())))
    r = _upload(client, ("notes.txt", b"hello"))
    assert "isn&#39;t a OneRoster file" in r.text and "Kept: good.zip" in r.text


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
    assert [(row["sourcedId"], row["line"], row["kind_total"]) for row in no_email] == [("U4", "5", "2"), ("U5", "6", "2")]
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
