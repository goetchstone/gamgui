"""The mock `gam` must fail the way real GAM does — tripwires for the mock itself.

A mock more permissive than GAM converts a live break into a green test (CLAUDE.md, "the mock lies"):
its catch-all used to exit 0, so 23 writes with no handler, a `delete events` without `doit` and an
invalid calendar role all "succeeded". Every builder the app calls must be classified below, every
write must be accepted in exactly the shape the builder emits, and a malformed shape must fail.
"""

from __future__ import annotations

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError, GAMErrorKind

CAL = "c_team@group.calendar.google.com"

# Every write builder the app calls, with the options it can emit.
WRITES = {
    "create_user": [C.create_user("new@example.com", "New", "Hire", "Temp-Pa55w0rd", org_unit="/Staff",
                                  notify="mgr@example.com"),
                    C.create_user("new@example.com", "New", "Hire", "Temp-Pa55w0rd", change_password=False)],
    "update_organization": [C.update_organization("alice@example.com", "Lead", "Sales"),
                            C.update_organization("alice@example.com", "", "Sales")],
    "set_suspended": [C.set_suspended("alice@example.com", True), C.set_suspended("alice@example.com", False)],
    "reset_password": [C.reset_password("alice@example.com")],
    "signout_user": [C.signout_user("alice@example.com")],
    "delete_user": [C.delete_user("alice@example.com")],
    "undelete_user": [C.undelete_user("alice@example.com")],
    "add_calendar_acl": [C.add_calendar_acl("alice@example.com", "bob@example.com"),
                         C.add_calendar_acl("alice@example.com", "group:sales@example.com", role="writer")],
    "delete_calendar_acl": [C.delete_calendar_acl("alice@example.com", "user:bob@example.com")],
    "add_calendar_acl_cal": [C.add_calendar_acl_cal(CAL, "bob@example.com"),
                             C.add_calendar_acl_cal(CAL, "domain", role="freebusyreader", send_notifications=True)],
    "delete_calendar_acl_cal": [C.delete_calendar_acl_cal(CAL, "user:bob@example.com")],
    "subscribe_calendar": [C.subscribe_calendar("bob@example.com", CAL),
                           C.subscribe_calendar("bob@example.com", CAL, selected=False)],
    "remove_calendar": [C.remove_calendar("alice@example.com", CAL)],
    "delete_event": [C.delete_event(CAL, "evt-1")],
    "remove_all_calendar_acls": [C.remove_all_calendar_acls("leaver@example.com")],
    "add_calendar_event": [C.add_calendar_event("mgr@example.com", "Confirm", "2026-07-23", "2026-07-24"),
                           C.add_calendar_event("mgr@example.com", "Confirm", "2026-07-23", "2026-07-24",
                                                description="d", attendee="it@example.com")],
    "create_datatransfer": [C.create_datatransfer("leaver@example.com", "drive,calendar", "mgr@example.com")],
    "create_tasklist": [C.create_tasklist("mgr@example.com", "Onboard Ada")],
    "create_task": [C.create_task("mgr@example.com", "MockTasklist_abc123", "Order a laptop", notes="n")],
    "send_email": [C.send_email("new@example.com", "Welcome", "<p>Hi</p>"),
                   C.send_email("new@example.com", "Welcome", "Hi", html=False)],
    "set_signature": [C.set_signature("alice@example.com", "Best,\nAlice"),
                      C.set_signature("alice@example.com", "", html=False)],
    "add_delegate": [C.add_delegate("leaver@example.com", "mgr@example.com")],
    "remove_delegate": [C.remove_delegate("alice@example.com", "assistant@example.com")],
    "set_vacation": [C.set_vacation("alice@example.com", "Away", "Back soon"),
                     C.set_vacation("alice@example.com", "Away", "Back", html=False, start="2026-07-01",
                                    end="2026-07-10", contacts_only=True, domain_only=True)],
    "vacation_off": [C.vacation_off("alice@example.com")],
    "add_forwarding_address": [C.add_forwarding_address("alice@example.com", "fwd@example.com")],
    "set_forward": [C.set_forward("alice@example.com", "fwd@example.com", a) for a in C.FORWARD_ACTIONS],
    "forward_off": [C.forward_off("alice@example.com")],
    "create_user_alias": [C.create_user_alias("a.anders@example.com", "alice@example.com")],
    "delete_alias": [C.delete_alias("a.anders@example.com")],
    "create_group": [C.create_group("new-team@example.com"),
                     C.create_group("new-team@example.com", "New team", "What it is for")],
    "add_group_member": [C.add_group_member("sales@example.com", "carol@example.com", r)
                         for r in ("member", "manager", "owner")],
    "remove_group_member": [C.remove_group_member("sales@example.com", "carol@example.com")],
}

# Reads stay canned, but each one the app issues must still have a handler.
READS = {
    "version": [C.version()],
    "check_svcacct": [C.check_svcacct("admin@example.com")],
    "print_users": [C.print_users(), C.print_users(query="isSuspended=false")],
    "print_cros": [C.print_cros("status:ACTIVE")],
    "print_filelist": [C.print_filelist("alice@example.com", "name contains 'x'")],
    "report_users": [C.report_users("2026-06-16", ["accounts:used_quota_in_mb"])],
    "info_user": [C.info_user("alice@example.com")],
    "print_calendar_acls": [C.print_calendar_acls("alice@example.com")],
    "print_resources": [C.print_resources()],
    "print_user_calendars": [C.print_user_calendars("alice@example.com")],
    "print_all_calendars": [C.print_all_calendars()],
    "print_calendar_acls_cal": [C.print_calendar_acls_cal(CAL)],
    "print_events": [C.print_events(CAL, query="standup")],
    "get_event": [C.get_event(CAL, "evt-1")],
    "print_datatransfers": [C.print_datatransfers("leaver@example.com")],
    "show_signature": [C.show_signature("alice@example.com")],
    "print_delegates": [C.print_delegates("alice@example.com")],
    "print_forwarding_addresses": [C.print_forwarding_addresses("alice@example.com")],
    "search_messages": [C.search_messages("alice@example.com", "from:x")],
    "show_vacation": [C.show_vacation("alice@example.com")],
    "print_groups": [C.print_groups()],
    "print_group_members": [C.print_group_members("sales@example.com")],
    "print_groups_member": [C.print_groups_member("alice@example.com")],
}

# Builders nothing in the app calls (plan item Q11 deletes them), and the `todrive` argv suffix.
NOT_CALLED = {"update_user", "unsubscribe_calendar", "delete_forwarding_address",
              "create_project", "oauth_create", "create_svcacct", "todrive_args"}


def test_every_builder_is_classified():
    # A new builder must land in WRITES (and get a strict mock handler) or READS before it ships.
    builders = {name for name, v in vars(C).items() if isinstance(v, staticmethod)}
    assert builders - set(WRITES) - set(READS) - NOT_CALLED == set()


@pytest.mark.parametrize("argv", [a for cases in {**WRITES, **READS}.values() for a in cases],
                         ids=lambda a: " ".join(a)[:60])
async def test_mock_accepts_every_shape_the_app_emits(runner, domain, argv):
    await runner.run_authenticated(domain, argv, serialize=True)


@pytest.mark.parametrize("argv,needle", [
    # Without doit GAM only reports what it would delete — "Event deleted." would be a lie.
    (["calendars", CAL, "delete", "events", "eventid", "evt-1", "sendupdates", "none"], "Use doit"),
    (C.add_calendar_acl("alice@example.com", "bob@example.com", role="admin"), "Invalid choice (admin)"),
    (C.add_calendar_acl_cal(CAL, "bob@example.com", role="viewer"), "Invalid choice (viewer)"),
    (["user", "alice@example.com", "signature"], "Missing argument"),
    (["user", "alice@example.com", "add", "delegate"], "Missing argument"),
    (C.set_vacation("alice@example.com", "Away", "Back", start="July 1"), "Invalid date"),
    (["update", "user", "alice@example.com", "organization", "title", "T"], "Missing argument"),
    (["update", "group", "sales@example.com", "add", "admin", "carol@example.com"], "Invalid argument"),
    (["create", "datatransfer", "leaver@example.com", "drive,mail", "mgr@example.com"], "Invalid choice (mail)"),
    (["user", "alice@example.com", "forward", "on", "bounce", "fwd@example.com"], "Invalid choice (bounce)"),
    (["user", "alice@example.com", "signout", "now"], "Invalid argument"),
    (["delete", "calendars", CAL], "mock: unhandled argv"),       # the catch-all fails now
    (["user", "alice@example.com", "delete", "calendars", CAL], "mock: unhandled argv"),
])
async def test_mock_rejects_a_malformed_shape(runner, domain, argv, needle):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, argv, serialize=True)
    assert needle in ei.value.stderr
    assert ei.value.exit_code != 0


@pytest.mark.parametrize("argv", [
    C.delete_user("missing@example.com"),
    C.update_organization("nonexistent@example.com", "T", "D"),
    C.remove_group_member("missing-group@example.com", "carol@example.com"),
    C.remove_delegate("alice@example.com", "missing@example.com"),
    C.delete_event(CAL, "missing-evt"),
    C.info_user("nobody@example.com"),        # info user is keyed on the address, not "always Alice"
])
async def test_mock_reports_a_missing_entity_as_not_found(runner, domain, argv):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, argv)
    assert ei.value.kind is GAMErrorKind.NOT_FOUND


async def test_info_user_is_keyed_on_the_address(connector):
    assert (await connector.get_user("carol@example.com")).given_name == "Carol"
    assert (await connector.get_user("bob@example.com")).suspended is True


async def test_mock_refuses_a_call_without_materialized_credentials(runner, tmp_path):
    # run_in_cfgdir hands GAM a bare dir, as the setup wizard does before import; an authenticated
    # command there must fail like GAM with no oauth2service.json, not answer from fixtures.
    bare = tmp_path / "bare"
    bare.mkdir()
    res = await runner.run_in_cfgdir(bare, C.print_users())
    assert res.returncode != 0 and "oauth2service.json" in res.stderr
    assert (await runner.run_in_cfgdir(bare, C.version())).returncode == 0   # needs no credentials


async def test_argv_recorder_sees_exactly_what_ran(runner, domain, gam_calls):
    tricky = "Best,\nAlice <a href='x'>  two  spaces</a>"
    await runner.run_authenticated(domain, C.set_signature("alice@example.com", tricky), serialize=True)
    assert gam_calls() == [["user", "alice@example.com", "signature", tricky, "html"]]
