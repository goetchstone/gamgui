from __future__ import annotations

from gamgui.core.gam.models import GAMUser
from gamgui.core.signatures import match_scope, render_signature, scope_options


def _u(email, **kw):
    return GAMUser.from_json({"primaryEmail": email, **kw})


def test_render_substitutes_variables():
    u = _u("a@e.com", name={"givenName": "Al", "familyName": "Ant"}, organizations=[{"title": "Director", "primary": True}])
    assert render_signature("{name} | {role} | {email}", u) == "Al Ant | Director | a@e.com"


def test_role_is_alias_for_title():
    u = _u("a@e.com", organizations=[{"title": "Boss", "primary": True}])
    assert render_signature("{title}={role}", u) == "Boss=Boss"


def test_match_scope_ou_includes_children_excludes_suspended():
    users = [
        _u("a@e.com", orgUnitPath="/Sales"),
        _u("b@e.com", orgUnitPath="/Sales/East"),
        _u("c@e.com", orgUnitPath="/IT", organizations=[{"department": "IT", "primary": True}]),
        _u("d@e.com", orgUnitPath="/Sales", suspended=True),
    ]
    assert {u.primary_email for u in match_scope(users, "ou", "/Sales")} == {"a@e.com", "b@e.com"}
    assert len(match_scope(users, "company")) == 3  # suspended excluded
    assert [u.primary_email for u in match_scope(users, "department", "IT")] == ["c@e.com"]


def test_match_scope_user_selects_single_active():
    users = [_u("a@e.com"), _u("b@e.com"), _u("c@e.com", suspended=True)]
    assert [u.primary_email for u in match_scope(users, "user", "a@e.com")] == ["a@e.com"]
    assert match_scope(users, "user", "A@E.COM")[0].primary_email == "a@e.com"  # case-insensitive
    assert match_scope(users, "user", "") == []        # empty selection never falls through to everyone
    assert match_scope(users, "user", "c@e.com") == []  # suspended excluded


def test_scope_options_lists_distinct():
    users = [
        _u("a@e.com", orgUnitPath="/Sales", organizations=[{"department": "Sales", "primary": True}]),
        _u("b@e.com", orgUnitPath="/IT"),
        _u("z@e.com", suspended=True),
    ]
    opts = scope_options(users)
    assert "/Sales" in opts["ous"] and "/IT" in opts["ous"]
    assert opts["departments"] == ["Sales"]
    assert opts["users"] == ["a@e.com", "b@e.com"]  # active only, sorted; suspended z@ excluded
