from __future__ import annotations

from gamgui.core.gam.models import GAMUser
from gamgui.core.usercache import UserCache
from gamgui.web.routes.users import PAGE_SIZE, _filter_users, _table_context


def _mk(n, suspended_every=0):
    out = []
    for i in range(n):
        d = {"primaryEmail": f"u{i}@e.com", "name": {"givenName": f"User{i}"}}
        if suspended_every and i % suspended_every == 0:
            d["suspended"] = True
        out.append(GAMUser.from_json(d))
    return out


def test_pagination_slices():
    ctx = _table_context(_mk(120), "", "all", 2)
    assert ctx["pages"] == 3 and ctx["page"] == 2 and ctx["total"] == 120
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
