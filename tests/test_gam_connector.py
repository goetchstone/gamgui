from __future__ import annotations

from gamgui.core.connectors.base import RiskLevel
from gamgui.core.gam.commands import EXPECTED_GAM_VERSION


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
    assert EXPECTED_GAM_VERSION in status.version


async def test_list_delegates(connector):
    delegates = await connector.list_delegates("alice@example.com")
    assert delegates == ["assistant@example.com", "backup@example.com"]


async def test_list_users_have_titles(connector):
    by_email = {u.primary_email: u for u in await connector.list_users()}
    assert by_email["alice@example.com"].title == "IT Director"


async def test_get_signature(connector):
    sig = await connector.get_signature("alice@example.com")
    assert "Best," in sig


async def test_usage_report(connector):
    from gamgui.core.reports import USAGE_PARAMS

    data = await connector.usage_report(USAGE_PARAMS)
    assert data["rows"]  # mock returns rows for the first date tried
    assert "bob@example.com" in {r.get("email") for r in data["rows"]}


async def test_list_user_groups(connector):
    groups = await connector.list_user_groups("alice@example.com")
    assert groups == ["sales@example.com", "staff@example.com"]


async def test_get_vacation(connector):
    vac = await connector.get_vacation("alice@example.com")
    assert vac.enabled is True
    assert vac.subject == "Out of office"


async def test_set_and_clear_vacation(connector):
    r = await connector.set_vacation("alice@example.com", "OOO", "away", start="2026-07-01")
    assert r.ok is True
    r2 = await connector.clear_vacation("alice@example.com")
    assert r2.ok is True


async def test_resolve_links_workspace_account(connector):
    from gamgui.core.connectors.person import Person

    person = Person(id="alice@example.com", primary_email="alice@example.com")
    account = await connector.resolve(person)
    assert account is not None
    assert account.native_id == "alice@example.com"


def _audit_rows(connector):
    return [(e["action"], e["target"], e["ok"]) for e in connector.audit.tail()]


async def test_reset_password_audits_its_follow_up_signout(connector, gam_calls):
    # The sign-out ends every session the leaver has — a write, so it goes through _run_write too.
    res = await connector.reset_password("alice@example.com")
    assert res.ok is True
    assert gam_calls()[-1] == ["user", "alice@example.com", "signout"]
    assert _audit_rows(connector) == [("reset_password", "alice@example.com", True),
                                      ("signout_user", "alice@example.com", True)]


async def test_a_failed_follow_up_signout_is_audited_but_the_reset_stands(connector, monkeypatch):
    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    real = connector.runner.run_authenticated

    async def signout_fails(domain, argv, **kw):
        if argv[-1:] == ["signout"]:
            raise GAMError(GAMErrorKind.PERMISSION_DENIED, exit_code=1, stderr="ERROR: 403: Forbidden")
        return await real(domain, argv, **kw)

    monkeypatch.setattr(connector.runner, "run_authenticated", signout_fails)
    res = await connector.reset_password("alice@example.com")
    assert res.ok is True                                   # best-effort: the reset itself succeeded
    assert _audit_rows(connector) == [("reset_password", "alice@example.com", True),
                                      ("signout_user", "alice@example.com", False)]
