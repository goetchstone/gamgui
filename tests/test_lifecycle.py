from __future__ import annotations

from datetime import date

import pytest

import shlex

from gamgui.core.gam.commands import GAMCommands
from gamgui.core.lifecycle import DEFAULT_MESSAGE, DEFAULT_SUBJECT, STEP_NAMES, build_offboard_steps, command_line

from .helpers import gam_writes


def test_offboard_steps_order_and_due_date():
    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj", "Msg", 30, date(2026, 6, 23))
    # Drive + Calendar are ONE transfer step now (a second same-user transfer 409s while the first runs).
    assert [s.key for s in steps] == ["password", "revoke", "forward", "delegate", "vacation", "transfer",
                                      "calacls", "reminder"]
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

        async def revoke_access(self, e):
            calls.append(("revoke_access", e)); return _R()

        async def forward_off(self, e):
            calls.append(("forward_off", e)); return _R()

        async def add_delegate(self, e, d):
            calls.append(("add_delegate", e, d)); return _R()

        async def set_vacation(self, e, s, m):
            calls.append(("set_vacation", e)); return _R()

        async def transfer_data(self, o, svc, n, privacy=""):
            calls.append(("transfer_data", o, svc, n, privacy)); return _R()

        async def remove_from_all_calendars(self, e):
            calls.append(("remove_from_all_calendars", e)); return _R()

        async def add_calendar_event(self, cal, summary, start, end, description="", attendee=""):
            calls.append(("add_calendar_event", cal)); return _R()

    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj", "Msg", 30, date(2026, 6, 23))

    for s in steps:
        await s.action(_FakeConn())

    assert [c[0] for c in calls] == [
        "reset_password", "revoke_access", "forward_off", "add_delegate", "set_vacation",
        "transfer_data", "remove_from_all_calendars", "add_calendar_event",
    ]
    transfers = [c for c in calls if c[0] == "transfer_data"]
    assert len(transfers) == 1                       # one transfer carrying both services
    assert transfers[0][2] == "drive,calendar"       # ONE argv-shaped service list
    assert transfers[0][4] == "all"                  # private and shared Drive files


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
    # …by email: GAM's `add event` sends no invitation unless told to (sendUpdates defaults to none).
    assert steps[-1].commands[0][-4:] == ["attendee", "it@e.com", "sendupdates", "all"]


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


def test_offboard_autoreply_is_sent_as_the_text_the_preview_shows():
    # The preview block shows the message with its line breaks (whitespace-pre-line); the reply is sent
    # as HTML, where a raw newline is just a space — and GAM only turns the two characters `\n` into
    # `<br/>` (setVacation). So the text is escaped and each line break sent as <br/>; a `<` or `&` the
    # operator typed is text, and a typed backslash can't become a line break.
    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "Subj",
                                 "Line one.\r\n\r\nAsk {manager} <IT> & co.\nC:\\new", 30, date(2026, 6, 23),
                                 manager_contact="Mo Gr (mgr@e.com)")
    vac = next(s for s in steps if s.key == "vacation")
    [argv] = vac.commands
    assert argv[argv.index("message") + 1] == (
        "Line one.<br/><br/>Ask Mo Gr (mgr@e.com) &lt;IT&gt; &amp; co.<br/>C:&#92;new")
    assert argv[argv.index("message") + 2] == "html"
    assert "Line one.\r\n\r\nAsk Mo Gr (mgr@e.com) <IT> & co." in vac.summary       # the preview: the text


@pytest.mark.asyncio
async def test_offboard_autoreply_does_not_inherit_the_leavers_old_vacation_settings(connector, gam_state):
    # GAM merges `vacation` into the stored settings. A leaver who once answered only people in their
    # organization, only their contacts, or set a last day (now past) kept all three: customers got
    # no auto-reply, or nobody did — and the step showed ✓. The auto-reply now names every setting.
    from gamgui.core.gam.models import Vacation

    leaver = "leaver@example.com"
    await connector.runner.run_authenticated("example.com", [   # the leaver's own earlier holiday reply
        "user", leaver, "vacation", "off", "contactsonly", "true", "domainonly", "true",
        "start", "2025-08-08", "end", "2025-08-15"], serialize=True)
    steps = build_offboard_steps(leaver, "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    assert (await next(s for s in steps if s.key == "vacation").action(connector)).ok
    shown = await connector.runner.run_authenticated("example.com", GAMCommands.show_vacation(leaver))
    vac = Vacation.from_show_text(shown)
    assert vac.enabled and not vac.contacts_only and not vac.domain_only
    assert "Start Date: Started" in shown and "End Date: NotSpecified" in shown   # no dates left over


@pytest.mark.asyncio
async def test_offboard_transfer_step_invokes_combined_service_list(connector):
    # Bug 1 regression: the single transfer step must audit ONE `create datatransfer` whose service
    # element is the "drive,calendar" list — proving we no longer fire two overlapping same-user
    # transfers (the second of which 409'd in production). It names the Drive privacy level `all`
    # (private AND shared files): without one, the Data Transfer API's own default decides whether the
    # files the leaver had shared move — and whatever stays is lost when the account is deleted.
    steps = build_offboard_steps("leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    transfer = next(s for s in steps if s.key == "transfer")
    res = await transfer.action(connector)
    assert res.ok
    rec = next(e for e in connector.audit.tail() if e["action"] == "transfer_data")
    assert rec["ok"] and rec["argv"] == [
        "create", "datatransfer", "leaver@example.com", "drive,calendar", "mgr@example.com", "all",
    ]
    assert transfer.commands == [rec["argv"]]


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
    # 50 with "Cannot change your own access level." Now classified OWN_ACL and tolerated,
    # so the step still counts as success (audited ok, tolerated). OWNACL makes the mock refuse it.
    steps = build_offboard_steps("OWNACL-leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    calacls = next(s for s in steps if s.key == "calacls")
    res = await calacls.action(connector)
    assert res.ok and "best-effort" in (res.detail or "")
    rec = next(e for e in connector.audit.tail() if e["action"] == "remove_from_all_calendars")
    assert rec["ok"] and rec.get("extra", {}).get("tolerated") is True


@pytest.mark.asyncio
async def test_offboard_calendar_sweep_clean_success_and_real_failure(connector):
    # The sweep's exit-0 path (the mock used to fail it unconditionally, so it was never exercised)
    # and a failure the connector must NOT tolerate (a missing scope is not a per-entity notice).
    ok = await connector.remove_from_all_calendars("leaver@example.com")
    assert ok.ok and not ok.detail
    rec = connector.audit.tail()[-1]
    assert rec["ok"] is True and rec["argv"] == ["all", "users", "delete", "calendaracls", "primary", "leaver@example.com"]
    assert "tolerated" not in rec.get("extra", {})

    bad = await connector.remove_from_all_calendars("SWEEPFAIL@example.com")
    assert not bad.ok and "insufficient authentication scopes" in bad.detail
    assert connector.audit.tail()[-1]["ok"] is False


@pytest.mark.asyncio
async def test_offboard_calendar_sweep_multi_user_stderr(connector):
    # Q8: a real sweep prints one stderr line per entity. All tolerable (not-applicable user, a user
    # without Calendar, the leaver's own ACL, amid GAM's "Getting all/Got N" chatter) -> best-effort
    # success; one real per-user failure among them -> the step fails and shows that failure, not the
    # benign tail line.
    ok = await connector.remove_from_all_calendars("SWEEPBENIGN-leaver@example.com")
    assert ok.ok and "best-effort" in (ok.detail or "")
    assert connector.audit.tail()[-1]["extra"]["tolerated"] is True
    from gamgui.core.gam.errors import GAMError, GAMErrorKind
    with pytest.raises(GAMError) as ei:                     # what that stderr held: every tolerated kind
        await connector.runner.run_authenticated(
            "example.com", GAMCommands.remove_all_calendar_acls("SWEEPBENIGN-leaver@example.com"), serialize=True)
    assert ei.value.kinds == {GAMErrorKind.NOT_FOUND, GAMErrorKind.SERVICE_NOT_ENABLED, GAMErrorKind.OWN_ACL}

    bad = await connector.remove_from_all_calendars("SWEEPMIXED-leaver@example.com")
    assert not bad.ok and "Internal error encountered" in bad.detail
    rec = connector.audit.tail()[-1]
    assert rec["ok"] is False and rec["extra"]["tolerated"] is False


@pytest.mark.asyncio
async def test_offboard_sweep_timeout_is_a_clear_step_failure(connector, monkeypatch):
    # The all-users sweep is still walking the domain when its timeout fires: the step fails and says
    # it was stopped partway (not a hang, not a tolerated "best-effort" success), and the routine goes
    # on to the manager's reminder.
    from gamgui.core.connectors import gam_connector
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.lifecycle import _run_offboard

    monkeypatch.setattr(gam_connector, "DOMAIN_WIDE_TIMEOUT", 0.5)
    steps = build_offboard_steps("SWEEPSLOW-leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps))
    await _run_offboard(job, connector, steps)
    assert (job.applied, job.failed_items) == (len(steps) - 1, ["Remove from everyone's calendars"])
    [line] = [ln for ln in job.log if ln.startswith("✗ ")]
    assert "timed out after 0.5s and was stopped" in line
    assert job.log[-1].startswith("✓ ") and "reminder" in job.log[-1]
    rec = next(e for e in connector.audit.tail() if e["action"] == "remove_from_all_calendars")
    assert rec["ok"] is False and rec["extra"]["tolerated"] is False


def test_quitting_mid_offboard_stops_the_sweep_and_audits_it(connector, gam_calls, tmp_path):
    # Quitting mid-offboard: the server's lifespan cancels the running job (leaving the TestClient
    # block is that shutdown). The hour-long sweep may already have deleted some users' ACLs, so the
    # audit log must say it was interrupted — it used to record nothing at all, not even a failure,
    # and its gam kept running (review F3). The steps before it stay audited as done.
    import asyncio
    import time

    from fastapi.testclient import TestClient

    from gamgui.web.jobs import start_job
    from gamgui.web.routes.lifecycle import _run_offboard
    from gamgui.web.server import AppState, create_app

    from .helpers import TEST_HOSTS

    state = AppState(vault=connector.runner.vault, runner=connector.runner, audit_domain="example.com",
                     connector=connector, token="t")
    steps = build_offboard_steps("SWEEPSLOW-leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as client:
        job = start_job(state.jobs, len(steps))

        async def _start() -> None:
            job.task = asyncio.create_task(_run_offboard(job, connector, steps))

        client.portal.call(_start)
        deadline = time.monotonic() + 10
        while not any(c[:4] == ["all", "users", "delete", "calendaracls"] for c in gam_calls()):
            assert time.monotonic() < deadline, "the sweep never started"
            time.sleep(0.02)
    assert job.task.cancelled()
    rec = connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("remove_from_all_calendars", "SWEEPSLOW-leaver@example.com", False)
    assert rec["argv"] == GAMCommands.remove_all_calendar_acls("SWEEPSLOW-leaver@example.com")
    assert "interrupted" in rec["extra"]["error"] and "part of its work" in rec["extra"]["error"]
    assert rec["extra"]["tolerated"] is False
    assert [e["ok"] for e in connector.audit.tail()[:-1]] == [True] * (len(steps) - 2)  # the steps before it
    assert list(tmp_path.glob("gamcfg-*")) == []        # the sweep's credentials were wiped too


@pytest.mark.asyncio
async def test_offboard_preview_commands_are_what_runs(connector, gam_calls):
    # The preview prints each step's `commands`; the connector builds its own argv. Every write the
    # mock received, in order, must be exactly the previewed list — any drift between the two fails here.
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.lifecycle import _run_offboard

    steps = build_offboard_steps("leaver@example.com", "mgr@example.com", "{employee} has left",
                                 "Line one.\nAsk {manager} — it's fine", 30, date(2026, 6, 23),
                                 notify="it@example.com", employee_name="Lee Ver",
                                 manager_contact="Mo Gr (mgr@example.com)")
    job = start_job({}, len(steps))
    await _run_offboard(job, connector, steps)
    assert (job.applied, job.failed_items) == (len(steps), [])
    assert gam_writes(gam_calls()) == [argv for s in steps for argv in s.commands]
    assert [len(s.commands) for s in steps] == [1] * len(steps)        # one command, one ✓/✗ per step


def test_offboard_step_dependencies_are_the_documented_ones():
    # The runbook's "When a step fails" table. Changing a rule is a decision: update both.
    steps = build_offboard_steps("leaver@e.com", "mgr@e.com", "s", "m", 30, date(2026, 6, 23))
    assert {s.key: s.requires for s in steps} == {
        "password": (), "revoke": ("password",), "forward": ("password",), "delegate": ("password",),
        "vacation": ("password", "delegate"),
        "transfer": ("password", "delegate"), "calacls": ("password",),
        "reminder": ("password", "delegate", "transfer"),
    }


async def _offboard(connector, user, manager="mgr@example.com"):
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.lifecycle import _run_offboard

    steps = build_offboard_steps(user, manager, "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps))
    await _run_offboard(job, connector, steps)
    assert job.finished and job.done == len(steps)
    return job


@pytest.mark.asyncio
async def test_offboard_failed_reset_stops_the_routine(connector, gam_calls):
    # The account can still sign in: nothing may announce the departure or move data.
    job = await _offboard(connector, "missing-leaver@example.com")
    assert (job.applied, job.failed_items) == (0, ["Reset password"])
    assert job.skipped == ["Revoke access & sign out", "Turn off forwarding", "Set delegate", "Set auto-responder",
                           "Transfer Drive & Calendar ownership",
                           "Remove from everyone's calendars", "30-day reminder for mgr@example.com"]
    assert [w[:3] for w in gam_writes(gam_calls())] == [["update", "user", "missing-leaver@example.com"]]
    assert job.log[1] == "– Revoke access & sign out — not run: “Reset password” didn't succeed"


@pytest.mark.asyncio
async def test_offboard_failed_delegate_stops_the_hand_over_but_not_the_sweep(connector, gam_calls):
    # The first write to the manager failed; the auto-reply names them and the transfer and the
    # reminder go to the same account, so those stop. The calendar sweep never touches the manager —
    # it still takes the leaver off colleagues' calendars.
    job = await _offboard(connector, "leaver@example.com", manager="missing-mgr@example.com")
    assert (job.applied, job.failed_items) == (4, ["Set delegate"])
    assert job.skipped == ["Set auto-responder", "Transfer Drive & Calendar ownership",
                           "30-day reminder for missing-mgr@example.com"]
    assert [w[:4] for w in gam_writes(gam_calls())] == [
        ["update", "user", "leaver@example.com", "password"], ["user", "leaver@example.com", "deprovision", "signout"],
        ["user", "leaver@example.com", "forward", "off"], ["user", "leaver@example.com", "add", "delegate"],
        ["all", "users", "delete", "calendaracls"]]


@pytest.mark.asyncio
async def test_an_interrupted_offboard_marks_what_it_never_reached(connector, monkeypatch):
    # Quitting mid-run cancels the job. Every step it never finished must be "not run", so the panel
    # can't say "complete" or promise the manager a reminder that was never added.
    import asyncio

    from gamgui.core.connectors import gam_connector
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.lifecycle import _run_offboard

    monkeypatch.setattr(gam_connector, "DOMAIN_WIDE_TIMEOUT", 30)
    steps = build_offboard_steps("SWEEPSLOW-leaver@example.com", "mgr@example.com", "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps))
    task = asyncio.create_task(_run_offboard(job, connector, steps))
    while job.current != "Remove from everyone's calendars":  # noqa: ASYNC110 — polls the job as the UI does
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.finished and job.applied == len(steps) - 2
    assert job.skipped == ["Remove from everyone's calendars", "30-day reminder for mgr@example.com"]
    assert job.log[-2:] == ["– Remove from everyone's calendars — interrupted before it finished",
                            "– 30-day reminder for mgr@example.com — not run: interrupted"]


def test_the_connected_admin_cannot_be_offboarded():
    # Revoking the account GamGUI is connected as deletes GamGUI's own OAuth token mid-run.
    from gamgui.core.gam.models import GAMUser
    from gamgui.core.lifecycle import check_addresses

    directory = [GAMUser(primary_email="admin@example.com", is_admin=True),
                 GAMUser(primary_email="mgr@example.com")]
    check = check_addresses(directory, "Admin@example.com", "mgr@example.com", connected_admin="admin@example.com")
    assert check.errors and "GamGUI is connected as admin@example.com" in check.errors[0]
    assert not check_addresses(directory, "admin@example.com", "mgr@example.com").errors


@pytest.mark.asyncio
async def test_offboard_failed_sign_out_is_a_failed_step_that_stops_nothing(connector, gam_calls):
    # The sign-out rode inside "Reset password" and its failure was swallowed: ✓ and "6 of 6
    # succeeded" while the leaver's sessions stayed open (failure-log 2026-09-23). Ending access is its
    # own step now: ✗, counted, re-runnable alone — and nothing waits on it (the reset already locked
    # new sign-ins, and a missing security scope must not strand the mailbox without its delegate).
    job = await _offboard(connector, "SIGNOUTFAIL-leaver@example.com")
    assert job.failed_items == ["Revoke access & sign out"] and job.skipped == []
    assert job.applied == len(STEP_NAMES) - 1
    [line] = [ln for ln in job.log if ln.startswith("✗ ")]
    assert line.startswith("✗ Revoke access & sign out — ") and "Sign Out Failed" in line
    audit = [(e["action"], e["ok"]) for e in connector.audit.tail()]
    assert ("revoke_access", False) in audit and ("reset_password", True) in audit


@pytest.mark.asyncio
async def test_offboard_turns_off_forwarding_and_a_failure_stops_nothing(connector, gam_calls):
    # The mailbox stays live for the delegate, so a leaver's auto-forward to a personal address kept
    # company mail flowing out until the account was deleted, 30+ days later. It is turned off after the
    # sign-out (the leaver could switch it on until then), on or not; a failure is a ✗ that stops nothing.
    await _offboard(connector, "leaver@example.com")
    writes = gam_writes(gam_calls())
    assert writes.index(["user", "leaver@example.com", "forward", "off"]) == 2       # after the revoke
    job = await _offboard(connector, "FWDFAIL-leaver@example.com")
    assert job.failed_items == ["Turn off forwarding"] and job.skipped == []
    [line] = [ln for ln in job.log if ln.startswith("✗ ")]
    assert "Gmail Service/App not enabled" in line


@pytest.mark.asyncio
async def test_offboard_failed_transfer_skips_only_the_reminder(connector, gam_calls):
    # The reminder asks the manager to approve deletion — which, without the transfer, loses the files.
    job = await _offboard(connector, "CONFLICT409-leaver@example.com")
    assert (job.applied, job.failed_items) == (6, ["Transfer Drive & Calendar ownership"])
    assert job.skipped == ["30-day reminder for mgr@example.com"]
    assert not [w for w in gam_writes(gam_calls()) if "event" in w]
    assert job.log[-1].startswith("– 30-day reminder") and "Transfer Drive" in job.log[-1]


def test_command_line_shows_argument_bounds_and_masks_secrets():
    argv = GAMCommands.set_vacation("a@example.com", "Jane has left", "It's \"done\"\nbye")
    line = command_line(argv)
    assert line.startswith("gam user a@example.com vacation on subject 'Jane has left' message ")
    assert shlex.split(line)[1:] == argv                             # each element's bounds survive
    assert "It's" in line and "'\"'\"'" not in line                   # an apostrophe stays readable
    # GAM generates `password random` itself — nothing secret to hide, so it reads as it runs.
    assert command_line(GAMCommands.reset_password("a@example.com")) == (
        "gam update user a@example.com password random changepassword off")
    typed = command_line(GAMCommands.create_user("n@example.com", "N", "U", "S3cret pw!", notify="b@example.com"))
    assert "S3cret" not in typed and typed.count("***redacted***") == 2
