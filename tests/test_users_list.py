"""The Users list's view (plan U8): search over five fields, sort by any column over the whole cached
list, a page-size choice, and the view in the URL — so a reload, Back, or a detail page's "← Users"
reopens the list as it was left. The browser half (Back, the tab hash) is tests/test_a11y.py's
test_back_from_a_user_reopens_the_list_as_left."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core.gam.models import GAMUser
from gamgui.web.routes.users import PAGE_SIZE, PAGE_SIZES, _filter_users, _list_url_from, _sort_users

from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture


def _user(i: int, **fields) -> GAMUser:
    return GAMUser.from_json({"primaryEmail": f"p{i:03}@example.com", "name": {"givenName": f"P{i:03}", "familyName": "Ex"},
                              **fields})


@pytest.fixture
def many(client, monkeypatch):  # noqa: F811
    """40 cached people: titles Clerk/Manager/none, OUs /A, /B, /C, every fifth suspended."""
    people = [_user(i, orgUnitPath=f"/{'ABC'[i % 3]}", suspended=i % 5 == 0,
                    organizations=[{"primary": True, "title": ("Clerk", "Manager", "")[i % 3]}]) for i in range(40)]

    async def users(force=False):
        return people
    monkeypatch.setattr(client.app.state.gamgui, "users", users)
    return client


def _emails(html: str) -> list[str]:
    return re.findall(r'<td class="px-5 py-1 text-brand-blueink">([^<]+@example\.com)</td>', html)


def test_search_matches_name_email_title_department_and_org_unit():
    people = [GAMUser.from_json({"primaryEmail": "a@example.com", "name": {"givenName": "Ann"}}),
              GAMUser.from_json({"primaryEmail": "b@example.com", "orgUnitPath": "/Field/North"}),
              GAMUser.from_json({"primaryEmail": "c@example.com", "organizations": [{"primary": True, "department": "Payroll"}]}),
              GAMUser.from_json({"primaryEmail": "d@example.com", "organizations": [{"primary": True, "title": "Buyer"}]})]
    found = {q: [u.primary_email for u in _filter_users(people, q, "all")] for q in ("ann", "north", "PAYROLL", "buyer", "c@")}
    assert found == {"ann": ["a@example.com"], "north": ["b@example.com"], "PAYROLL": ["c@example.com"],
                     "buyer": ["d@example.com"], "c@": ["c@example.com"]}


def test_each_column_sorts_both_ways_with_blanks_last():
    people = [_user(i, orgUnitPath=f"/{'CAB'[i % 3]}", suspended=i == 1,
                    organizations=[{"primary": True, "title": ("", "Buyer", "Agent")[i % 3]}]) for i in range(6)]
    order = {(s, d): [u.primary_email[:4] for u in _sort_users(people, s, d)]
             for s in ("name", "email", "title", "ou", "status") for d in (False, True)}
    assert order[("name", False)] == ["p000", "p001", "p002", "p003", "p004", "p005"]
    assert order[("name", True)] == order[("email", True)] == ["p005", "p004", "p003", "p002", "p001", "p000"]
    assert order[("title", False)] == ["p002", "p005", "p001", "p004", "p000", "p003"]   # no title: last
    assert order[("title", True)] == ["p001", "p004", "p002", "p005", "p000", "p003"]    # ...either way
    assert order[("ou", False)] == ["p001", "p004", "p002", "p005", "p000", "p003"]      # ties by name
    assert order[("status", False)][-1] == "p001" and order[("status", True)][0] == "p001"


def test_the_sort_runs_over_the_whole_list_before_the_page(many):
    first = many.get("/users/table", params={"sort": "email", "desc": 1})
    assert _emails(first.text)[:2] == ["p039@example.com", "p038@example.com"] and len(_emails(first.text)) == PAGE_SIZE
    second = many.get("/users/table", params={"sort": "email", "desc": 1, "page": 2})
    assert _emails(second.text)[0] == "p024@example.com"
    assert 'aria-sort="descending"' in second.text and second.text.count("aria-sort=") == 1
    # The sorted column's header flips it; another column's starts ascending — both back to page 1.
    assert 'hx-get="/users/table?sort=email"' in second.text and 'hx-get="/users/table?sort=title&amp;desc=1"' not in second.text
    assert 'hx-get="/users/table?sort=title"' in second.text


def test_a_page_size_choice_and_an_unknown_one_falls_back(many):
    assert len(_emails(many.get("/users/table", params={"size": 25}).text)) == 25
    assert len(_emails(many.get("/users/table", params={"size": 7}).text)) == PAGE_SIZE
    page = many.get("/users", params={"size": 50}).text
    assert re.search(r'<option value="50" selected>50 per page</option>', page)
    assert [int(n) for n in re.findall(r'<option value="(\d+)"', page)] == list(PAGE_SIZES)
    assert "40 total" in page and "Next" not in page


def test_the_table_keeps_the_address_bar_on_its_view(many):
    r = many.get("/users/table", params={"q": " clerk ", "scope": "active", "sort": "ou", "desc": 1, "size": 25,
                                         "page": 1, "refresh": 1})
    assert r.headers["HX-Replace-Url"] == "/users?q=clerk&scope=active&sort=ou&desc=1&size=25"
    assert many.get("/users/table").headers["HX-Replace-Url"] == "/users"           # the default view is plain /users
    junk = many.get("/users/table", params={"scope": "everyone", "sort": "password", "page": 99})
    assert junk.headers["HX-Replace-Url"] == "/users?page=3"                        # unknown values fall back; page clamps


def test_the_url_reopens_the_same_view(many):
    page = many.get("/users", params={"q": "manager", "scope": "active", "sort": "email", "desc": 1}).text
    assert 'name="q" value="manager"' in page and '<option value="active" selected>' in page
    shown = _emails(page)
    assert len(shown) == 11 and shown[:3] == ["p037@example.com", "p034@example.com", "p031@example.com"]
    assert 'aria-sort="descending"' in page
    assert 'type="hidden" form="users-filter" name="sort" value="email"' in page
    assert 'type="hidden" form="users-filter" name="desc" value="1"' in page


def test_a_user_link_carries_the_view_to_the_detail_pages_back_link(many):
    table = many.get("/users/table", params={"scope": "active", "sort": "title", "page": 2}).text
    link = unescape(re.search(r'href="(/users/detail\?[^"]+)"', table).group(1))
    assert link.endswith("&back=scope%3Dactive%26sort%3Dtitle%26page%3D2")
    detail = many.get(link).text
    assert '<a href="/users?scope=active&amp;sort=title&amp;page=2"' in detail
    assert '<a href="/users" class' in many.get("/users/detail", params={"email": "p000@example.com"}).text


@pytest.mark.parametrize("back, url", [
    ("", "/users"),
    ("q=a&page=2", "/users?q=a&page=2"),
    ("https://evil.example/&q=a", "/users?q=a"),             # a key that isn't the list's goes nowhere
    ("q=x%22%3E%3Cscript%3E", "/users?q=x%22%3E%3Cscript%3E"),  # re-encoded, never raw
    ("page=-4&size=10000&desc=yes&sort=../../x", "/users"),
])
def test_the_back_link_only_ever_reopens_the_list(back, url):
    assert _list_url_from(back) == url


def test_a_pager_click_lands_focus_on_the_count_line(many):
    moved = many.get("/users/table", params={"page": 2}, headers={"HX-Trigger": "users-next"}).text
    assert re.search(r'<span tabindex="-1" data-focus>40 total · showing 16–30 · page 2 of 3</span>', moved)
    sorted_ = many.get("/users/table", params={"sort": "title"}, headers={"HX-Trigger": "sort-title"}).text
    assert "data-focus" not in sorted_          # a sort keeps focus on its header (htmx restores it by id)
    assert 'id="sort-title"' in sorted_
