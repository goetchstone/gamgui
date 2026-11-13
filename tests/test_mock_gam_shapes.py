"""The mock prints and refuses what GAM's source does: its reads' CSV, and the writes it once let through.

Each case below once passed here where GAM would answer otherwise (failure log 2026-11-04, the mock's
reads and writes). SwiftGamGui read these from GAM's source at 7.48; the key columns, the transfer refusal,
a missing group and a missing event were re-read here at 7.50.00. None was captured from a tenant, so a
greener mock proves the reader, not the command.
"""

from __future__ import annotations

from datetime import date

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.parser import parse_records
from gamgui.core.lifecycle import build_offboard_steps, run_offboard
from gamgui.web.jobs import start_job

from .test_mock_gam import READS

CAL = "c_team@group.calendar.google.com"

HEADERS = {
    "print_users": "primaryEmail,JSON",
    "print_groups": "email,JSON",
    "print_group_members": "group,JSON",
    "print_domains": "domainName,JSON",
    "print_cros": "deviceId,JSON",
    "print_resources": "resourceId,resourceName,JSON",
    "print_user_calendars": "primaryEmail,calendarId,JSON",
    "print_all_calendars": "primaryEmail,calendarId,JSON",
    "print_chrome_profiles": "name,profileId,JSON",
    "print_mobile_devices": "resourceId,JSON",
    "print_devices": "name,JSON",
    "print_script_projects": "Owner,id,name,JSON",
    "print_owned_calendars": "primaryEmail,calendarId,JSON",
    "print_filters": "User,id,from,subject,forward,archive,label,JSON",
    "print_calendar_acls": "primaryEmail,calendarId,JSON",
    "print_calendar_acls_cal": "calendarId,JSON",
    "print_events": "calendarId,id,JSON",
    "get_event": "calendarId,id,JSON",
    "print_filelist": "Owner,id,name,owners,owners.0.displayName,owners.0.emailAddress,JSON",
    # Read from the 7.51.00 build (doPrintCourses, doPrintCourseParticipants); the operator has no Education domain to see them on.
    "print_courses": "id,JSON",
    "print_course_participants": "courseId,courseName,JSON-teachers,JSON-students",
    "print_all_course_participants": "courseId,courseName,JSON-teachers,JSON-students",
}


async def _fails(runner, domain, argv) -> GAMError:
    with pytest.raises(GAMError) as err:
        await runner.run_authenticated(domain, argv, serialize=True)
    return err.value


def test_every_formatjson_read_has_its_gam_header():
    # A new formatjson read says its key columns here; `info user` prints one JSON record, not a table.
    emitting = {name for name, cases in READS.items() for argv in cases
                if "formatjson" in argv and argv[:2] != ["info", "user"]}
    assert emitting == set(HEADERS)


@pytest.mark.parametrize("name", sorted(HEADERS))
async def test_a_formatjson_read_prints_gams_key_columns(runner, domain, name):
    if name == "get_event":
        argv = C.get_event(CAL, "evt-allhands")
    else:
        argv = READS[name][0]
    out = await runner.run_authenticated(domain, argv)
    assert out.splitlines()[0] == HEADERS[name]
    assert parse_records(out)


@pytest.mark.hand_built_argv   # the flattened forms the app never sends
@pytest.mark.parametrize("argv", [
    ["print", "groups", "fields", "email"],
    ["print", "group-members", "group", "sales@example.com"],
    ["print", "cros", "fields", "deviceId"],
    ["print", "resources", "fields", "id"],
    ["user", "alice@example.com", "print", "calendars", "fields", "id"],
    ["user", "alice@example.com", "print", "calendaracls", "primary"],
    ["calendars", CAL, "print", "calendaracls"],
    ["calendars", CAL, "print", "events", "fields", "id"],
    ["user", "alice@example.com", "print", "filelist", "fields", "id"],
])
async def test_a_read_shape_not_modelled_is_refused_not_guessed(runner, domain, argv):
    assert "isn't modelled" in (await _fails(runner, domain, argv)).stderr


async def test_the_key_columns_are_not_inside_a_rule_record(runner, domain):
    # GAM's JSON holds each rule's id, role and scope only: an owner read from the record would be a guess.
    rec = parse_records(await runner.run_authenticated(domain, C.print_calendar_acls("alice@example.com")))[0]
    assert rec["primaryEmail"] == "alice@example.com" and rec["calendarId"] == "alice@example.com"
    out = await runner.run_authenticated(domain, C.print_calendar_acls("alice@example.com"))
    assert '""primaryEmail""' not in out and '""calendarId""' not in out


async def test_an_event_id_reads_that_event(runner, domain):
    recs = parse_records(await runner.run_authenticated(domain, C.get_event(CAL, "evt-allhands")))
    assert [r["id"] for r in recs] == ["evt-allhands"]
    err = await _fails(runner, domain, C.get_event(CAL, "evt-gone"))
    assert err.exit_code == 50 and f"Calendar: {CAL}, Event: evt-gone, Print Failed: Not Found" in err.stderr


async def test_an_event_search_that_finds_nothing_exits_no_entities_found(runner, domain):
    err = await _fails(runner, domain, C.print_events(CAL, query="NOMATCH"))
    assert err.exit_code == 60 and err.stdout.strip() == "calendarId,id,JSON"


@pytest.mark.parametrize("group", ["xsales@example.com", "alice@example.com", "sales@example.co"])
async def test_group_members_answers_a_whole_group_address_only(runner, domain, group):
    err = await _fails(runner, domain, C.print_group_members(group))
    assert err.exit_code == 56 and f"Group: {group}, Does not exist" in err.stderr


async def test_group_members_matches_a_group_in_any_case(runner, domain):
    assert parse_records(await runner.run_authenticated(domain, C.print_group_members("SALES@example.com")))


@pytest.mark.parametrize("taken,holder", [
    ("alice@example.com", "User: alice@example.com"),
    ("A.Anders@example.com", "User Alias: a.anders@example.com, User: alice@example.com"),
    ("sales@example.com", "Group: sales@example.com"),
    ("exists@example.com", "User: exists@example.com"),
])
async def test_create_of_a_taken_address_names_what_holds_it(runner, domain, taken, holder):
    err = await _fails(runner, domain, C.create_user(taken, "New", "Hire", "Temp-Pa55w0rd"))
    assert err.exit_code == 57 and f"User: {taken}, Create Failed: Duplicate, {holder}" in err.stderr


async def test_create_twice_is_a_duplicate_while_the_mock_keeps_state(runner, domain, gam_state):
    argv = C.create_user("ada@example.com", "Ada", "Byte", "Temp-Pa55w0rd")
    await runner.run_authenticated(domain, argv, serialize=True)
    assert (await _fails(runner, domain, argv)).exit_code == 57


async def test_create_in_an_org_unit_that_does_not_exist_fails(runner, domain):
    err = await _fails(runner, domain, C.create_user("ada@example.com", "Ada", "Byte", "Temp-Pa55w0rd",
                                                     org_unit="/missing"))
    assert err.exit_code == 50 and "Create Failed: Invalid Organizational Unit" in err.stderr


async def test_create_refuses_a_name_over_sixty_characters(runner, domain):
    argv = C.create_user("ada@example.com", "é" * 61, "Byte", "Temp-Pa55w0rd")
    assert (await _fails(runner, domain, argv)).exit_code == 2
    await runner.run_authenticated(domain, C.create_user("ada@example.com", "é" * 60, "Byte", "Temp-Pa55w0rd"),
                                   serialize=True)


async def test_subscribe_prints_gams_indented_line(runner, domain, gam_state):
    argv = C.subscribe_calendar("bob@example.com", CAL)
    out = await runner.run_authenticated(domain, argv, serialize=True)
    assert out.splitlines() == [f"  User: bob@example.com, Calendar: {CAL}, Added"]
    err = await _fails(runner, domain, argv)
    assert err.exit_code == 50 and f"  User: bob@example.com, Calendar: {CAL}, Add Failed: Duplicate" in err.stderr


async def test_subscribe_fails_for_a_non_user_and_a_missing_calendar(runner, domain):
    err = await _fails(runner, domain, C.subscribe_calendar("nobody@example.com", CAL))
    assert err.exit_code == 50 and "invalid_grant" in err.stderr
    err = await _fails(runner, domain, C.subscribe_calendar("bob@example.com", "missing@group.calendar.google.com"))
    assert err.exit_code == 50 and "Add Failed: Not Found" in err.stderr


@pytest.mark.parametrize("old,new", [
    ("alice@example.com", "alice@example.com"),
    ("alice@example.com", "a.anders@example.com"),    # her own alias: GAM compares user IDs
    ("a.anders@example.com", "ALICE@example.com"),
])
async def test_transfer_to_the_same_account_is_a_usage_error(runner, domain, old, new):
    err = await _fails(runner, domain, C.create_datatransfer(old, "drive", new))
    assert err.exit_code == 2 and "New owner must differ from old owner" in err.stderr


@pytest.mark.parametrize("old,new,who", [
    ("nobody@example.com", "alice@example.com", "nobody@example.com"),
    ("alice@example.com", "nobody@example.com", "nobody@example.com"),
])
async def test_transfer_resolves_each_owner_before_anything_else(runner, domain, old, new, who):
    err = await _fails(runner, domain, C.create_datatransfer(old, "drive", new))
    assert err.exit_code == 56 and err.kind is GAMErrorKind.NOT_FOUND
    assert f"ERROR: User: {who}, Does not exist" in err.stderr


async def test_transfer_prints_gams_words_and_a_second_one_is_googles_409(runner, domain, gam_state):
    argv = C.create_datatransfer("carol@example.com", "drive,calendar", "a.anders@example.com")
    out = await runner.run_authenticated(domain, argv, serialize=True)
    assert out.startswith("Transfer Request Performed\n  Transfer ID: ")
    assert "  Service: Drive and Docs,Calendar\n  From: carol@example.com\n  To: a.anders@example.com" in out
    err = await _fails(runner, domain, argv)
    assert err.exit_code == 9 and "already in progress" in err.stderr


async def test_deprovision_of_a_suspended_user_with_codes_exits_51_after_the_other_parts(runner, domain):
    err = await _fails(runner, domain, C.deprovision_user("bob@example.com"))
    assert err.exit_code == 51
    assert "Not Deprovisioned: User is suspended. You must unsuspend to process backupcodes" in err.stderr
    assert "Access Tokens: 0" in err.stdout


async def test_deprovision_of_a_suspended_user_without_codes_exits_0(runner, domain, gam_state):
    (gam_state / "nobackupcodes").mkdir()
    (gam_state / "nobackupcodes" / "bob@example.com").touch()
    await runner.run_authenticated(domain, C.deprovision_user("bob@example.com"), serialize=True)


async def test_offboarding_a_suspended_leaver_says_why_and_goes_on(connector):
    # The app already warns about a suspended leaver in the preview; this holds the run to GAM's reason.
    steps = build_offboard_steps("bob@example.com", "alice@example.com", "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps))
    await run_offboard(job, connector, steps)
    [line] = [ln for ln in job.log if ln.startswith("✗ ") and "Revoke" in ln]
    assert "You must unsuspend to process backupcodes" in line
    assert job.skipped == []


async def test_forwarding_addresses_are_that_users_own(runner, domain):
    out = await runner.run_authenticated(domain, C.print_forwarding_addresses("alice@example.com"))
    assert parse_records(out) == [{"User": "alice@example.com", "forwardingEmail": "fwd@example.com",
                                   "verificationStatus": "accepted"}]
    assert parse_records(await runner.run_authenticated(domain, C.print_forwarding_addresses("carol@example.com"))) == []


async def test_forwarding_addresses_fail_for_a_non_user_and_with_gmail_off(runner, domain, gam_state):
    err = await _fails(runner, domain, C.print_forwarding_addresses("nobody@example.com"))
    assert err.exit_code == 50 and "invalid_grant" in err.stderr
    (gam_state / "nogmail").mkdir()
    (gam_state / "nogmail" / "alice@example.com").touch()
    err = await _fails(runner, domain, C.print_forwarding_addresses("alice@example.com"))
    assert err.exit_code == 73 and err.kind is GAMErrorKind.SERVICE_NOT_ENABLED
    assert err.stdout.strip() == "User,forwardingEmail,verificationStatus"


async def test_deleting_a_delegate_they_do_not_have_fails(runner, domain):
    err = await _fails(runner, domain, C.remove_delegate("carol@example.com", "alice@example.com"))
    assert err.exit_code == 50 and "Delete Failed: Not Found" in err.stderr
    await runner.run_authenticated(domain, C.remove_delegate("carol@example.com", "helpdesk@example.com"),
                                   serialize=True)


async def test_subscribe_needs_access_and_a_bare_group_address_is_a_persons_rule(runner, domain, gam_state):
    # calendarList.insert answers Not Found for a calendar the person can't see. GAM reads a bare address
    # as user:<address>, so sharing with a group's bare address gives its members nothing; group:<address>
    # does. The mock once let every subscribe succeed, which hid exactly that.
    out = await runner.run_authenticated(domain, C.add_calendar_acl_cal(CAL, "sales@example.com"), serialize=True)
    assert "Scope: user:sales@example.com" in out
    err = await _fails(runner, domain, C.subscribe_calendar("bob@example.com", CAL))
    assert err.exit_code == 50 and f"User: bob@example.com, Calendar: {CAL}, Add Failed: Not Found" in err.stderr
    await runner.run_authenticated(domain, C.add_calendar_acl_cal(CAL, "group:sales@example.com"), serialize=True)
    out = await runner.run_authenticated(domain, C.subscribe_calendar("bob@example.com", CAL), serialize=True)
    assert f"User: bob@example.com, Calendar: {CAL}, Added" in out
    # A bare address removes the person's rule it named, never the group's; a rule not there is a 404.
    await runner.run_authenticated(domain, C.delete_calendar_acl_cal(CAL, "sales@example.com"), serialize=True)
    err = await _fails(runner, domain, C.delete_calendar_acl_cal(CAL, "sales@example.com"))
    assert err.exit_code == 50 and "(Scope: user:sales@example.com), Delete Failed: Not Found" in err.stderr
    await runner.run_authenticated(domain, C.delete_calendar_acl_cal(CAL, "group:sales@example.com"), serialize=True)
    err = await _fails(runner, domain, C.subscribe_calendar("carol@example.com", CAL))
    assert "Add Failed: Not Found" in err.stderr
