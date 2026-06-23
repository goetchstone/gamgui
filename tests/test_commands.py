from __future__ import annotations

import pytest

from gamgui.core.gam.commands import GAMCommands, build_user_query


def test_print_users_requests_list_fields():
    argv = GAMCommands.print_users()
    assert argv[:2] == ["print", "users"]
    assert argv[-1] == "formatjson"
    fields = argv[argv.index("fields") + 1]
    # without explicit fields GAM returns only primaryEmail, so we must request these
    assert all(f in fields for f in ("name", "suspended", "orgUnitPath"))


def test_print_users_with_query_and_fields():
    argv = GAMCommands.print_users(query="email:a*", fields=["primaryEmail", "suspended"])
    assert argv == ["print", "users", "query", "email:a*", "fields", "primaryEmail,suspended", "formatjson"]


def test_info_user():
    argv = GAMCommands.info_user("a@e.com")
    assert argv[:3] == ["info", "user", "a@e.com"]
    assert argv[-1] == "formatjson" and "fields" in argv


def test_update_organization_sets_title_and_department():
    argv = GAMCommands.update_organization("a@e.com", title="Design Lead", department="Old Saybrook")
    assert argv == ["update", "user", "a@e.com", "organization",
                    "title", "Design Lead", "department", "Old Saybrook", "primary"]


def test_calendar_acl_commands():
    assert GAMCommands.print_calendar_acls("a@e.com") == ["user", "a@e.com", "print", "calendaracls", "primary", "formatjson"]
    assert GAMCommands.add_calendar_acl("a@e.com", "bob@e.com", role="reader") == \
        ["user", "a@e.com", "add", "calendaracls", "primary", "reader", "bob@e.com"]
    assert GAMCommands.delete_calendar_acl("a@e.com", "bob@e.com") == \
        ["user", "a@e.com", "delete", "calendaracls", "primary", "bob@e.com"]


def test_calendar_event_commands():
    assert GAMCommands.print_resources("aspen")[:4] == ["print", "resources", "fields", "id,name,email,resourcetype,buildingid"]
    assert GAMCommands.print_resources("aspen")[-3:] == ["query", "aspen", "formatjson"]
    assert GAMCommands.print_user_calendars("a@e.com")[:4] == ["user", "a@e.com", "print", "calendars"]
    assert GAMCommands.print_all_calendars()[:4] == ["all", "users", "print", "calendars"]
    assert GAMCommands.print_calendar_acls_cal("room@x") == ["calendars", "room@x", "print", "calendaracls", "formatjson"]

    ev = GAMCommands.print_events("room@x", query="standup", after="2026-01-01")
    assert ev[:4] == ["calendars", "room@x", "print", "events"]
    assert "query" in ev and "standup" in ev and "after" in ev and ev[-1] == "formatjson"

    # Destructive: includes `doit` only when asked; targets a specific event id.
    assert GAMCommands.delete_event("room@x", "evt1", doit=True) == \
        ["calendars", "room@x", "delete", "events", "eventid", "evt1", "doit", "sendupdates", "none"]
    assert "doit" not in GAMCommands.delete_event("room@x", "evt1", doit=False)


def test_calendar_delete_vs_unsubscribe_commands():
    # GAM footgun, verified against GAM7 source: `remove calendars` permanently DELETES the secondary
    # calendar (Calendars.delete); `delete calendars` only UNSUBSCRIBES the user (CalendarList.delete).
    assert GAMCommands.remove_calendar("owner@e.com", "c_x@group.calendar.google.com") == \
        ["user", "owner@e.com", "remove", "calendars", "c_x@group.calendar.google.com"]
    assert GAMCommands.unsubscribe_calendar("u@e.com", "c_x@group.calendar.google.com") == \
        ["user", "u@e.com", "delete", "calendars", "c_x@group.calendar.google.com"]
    # No `doit` token — GAM7 rejects extraneous args on `remove calendars`.
    assert "doit" not in GAMCommands.remove_calendar("o@e.com", "c_x@group.calendar.google.com")


def test_lifecycle_commands():
    assert GAMCommands.reset_password("a@e.com") == ["update", "user", "a@e.com", "password", "random", "changepassword", "off"]
    assert GAMCommands.create_datatransfer("a@e.com", "drive", "b@e.com") == ["create", "datatransfer", "a@e.com", "drive", "b@e.com"]
    assert GAMCommands.remove_all_calendar_acls("a@e.com") == ["all", "users", "delete", "calendaracls", "primary", "a@e.com"]
    assert GAMCommands.delete_user("a@e.com") == ["delete", "user", "a@e.com"]
    ev = GAMCommands.add_calendar_event("mgr@e.com", "Confirm delete", "2026-07-23", "2026-07-24", description="d", attendee="it@e.com")
    assert ev[:7] == ["user", "mgr@e.com", "add", "event", "primary", "summary", "Confirm delete"]
    assert "start" in ev and "allday" in ev and "2026-07-23" in ev and "description" in ev and "attendee" in ev


def test_set_suspended_on_off():
    assert GAMCommands.set_suspended("a@e.com", True) == ["update", "user", "a@e.com", "suspended", "on"]
    assert GAMCommands.set_suspended("a@e.com", False) == ["update", "user", "a@e.com", "suspended", "off"]


def test_set_signature_html_flag():
    assert GAMCommands.set_signature("a@e.com", "Hi", html=True) == ["user", "a@e.com", "signature", "Hi", "html"]
    assert GAMCommands.set_signature("a@e.com", "Hi", html=False) == ["user", "a@e.com", "signature", "Hi"]


def test_show_signature_and_groups_member():
    assert GAMCommands.show_signature("a@e.com") == ["user", "a@e.com", "show", "signature"]
    assert GAMCommands.print_groups_member("a@e.com") == ["print", "groups", "member", "a@e.com"]


def test_print_delegates_has_no_formatjson():
    # `print delegates` errors if given formatjson; it returns plain CSV.
    argv = GAMCommands.print_delegates("a@e.com")
    assert argv == ["user", "a@e.com", "print", "delegates"]
    assert "formatjson" not in argv


def test_delegate_add_remove():
    assert GAMCommands.add_delegate("a@e.com", "b@e.com") == ["user", "a@e.com", "add", "delegate", "b@e.com"]
    assert GAMCommands.remove_delegate("a@e.com", "b@e.com") == ["user", "a@e.com", "delete", "delegate", "b@e.com"]


def test_group_member_role_validation():
    assert GAMCommands.add_group_member("g@e.com", "a@e.com", role="manager") == [
        "update", "group", "g@e.com", "add", "manager", "a@e.com",
    ]
    with pytest.raises(ValueError):
        GAMCommands.add_group_member("g@e.com", "a@e.com", role="emperor")


def test_signature_value_is_a_single_arg_not_shell():
    # A signature with shell metacharacters must remain ONE argv element (no injection surface).
    argv = GAMCommands.set_signature("a@e.com", "Hi; rm -rf / `whoami`", html=False)
    assert argv[3] == "Hi; rm -rf / `whoami`"


def test_vacation_commands():
    assert GAMCommands.vacation_off("a@e.com") == ["user", "a@e.com", "vacation", "off"]
    assert GAMCommands.show_vacation("a@e.com") == ["user", "a@e.com", "show", "vacation"]
    argv = GAMCommands.set_vacation("a@e.com", "S", "M", html=True, start="2026-07-01", contacts_only=True)
    assert argv[:6] == ["user", "a@e.com", "vacation", "on", "subject", "S"]
    assert "message" in argv and "M" in argv and "html" in argv
    assert "contactsonly" in argv and "start" in argv
    assert "formatjson" not in argv  # vacation/show don't take formatjson


def test_list_fields_include_organizations():
    from gamgui.core.gam.commands import USER_LIST_FIELDS

    assert "organizations" in USER_LIST_FIELDS  # carries job title for the list


def test_build_user_query():
    assert build_user_query("") is None
    assert build_user_query("", include_suspended=False) == "isSuspended=false"
    q = build_user_query("jane")
    assert "email:jane*" in q and "givenName:jane*" in q
