from __future__ import annotations

from gamgui.core.connectors.base import RiskLevel


async def test_list_users(connector):
    users = await connector.list_users()
    assert len(users) == 3
    by_email = {u.primary_email: u for u in users}
    assert by_email["bob@example.com"].suspended is True
    assert by_email["alice@example.com"].suspended is False


async def test_get_user(connector):
    u = await connector.get_user("alice@example.com")
    assert u.given_name == "Alice"
    assert u.aliases == ["a.anders@example.com"]


async def test_list_group_members(connector):
    members = await connector.list_group_members("team@example.com")
    assert len(members) == 2
    assert members[1].role == "MANAGER"


async def test_set_signature_succeeds_and_audits_redacted(connector):
    res = await connector.set_signature("alice@example.com", "Best,\nAlice", html=True)
    assert res.ok is True
    last = connector.audit.tail()[-1]
    assert last["action"] == "set_signature"
    assert last["ok"] is True
    assert "Best,\nAlice" not in last["argv"]  # signature value redacted in the audit log


async def test_plan_suspend_is_destructive(connector):
    previews = connector.plan_suspend(["alice@example.com"])
    assert len(previews) == 1
    assert previews[0].risk == RiskLevel.DESTRUCTIVE
    assert previews[0].argv == ["update", "user", "alice@example.com", "suspended", "on"]


async def test_apply_runs_planned_changes(connector):
    previews = connector.plan_suspend(["alice@example.com"])
    results = await connector.apply(previews)
    assert results[0].ok is True


async def test_connection_test_ok(connector):
    status = await connector.test()
    assert status.ok is True
    assert "7.46.01" in status.version


async def test_list_delegates(connector):
    delegates = await connector.list_delegates("alice@example.com")
    assert delegates == ["assistant@example.com", "backup@example.com"]


async def test_resolve_links_workspace_account(connector):
    from gamgui.core.connectors.person import Person

    person = Person(id="alice@example.com", primary_email="alice@example.com")
    account = await connector.resolve(person)
    assert account is not None
    assert account.native_id == "alice@example.com"
