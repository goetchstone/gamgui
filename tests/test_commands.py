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


def test_set_suspended_on_off():
    assert GAMCommands.set_suspended("a@e.com", True) == ["update", "user", "a@e.com", "suspended", "on"]
    assert GAMCommands.set_suspended("a@e.com", False) == ["update", "user", "a@e.com", "suspended", "off"]


def test_set_signature_html_flag():
    assert GAMCommands.set_signature("a@e.com", "Hi", html=True) == ["user", "a@e.com", "signature", "Hi", "html"]
    assert GAMCommands.set_signature("a@e.com", "Hi", html=False) == ["user", "a@e.com", "signature", "Hi"]


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


def test_build_user_query():
    assert build_user_query("") is None
    assert build_user_query("", include_suspended=False) == "isSuspended=false"
    q = build_user_query("jane")
    assert "email:jane*" in q and "givenName:jane*" in q
