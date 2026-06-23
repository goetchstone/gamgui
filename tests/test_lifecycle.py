from __future__ import annotations

from datetime import date

from gamgui.core.lifecycle import build_offboard_steps


def test_offboard_steps_order_and_due_date():
    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj", "Msg", 30, date(2026, 6, 23))
    assert [s.key for s in steps] == ["password", "delegate", "vacation", "drive", "calendar", "calacls", "reminder"]
    assert "30-day" in steps[-1].label
    assert "2026-07-23" in steps[-1].summary  # 2026-06-23 + 30 days


async def test_offboard_steps_call_the_right_connector_methods():
    calls = []

    class _R:
        ok = True
        detail = ""

    class _FakeConn:
        async def reset_password(self, e):
            calls.append(("reset_password", e)); return _R()

        async def add_delegate(self, e, d):
            calls.append(("add_delegate", e, d)); return _R()

        async def set_vacation(self, e, s, m):
            calls.append(("set_vacation", e)); return _R()

        async def transfer_data(self, o, svc, n):
            calls.append(("transfer_data", o, svc, n)); return _R()

        async def remove_from_all_calendars(self, e):
            calls.append(("remove_from_all_calendars", e)); return _R()

        async def add_calendar_event(self, cal, summary, start, end, description="", attendee=""):
            calls.append(("add_calendar_event", cal)); return _R()

    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj", "Msg", 30, date(2026, 6, 23))

    for s in steps:
        await s.action(_FakeConn())

    assert [c[0] for c in calls] == [
        "reset_password", "add_delegate", "set_vacation",
        "transfer_data", "transfer_data", "remove_from_all_calendars", "add_calendar_event",
    ]
    transfers = [c for c in calls if c[0] == "transfer_data"]
    assert transfers[0][2] == "drive" and transfers[1][2] == "calendar"  # drive then calendar
