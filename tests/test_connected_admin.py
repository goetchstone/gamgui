"""A write that cuts an account off is refused on the admin GamGUI signs in as, whoever asks for it.

Failure log 2026-11-04, the connected admin. The check is in `_run_write`, so these hold each write and each
route to it; compared by Google account ID, with an unconfirmed admin refusing. Offline only: GAM's `id` is the
mock's, and the token's `sub` a fixture's.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from gamgui.core import lifecycle
from gamgui.core.gam.commands import GAMCommands as C, cut_off_account
from gamgui.web.jobs import start_job

from .helpers import ADMIN, ADMIN_ID, gam_writes
from .test_mock_gam import WRITES
from .test_users_web import client  # noqa: F401 - the app with the mock behind it

ALICE_ID = "100000000000000000001"
CUTS_OFF = {"delete_user", "reset_password", "set_password", "signout_user", "deprovision_user"}   # and suspend, below


def _token(email=ADMIN, sub=ADMIN_ID) -> str:
    return json.dumps({"decoded_id_token": {"email": email, "sub": sub}, "refresh_token": "t"})


def test_only_the_writes_that_cut_an_account_off_count():
    found = {name for name, cases in WRITES.items() for argv in cases if cut_off_account(argv)}
    assert found == CUTS_OFF | {"set_suspended"}
    assert cut_off_account(C.set_suspended("a@e.com", False)) is None      # unsuspend restores access
    assert cut_off_account(C.set_suspended("a@e.com", True)) == "a@e.com"
    assert cut_off_account(C.update_organization("a@e.com", "T", "D")) is None
    assert cut_off_account(C.undelete_user("a@e.com")) is None


@pytest.mark.parametrize("argv,who", [
    (["delete", "user", "a@e.com", "noactionifalias"], "a@e.com"),
    (["user", "a@e.com", "deprov"], "a@e.com"),
    (["user", "a@e.com", "deprovision", "popimap"], "a@e.com"),
    (["update", "user", "a@e.com", "firstname", "A", "password", "x"], "a@e.com"),
    (["update", "user", "a@e.com", "suspended", "true"], "a@e.com"),
    (["update", "user", "a@e.com", "suspended"], "a@e.com"),
    (["UPDATE", "users", "a@e.com", "Suspended", "on"], "a@e.com"),
    (["users", "a@e.com", "sign_out"], "a@e.com"),
    (["all", "users", "signout"], "all users"),
])
def test_a_variant_of_a_cut_off_shape_counts_too(argv, who):
    assert cut_off_account(argv) == who


@pytest.mark.parametrize("write", ["delete_user", "signout_user", "reset_password", "revoke_access"])
@pytest.mark.parametrize("address", [ADMIN, "Admin@Example.com", f" {ADMIN} "])
async def test_each_write_is_refused_on_the_admin_in_any_spelling(connector, gam_calls, write, address):
    result = await getattr(connector, write)(address)
    assert not result.ok and "connected as" in result.detail and "sign GamGUI out" in result.detail
    assert gam_writes(gam_calls()) == []


async def test_suspend_is_refused_on_the_admin_and_unsuspend_is_not(connector, gam_calls):
    [refused] = await connector.apply(connector.plan_suspend([ADMIN], suspend=True))
    assert not refused.ok and "connected as" in refused.detail
    assert gam_writes(gam_calls()) == []
    [ok] = await connector.apply(connector.plan_suspend([ADMIN], suspend=False))
    assert ok.ok


async def test_the_refusal_is_audited_and_nothing_is_written(connector):
    await connector.delete_user(ADMIN)
    rec = connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("delete_user", ADMIN, False)
    assert "connected as" in rec["extra"]["error"]


async def test_the_refusal_says_why_as_the_remediation_not_gams_error(connector):
    result = await connector.signout_user(ADMIN)
    assert result.remediation == result.detail and "connected as" in result.remediation


async def test_an_alias_of_the_admin_is_refused_by_account_id(connector, vault, domain, gam_calls):
    vault.set(domain, "oauth2", _token("alice@example.com", ALICE_ID))
    result = await connector.signout_user("a.anders@example.com")
    assert not result.ok and "(alice@example.com)" in result.detail
    assert gam_writes(gam_calls()) == []


async def test_a_token_whose_address_was_renamed_still_compares_by_id(connector, vault, domain, gam_calls):
    # The admin is alice@'s account, and the token still names her alias: her primary address is the same ID.
    vault.set(domain, "oauth2", _token("a.anders@example.com", ALICE_ID))
    result = await connector.delete_user("alice@example.com")
    assert not result.ok and "sign GamGUI out" in result.detail
    assert gam_writes(gam_calls()) == []


async def test_someone_else_is_read_once_then_written(connector, gam_calls):
    assert (await connector.signout_user("carol@example.com")).ok
    calls = gam_calls()
    assert C.info_user("carol@example.com", fields=("primaryEmail", "id")) in calls
    assert gam_writes(calls) == [C.signout_user("carol@example.com")]


async def test_a_write_that_cuts_no_one_off_reads_nothing(connector, gam_calls):
    assert (await connector.forward_off("carol@example.com")).ok
    assert gam_calls() == [C.forward_off("carol@example.com")]


@pytest.mark.parametrize("oauth2,why", [
    ("not json", "doesn't say"),
    (json.dumps({"refresh_token": "t"}), "doesn't say"),
    (json.dumps({"decoded_id_token": {"email": ADMIN}}), "doesn't say"),                        # no sub
    (_token(sub=""), "doesn't say"),
    (_token(sub="abc"), "doesn't say"),                                                         # not Google's digits
    (_token(sub="9" * 65), "doesn't say"),
    (_token(sub=ALICE_ID), "another account"),                                                  # GAM reads another ID
    (_token(email="nobody@example.com", sub=ADMIN_ID), "couldn't read"),                        # GAM can't read them
])
async def test_an_unconfirmed_admin_refuses_every_cut_off_write(connector, vault, domain, gam_calls, oauth2, why):
    vault.set(domain, "oauth2", oauth2)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "can't confirm which account" in result.detail and why in result.detail
    assert gam_writes(gam_calls()) == []
    assert (await connector.forward_off("carol@example.com")).ok      # a change that cuts no one off needs no admin


async def test_a_re_import_is_not_covered_by_the_last_confirmation(connector, vault, domain):
    assert (await connector.signout_user("carol@example.com")).ok
    vault.set(domain, "oauth2", _token(sub=ALICE_ID))
    assert "can't confirm" in (await connector.signout_user("carol@example.com")).detail


@pytest.mark.parametrize("address,says", [
    ("nobody@example.com", "GAM has no account nobody@example.com"),
    ("a,b@example.com", "GAM has no account a,b@example.com"),            # a spelling GAM can't read as one account
])
async def test_only_an_account_gam_reads_can_be_cut_off(connector, gam_calls, address, says):
    result = await connector.signout_user(address)
    assert not result.ok and says in result.detail
    assert gam_writes(gam_calls()) == []


@pytest.mark.parametrize("record", [
    '{"primaryEmail": "carol@example.com"}', "", '{"primaryEmail": "carol@example.com", "id": 100}',
    '{"primaryEmail": "carol@example.com", "id": " 100000000000000000009 "}',
    '[{"primaryEmail": "carol@example.com", "id": "1"}, {"primaryEmail": "admin@example.com", "id": "100000000000000000009"}]',
])
async def test_a_record_without_an_id_refuses(connector, gam_calls, monkeypatch, record):
    real = connector.runner.run_authenticated

    async def no_id(domain, argv, **kw):
        return record if argv[:3] == ["info", "user", "carol@example.com"] else await real(domain, argv, **kw)

    monkeypatch.setattr(connector.runner, "run_authenticated", no_id)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "didn't say whose account" in result.detail


async def test_a_broken_check_refuses_and_is_on_the_record(connector, gam_calls, monkeypatch):
    async def broken(commands, previewing=False):
        raise RuntimeError("the check went wrong")

    monkeypatch.setattr(connector, "cut_off_refusal", broken)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "the check went wrong" in result.detail
    assert gam_writes(gam_calls()) == []
    assert connector.audit.tail()[-1]["ok"] is False


async def test_a_keychain_that_refuses_leaves_the_admin_unconfirmed(connector, gam_calls, monkeypatch):
    def denied(_domain):
        raise RuntimeError("keychain denied: secret-looking-text")

    monkeypatch.setattr(connector.runner.vault, "oauth_admin_email", denied)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "couldn't be read" in result.detail and "secret-looking-text" not in result.detail
    assert gam_writes(gam_calls()) == []


def test_a_keychain_that_refuses_does_not_break_a_preview(client, gam_calls, monkeypatch):  # noqa: F811
    def denied(_domain):
        raise RuntimeError("keychain denied")

    monkeypatch.setattr(client.app.state.gamgui.connector.runner.vault, "oauth_admin_email", denied)
    for path in ("/users/suspend/preview", "/users/delete/confirm"):
        r = client.post(path, data={"email": "carol@example.com"})
        assert r.status_code == 200 and "couldn" in r.text, path
    r = client.post("/builder/preview", data={"cid": "build.suspend_user", "email": "carol@example.com"})
    assert r.status_code == 200 and 'name="preview"' not in r.text
    assert gam_writes(gam_calls()) == []


async def test_a_token_nested_too_deep_is_unconfirmed(connector, vault, domain, gam_calls):
    vault.set(domain, "oauth2", "[" * 200000)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "confirm which account" in result.detail


async def test_a_failed_read_refuses_in_gams_words(connector, gam_calls, monkeypatch):
    real = connector.runner.run_authenticated

    async def refused(domain, argv, **kw):
        if argv[:3] == ["info", "user", "carol@example.com"]:
            raise RuntimeError("the read went wrong")
        return await real(domain, argv, **kw)

    monkeypatch.setattr(connector.runner, "run_authenticated", refused)
    result = await connector.signout_user("carol@example.com")
    assert not result.ok and "the read went wrong" in result.detail
    assert gam_writes(gam_calls()) == []


async def test_offboarding_the_admin_runs_no_step_that_cuts_it_off(connector, gam_calls):
    steps = lifecycle.build_offboard_steps(ADMIN, "alice@example.com", "s", "m", 30, date(2026, 6, 23))
    job = start_job({}, len(steps))
    await lifecycle.run_offboard(job, connector, steps)
    assert job.applied == 0 and job.failed_items == ["Reset password"]
    assert gam_writes(gam_calls()) == []


# --- each route says so at the preview, and the run holds ---------------------------------------------------

def test_users_routes_refuse_the_admin(client, gam_calls):  # noqa: F811
    for path, data in [("/users/suspend/preview", {"email": ADMIN}),
                       ("/users/delete/confirm", {"email": ADMIN}),
                       ("/users/signout", {"email": ADMIN}),
                       ("/users/suspend/apply", {"email": ADMIN, "confirmed": "1"}),
                       ("/users/delete/apply", {"email": ADMIN, "confirmed": "1", "confirm_email": ADMIN})]:
        r = client.post(path, data=data)
        assert "connected as" in r.text, path
    assert gam_writes(gam_calls()) == []


def test_the_suspend_preview_of_someone_else_still_asks_to_confirm(client):  # noqa: F811
    r = client.post("/users/suspend/preview", data={"email": "carol@example.com"})
    assert "Confirm" in r.text and "connected as" not in r.text


def test_the_builder_blocks_it_at_the_preview_and_holds_nothing(client, gam_calls):  # noqa: F811
    for cid in ("build.suspend_user", "build.delete_user", "build.signout_user", "build.reset_password"):
        r = client.post("/builder/preview", data={"cid": cid, "email": ADMIN})
        assert "connected as" in r.text and 'name="preview"' not in r.text, cid
    client.post("/builder/sequence/add", data={"cid": "build.suspend_user", "email": ADMIN})
    r = client.post("/builder/sequence/preview")
    assert "connected as" in r.text and 'name="preview"' not in r.text
    assert gam_writes(gam_calls()) == []


def test_a_preview_passes_an_address_an_earlier_step_may_restore(client):  # noqa: F811
    client.post("/builder/sequence/add", data={"cid": "build.undelete_user", "email": "gone@example.com"})
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "gone@example.com"})
    r = client.post("/builder/sequence/preview")
    assert "connected as" not in r.text and "has no account" not in r.text and 'name="preview"' in r.text


def test_offboarding_with_an_unconfirmed_admin_stops_at_the_preview(client, gam_calls):  # noqa: F811
    # The shape SwiftGamGui's review exploited: an unknown admin must refuse, not skip the check.
    client.app.state.gamgui.vault.set("example.com", "oauth2", "not json")
    r = client.post("/lifecycle/offboard/preview", data={"user": "carol@example.com", "manager": "alice@example.com"})
    assert "confirm which account" in r.text
    assert gam_writes(gam_calls()) == []
