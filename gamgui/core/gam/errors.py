"""Typed errors for GAM invocations.

GAM communicates failure through a non-zero exit code plus human-readable text on
stderr. We translate that into a small, stable taxonomy so the UI can show useful
remediation instead of raw CLI noise.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import List, Optional, Pattern, Tuple


class GAMErrorKind(enum.Enum):
    """Coarse classification of a failed GAM run."""

    AUTH_EXPIRED = "auth_expired"
    SCOPE_MISSING = "scope_missing"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
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
    GAMErrorKind.NOT_AUTHENTICATED: "GAM is not configured yet. Complete the setup wizard first.",
    GAMErrorKind.TIMEOUT: "The command timed out. Check connectivity and retry.",
    GAMErrorKind.UNKNOWN: "GAM reported an error. See details below.",
}


# Ordered (first match wins) stderr patterns → kind. Order matters: more specific first.
_PATTERNS: List[Tuple[Pattern[str], GAMErrorKind]] = [
    (re.compile(r"invalid_grant|token has been expired or revoked", re.I), GAMErrorKind.AUTH_EXPIRED),
    (re.compile(r"insufficient.*scope|access_denied.*scope|not authorized to access", re.I), GAMErrorKind.SCOPE_MISSING),
    (re.compile(r"rate.?limit|quota|userRateLimitExceeded|too many requests|\b429\b", re.I), GAMErrorKind.RATE_LIMITED),
    (re.compile(r"does not exist|not found|notFound|resource.*not found|\b404\b", re.I), GAMErrorKind.NOT_FOUND),
    (re.compile(r"forbidden|permission denied|insufficientPermissions|\b403\b", re.I), GAMErrorKind.PERMISSION_DENIED),
    (re.compile(r"please run.*oauth|no.*credentials|oauth2\.txt.*not found|service account", re.I), GAMErrorKind.NOT_AUTHENTICATED),
]


def classify_stderr(stderr: str) -> GAMErrorKind:
    """Map GAM stderr text to a :class:`GAMErrorKind`."""
    text = stderr or ""
    for pattern, kind in _PATTERNS:
        if pattern.search(text):
            return kind
    return GAMErrorKind.UNKNOWN


@dataclass
class GAMError(Exception):
    """Raised when a GAM command fails.

    Attributes
    ----------
    kind: the coarse classification used to drive the UI.
    exit_code: GAM's process exit code (``None`` if the process never returned, e.g. timeout).
    stderr: the raw stderr captured from GAM (already redaction-safe — GAM does not echo secrets).
    argv: the gam argument list that was run (binary path excluded), for diagnostics.
    """

    kind: GAMErrorKind
    exit_code: Optional[int]
    stderr: str = ""
    argv: Optional[List[str]] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def remediation(self) -> str:
        return _REMEDIATION[self.kind]

    @property
    def message(self) -> str:
        tail = (self.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else ""
        base = f"GAM failed ({self.kind.value}, exit={self.exit_code})"
        return f"{base}: {detail}" if detail else base

    @classmethod
    def from_run(cls, exit_code: Optional[int], stderr: str, argv: Optional[List[str]] = None) -> "GAMError":
        kind = GAMErrorKind.TIMEOUT if exit_code is None else classify_stderr(stderr)
        return cls(kind=kind, exit_code=exit_code, stderr=stderr, argv=argv)
