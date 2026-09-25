"""The Groups board (/groups, plan U7 and A6): a group finder and a people type-ahead served capped from the
cached directory, a member list with roles a page at a time, add with a role the GAMCommands builder
validates, and remove only from a confirm step — all through the strict mock `gam`."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.routes import groups as board
from gamgui.web.server import AppState, create_app

from .helpers import TEST_HOSTS, assert_ok_partial, gam_writes

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
SALES = "sales@example.com"     # mock members: alice (member), bob (manager)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _audited(client):
    return [(e["action"], e["target"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def test_the_board_lists_groups_from_the_cache_and_renders_no_directory(client, gam_calls):
    for _ in range(2):
        r = client.get("/groups")
        assert r.status_code == 200
        assert all(g in r.text for g in (SALES, "staff@example.com", "it@example.com"))
    assert "alice@example.com" not in r.text                   # no people pool: the directory stays server-side
    assert [c[:2] for c in gam_calls()] == [["print", "groups"]]   # one cached read (st.groups), not one per visit


def test_the_group_finder_matches_address_or_name_and_is_capped(client, monkeypatch):
    assert "it@example.com" in client.get("/groups/search", params={"q": "IT"}).text      # by name
    assert "No group matches" in client.get("/groups/search", params={"q": "zzz"}).text

    async def many(force=False, stale_ok=False):
        return [GAMGroup(email=f"team{i:02}@example.com", name=f"Team {i}") for i in range(40)]
    monkeypatch.setattr(client.app.state.gamgui, "groups", many)
    r = client.get("/groups/search", params={"q": "team", "selected": "team03@example.com"})
    assert r.text.count("data-group=") == board.PICK_LIMIT and "keep typing to narrow" in r.text
    assert re.search(r'data-group="team03@example.com" aria-current="true"', r.text)


def test_the_people_type_ahead_is_capped_and_offers_options(client, monkeypatch):
    r = client.get("/groups/people", params={"q": "car"})
    assert 'role="option"' in r.text and 'data-val="carol@example.com"' in r.text
    assert "alice@example.com" not in r.text

    async def many(force=False, stale_ok=False):
        return [GAMUser(primary_email=f"person{i:02}@example.com") for i in range(40)]
    monkeypatch.setattr(client.app.state.gamgui, "users", many)
    r = client.get("/groups/people", params={"q": "person"})
    assert r.text.count('role="option"') == board.PICK_LIMIT and "keep typing to narrow" in r.text


def test_a_picked_group_shows_its_members_with_roles(client):
    r = client.get("/groups/members", params={"group": SALES})
    assert r.status_code == 200
    rows = re.findall(r'<a href="/users/detail\?email=([^"]+)"[^>]*>[^<]*</a>\s*</td>\s*<td[^>]*>(\w+)</td>', r.text)
    assert rows == [("bob%40example.com", "Manager"), ("alice%40example.com", "Member")]   # managers first
    assert re.search(r'<select name="role"[^>]*>\s*<option value="member">Member</option><option value="manager">'
                     r'Manager</option><option value="owner">Owner</option>', r.text)
    assert "2 members" in _text(r.text)
    # The same panel, straight from a link (a user's group chip).
    page = client.get("/groups", params={"group": SALES})
    assert 'id="member-email"' in page.text and "Manager" in page.text


def test_add_sends_the_role_through_the_builder(client, gam_calls):
    r = client.post("/groups/members", data={"group": SALES, "email": "carol@example.com", "role": "owner"})
    assert_ok_partial(r)
    assert "Added carol@example.com to sales@example.com as owner." in _text(r.text)
    assert "data-added" in r.text and 'data-announce="group-board"' in r.text   # spoken; focus stays in the form
    assert gam_writes(gam_calls()) == [["update", "group", SALES, "add", "owner", "carol@example.com"]]
    assert _audited(client) == [("add_group_member", "carol@example.com", True)]


@pytest.mark.parametrize("form, why", [
    ({"email": "carol@example.com", "role": "admin"}, "Pick a role: member, manager or owner."),
    ({"email": "  ", "role": "member"}, "Enter the email address of the person to add."),
    ({"email": "carol@example.com", "role": "member", "group": " "}, "Pick a group first."),
    ({"email": "carol@example.com", "role": "member", "group": "a@example.com,b@example.com"}, "Pick a group first."),
    # Free text meets GAM's <UserTypeEntity>: the admin, name@<domain>, or two people.
    ({"email": "oauthuser", "role": "member"}, "“oauthuser” isn't one email address."),
    ({"email": "Carol Clark", "role": "member"}, "“Carol Clark” isn't one email address."),
    ({"email": "carol@example.com,dave@example.com", "role": "member"}, "isn't one email address."),
])
def test_a_bad_add_is_refused_in_words_before_gam(client, gam_calls, form, why):
    r = client.post("/groups/members", data={"group": SALES, **form})
    assert why in _text(r.text) and "data-focus" in r.text
    assert gam_writes(gam_calls()) == [] and _audited(client) == []


def test_a_failed_add_says_why(client):
    # A typo'd group 404s in GAM; the board must say so rather than re-render as if it worked.
    r = client.post("/groups/members", data={"group": "missing@example.com", "email": "carol@example.com"})
    assert "border-amber-300 bg-amber-50" in r.text and "not found" in r.text
    assert "Couldn't add carol@example.com to missing@example.com." in _text(r.text)
    assert _audited(client) == [("add_group_member", "carol@example.com", False)]


def test_remove_runs_only_from_its_confirm_step(client, gam_calls):
    bare = client.post("/groups/members/remove", data={"group": SALES, "email": "alice@example.com"})
    assert "needs confirmation" in bare.text and gam_writes(gam_calls()) == []

    step = client.post("/groups/members/remove/preview",
                       data={"group": SALES, "email": "alice@example.com", "role": "MEMBER"})
    assert "Remove alice@example.com from sales@example.com?" in _text(step.text)
    assert 'data-focus' in step.text and 'data-clear="member-confirm"' in step.text      # focus in, Cancel back out
    assert gam_writes(gam_calls()) == []                                                 # a preview writes nothing
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', step.text))
    assert fields["confirmed"] == "1"

    r = client.post("/groups/members/remove", data=fields)
    assert_ok_partial(r)
    assert "Removed alice@example.com from sales@example.com." in _text(r.text)
    assert gam_writes(gam_calls()) == [["update", "group", SALES, "remove", "alice@example.com"]]
    assert _audited(client) == [("remove_group_member", "alice@example.com", True)]


def test_the_confirm_step_warns_before_removing_an_owner(client):
    r = client.post("/groups/members/remove/preview", data={"group": SALES, "email": "bob@example.com", "role": "OWNER"})
    assert "make sure the group keeps one" in r.text
    assert "Pick a member to remove." in client.post("/groups/members/remove/preview", data={"group": SALES}).text


def test_remove_refuses_a_member_that_isnt_one_address(client, gam_calls):
    for who in ("oauthuser", "alice@example.com,bob@example.com"):
        form = {"group": SALES, "email": who, "confirmed": "1"}
        assert "Pick a member to remove." in client.post("/groups/members/remove/preview", data=form).text
        assert "Pick a member to remove." in client.post("/groups/members/remove", data=form).text
    assert gam_writes(gam_calls()) == [] and _audited(client) == []


def test_members_come_a_page_at_a_time_and_filter_server_side(client, monkeypatch):
    people = [GroupMember(email=f"m{i:03}@example.com") for i in range(120)] + [
        GroupMember(email="lead@example.com", role="OWNER"), GroupMember(email="subteam@example.com", member_type="GROUP")]

    async def members(group):
        return people
    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", members)

    first = client.get("/groups/members/list", params={"group": SALES})
    assert first.text.count("Remove…</button>") == board.MEMBERS_PAGE
    assert "122 members · showing 1–50" in _text(first.text) and "Page 1 of 3" in first.text
    assert first.text.index("lead@example.com") < first.text.index("m000@example.com")      # owners first

    last = client.get("/groups/members/list", params={"group": SALES, "page": 9})             # clamped to the last page
    assert "Page 3 of 3" in last.text and last.text.count("Remove…</button>") == 22
    assert 'href="/groups?group=subteam%40example.com"' in last.text                         # a nested group links to the board

    narrowed = client.get("/groups/members/list", params={"group": SALES, "q": "m11"})
    assert "10 of 122 members match “m11”" in _text(narrowed.text) and "Page 1" not in narrowed.text
    assert "No member matches" in client.get("/groups/members/list", params={"group": SALES, "q": "zzz"}).text


def test_a_member_with_no_address_has_no_remove(client, monkeypatch):
    # "Everyone in the domain" is a CUSTOMER member with no email: there is no address to confirm or remove.
    async def members(group):
        return [GroupMember(email="", member_type="CUSTOMER"), GroupMember(email="alice@example.com")]
    monkeypatch.setattr(client.app.state.gamgui.connector, "list_group_members", members)
    r = client.get("/groups/members/list", params={"group": SALES})
    assert "customer" in r.text and r.text.count("Remove…</button>") == 1


def test_a_group_gam_cannot_read_says_why(client):
    r = client.get("/groups/members", params={"group": "nosuch@example.com"})
    assert "border-amber-300 bg-amber-50" in r.text and "No members yet" not in r.text


def test_the_board_needs_a_connection(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        assert "Connect a domain first" in c.get("/groups").text
        assert "Not connected." in c.get("/groups/members/list", params={"group": SALES}).text
        assert "Not connected." in c.post("/groups/members", data={"group": SALES, "email": "a@example.com"}).text
