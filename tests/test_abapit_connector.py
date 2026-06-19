"""Proves abapit's ABM/Mosyle clients integrate as gamgui connectors — offline, via the demo fleet.

Skipped automatically if abapit isn't installed (the GAM-only build doesn't require it).
"""

from __future__ import annotations

import pytest

pytest.importorskip("abapit")

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.abapit_connector import demo_apple_connector
from gamgui.core.connectors.base import Capability, ConnectorID
from gamgui.core.connectors.person import ConnectorAccount, Person


@pytest.fixture
def conn(tmp_path):
    return demo_apple_connector(audit=AuditLog(tmp_path / "audit.jsonl"))


async def test_identity_and_capabilities(conn):
    assert conn.id == ConnectorID.APPLE_BUSINESS_MANAGER
    assert Capability.MDM in conn.capabilities
    assert conn.is_demo is True


async def test_connection_test_ok(conn):
    status = await conn.test()
    assert status.ok is True
    assert "Demo" in status.detail


async def test_reads_pass_json_api_through(conn):
    devices = await conn.list_devices()
    users = await conn.list_users()
    assert len(devices) == 84
    assert len(users) == 24
    assert devices[0]["type"] == "orgDevices"
    assert "attributes" in devices[0]


async def test_resolve_by_email(conn):
    users = await conn.list_users()
    email = users[0]["attributes"]["email"]
    account = await conn.resolve(Person(id=email, primary_email=email))
    assert account is not None
    assert account.connector_id == ConnectorID.APPLE_BUSINESS_MANAGER
    assert account.raw["attributes"]["email"] == email


async def test_resolve_unknown_returns_none(conn):
    account = await conn.resolve(Person(id="nobody@nowhere.tld", primary_email="nobody@nowhere.tld"))
    assert account is None


async def test_one_person_links_workspace_and_abm(conn):
    """The unifying model: a single Person can hold accounts in multiple systems at once."""
    users = await conn.list_users()
    email = users[0]["attributes"]["email"]
    person = Person(id=email, primary_email=email)
    # They have a Google Workspace account...
    person.link(ConnectorAccount(connector_id=ConnectorID.GOOGLE_WORKSPACE, native_id=email, raw={}))
    # ...and we resolve + link their Apple Business Manager account.
    person.link(await conn.resolve(person))
    assert set(person.accounts.keys()) == {
        ConnectorID.GOOGLE_WORKSPACE,
        ConnectorID.APPLE_BUSINESS_MANAGER,
    }
