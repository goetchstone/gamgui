from __future__ import annotations

from gamgui.core.gam.models import GAMUser
from gamgui.core.usercache import UserCache
from gamgui.web.routes.users import PAGE_SIZE, _filter_users, _table_context


def _mk(n, suspended_every=0):
    out = []
    for i in range(n):
        d = {"primaryEmail": f"u{i}@e.com", "name": {"givenName": f"User{i:03}"}}   # the list sorts by name
        if suspended_every and i % suspended_every == 0:
            d["suspended"] = True
        out.append(GAMUser.from_json(d))
    return out


def test_pagination_slices():
    total = PAGE_SIZE * 12          # exercise several pages regardless of the configured page size
    ctx = _table_context(_mk(total), "", "all", 2)
    assert ctx["pages"] == 12 and ctx["page"] == 2 and ctx["total"] == total
    assert len(ctx["users"]) == PAGE_SIZE
    assert ctx["users"][0].primary_email == f"u{PAGE_SIZE}@e.com"


def test_pagination_clamps_overflow_page():
    ctx = _table_context(_mk(10), "", "all", 99)
    assert ctx["page"] == 1 and ctx["pages"] == 1


def test_filter_search_and_scope():
    users = _mk(20, suspended_every=5)  # u0,u5,u10,u15 suspended
    assert [u.primary_email for u in _filter_users(users, "u7", "all")] == ["u7@e.com"]
    assert len(_filter_users(users, "", "active")) == 16
    assert len(_filter_users(users, "", "suspended")) == 4


async def test_user_cache_ttl_force_invalidate():
    cache = UserCache(ttl=300)
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return ["x"]

    assert await cache.get(fetch) == ["x"]
    await cache.get(fetch)  # served from cache
    assert calls["n"] == 1
    await cache.get(fetch, force=True)  # forced refresh
    assert calls["n"] == 2
    cache.invalidate()
    await cache.get(fetch)  # re-fetch after invalidation
    assert calls["n"] == 3


async def test_user_cache_patch_updates_one_record_and_keeps_the_lists_age(monkeypatch):
    # Plan U12: a write whose new values are known patches its record, so the next page doesn't re-run
    # `gam print users` — but the rest of the list is no fresher, so the TTL and force still re-fetch.
    from gamgui.core import clock

    t = {"now": 1000.0}
    monkeypatch.setattr(clock, "now", lambda: t["now"])
    cache, calls = UserCache(ttl=300), {"n": 0}

    async def fetch():
        calls["n"] += 1
        return ["a", "b", "c"]

    held = await cache.get(fetch)
    t["now"] += 200
    cache.patch(lambda i: i == "b", str.upper)
    assert await cache.get(fetch) == ["a", "B", "c"] and calls["n"] == 1
    assert held == ["a", "b", "c"]                  # a new list: what a caller already holds isn't edited
    assert cache.age_seconds == 200                 # the patch didn't reset the age
    cache.patch(lambda i: i == "a")                 # no change: the record goes (a delete)
    assert await cache.get(fetch) == ["B", "c"] and calls["n"] == 1
    t["now"] += 101                                 # past the TTL of the original fetch
    assert await cache.get(fetch) == ["a", "b", "c"] and calls["n"] == 2
    cache.patch(lambda i: i == "b", str.upper)
    assert await cache.get(fetch, force=True) == ["a", "b", "c"] and calls["n"] == 3


async def test_user_cache_patch_nothing_matching_drops_the_list():
    # A write to an account the cache doesn't show (an alias, a minutes-old user): its effect on the
    # list isn't known, so the whole list goes, as before U12.
    cache = UserCache()

    async def fetch():
        return ["a"]

    await cache.get(fetch)
    cache.patch(lambda i: i == "zz", str.upper)
    assert cache.age_seconds is None


async def test_user_cache_does_not_keep_a_fetch_that_raced_a_write():
    # A `print users` that started before a write landed may not show it: returned, but not kept.
    import asyncio

    cache, started, release, calls = UserCache(), asyncio.Event(), asyncio.Event(), {"n": 0}

    async def slow():
        calls["n"] += 1
        started.set()
        await release.wait()
        return ["stale"]

    task = asyncio.create_task(cache.get(slow))
    await started.wait()
    cache.patch(lambda i: True, str.upper)          # nothing cached yet: nothing to patch, but it counts
    release.set()
    assert await task == ["stale"]
    assert cache.age_seconds is None                # not kept: the next read fetches again
