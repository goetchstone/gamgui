"""Typed errors for GAM invocations.

GAM communicates failure through a non-zero exit code plus human-readable text on
stderr. We translate that into a small, stable taxonomy so the UI can show useful
remediation instead of raw CLI noise.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Optional, Pattern, Tuple

from ..audit import redact_argv

# GAM echoes the full command line on a usage error, so a submitted `password`/`notifypassword` value
# can appear in stderr. Mask it before the GAMError can be logged, shown, or its .message built.
_SECRET_IN_STDERR: Pattern[str] = re.compile(r"(?i)\b(password|notifypassword)\s+\S+")


def _scrub_stderr(stderr: str) -> str:
    return _SECRET_IN_STDERR.sub(r"\1 ***redacted***", stderr or "")


class GAMErrorKind(enum.Enum):
    """Coarse classification of a failed GAM run."""

    AUTH_EXPIRED = "auth_expired"
    SCOPE_MISSING = "scope_missing"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    # Google refused to remove a calendar owner's OWN access ("Cannot change your own access level") —
    # the one refusal the offboarding sweep expects (the leaver's own calendar), unlike a real 403.
    OWN_ACL = "own_acl"
    # A user for whom a Google service (Calendar, Gmail…) is off: GAM's "<Service> Service/App not
    # enabled" per-user warning — not the account-wide "<API> not enabled. Please run …" failure.
    SERVICE_NOT_ENABLED = "service_not_enabled"
    NOT_AUTHENTICATED = "not_authenticated"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# Human remediation text shown alongside the raw error.
_REMEDIATION = {
    GAMErrorKind.AUTH_EXPIRED: "Your sign-in expired. Re-run setup to refresh authorization.",
    GAMErrorKind.SCOPE_MISSING: (
        "A required API scope is not authorized. Re-do the Domain-Wide Delegation step "
        "in the setup wizard."
    ),
    GAMErrorKind.RATE_LIMITED: "Google is rate-limiting requests. Wait a moment and retry.",
    GAMErrorKind.NOT_FOUND: "The requested user, group, or resource was not found.",
    GAMErrorKind.PERMISSION_DENIED: (
        "The authorized account lacks permission for this action. Check the admin role and scopes."
    ),
    GAMErrorKind.OWN_ACL: (
        "Google doesn't let a calendar's owner remove their own access — expected for the departing "
        "user's own calendar."
    ),
    GAMErrorKind.SERVICE_NOT_ENABLED: (
        "That Google service is turned off for this user — check their licence, or the service's "
        "on/off setting for their organizational unit in the Admin console."
    ),
    GAMErrorKind.NOT_AUTHENTICATED: "GAM is not configured yet. Complete the setup wizard first.",
    GAMErrorKind.TIMEOUT: "The command timed out. Check connectivity and retry.",
    GAMErrorKind.UNKNOWN: "GAM reported an error. See details below.",
}


# Ordered (first match wins, per line) stderr patterns → kind. Order matters: more specific first.
_PATTERNS: List[Tuple[Pattern[str], GAMErrorKind]] = [
    (re.compile(r"invalid_grant|token has been expired or revoked", re.I), GAMErrorKind.AUTH_EXPIRED),
    (re.compile(r"insufficient.*scope|access_denied.*scope|not authorized to access", re.I), GAMErrorKind.SCOPE_MISSING),
    (re.compile(r"rate.?limit|quota|userRateLimitExceeded|too many requests|\b429\b", re.I), GAMErrorKind.RATE_LIMITED),
    (re.compile(r"does not exist|not found|notFound|resource.*not found|\b404\b", re.I), GAMErrorKind.NOT_FOUND),
    # A user without the service (GAM's userServiceNotEnabledWarning: "User: x, Calendar Service/App
    # not enabled"). Narrow on purpose: GAM's account-wide "Calendar not enabled. Please run "gam update
    # project"…" is a real failure and must stay UNKNOWN. Tolerated by the offboarding calendar sweep.
    (re.compile(r"Service/App not enabled", re.I), GAMErrorKind.SERVICE_NOT_ENABLED),
    # Deleting your OWN owner ACL is refused ("Cannot change your own access level" / cannotChangeOwnAcl).
    # Its own kind, before the generic 403 pattern (GAM's line carries no "403"/"forbidden" token): the
    # offboarding sweep tolerates exactly this refusal, never a real permission failure.
    (re.compile(r"cannot change your own access level|cannotChangeOwnAcl", re.I), GAMErrorKind.OWN_ACL),
    (re.compile(r"forbidden|permission denied|insufficientPermissions|\b403\b", re.I), GAMErrorKind.PERMISSION_DENIED),
    (re.compile(r"please run.*oauth|no.*credentials|oauth2\.txt.*not found|service account", re.I), GAMErrorKind.NOT_AUTHENTICATED),
]


# GAM's own progress chatter on stderr (gam.cfg show_gettings, on by default): "Getting all Users, may
# take some time on a large Google Workspace Account..." / "Got 150 Users: a@x - z@x". Not an error line.
_PROGRESS_LINE: Pattern[str] = re.compile(r"(Getting all |Got \d+ )")

# Most severe first, for a stderr whose lines disagree. An account-wide failure outranks a per-entity
# one, and an unrecognized error outranks the per-entity refusals a best-effort sweep prints beside it,
# so a real failure is never reported as the benign kind next to it.
_SEVERITY: List[GAMErrorKind] = [
    GAMErrorKind.AUTH_EXPIRED, GAMErrorKind.NOT_AUTHENTICATED, GAMErrorKind.SCOPE_MISSING,
    GAMErrorKind.RATE_LIMITED, GAMErrorKind.TIMEOUT, GAMErrorKind.UNKNOWN,
    GAMErrorKind.PERMISSION_DENIED, GAMErrorKind.OWN_ACL, GAMErrorKind.SERVICE_NOT_ENABLED,
    GAMErrorKind.NOT_FOUND,
]


def _classify_line(line: str) -> GAMErrorKind:
    for pattern, kind in _PATTERNS:
        if pattern.search(line):
            return kind
    return GAMErrorKind.UNKNOWN


def _error_lines(stderr: str) -> List[Tuple[GAMErrorKind, str]]:
    """Each stderr line that reports something, with its kind. A multi-entity command (``all users
    ...``) prints one line per entity, so a stderr can hold benign and real failures side by side —
    classifying the whole text by its first match let one "not found" mask the rest."""
    lines = (raw.strip() for raw in (stderr or "").splitlines())
    return [(_classify_line(line), line) for line in lines if line and not _PROGRESS_LINE.match(line)]


def _worst(kinds: Iterable[GAMErrorKind]) -> GAMErrorKind:
    return min(kinds, key=_SEVERITY.index, default=GAMErrorKind.UNKNOWN)


def classify_stderr(stderr: str) -> GAMErrorKind:
    """Map GAM stderr text to its most severe :class:`GAMErrorKind`, line by line."""
    return _worst(kind for kind, _ in _error_lines(stderr))


@dataclass
class GAMError(Exception):
    """Raised when a GAM command fails.

    Attributes
    ----------
    kind: the coarse classification used to drive the UI.
    exit_code: GAM's process exit code (``None`` if the process never returned, e.g. timeout).
    stderr: GAM's stderr, scrubbed of secrets in __post_init__ (GAM echoes the command line — incl.
        a submitted password — on a usage error, so this must not be trusted raw).
    argv: the gam argument list that was run (binary path excluded), for diagnostics.
    kinds: the kind of EVERY error line (``kind`` is the most severe of them) — what a best-effort
        caller checks, so one real failure among benign per-entity notices is never tolerated.
    """

    kind: GAMErrorKind
    exit_code: Optional[int]
    stderr: str = ""
    argv: Optional[List[str]] = None
    kinds: FrozenSet[GAMErrorKind] = frozenset()

    def __post_init__(self) -> None:
        # Redact any secret the failed command carried BEFORE this exception is logged, shown, or its
        # .message is built — GAM echoes the command line (incl. the password) on a usage error.
        self.argv = redact_argv(self.argv)
        self.stderr = _scrub_stderr(self.stderr)
        self.kinds = frozenset(self.kinds) or frozenset({self.kind})
        super().__init__(self.message)

    @property
    def remediation(self) -> str:
        return _REMEDIATION[self.kind]

    @property
    def message(self) -> str:
        # The last line of the reported kind: in a mixed stderr the tail can be a benign notice.
        lines = _error_lines(self.stderr)
        shown = [line for kind, line in lines if kind is self.kind] or [line for _, line in lines]
        detail = shown[-1] if shown else ""
        base = f"GAM failed ({self.kind.value}, exit={self.exit_code})"
        return f"{base}: {detail}" if detail else base

    @classmethod
    def from_run(cls, exit_code: Optional[int], stderr: str, argv: Optional[List[str]] = None) -> "GAMError":
        if exit_code is None:
            return cls(kind=GAMErrorKind.TIMEOUT, exit_code=None, stderr=stderr, argv=argv)
        kinds = frozenset(kind for kind, _ in _error_lines(stderr))
        return cls(kind=_worst(kinds), exit_code=exit_code, stderr=stderr, argv=argv, kinds=kinds)
