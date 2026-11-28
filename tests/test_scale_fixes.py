"""The app-side costs scripts/scale_bench.py found growing with the domain (docs/scale.md), each held here."""

from __future__ import annotations

import pytest

from gamgui.core import clock
from gamgui.core.gam.models import GAMUser
from gamgui.core.usercache import UserCache
from gamgui.web.server import CachePatches

from .test_users_web import client  # noqa: F401


def _users(n):
    return [GAMUser(primary_email=f"u{i}@example.com") for i in range(n)]


def test_many_patches_are_one_rebuild_and_a_stranger_drops_the_list():
    cache = UserCache()
    cache._items = _users(5)
    before = cache._items
    cache.patch_many(["u1@example.com", "u3@example.com"], lambda u: u.primary_email, lambda u: u)
    assert cache._items is not before and len(cache._items) == 5       # one new list, never an edit in place
    cache.patch_many(["u1@example.com", "nobody@example.com"], lambda u: u.primary_email, lambda u: u)
    assert cache._items is None                                         # an account it doesn't show as it is


def test_a_bulk_run_s_patches_land_once_a_second_and_at_the_end(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    flushed = []

    class St:
        def patch_users(self, emails, **changes):
            flushed.append((list(emails), changes))

    batch = CachePatches(St(), {"suspended": True})
    batch.add("a@example.com")
    batch.add("b@example.com")
    assert flushed == []                                                 # not one rebuild an account
    now[0] += 1.5
    batch.add("c@example.com")
    assert flushed == [(["a@example.com", "b@example.com", "c@example.com"], {"suspended": True})]
    batch.add("d@example.com")
    batch.flush()
    assert flushed[-1] == (["d@example.com"], {"suspended": True})


def test_a_licence_card_reads_the_seats_once(client, gam_calls):  # noqa: F811
    # The card read every seat in the domain on every click (docs/scale.md).
    for _ in range(3):
        assert client.get("/users/licenses?email=alice%40example.com").status_code == 200
    assert sum(1 for c in gam_calls() if c[:2] == ["print", "licenses"]) == 1


@pytest.mark.parametrize("write", ["add_license", "swap_license", "remove_license"])
async def test_every_licence_change_clears_the_card_s_seats(connector, monkeypatch, write):
    from gamgui.core.gam.models import LicenseSeat

    async def ran(*a, **k):
        return None
    monkeypatch.setattr(connector, "_run_write", ran)
    connector._seats = (clock.now(), ["stale"])
    seat = LicenseSeat(user="a@example.com", product_id="Google-Apps", sku_id="1010020027", sku="Business Starter")
    await (getattr(connector, write)("a@example.com", seat, "1010020028") if write == "swap_license"
           else getattr(connector, write)("a@example.com", seat))
    assert connector._seats is None


@pytest.mark.parametrize("path", ["/signatures", "/users/bulk", "/users/bulk-ou"])
def test_a_picker_page_reads_the_groups_once(client, gam_calls, path):  # noqa: F811
    for _ in range(3):
        assert client.get(path).status_code == 200
    assert sum(1 for c in gam_calls() if c[:2] == ["print", "groups"]) == 1


async def test_the_directory_read_has_room_for_a_large_domain(connector, monkeypatch):
    from gamgui.core.connectors.gam_connector import DIRECTORY_TIMEOUT
    seen = {}

    async def run(domain, argv, timeout=None, serialize=False, **kw):
        seen["timeout"] = timeout
        return ""
    monkeypatch.setattr(connector.runner, "run_authenticated", run)
    await connector.list_users()
    assert seen["timeout"] == DIRECTORY_TIMEOUT >= 600
