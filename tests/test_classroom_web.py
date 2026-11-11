"""The Classroom screen (/classroom, plan 2026-11-11 C1.3): a class finder over the cached index, a teacher's classes read
live only when asked, and a class card with its roster — read only, through the strict mock `gam`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.classroom import Course
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.routes import classroom as screen
from gamgui.web.server import TEMPLATES, AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CHEM = "500000000002"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _course_reads(gam_calls):
    return [argv for argv in gam_calls() if argv[:2] in (["print", "courses"], ["print", "course-participants"])]


def test_the_page_says_it_only_reads_and_loads_its_list_after(client, gam_calls):
    # A cold index is a domain-wide read: the page itself makes none, and its list loads on load.
    r = client.get("/classroom")
    assert r.status_code == 200
    assert "only reads Classroom" in r.text and "hasn't been checked on a real school domain" in r.text
    assert 'hx-get="/classroom/search" hx-trigger="load"' in r.text
    assert not _course_reads(gam_calls)


def test_the_list_holds_the_open_classes_from_one_index_read(client, gam_calls):
    r = client.get("/classroom/search")
    for name in ("Algebra", "Biology", "Chemistry, &#34;Honors&#34;"):
        assert name in r.text
    assert "World History 2025" not in r.text          # archived: not in the index
    assert "3 open classes, active or not yet accepted" in r.text
    assert _course_reads(gam_calls) == [["print", "courses", "states", "active,provisioned", "fields",
                                         "id,name,section,room,coursestate,ownerid,creationtime,updatetime,alternatelink",
                                         "formatjson"]]


def test_the_index_is_read_once_and_searched_in_memory(client, gam_calls):
    client.get("/classroom/search")
    r = client.get("/classroom/search", params={"q": "bio", "live": "0"})
    assert "Biology" in r.text and "Algebra" not in r.text and "1 open class match" in r.text
    assert len(_course_reads(gam_calls)) == 1


def test_results_are_paged_server_side(client, monkeypatch):
    monkeypatch.setattr(screen, "PAGE_SIZE", 2)
    first = client.get("/classroom/search", params={"q": ""}).text
    assert "showing 1–2" in first and "Page 1 of 2" in first
    second = client.get("/classroom/search", params={"q": "", "page": "2"}).text
    assert "showing 3–3" in second and "Page 2 of 2" in second


def test_typing_an_address_waits_for_enter_and_reads_nothing(client, gam_calls):
    client.get("/classroom/search")
    before = len(_course_reads(gam_calls))
    r = client.get("/classroom/search", params={"q": "alice@example.com", "live": "0"})
    assert "Press Enter to read alice@example.com" in r.text
    assert len(_course_reads(gam_calls)) == before


def test_a_teachers_classes_are_read_live_archived_too(client, gam_calls):
    r = client.get("/classroom/search", params={"q": "admin@example.com"})
    assert "World History 2025" in r.text and "archived ones included" in r.text
    assert ["print", "courses", "teacher", "admin@example.com", "states", "active,provisioned,archived", "owneremail",
            "fields", "id,name,section,room,coursestate,ownerid,creationtime,updatetime,alternatelink",
            "formatjson"] in gam_calls()


def test_a_teacher_classroom_doesnt_know_is_named(client):
    r = client.get("/classroom/search", params={"q": "nobody@example.com"})
    assert "Classroom doesn&#39;t know nobody@example.com." in r.text


def test_two_addresses_are_refused_before_gam(client, gam_calls):
    r = client.get("/classroom/search", params={"q": "a@example.com,b@example.com"})
    assert "isn&#39;t one address" in r.text
    assert not _course_reads(gam_calls)


def test_an_admin_token_without_classroom_points_at_setup(client, gam_state):
    (gam_state / "noclassroom").touch()
    r = client.get("/classroom/search")
    assert "can&#39;t read Classroom" in r.text and "Setup" in r.text


def test_a_failed_index_read_is_said_again_not_rerun_per_keystroke(client, gam_state, gam_calls):
    # On a domain without Classroom each typing pause once ran a domain-wide read that could only fail.
    (gam_state / "noclassroom").touch()
    for q in ("b", "bi", "bio"):
        r = client.get("/classroom/search", params={"q": q, "live": "0"})
        assert "can&#39;t read Classroom" in r.text and "Try again" in r.text
    assert len(_course_reads(gam_calls)) == 1
    (gam_state / "noclassroom").unlink()
    r = client.get("/classroom/search", params={"q": "bio", "refresh": "1"})   # Try again reads at once
    assert "Biology" in r.text and len(_course_reads(gam_calls)) == 2


def test_a_class_card_shows_its_owner_and_roster(client):
    r = client.get("/classroom/class", params={"id": CHEM})
    assert "Period 4" in r.text and "C3" in r.text
    assert 'href="/users/detail?email=alice%40example.com"' in r.text
    for who in ("Alice Anders", "Bob Brown", "Carol Clark"):
        assert who in r.text
    assert "Teachers · 1" in r.text and "Students · 2" in r.text
    assert 'href="https://classroom.google.com/c/NTAwMDAwMDAwMDAy"' in r.text


def test_a_deep_link_opens_a_class_and_the_list_keeps_it_marked(client):
    r = client.get("/classroom", params={"course": CHEM})
    assert "Carol Clark" in r.text and f'id="class-selected" name="selected" value="{CHEM}"' in r.text
    listed = client.get("/classroom/search", params={"selected": CHEM}).text
    assert f'data-course="{CHEM}" aria-current="true"' in listed
    assert listed.count('aria-current="true"') == 1


@pytest.mark.parametrize("bad", ["1,2", "abc", "d:alias", "1 2"])
def test_a_malformed_class_id_is_refused_before_gam(client, gam_calls, bad):
    r = client.get("/classroom/class", params={"id": bad})
    assert "isn&#39;t a Classroom class ID" in r.text
    assert not _course_reads(gam_calls)


def test_a_class_classroom_doesnt_have_is_named(client):
    r = client.get("/classroom/class", params={"id": "999999"})
    assert "Classroom doesn&#39;t know class 999999." in r.text


def test_a_link_from_google_is_shown_only_as_classroom_https(client):
    course = Course(id="1", name="X", link="javascript:alert(1)")
    html = TEMPLATES.get_template("_classroom_card.html").render(
        card={"error": "", "course": course, "owner": "", "roster": None, "roster_error": "", "shown": 10})
    assert "javascript:" not in html and "Open the class" not in html


def test_a_tenant_switch_drops_the_class_index(client):
    client.get("/classroom/search")
    st = client.app.state.gamgui
    assert st.course_cache.cached
    st.activate(st.connector)
    st.course_index_failed = (0.0, "x")
    st.activate(st.connector)
    assert st.course_cache.cached is None and st.course_index_failed is None


def test_the_screen_posts_nothing(client):
    # Read only (the plan's E2): no route under /classroom writes to Google. The only POSTs are the import's two, which keep
    # or delete a roster on this Mac and call no GAM (LOCAL_ONLY in test_write_routes_guarded; test_roster_web checks the
    # argv log). The OpenAPI schema is FastAPI's flattened view of every route (app.routes nests included routers).
    paths = {p: ops for p, ops in client.app.openapi()["paths"].items() if p.startswith("/classroom")}
    assert set(paths) == {"/classroom", "/classroom/search", "/classroom/class", "/classroom/person", "/classroom/owned.csv",
                          "/classroom/creators", "/classroom/import", "/classroom/import/upload", "/classroom/import/purge",
                          "/classroom/import/issues.csv"}   # the scan sees the screen
    posts = {p for p, ops in paths.items() if set(ops) != {"get"}}
    assert posts == {"/classroom/import/upload", "/classroom/import/purge"}
    assert all(set(paths[p]) == {"post"} for p in posts)
