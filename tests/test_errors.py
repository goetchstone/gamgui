"""Direct unit tests for the stderr classifier (the taxonomy that drives UI remediation)."""

from __future__ import annotations

import pytest

from gamgui.core.gam.errors import GAMErrorKind, classify_stderr


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
