"""Direct unit tests for the stderr classifier (the taxonomy that drives UI remediation)."""

from __future__ import annotations

import pytest

from gamgui.core.gam.errors import GAMError, GAMErrorKind, classify_stderr


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ERROR: 404: Entity User does not exist - notFound", GAMErrorKind.NOT_FOUND),
        ("ERROR: 403: Request had insufficient authentication scopes", GAMErrorKind.SCOPE_MISSING),
        ("ERROR: 429: userRateLimitExceeded - rate limit", GAMErrorKind.RATE_LIMITED),
        ("ERROR: invalid_grant: Token has been expired or revoked", GAMErrorKind.AUTH_EXPIRED),
        ("ERROR: 403: forbidden - insufficientPermissions", GAMErrorKind.PERMISSION_DENIED),
        ("something GAM has never said before", GAMErrorKind.UNKNOWN),
    ],
)
def test_classify_stderr(stderr, expected):
    assert classify_stderr(stderr) == expected


def test_own_acl_deletion_is_its_own_kind_not_a_generic_permission_refusal():
    # The departing user's own primary-calendar owner ACL cannot be removed. GAM's line here carries
    # no "403"/"forbidden" token. It is its own kind — the only refusal the all-users calendar sweep
    # tolerates — so a real 403 for some other user is never swept up with it.
    line = ("    Calendar: alice@example.com, Calendar ACL: (Scope: user:alice@example.com), "
            "Delete Failed: Cannot change your own access level.")
    assert classify_stderr(line) is GAMErrorKind.OWN_ACL
    assert classify_stderr("CANNOT CHANGE YOUR OWN ACCESS LEVEL") is GAMErrorKind.OWN_ACL
    assert classify_stderr("cannotChangeOwnAcl") is GAMErrorKind.OWN_ACL
    assert classify_stderr("ERROR: 403: forbidden - insufficientPermissions") is GAMErrorKind.PERMISSION_DENIED


def test_a_user_without_the_service_is_its_own_kind():
    # GAM's userServiceNotEnabledWarning (read from the vendored 7.48.11 build): a user whose Calendar
    # (or Gmail…) is off. Per user, so the calendar sweep can tolerate it — unlike GAM's account-wide
    # "<API> not enabled. Please run "gam update project"…", a real failure the regex must not catch.
    line = "User: dave@example.com, Calendar Service/App not enabled (4/120)"
    assert classify_stderr(line) is GAMErrorKind.SERVICE_NOT_ENABLED
    assert "turned off for this user" in GAMError.from_run(73, line).remediation
    api = ('ERROR: Calendar not enabled. Please run "gam update project" and '
           '"gam user user@domain.com update serviceaccount"')
    assert classify_stderr(api) is GAMErrorKind.UNKNOWN


# A multi-entity run (`all users delete calendaracls ...`) prints GAM's progress chatter and one line per
# entity. Hand-written in GAM's shape (the progress wording is from the vendored GamUpdate.txt).
_PROGRESS = ("Getting all Users, may take some time on a large Google Workspace Account...\n"
             "Got 3 Users: alice@example.com - carol@example.com\n")
_NOT_APPLICABLE = "User: bob@example.com, Service not applicable/Does not exist\n"
_OWN_ACL = ("    Calendar: carol@example.com, Calendar ACL: (Scope: user:carol@example.com), "
            "Delete Failed: Cannot change your own access level.\n")
_REAL = ("    Calendar: alice@example.com, Calendar ACL: (Scope: user:carol@example.com), "
         "Delete Failed: Internal error encountered.\n")
_NO_CALENDAR = "User: dave@example.com, Calendar Service/App not enabled (3/3)\n"


def test_every_line_tolerable_keeps_only_tolerable_kinds():
    err = GAMError.from_run(50, _PROGRESS + _NOT_APPLICABLE + _NO_CALENDAR + _OWN_ACL)
    assert err.kinds == {GAMErrorKind.NOT_FOUND, GAMErrorKind.SERVICE_NOT_ENABLED,
                         GAMErrorKind.OWN_ACL}                                   # progress lines skipped
    assert err.kind is GAMErrorKind.OWN_ACL


def test_a_real_error_among_tolerable_lines_wins():
    # Q8: whole-text first-match classified this NOT_FOUND ("Does not exist" on the first entity line),
    # so the offboarding sweep reported success on a partial failure.
    stderr = _PROGRESS + _NOT_APPLICABLE + _REAL + _OWN_ACL
    err = GAMError.from_run(50, stderr)
    assert classify_stderr(stderr) is GAMErrorKind.UNKNOWN and err.kind is GAMErrorKind.UNKNOWN
    assert GAMErrorKind.UNKNOWN in err.kinds
    assert "Internal error encountered" in err.message        # not the benign own-ACL tail line


def test_an_account_wide_error_outranks_per_entity_lines():
    stderr = _NOT_APPLICABLE + "ERROR: 403: Request had insufficient authentication scopes\n" + _OWN_ACL
    assert classify_stderr(stderr) is GAMErrorKind.SCOPE_MISSING


def test_no_error_line_is_unknown():
    assert classify_stderr(_PROGRESS) is GAMErrorKind.UNKNOWN
    assert GAMError.from_run(1, "").kinds == {GAMErrorKind.UNKNOWN}
    assert GAMError.from_run(None, "x").kinds == {GAMErrorKind.TIMEOUT}

