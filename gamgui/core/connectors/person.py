"""The unifying identity model.

A :class:`Person` is one human. Each system they exist in is a :class:`ConnectorAccount` linked
into ``person.accounts``. This is what lets a future "offboard" act across Workspace + ABM + Mosyle
+ PBXact in one operation, even though the MVP only populates the Google Workspace account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

from .base import ConnectorID

if TYPE_CHECKING:
    from ..gam.models import GAMUser


@dataclass
class ConnectorAccount:
    connector_id: ConnectorID
    native_id: str                       # primary email / Apple ID / device-user / extension
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Person:
    id: str                              # stable internal id (primary email by default)
    primary_email: str
    given_name: str = ""
    family_name: str = ""
    status: str = "active"               # active | suspended | offboarded
    accounts: Dict[ConnectorID, ConnectorAccount] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        name = f"{self.given_name} {self.family_name}".strip()
        return name or self.primary_email

    def link(self, account: ConnectorAccount) -> None:
        self.accounts[account.connector_id] = account

    def account_for(self, connector_id: ConnectorID) -> Optional[ConnectorAccount]:
        return self.accounts.get(connector_id)

    @classmethod
    def from_gam_user(cls, user: "GAMUser") -> "Person":
        person = cls(
            id=user.primary_email,
            primary_email=user.primary_email,
            given_name=user.given_name,
            family_name=user.family_name,
            status="suspended" if user.suspended else "active",
        )
        person.link(
            ConnectorAccount(
                connector_id=ConnectorID.GOOGLE_WORKSPACE,
                native_id=user.primary_email,
                raw=user.raw,
            )
        )
        return person
