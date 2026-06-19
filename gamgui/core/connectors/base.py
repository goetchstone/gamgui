"""The connector abstraction.

A *connector* is one external system we can manage (Google Workspace today; Apple Business
Manager, Mosyle, and PBXact later). Each connector knows how to find a :class:`Person` in its
system, *plan* a change (a dry-run producing :class:`ChangePreview` objects), and *apply* approved
changes. The lifecycle layer (later) composes connectors to onboard/offboard a person everywhere at
once. The destructive-op guard operates on :class:`ChangePreview` objects, so every connector gets
safety for free.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set

if TYPE_CHECKING:  # avoid a runtime import cycle; annotations are strings under `from __future__`
    from .person import ConnectorAccount, Person


class ConnectorID(enum.Enum):
    GOOGLE_WORKSPACE = "google_workspace"
    APPLE_BUSINESS_MANAGER = "apple_business_manager"
    MOSYLE = "mosyle"
    PBXACT = "pbxact"


class Capability(enum.Enum):
    DIRECTORY = "directory"
    GROUPS = "groups"
    MAIL = "mail"
    MDM = "mdm"
    TELEPHONY = "telephony"


class RiskLevel(enum.IntEnum):
    READ_ONLY = 0
    LOW = 1
    DESTRUCTIVE = 2


class LifecycleAction(enum.Enum):
    ONBOARD = "onboard"
    OFFBOARD = "offboard"
    SUSPEND = "suspend"
    UNSUSPEND = "unsuspend"
    UPDATE = "update"


@dataclass
class ConnectionStatus:
    ok: bool
    detail: str = ""
    version: str = ""


@dataclass
class ChangePreview:
    """One concrete change a connector would make, surfaced *before* it runs (dry-run)."""

    connector_id: ConnectorID
    target: str              # the affected identity (email / extension / Apple ID)
    summary: str             # human-readable description for the confirmation UI
    risk: RiskLevel
    argv: Optional[List[str]] = None   # connector-specific payload (for GAM: the command to run)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChangeResult:
    preview: ChangePreview
    ok: bool
    detail: str = ""


class Connector:
    """Base class connectors implement. Methods are async; subclasses override as supported."""

    id: ConnectorID
    capabilities: Set[Capability] = set()

    async def test(self) -> ConnectionStatus:  # pragma: no cover - interface
        raise NotImplementedError

    async def resolve(self, person: "Person") -> "Optional[ConnectorAccount]":  # pragma: no cover
        raise NotImplementedError

    async def plan(self, action: LifecycleAction, person: "Person") -> List[ChangePreview]:  # pragma: no cover
        raise NotImplementedError

    async def apply(self, changes: Sequence[ChangePreview]) -> List[ChangeResult]:  # pragma: no cover
        raise NotImplementedError
