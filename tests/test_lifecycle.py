from __future__ import annotations

from datetime import date

import pytest

from gamgui.core.lifecycle import DEFAULT_MESSAGE, DEFAULT_SUBJECT, build_offboard_steps


def test_offboard_steps_order_and_due_date():
    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj", "Msg", 30, date(2026, 6, 23))
    # Drive + Calendar are ONE transfer step now (a second same-user transfer 409s while the first runs).
    assert [s.key for s in steps] == ["password", "delegate", "vacation", "transfer", "calacls", "reminder"]
    transfer = next(s for s in steps if s.key == "transfer")
    assert transfer.label == "Transfer Drive & Calendar ownership"
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
        "transfer_data", "remove_from_all_calendars", "add_calendar_event",
    ]
    transfers = [c for c in calls if c[0] == "transfer_data"]
    assert len(transfers) == 1                       # one transfer carrying both services
    assert transfers[0][2] == "drive,calendar"       # ONE argv-shaped service list


async def test_offboard_reminder_invites_notify_target():
    captured = {}

    class _R:
        ok = True
        detail = ""

    class _FakeConn:
        async def add_calendar_event(self, cal, summary, start, end, description="", attendee=""):
            captured.update(cal=cal, attendee=attendee)
            return _R()

    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "s", "m", 30, date(2026, 6, 23), notify="it@e.com")
    await steps[-1].action(_FakeConn())  # the reminder step
    assert captured["cal"] == "mgr@e.com" and captured["attendee"] == "it@e.com"
    assert "invites it@e.com" in steps[-1].summary


async def test_offboard_autoreply_substitutes_employee_and_manager():
    captured = {}

    class _R:
        ok = True
        detail = ""

    class _FakeConn:
        async def set_vacation(self, email, subject, message):
            captured.update(email=email, subject=subject, message=message)
            return _R()

    steps = build_offboard_steps("jane@e.com", "bob@e.com", DEFAULT_SUBJECT, DEFAULT_MESSAGE,
                                 30, date(2026, 6, 23), employee_name="Jane Doe")
    vac = next(s for s in steps if s.key == "vacation")
    await vac.action(_FakeConn())

    assert captured["email"] == "jane@e.com"
    assert "Jane Doe" in captured["subject"] and "Jane Doe" in captured["message"]
    assert "bob@e.com" in captured["message"]                                   # manager as contact
    assert "{employee}" not in captured["message"] and "{manager}" not in captured["message"]
    assert "Jane Doe" in vac.summary and "bob@e.com" in vac.summary             # preview shows it filled


@pytest.mark.asyncio
async def test_offboard_transfer_step_invokes_combined_service_list(connector):
    # Bug 1 regression: the single transfer step must audit ONE `create datatransfer` whose service
    # element is the "drive,calendar" list — proving we no longer fire two overlapping same-user
    # transfers (the second of which 409'd in production).
    steps = build_offboard_steps("leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    transfer = next(s for s in steps if s.key == "transfer")
    res = await transfer.action(connector)
    assert res.ok
    rec = next(e for e in connector.audit.tail() if e["action"] == "transfer_data")
    assert rec["ok"] and rec["argv"] == [
        "create", "datatransfer", "leaver@example.com", "drive,calendar", "mgr@example.com",
    ]


@pytest.mark.asyncio
async def test_second_same_user_transfer_still_409s(connector):
    # Proves the regression is real: a second overlapping transfer for the same user 409s. The fix is
    # NOT to swallow this — it's to never issue two (Bug 1 merges to one), so this stays a hard failure.
    from gamgui.core.gam.errors import GAMError

    res = await connector.transfer_data("CONFLICT409@example.com", "calendar", "mgr@example.com")
    assert not res.ok
    with pytest.raises(GAMError) as ei:
        await connector.runner.run_authenticated(
            "example.com",
            ["create", "datatransfer", "CONFLICT409@example.com", "calendar", "mgr@example.com"],
            serialize=True,
        )
    assert ei.value.exit_code == 9 and "already in progress" in ei.value.stderr


@pytest.mark.asyncio
async def test_offboard_calendar_sweep_tolerates_own_acl(connector):
    # Bug 2 regression: the all-users sweep hits the departing user's OWN primary calendar; GAM exits
    # 50 with "Cannot change your own access level." Now classified PERMISSION_DENIED and tolerated,
    # so the step still counts as success (audited ok, tolerated).
    steps = build_offboard_steps("leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    calacls = next(s for s in steps if s.key == "calacls")
    res = await calacls.action(connector)
    assert res.ok and "best-effort" in (res.detail or "")
    rec = next(e for e in connector.audit.tail() if e["action"] == "remove_from_all_calendars")
    assert rec["ok"] and rec.get("extra", {}).get("tolerated") is True
