"""Classroom beside the people screens (plan 2026-11-11 C1.4–C1.6): a person's classes on their Users page, the classes a
leaver owns named at offboarding with a list to hand over, and who can create classes. Read only, through the strict mock."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.models import GAMGroup
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.setup import classroom_readable
from gamgui.web.server import TEMPLATES, AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
# An admin token that records its scopes, none of them Classroom's: a domain that doesn't use Classroom.
NO_CLASSROOM = json.dumps({**json.loads(OAUTH2_FAKE), "scopes": ["https://www.googleapis.com/auth/admin.directory.user"]})


def _client(tmp_path, monkeypatch, oauth2: str = OAUTH2_FAKE):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": oauth2, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    return TestClient(create_app(state, allowed_hosts=TEST_HOSTS))


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        c.get("/?token=t")
        yield c


@pytest.fixture
def no_classroom(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, NO_CLASSROOM) as c:
        c.get("/?token=t")
        yield c


def _course_reads(gam_calls):
    return [argv for argv in gam_calls() if argv[:2] == ["print", "courses"]]


def test_classroom_readable_is_false_only_on_a_token_known_to_lack_it():
    vault = SecretsVault(InMemoryBackend())
    vault.set("ex.com", "oauth2", NO_CLASSROOM)
    assert classroom_readable(vault, "ex.com") is False
    vault.set("ex.com", "oauth2", OAUTH2_FAKE)          # no scopes recorded: unknown, so the read is tried
    assert classroom_readable(vault, "ex.com") is None


# --- C1.4: a person's classes -------------------------------------------------------------------------------------
def test_the_users_page_offers_their_classes_without_reading_them(client, gam_calls):
    r = client.get("/users/detail", params={"email": "alice@example.com"})
    assert "Show their classes" in r.text and 'hx-get="/classroom/person?email=alice%40example.com"' in r.text
    assert not _course_reads(gam_calls)


def test_the_users_page_leaves_classroom_off_where_the_token_cant_read_it(no_classroom):
    assert "Show their classes" not in no_classroom.get("/users/detail", params={"email": "alice@example.com"}).text


def test_a_teachers_classes_say_which_they_own(client):
    r = client.get("/classroom/person", params={"email": "alice@example.com"})
    assert "Teaches · 3" in r.text and "In · 0" in r.text
    assert r.text.count("· owner") == 3
    assert 'href="/classroom?course=500000000002"' in r.text


def test_a_students_classes_include_last_years(client):
    r = client.get("/classroom/person", params={"email": "carol@example.com"})
    assert "In · 3" in r.text and "World History 2025" in r.text and "Archived" in r.text


def test_one_cause_is_said_once(client, gam_state):
    (gam_state / "noclassroom").touch()
    r = client.get("/classroom/person", params={"email": "alice@example.com"})
    assert r.text.count("can&#39;t read Classroom") == 1


def test_a_token_known_to_lack_classroom_reads_nothing(no_classroom, gam_calls):
    r = no_classroom.get("/classroom/person", params={"email": "alice@example.com"})
    assert "can&#39;t read Classroom" in r.text
    assert not _course_reads(gam_calls)


# --- C1.5: the classes a leaver owns ------------------------------------------------------------------------------
def _offboard(c, user="alice@example.com"):
    return c.post("/lifecycle/offboard/preview", data={"user": user, "manager": "carol@example.com"})


def test_offboarding_names_the_classes_the_leaver_owns_and_hands_over_a_list(client):
    r = _offboard(client)
    assert "owns 3 Classroom classes" in r.text and "GamGUI doesn't change Classroom" in r.text
    assert "Biology" in r.text
    assert '<a href="/classroom/owned.csv?email=alice%40example.com" download' in r.text


def test_offboarding_someone_who_owns_none_says_nothing_about_classroom(client):
    assert "Classroom class" not in _offboard(client, "carol@example.com").text


async def test_an_account_classroom_has_never_seen_owns_no_class(connector, gam_calls):
    # Classroom answering that it doesn't know someone as a teacher (exit 56) means they own none, not a failed read.
    assert await connector.courses_owned_by("nobody@example.com") == []
    assert ["print", "courses", "teacher", "nobody@example.com", "owneremail", "fields",
            "id,name,section,room,coursestate,ownerid,creationtime,updatetime,alternatelink", "formatjson"] in gam_calls()


async def test_owned_classes_are_read_in_every_state_and_other_failures_raise(connector, gam_state):
    assert {c.name for c in await connector.courses_owned_by("admin@example.com")} == {"World History 2025"}
    (gam_state / "noclassroom").touch()
    with pytest.raises(GAMError) as err:
        await connector.courses_owned_by("alice@example.com")
    assert err.value.kind is GAMErrorKind.SCOPE_MISSING


def test_the_owned_list_has_a_filename_safe_name(client):
    r = client.get("/classroom/owned.csv", params={"email": "o'neil.a-b_c@example.com"})
    assert r.headers["content-disposition"] == 'attachment; filename="classes-owned-by-oneil.a-b_c.csv"'


def test_the_creators_panel_reads_nothing_where_classroom_isnt_used(no_classroom, gam_calls):
    assert no_classroom.get("/classroom/creators").text == ""
    assert not [a for a in gam_calls() if a[:2] == ["print", "groups"]]


def test_offboarding_says_when_the_classes_couldnt_be_read(client, gam_state):
    (gam_state / "noclassroom").touch()
    r = _offboard(client)
    assert "Couldn't read the Classroom classes alice@example.com owns" in r.text


def test_offboarding_on_a_domain_without_classroom_reads_none(no_classroom, gam_calls):
    r = _offboard(no_classroom)
    assert r.status_code == 200 and "Classroom" not in r.text
    assert not _course_reads(gam_calls)


def test_the_owned_list_is_a_formula_safe_csv(client):
    r = client.get("/classroom/owned.csv", params={"email": "alice@example.com"})
    assert r.headers["content-disposition"] == 'attachment; filename="classes-owned-by-alice.csv"'
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["courseId", "name", "section", "state", "owner", "link"]
    assert sorted(row[1] for row in rows[1:]) == ["Algebra", "Biology", 'Chemistry, "Honors"']


def test_the_owned_list_refuses_anything_but_one_address(client, gam_calls):
    assert client.get("/classroom/owned.csv", params={"email": "a@x.com,b@x.com"}).status_code == 400
    assert not _course_reads(gam_calls)


# --- C1.6: who can create classes ---------------------------------------------------------------------------------
def test_no_teachers_group_says_where_to_look(client):
    r = client.get("/classroom/creators")
    assert "No “Classroom Teachers” group found" in r.text


def test_the_teachers_group_links_to_groups_with_its_size():
    html = TEMPLATES.get_template("_classroom_creators.html").render(
        group=GAMGroup(email="classroom_teachers@example.com", name="Classroom Teachers", members_count=12), error="")
    assert 'href="/groups?group=classroom_teachers%40example.com"' in html and "12 direct members" in html
