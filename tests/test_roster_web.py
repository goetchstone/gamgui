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
