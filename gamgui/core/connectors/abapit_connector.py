"""Adapter that exposes abapit's Apple Business Manager / Mosyle clients as gamgui connectors.

abapit (``/Users/goetch/abapit``) already implements ABM and Mosyle as duck-typed, synchronous
``httpx`` clients (``devices()``, ``users()``, ``ping()`` …) returning JSON:API-shaped dicts. Rather
than reimplement any of that, we *wrap* an abapit client: each gamgui ``Connector`` method calls the
corresponding abapit method inside ``asyncio.to_thread`` (the only seam needed, since abapit is sync
and gamgui's interface is async).

abapit is an optional dependency — imported lazily — so the GAM-only build runs without it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set

from ..audit import AuditLog
from .base import (
    Capability,
    ChangePreview,
    ChangeResult,
    ConnectionStatus,
    Connector,
    ConnectorID,
    LifecycleAction,
)
from .person import ConnectorAccount, Person


class AbapitConnector(Connector):
    """Wrap one abapit client (real or demo) behind gamgui's async Connector interface."""

    def __init__(
        self,
        client: Any,
        connector_id: ConnectorID,
        capabilities: Set[Capability],
        audit: Optional[AuditLog] = None,
    ) -> None:
        self._client = client
        self.id = connector_id
        self.capabilities = capabilities
        self.audit = audit or AuditLog()

    @property
    def is_demo(self) -> bool:
        return bool(getattr(self._client, "is_demo", False))

    async def test(self) -> ConnectionStatus:
        try:
            await asyncio.to_thread(self._client.ping)
        except Exception as exc:
            return ConnectionStatus(ok=False, detail=str(exc))
        return ConnectionStatus(ok=True, detail="Demo data" if self.is_demo else "Ready.")

    async def list_devices(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._client.devices)

    async def list_users(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._client.users)

    async def resolve(self, person: Person) -> Optional[ConnectorAccount]:
        """Find this person's account in the Apple/Mosyle directory by email / managed Apple ID."""
        users = await asyncio.to_thread(self._client.users)
        wanted = person.primary_email.strip().lower()
        for u in users:
            attrs = u.get("attributes", {}) if isinstance(u, dict) else {}
            candidates = {
                str(attrs.get("email", "")).lower(),
                str(attrs.get("managedAppleAccount", "")).lower(),
            }
            if wanted and wanted in candidates:
                return ConnectorAccount(connector_id=self.id, native_id=str(u.get("id", "")), raw=u)
        return None

    async def plan(self, action: LifecycleAction, person: Person) -> List[ChangePreview]:
        # Device lifecycle (assignment, etc.) lands in a later phase; reads + resolve are wired now.
        return []

    async def apply(self, changes) -> List[ChangeResult]:  # pragma: no cover - not yet implemented
        return [ChangeResult(preview=c, ok=False, detail="apply not implemented for this connector yet") for c in changes]


# --- constructors ---------------------------------------------------------------------

_ABM_CAPS = {Capability.DIRECTORY, Capability.MDM}
_MOSYLE_CAPS = {Capability.DIRECTORY, Capability.MDM}


def _build_client(org: Any) -> Any:
    from abapit.factory import build_client  # lazy: abapit is optional

    return build_client(org)


def apple_business_connector(org: Any, audit: Optional[AuditLog] = None) -> AbapitConnector:
    """Build an ABM connector from an abapit ``Org`` (provider != 'mosyle')."""
    return AbapitConnector(_build_client(org), ConnectorID.APPLE_BUSINESS_MANAGER, _ABM_CAPS, audit)


def mosyle_connector(org: Any, audit: Optional[AuditLog] = None) -> AbapitConnector:
    """Build a Mosyle connector from an abapit ``Org`` (provider == 'mosyle')."""
    return AbapitConnector(_build_client(org), ConnectorID.MOSYLE, _MOSYLE_CAPS, audit)


def demo_apple_connector(audit: Optional[AuditLog] = None) -> AbapitConnector:
    """An ABM connector backed by abapit's seeded demo fleet — no credentials required."""
    from abapit.demo import DemoClient  # lazy

    return AbapitConnector(DemoClient(), ConnectorID.APPLE_BUSINESS_MANAGER, _ABM_CAPS, audit)
