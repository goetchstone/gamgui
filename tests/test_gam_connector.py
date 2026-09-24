from __future__ import annotations

import pytest

from gamgui.core.connectors.base import RiskLevel
from gamgui.core.gam.commands import EXPECTED_GAM_VERSION, GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind

# The per-user reads, and the argv each must send for the user asked about. The mock answers them per
# user, as GAM does (each fixture user has different data; an address that isn't a user fails), so a
# connector reading the wrong user — or a fixed one — fails here. It used to answer Alice's data for
# anyone, and five wrong-target reads passed the whole suite (review F20).
# For an address that isn't a user, the Gmail/Calendar reads fail as GAM's token request for it does
# (NOT_FOUND); `print groups member` gets the Directory API's "Invalid Input: memberKey", which names
# no user and stays UNKNOWN.
PER_USER_READS = [
    ("list_delegates", GAMCommands.print_delegates, GAMErrorKind.NOT_FOUND),
    ("get_signature", GAMCommands.show_signature, GAMErrorKind.NOT_FOUND),
    ("list_user_groups", GAMCommands.print_groups_member, GAMErrorKind.UNKNOWN),
    ("get_vacation", GAMCommands.show_vacation, GAMErrorKind.NOT_FOUND),
    ("list_calendar_acls", GAMCommands.print_calendar_acls, GAMErrorKind.NOT_FOUND),
]


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


async def test_a_failed_write_says_why_in_words_and_keeps_gams_error(connector, monkeypatch):
    res = await connector.set_signature("gone-missing@example.com", "Hi", html=True)
    assert res.ok is False
    assert res.remediation == "The requested user, group, or resource was not found."
    assert "Does not exist" in res.detail                 # GAM's own line, for a details disclosure

    async def no_binary(*_a, **_k):
        raise RuntimeError("GAM binary not found at /nowhere/gam")

    monkeypatch.setattr(connector.runner, "run_authenticated", no_binary)
    res = await connector.set_signature("alice@example.com", "Hi", html=True)
    assert res.remediation.startswith("Something went wrong talking to GAM") and "/nowhere/gam" in res.detail


@pytest.mark.parametrize("fail, kind", [("auth", "AUTH_EXPIRED"), ("scope", "SCOPE_MISSING"),
                                        ("notfound", "NOT_FOUND")])
async def test_a_failed_write_carries_its_error_kind(connector, fail, kind):
    # A bulk loop reads it to stop on a failure every later call would share (sign-in expired, a
    # scope missing) instead of reporting the same cause once per remaining user.
    from gamgui.core.gam.errors import GAMErrorKind

    res = await connector._run_write("probe", "alice@example.com", ["MOCKFAIL", fail], RiskLevel.LOW)
    assert res.ok is False and res.kind is GAMErrorKind[kind]
    ok = await connector.set_signature("alice@example.com", "Hi", html=True)
    assert ok.ok and ok.kind is None


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


async def test_reset_password_runs_only_the_reset(connector, gam_calls):
    # Ending sessions is offboarding's own step now (revoke_access): as the reset's follow-up, a failed
    # sign-out was audited but swallowed, so the run showed "✓ Reset password" (failure-log 2026-09-23).
    res = await connector.reset_password("alice@example.com")
    assert res.ok is True
    assert gam_calls()[-1] == ["update", "user", "alice@example.com", "password", "random", "changepassword", "off"]
    assert _audit_rows(connector) == [("reset_password", "alice@example.com", True)]


async def test_revoke_access_is_one_audited_deprovision_whose_failure_is_returned(connector, gam_calls):
    res = await connector.revoke_access("alice@example.com")
    assert res.ok is True
    assert gam_calls()[-1] == ["user", "alice@example.com", "deprovision", "signout"]
    bad = await connector.revoke_access("SIGNOUTFAIL-alice@example.com")
    assert bad.ok is False and "Sign Out Failed" in bad.detail          # returned, not swallowed
    assert _audit_rows(connector) == [("revoke_access", "alice@example.com", True),
                                      ("revoke_access", "SIGNOUTFAIL-alice@example.com", False)]


@pytest.mark.parametrize("read, builder, unknown", PER_USER_READS, ids=[r[0] for r in PER_USER_READS])
async def test_a_per_user_read_asks_gam_about_that_user(connector, gam_calls, read, builder, unknown):
    alice = await getattr(connector, read)("alice@example.com")
    carol = await getattr(connector, read)("carol@example.com")
    assert gam_calls() == [builder("alice@example.com"), builder("carol@example.com")]
    assert alice != carol
    with pytest.raises(GAMError) as ei:
        await getattr(connector, read)("nobody@example.com")
    assert ei.value.kind is unknown and ei.value.exit_code == 50


async def test_per_user_reads_return_that_users_data(connector):
    carol = "carol@example.com"
    assert await connector.list_delegates(carol) == ["helpdesk@example.com"]
    assert await connector.list_delegates("bob@example.com") == []
    assert await connector.get_signature(carol) == "Carol Clark<br>Operations"
    assert await connector.list_user_groups(carol) == ["it@example.com"]
    vac = await connector.get_vacation(carol)
    assert (vac.enabled, vac.subject, vac.message) == (False, "Conference week", "Carol is at a conference.")
    acls = await connector.list_calendar_acls(carol)
    assert [(a.scope_value, a.role) for a in acls] == [(carol, "owner"), ("helpdesk@example.com", "reader")]
