"""The mock `gam` must fail the way real GAM does — tripwires for the mock itself.

A mock more permissive than GAM converts a live break into a green test (CLAUDE.md, "the mock lies"):
its catch-all used to exit 0, so 23 writes with no handler, a `delete events` without `doit` and an
invalid calendar role all "succeeded". Every builder the app calls must be classified below, every
write must be accepted in exactly the shape the builder emits, and a malformed shape must fail.
"""

from __future__ import annotations

import pytest

from gamgui.core.gam.commands import CALENDAR_ACL_ROLES, GAMCommands as C
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.setup import DWD_SCOPES

CAL = "c_team@group.calendar.google.com"
DWD = [scope for scope, _ in DWD_SCOPES]

# Every write builder the app calls, with the options it can emit.
WRITES = {
    "create_user": [C.create_user("new@example.com", "New", "Hire", "Temp-Pa55w0rd", org_unit="/Staff",
                                  notify="mgr@example.com"),
                    C.create_user("new@example.com", "New", "Hire", "Temp-Pa55w0rd", change_password=False)],
    "update_organization": [C.update_organization("alice@example.com", "Lead", "Sales"),
                            C.update_organization("alice@example.com", "", "Sales")],
    "set_suspended": [C.set_suspended("alice@example.com", True), C.set_suspended("alice@example.com", False)],
    "reset_password": [C.reset_password("alice@example.com")],
    "signout_user": [C.signout_user("alice@example.com")],
    "deprovision_user": [C.deprovision_user("alice@example.com")],
    "delete_user": [C.delete_user("alice@example.com")],
    "undelete_user": [C.undelete_user("alice@example.com")],
    "add_calendar_acl": [C.add_calendar_acl("alice@example.com", "bob@example.com"),
                         C.add_calendar_acl("alice@example.com", "group:sales@example.com", role="writer")]
                        + [C.add_calendar_acl("alice@example.com", "bob@example.com", role=r)
                           for r in CALENDAR_ACL_ROLES],
    "delete_calendar_acl": [C.delete_calendar_acl("alice@example.com", "user:bob@example.com")],
    "add_calendar_acl_cal": [C.add_calendar_acl_cal(CAL, "bob@example.com"),
                             C.add_calendar_acl_cal(CAL, "domain", role="freebusyreader", send_notifications=True)]
                            + [C.add_calendar_acl_cal(CAL, "bob@example.com", role=r) for r in CALENDAR_ACL_ROLES],
    "delete_calendar_acl_cal": [C.delete_calendar_acl_cal(CAL, "user:bob@example.com")],
    "subscribe_calendar": [C.subscribe_calendar("bob@example.com", CAL),
                           C.subscribe_calendar("bob@example.com", CAL, selected=False)],
    "remove_calendar": [C.remove_calendar("alice@example.com", CAL)],
    "delete_event": [C.delete_event(CAL, "evt-1")],
    "remove_all_calendar_acls": [C.remove_all_calendar_acls("leaver@example.com")],
    "add_calendar_event": [C.add_calendar_event("mgr@example.com", "Confirm", "2026-07-23", "2026-07-24"),
                           C.add_calendar_event("mgr@example.com", "Confirm", "2026-07-23", "2026-07-24",
                                                description="d", attendee="it@example.com")],
    "create_datatransfer": [C.create_datatransfer("leaver@example.com", "drive,calendar", "mgr@example.com")]
                           + [C.create_datatransfer("leaver@example.com", "drive,calendar", "mgr@example.com", privacy=p)
                              for p in C.TRANSFER_PRIVACY],
    "create_tasklist": [C.create_tasklist("mgr@example.com", "Onboard Ada")],
    "create_task": [C.create_task("mgr@example.com", "MockTasklist_abc123", "Order a laptop", notes="n")],
    "send_email": [C.send_email("new@example.com", "Welcome", "<p>Hi</p>"),
                   C.send_email("new@example.com", "Welcome", "Hi", html=False)],
    "set_signature": [C.set_signature("alice@example.com", "Best,\nAlice"),
                      C.set_signature("alice@example.com", "", html=False)],
    "add_delegate": [C.add_delegate("leaver@example.com", "mgr@example.com")],
    "remove_delegate": [C.remove_delegate("alice@example.com", "assistant@example.com")],
    "set_vacation": [C.set_vacation("alice@example.com", "Away", "Back soon"),
                     C.set_vacation("alice@example.com", "Away", "Back", html=False, start="2026-07-01",
                                    end="2026-07-10", contacts_only=True, domain_only=True)],
    "vacation_off": [C.vacation_off("alice@example.com")],
    "add_forwarding_address": [C.add_forwarding_address("alice@example.com", "fwd@example.com")],
    "set_forward": [C.set_forward("alice@example.com", "fwd@example.com", a) for a in C.FORWARD_ACTIONS],
    "forward_off": [C.forward_off("alice@example.com")],
    "create_user_alias": [C.create_user_alias("a.anders@example.com", "alice@example.com")],
    "delete_alias": [C.delete_alias("a.anders@example.com")],
    "create_group": [C.create_group("new-team@example.com"),
                     C.create_group("new-team@example.com", "New team", "What it is for")],
    "add_group_member": [C.add_group_member("sales@example.com", "carol@example.com", r)
                         for r in ("member", "manager", "owner")],
    "remove_group_member": [C.remove_group_member("sales@example.com", "carol@example.com")],
}

# Reads stay canned, but each one the app issues must still have a handler. The per-user ones answer
# for their target (tests/test_gam_connector.py::test_a_per_user_read_asks_gam_about_that_user).
READS = {
    "version": [C.version()],
    "check_svcacct": [C.check_svcacct("admin@example.com", DWD)],
    "print_users": [C.print_users(), C.print_users(query="isSuspended=false")],
    "print_cros": [C.print_cros("status:ACTIVE")],
    "print_filelist": [C.print_filelist("alice@example.com", "name contains 'x'")],
    "report_users": [C.report_users("2026-06-16", ["accounts:used_quota_in_mb"])],
    "info_user": [C.info_user("alice@example.com")],
    "print_calendar_acls": [C.print_calendar_acls("alice@example.com")],
    "print_resources": [C.print_resources()],
    "print_user_calendars": [C.print_user_calendars("alice@example.com")],
    "print_all_calendars": [C.print_all_calendars()],
    "print_calendar_acls_cal": [C.print_calendar_acls_cal(CAL)],
    "print_events": [C.print_events(CAL, query="standup")],
    "get_event": [C.get_event(CAL, "evt-1")],
    "print_datatransfers": [C.print_datatransfers("leaver@example.com")],
    "show_signature": [C.show_signature("alice@example.com")],
    "print_delegates": [C.print_delegates("alice@example.com")],
    "print_forwarding_addresses": [C.print_forwarding_addresses("alice@example.com")],
    "search_messages": [C.search_messages("alice@example.com", "from:x")],
    "show_vacation": [C.show_vacation("alice@example.com")],
    "print_groups": [C.print_groups()],
    "print_domains": [C.print_domains()],
    "print_group_members": [C.print_group_members("sales@example.com")],
    "print_groups_member": [C.print_groups_member("alice@example.com")],
}

# Not a command: the `todrive` argv suffix a Builder read's export appends (export_to_sheet).
NOT_A_COMMAND = {"todrive_args"}


def test_every_builder_is_classified():
    # A new builder must land in WRITES (and get a strict mock handler) or READS before it ships.
    builders = {name for name, v in vars(C).items() if isinstance(v, staticmethod)}
    assert builders - set(WRITES) - set(READS) - NOT_A_COMMAND == set()


def test_every_builder_has_a_caller_in_the_app():
    # A builder nothing calls is argv no flow reviews or runs, yet the contract and the mock vouch for
    # it; six once sat here (plan Q11), one in two grammar forms at once. Build it when a flow needs it.
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "gamgui"
    src = "\n".join(p.read_text() for p in root.rglob("*.py") if p.name != "commands.py")
    builders = {name for name, v in vars(C).items() if isinstance(v, staticmethod)}
    assert {b for b in builders if not re.search(rf"\bGAMCommands\.{b}\b", src)} == set()


@pytest.mark.parametrize("argv", [a for cases in {**WRITES, **READS}.values() for a in cases],
                         ids=lambda a: " ".join(a)[:60])
async def test_mock_accepts_every_shape_the_app_emits(runner, domain, argv):
    await runner.run_authenticated(domain, argv, serialize=True)


@pytest.mark.hand_built_argv   # partial `vacation` shapes (set_vacation names every field) to seed the merge
async def test_mock_vacation_merges_like_gam(runner, domain, gam_state):
    # GAM's setVacation updates only the fields the command names; a stateless mock (or one that
    # replaced the settings) would hide a leftover "domain only" or end date behind a green test.
    await runner.run_authenticated(domain, ["user", "a@example.com", "vacation", "on", "domainonly", "true",
                                            "end", "2025-08-15"], serialize=True)
    await runner.run_authenticated(domain, ["user", "a@example.com", "vacation", "on", "subject", "S"],
                                   serialize=True)
    shown = await runner.run_authenticated(domain, C.show_vacation("a@example.com"))
    assert "Domain Only: True" in shown and "End Date: 2025-08-15" in shown and "Subject: S" in shown
    await runner.run_authenticated(domain, ["user", "a@example.com", "vacation", "on", "domainonly", "false",
                                            "end", "NotSpecified"], serialize=True)
    shown = await runner.run_authenticated(domain, C.show_vacation("a@example.com"))
    assert "Domain Only: False" in shown and "End Date: NotSpecified" in shown


@pytest.mark.parametrize("argv,needle", [
    # Without doit GAM only reports what it would delete — "Event deleted." would be a lie.
    (["calendars", CAL, "delete", "events", "eventid", "evt-1", "sendupdates", "none"], "Use doit"),
    # The builders refuse a bad role now (ValueError); the mock must still reject one GAM would.
    (["user", "alice@example.com", "add", "calendaracls", "primary", "admin", "bob@example.com"],
     "Invalid choice (admin)"),
    (["calendars", CAL, "add", "calendaracls", "viewer", "bob@example.com"], "Invalid choice (viewer)"),
    (["user", "alice@example.com", "signature"], "Missing argument"),
    (["user", "alice@example.com", "add", "delegate"], "Missing argument"),
    (C.set_vacation("alice@example.com", "Away", "Back", start="July 1"), "Invalid date"),
    (["update", "user", "alice@example.com", "organization", "title", "T"], "Missing argument"),
    (["update", "group", "sales@example.com", "add", "admin", "carol@example.com"], "Invalid argument"),
    (["create", "datatransfer", "leaver@example.com", "drive,mail", "mgr@example.com"], "Invalid choice (mail)"),
    # A privacy level is a Drive parameter: GAM (_assignAppParameter) refuses it when no listed app takes it.
    (["create", "datatransfer", "leaver@example.com", "calendar", "mgr@example.com", "all"],
     "No data transfer application for key PRIVACY_LEVEL"),
    (["user", "alice@example.com", "forward", "on", "bounce", "fwd@example.com"], "Invalid choice (bounce)"),
    (["user", "alice@example.com", "signout", "now"], "Invalid argument"),
    (["user", "alice@example.com", "deprovision", "signout", "now"], "Invalid argument"),
    (["delete", "calendars", CAL], "mock: unhandled argv"),       # the catch-all fails now
    (["user", "alice@example.com", "delete", "calendars", CAL], "mock: unhandled argv"),
    # The per-user reads take only what the grammar gives them (formatjson is the classic trap).
    (["user", "alice@example.com", "print", "delegates", "formatjson"], "Invalid argument"),
    (["user", "alice@example.com", "show", "signature", "formatjson"], "Invalid argument"),
    (["user", "alice@example.com", "show", "vacation", "formatjson"], "Invalid argument"),
    (["print", "groups", "member", "alice@example.com", "bogus"], "Invalid argument"),
    (["user", "alice@example.com", "print", "calendaracls", "primary", "formatjson", "x"], "Invalid argument"),
    # `todrive <ToDriveAttribute>*`: only on a print/report read, and only in the shape the Builder emits.
    (["user", "alice@example.com", "show", "vacation", "todrive"], "Invalid argument"),
    (C.info_user("alice@example.com") + ["todrive"], "Invalid argument: todrive"),
    (C.check_svcacct("admin@example.com", DWD) + ["todrive", "tduser", "boss@example.com"],
     "Invalid argument: todrive"),
    # admin.directory.* are client-access (admin-token) scopes: GAM refuses them as service-account ones.
    (C.check_svcacct("admin@example.com", ["https://www.googleapis.com/auth/admin.directory.user"]),
     "Invalid choice (https://www.googleapis.com/auth/admin.directory.user)"),
    (["user", "admin@example.com", "check", "serviceaccount", "scopes"], "Missing argument"),
    (["user", "admin@example.com", "check", "serviceaccount", "scopes", ""], "Empty argument"),
    (C.print_delegates("alice@example.com") + ["todrive", "tdshare", "x@example.com", "writer"], "Invalid argument"),
    (C.print_delegates("alice@example.com") + ["todrive", "tduser"], "Missing argument"),
    (C.print_delegates("alice@example.com") + ["todrive", "tduser", ""], "Empty argument"),
    (C.print_users() + ["todrive", "tdtitle"], "Missing argument"),
    (C.print_users() + ["todrive", "tduser", "boss@example.com", "extra"], "Invalid argument"),
    (C.print_users() + ["todrive", "tdtitle", "T", "tduser", "boss@example.com"], "Invalid argument: tduser"),
    (C.print_users() + ["todrive", "tduser", "a@example.com", "tduser", "b@example.com"], "Invalid argument: tduser"),
])
@pytest.mark.hand_built_argv   # malformed on purpose: the grammar sweep would (rightly) refuse every one
async def test_mock_rejects_a_malformed_shape(runner, domain, argv, needle):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, argv, serialize=True)
    assert needle in ei.value.stderr
    assert ei.value.exit_code != 0


@pytest.mark.parametrize("argv", [
    C.delete_user("missing@example.com"),
    C.update_organization("nonexistent@example.com", "T", "D"),
    C.remove_group_member("missing-group@example.com", "carol@example.com"),
    C.remove_delegate("alice@example.com", "missing@example.com"),
    C.delete_event(CAL, "missing-evt"),
    C.info_user("nobody@example.com"),        # info user is keyed on the address, not "always Alice"
    # So are the per-user Gmail/Calendar reads: GAM can't get a token for an address that isn't a user.
    C.print_delegates("nobody@example.com"),
    C.show_signature("nobody@example.com"),
    C.show_vacation("nobody@example.com"),
    C.print_calendar_acls("nobody@example.com"),
])
async def test_mock_reports_a_missing_entity_as_not_found(runner, domain, argv):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, argv)
    assert ei.value.kind is GAMErrorKind.NOT_FOUND


async def test_info_user_is_keyed_on_the_address(connector):
    assert (await connector.get_user("carol@example.com")).given_name == "Carol"
    assert (await connector.get_user("bob@example.com")).suspended is True


async def test_mock_refuses_a_call_without_materialized_credentials(runner, tmp_path):
    # A GAMCFGDIR with no credentials in it (as `version` gets): an authenticated command there must
    # fail like GAM with no oauth2service.json, not answer from fixtures.
    bare = tmp_path / "bare"
    bare.mkdir()
    res = await runner._exec(C.print_users(), bare, 15)
    assert res.returncode != 0 and "oauth2service.json" in res.stderr
    assert (await runner._exec(C.version(), bare, 15)).returncode == 0   # needs no credentials


async def test_argv_recorder_sees_exactly_what_ran(runner, domain, gam_calls):
    tricky = "Best,\nAlice <a href='x'>  two  spaces</a>"
    await runner.run_authenticated(domain, C.set_signature("alice@example.com", tricky), serialize=True)
    assert gam_calls() == [["user", "alice@example.com", "signature", tricky, "html"]]


async def test_mock_fails_a_per_user_read_of_an_unknown_address_as_gam_does(runner, domain):
    # GAM 7.48.11, read from the vendored build: the token request for an address that isn't a user
    # fails and is reported against it (handleOAuthTokenError → entityActionFailedWarning, exit 50);
    # `print groups member` gets the Directory API's "Invalid Input: memberKey" (invalidMember →
    # entityActionFailedExit). Real wording approximate — not captured from a tenant.
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.show_vacation("nobody@example.com"))
    assert ei.value.exit_code == 50
    assert "User: nobody@example.com, User:, Show Failed: invalid_grant: Invalid email or User ID" in ei.value.stderr
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.print_groups_member("nobody@example.com"))
    assert ei.value.exit_code == 50 and "Invalid Input: memberKey" in ei.value.stderr


@pytest.mark.parametrize("todrive", [C.todrive_args(), C.todrive_args("boss@example.com"),
                                     C.todrive_args(title="Delegates"), C.todrive_args("boss@example.com", "D")],
                         ids=lambda a: " ".join(a))
@pytest.mark.parametrize("read", [C.print_delegates("alice@example.com"), C.print_users(), C.print_groups(),
                                  C.print_calendar_acls("alice@example.com")], ids=lambda a: " ".join(a[:4]))
async def test_mock_accepts_the_todrive_shapes_the_builder_emits(runner, domain, read, todrive):
    await runner.run_authenticated(domain, read + todrive)


@pytest.mark.parametrize("delegate", ["delegate-exists@example.com", "assistant@example.com"])
async def test_mock_refuses_a_delegate_that_already_exists(runner, domain, delegate):
    # GAM's processDelegates catches Gmail's alreadyExists and reports it against the pair
    # (entityActionFailedWarning, ACTION_FAILED_RC), read from the vendored build. *exists* is the
    # mock's trigger; a delegate the user already has (the `print delegates` data) fails the same way.
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.add_delegate("alice@example.com", delegate), serialize=True)
    assert ei.value.exit_code == 50
    assert f"User: alice@example.com, Delegate: {delegate}, Add Failed: Delegate already exists." in ei.value.stderr


async def test_mock_delegates_persist_like_gmail_with_state(connector, gam_state):
    # With GAM_MOCK_STATE the mock keeps each user's delegates the way Gmail does: an add shows in the
    # next `print delegates`, a second add of the same address fails, and a removal really removes it.
    alice = "alice@example.com"
    assert (await connector.add_delegate(alice, "carol@example.com")).ok
    assert await connector.list_delegates(alice) == ["assistant@example.com", "backup@example.com", "carol@example.com"]
    again = await connector.add_delegate(alice, "Carol@Example.com")
    assert not again.ok and "already exists" in again.detail
    assert (await connector.remove_delegate(alice, "assistant@example.com")).ok
    assert await connector.list_delegates(alice) == ["backup@example.com", "carol@example.com"]
    assert (await connector.add_delegate(alice, "assistant@example.com")).ok


async def test_mock_check_serviceaccount_reports_the_scopes_asked_as_gam_does(runner, domain):
    # It listed admin.directory.user/group as delegation PASS lines — client-access scopes in GAM, which
    # `check serviceaccount` never checks. GAM (checkServiceAccount, vendored 7.48.11) prints each scope
    # asked, lowercased and sorted, as "  {scope:73} PASS (j/n)".
    out = await runner.run_authenticated(domain, C.check_svcacct("admin@example.com", [s.upper() for s in DWD]))
    rows = [line.split() for line in out.splitlines() if line.startswith("  https://")]
    assert [r[0] for r in rows] == sorted(DWD)
    assert [r[1:] for r in rows] == [["PASS", f"({j}/{len(DWD)})"] for j in range(1, len(DWD) + 1)]
    assert "admin.directory" not in out and "All scopes PASSED!" in out


@pytest.mark.hand_built_argv   # the bare form: the app never sends it now, but GAM accepts it
async def test_mock_bare_check_serviceaccount_fails_on_gams_larger_default_set(runner, domain):
    # A bare check asks about GAM's own default scopes; the simulated tenant authorized only DWD_SCOPES,
    # so GAM prints FAIL for the rest, the Admin-console link, and exits SCOPES_NOT_AUTHORIZED_RC (1).
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, ["user", "admin@example.com", "check", "serviceaccount"])
    assert ei.value.exit_code == 1
    # GAM prints the whole answer on stdout (printLine), not stderr — the error must carry it.
    assert "https://www.googleapis.com/auth/keep" in ei.value.stdout and " FAIL (" in ei.value.stdout
    assert "Some scopes FAILED or should be DISABLED!" in ei.value.stdout and ei.value.stderr == ""


# The simulated tenant behind a `*partialdwd*` admin authorized all of DWD_SCOPES but these two.
PARTIAL_MISSING = ["https://www.googleapis.com/auth/gmail.settings.sharing", "https://www.googleapis.com/auth/tasks"]


async def test_mock_check_serviceaccount_fails_a_scope_the_tenant_did_not_authorize(runner, domain):
    # What the vendored 7.48.11 checkServiceAccount does when a scope asked for fails: every row still
    # prints ("  {scope:73} FAIL (j/n)"), then authorizeScopes(SCOPE_AUTHORIZATION_FAILED) prints the
    # short link and the admin.google.com link — whose clientScopeToAdd is the scopes checked plus
    # userinfo.email, sorted — all on stdout, and GAM exits SCOPES_NOT_AUTHORIZED_RC (1).
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.check_svcacct("partialdwd@example.com", DWD))
    err = ei.value
    assert err.exit_code == 1 and err.stderr == ""
    rows = {line.split()[0]: line.split()[1] for line in err.stdout.splitlines() if line.startswith("  https://")}
    assert sorted(rows) == sorted(DWD)
    assert sorted(s for s, st in rows.items() if st == "FAIL") == PARTIAL_MISSING
    lines = err.stdout.splitlines()
    at = lines.index("Some scopes FAILED or should be DISABLED!")
    assert lines[at + 1] == "To update authorization, please go to the following link in your browser:"
    assert lines[at + 2].startswith("https://gam-shortn.appspot.com/")
    long_url = lines[at + 3].strip()
    wanted = ",".join(sorted(DWD + ["https://www.googleapis.com/auth/userinfo.email"]))
    assert long_url.startswith(f"https://admin.google.com/ac/owl/domainwidedelegation?clientScopeToAdd={wanted}&")
    assert long_url.endswith("&overwriteClientId=true&authuser=partialdwd@example.com")
