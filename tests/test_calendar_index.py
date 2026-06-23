"""The persistent calendar index (SQLite) + the domain scan that fills it.

This is what makes calendar name-search scale past a handful of employees: scan the domain once in
the background, store it locally, then search the index instantly — even across app restarts.
"""

from __future__ import annotations

import os
import stat

import pytest

from gamgui.core.calendar_index import CalendarIndex, IndexedCalendar


def test_index_crud_and_status(tmp_path):
    idx = CalendarIndex(tmp_path / "c.db")
    assert idx.is_empty() and idx.status().count == 0

    n = idx.replace_all("d.com", [
        IndexedCalendar("c_x@group.calendar.google.com", "Team X", "o@d.com", "secondary", 3),
        IndexedCalendar("room@resource.calendar.google.com", "Aspen", "", "room", 0),
    ])
    assert n == 2 and not idx.is_empty()
    st = idx.status()
    assert st.count == 2 and st.domain == "d.com" and st.updated_at is not None

    assert idx.search("team")[0].summary == "Team X"          # name match
    assert idx.search("aspen")[0].kind == "room"              # rooms indexed too
    assert idx.search("nothing-here") == []
    # Rooms sort first, then by name.
    assert [c.kind for c in idx.search("")][0] == "room"

    idx.remove("c_x@group.calendar.google.com")               # e.g. right after deleting it
    assert idx.search("team") == [] and idx.status().count == 1


def test_replace_all_is_atomic_swap(tmp_path):
    idx = CalendarIndex(tmp_path / "c.db")
    idx.replace_all("d.com", [IndexedCalendar("a@group.calendar.google.com", "Old", "", "secondary", 1)])
    idx.replace_all("d.com", [IndexedCalendar("b@group.calendar.google.com", "New", "", "secondary", 1)])
    summaries = {c.summary for c in idx.search("")}
    assert summaries == {"New"}                               # the old scan is fully replaced


def test_index_file_is_owner_only(tmp_path):
    # Calendar names + owner emails are domain-sensitive — keep them off other local accounts.
    idx = CalendarIndex(tmp_path / "sub" / "c.db")
    idx.replace_all("d.com", [IndexedCalendar("c_x@group.calendar.google.com", "X", "o@d.com", "secondary", 1)])
    assert stat.S_IMODE(os.stat(idx.path).st_mode) & 0o077 == 0      # file: no group/other access
    assert stat.S_IMODE(os.stat(idx.path.parent).st_mode) & 0o077 == 0  # dir: 0700


def test_search_treats_like_wildcards_literally(tmp_path):
    idx = CalendarIndex(tmp_path / "c.db")
    idx.replace_all("d.com", [
        IndexedCalendar("c_a@group.calendar.google.com", "Sales", "", "secondary", 1),
        IndexedCalendar("c_b@group.calendar.google.com", "S_les", "", "secondary", 1),
    ])
    # '_' must match literally, not as the SQL single-char wildcard (which would also hit "Sales").
    assert [c.summary for c in idx.search("S_l")] == ["S_les"]


def test_index_survives_reopen(tmp_path):
    path = tmp_path / "c.db"
    CalendarIndex(path).replace_all("d.com", [
        IndexedCalendar("c_x@group.calendar.google.com", "Persisted", "o@d.com", "secondary", 1)])
    # A brand-new instance (mimics an app restart) reads the same file.
    assert CalendarIndex(path).search("persisted")[0].summary == "Persisted"


@pytest.mark.asyncio
async def test_scan_all_calendars_filters_and_aggregates(connector):
    """Against the mock tenant: keep shared calendars + rooms, drop primaries/holidays, find owners."""
    cals = await connector.scan_all_calendars()
    by_id = {c.id: c for c in cals}

    house = by_id["c_house123@group.calendar.google.com"]
    assert house.summary == "House Call Calendar"
    assert house.owner == "alice@example.com"                 # the accessRole=owner row
    assert house.subscribers == 2 and house.kind == "secondary"

    assert "alice@example.com" not in by_id                   # a user's PRIMARY calendar is excluded
    assert any(c.kind == "room" for c in cals)                # rooms folded in
    assert all(c.kind == "room" or c.id.endswith("@group.calendar.google.com") for c in cals)
