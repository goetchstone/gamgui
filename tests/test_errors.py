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


def test_own_acl_deletion_is_permission_denied():
    # The departing user's own primary-calendar owner ACL cannot be removed. GAM's line here carries
    # no "403"/"forbidden" token, so a dedicated pattern maps it (case-insensitively) to a permission
    # refusal — which the all-users calendar sweep tolerates.
    line = ("    Calendar: achenard@saybrookhome.com, Calendar ACL: (Scope: user:achenard@saybrookhome.com), "
            "Delete Failed: Cannot change your own access level.")
    assert classify_stderr(line) == GAMErrorKind.PERMISSION_DENIED
    assert classify_stderr("CANNOT CHANGE YOUR OWN ACCESS LEVEL") == GAMErrorKind.PERMISSION_DENIED


# A multi-entity run (`all users delete calendaracls ...`) prints GAM's progress chatter and one line per
# entity. Hand-written in GAM's shape (the progress wording is from the vendored GamUpdate.txt).
_PROGRESS = ("Getting all Users, may take some time on a large Google Workspace Account...\n"
             "Got 3 Users: alice@example.com - carol@example.com\n")
_NOT_APPLICABLE = "User: bob@example.com, Service not applicable/Does not exist\n"
_OWN_ACL = ("    Calendar: carol@example.com, Calendar ACL: (Scope: user:carol@example.com), "
            "Delete Failed: Cannot change your own access level.\n")
_REAL = ("    Calendar: alice@example.com, Calendar ACL: (Scope: user:carol@example.com), "
         "Delete Failed: Internal error encountered.\n")


def test_every_line_tolerable_keeps_only_tolerable_kinds():
    err = GAMError.from_run(50, _PROGRESS + _NOT_APPLICABLE + _OWN_ACL)
    assert err.kinds == {GAMErrorKind.NOT_FOUND, GAMErrorKind.PERMISSION_DENIED}   # progress lines skipped
    assert err.kind is GAMErrorKind.PERMISSION_DENIED


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

