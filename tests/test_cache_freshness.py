"""Plan U12b: past its TTL the directory cache serves a page that shows its age the old list while one
refresh runs behind it — and the safety checks never decide on a stale list."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gamgui.core import clock
from gamgui.core.gam.models import GAMUser
from gamgui.core.usercache import UserCache
from gamgui.web.routes import builder, lifecycle
from gamgui.web.server import AppState, ago

from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture


@pytest.fixture
def t(monkeypatch):
    now = {"now": 1000.0}
    monkeypatch.setattr(clock, "now", lambda: now["now"])
    return now


def _u(email: str) -> GAMUser:
    return GAMUser.from_json({"primaryEmail": email, "name": {"fullName": email.split("@")[0]}})


async def test_past_the_ttl_a_stale_ok_read_gets_the_old_list_and_starts_exactly_one_refresh(t):
    cache, calls, release = UserCache(ttl=300), {"n": 0}, asyncio.Event()

    async def fetch():
        calls["n"] += 1
        if calls["n"] > 1:
            await release.wait()
        return [f"v{calls['n']}"]

    assert await cache.get(fetch) == ["v1"]
    t["now"] += 301
    for _ in range(3):                                   # three page loads while the refresh runs
        assert await cache.get(fetch, stale_ok=True) == ["v1"]
        await asyncio.sleep(0)
    assert cache.refreshing and cache.age_seconds == 301  # the label says how old, and that it's refreshing
    release.set()
    await cache._refresh
    assert calls["n"] == 2                               # one refresh, not one per page load
    assert not cache.refreshing and cache.age_seconds == 0
    assert await cache.get(fetch, stale_ok=True) == ["v2"] and calls["n"] == 2


async def test_without_stale_ok_a_stale_list_is_reread_and_a_failed_read_raises(t):
    cache, fail = UserCache(ttl=300), {"on": False}

    async def fetch():
        if fail["on"]:
            raise RuntimeError("quota")
        return ["fresh"]

    await cache.get(fetch)
    t["now"] += 301
    fail["on"] = True
    with pytest.raises(RuntimeError):                    # never falls back to the old list
        await cache.get(fetch)
    assert await cache.get(fetch, stale_ok=True) == ["fresh"]   # a page that shows the age still gets it
    await cache._refresh                                 # its background refresh failed quietly ...
    assert cache.age_seconds == 301                      # ... and the old list and its age stay


async def test_a_refresh_that_raced_an_invalidate_is_not_kept(t):
    # A tenant switch drops the list; a refresh already reading the old tenant must not put it back.
    cache, release = UserCache(ttl=300), asyncio.Event()

    async def fetch():
        await release.wait()
        return ["old-tenant"]

    release.set()
    await cache.get(fetch)
    release.clear()
    t["now"] += 301
    await cache.get(fetch, stale_ok=True)
    await asyncio.sleep(0)
    cache.invalidate()
    release.set()
    await cache._refresh
    assert cache.age_seconds is None


def _state(users_fetch) -> AppState:
    conn = SimpleNamespace(domain="example.com", list_users=users_fetch)
    return AppState(vault=None, runner=None, connector=conn)   # type: ignore[arg-type]


async def test_offboarding_address_check_rereads_a_stale_list(t):
    lists = [[_u("old@example.com"), _u("boss@example.com")],
             [_u("leaver@example.com"), _u("boss@example.com")]]

    async def fetch(fields=None):
        return lists.pop(0)

    st = _state(fetch)
    await st.users()
    t["now"] += 301
    check = await lifecycle._check(st, "leaver@example.com", "boss@example.com")
    assert not check.errors and not lists                 # checked against the re-read, not the old list


async def test_offboarding_address_check_fails_closed_when_the_reread_fails(t):
    calls = {"n": 0}

    async def fetch(fields=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("network down")
        return [_u("leaver@example.com"), _u("boss@example.com")]

    st = _state(fetch)
    await st.users()
    t["now"] += 301
    check = await lifecycle._check(st, "leaver@example.com", "boss@example.com")
    assert check.errors and "Couldn't read the directory" in check.errors[0]   # the stale list isn't used


async def test_alias_delete_check_asks_gam_not_the_cache():
    # guard.alias_deletes' lookups are live `info user` reads: a cached list, stale or not, is never consulted.
    class NoCache:
        async def get(self, *a, **k):
            raise AssertionError("the alias check read the cache")

    async def primary_address(addr):
        return "owner@example.com"

    st = SimpleNamespace(connector=SimpleNamespace(primary_address=primary_address), user_cache=NoCache())
    blocked = await builder._alias_deletes(st, SimpleNamespace(typed_emails=["alias@example.com"]))
    assert blocked and "owner@example.com" in blocked[0]


def test_ago_words():
    assert [ago(s) for s in (0, 59, 60, 199, 3600, 90000)] == [
        "just now", "just now", "1 min ago", "3 min ago", "1 h ago", "1 d ago"]


def test_users_reports_and_pickers_say_how_old_the_directory_is(client):  # noqa: F811
    assert "total · as of just now" in client.get("/users/table").text
    reports = client.get("/reports").text
    assert "Directory as of just now" in reports and 'href="/reports?refresh=1"' in reports
    finder = client.get("/groups/search?q=").text
    assert "Groups as of just now" in finder and 'hx-get="/groups/search?refresh=1&amp;q=' in finder
    assert "Directory as of just now" in client.get("/builder/pick?kind=users&q=").text
