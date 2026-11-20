"""Move one person to another org unit (roadmap 4.2): the tree read live, from → to read again at the preview and held,
an unknown path refused as GAM refuses it, and one audited `update org <ou> move user <email>`."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes
from .test_users_web import client  # noqa: F401

ALICE = "alice@example.com"     # in /Staff


# --- the tree --------------------------------------------------------------------------------------------------------------

async def test_the_tree_is_the_root_then_every_unit(connector):
    assert await connector.org_units() == ["/", "/Sales", "/Sales/West", "/Staff", "/Students"]


async def test_a_tenant_with_no_units_is_the_root_alone(connector, gam_state):
    (gam_state / "no_org_units").write_text("")     # GAM still writes the titles: an answer
    assert await connector.org_units() == ["/"]


async def test_a_refused_read_raises(connector, gam_state):
    (gam_state / "org_units_refused").write_text("")
    with pytest.raises(GAMError):
        await connector.org_units()


async def test_no_answer_at_all_is_incomplete_never_the_root_alone(connector, monkeypatch):
    async def silent(domain, argv, timeout=None, serialize=False):
        return ""

    monkeypatch.setattr(connector.runner, "run_authenticated", silent)
    with pytest.raises(Incomplete):
        await connector.org_units()


async def test_the_mock_refuses_an_unknown_unit_before_anyone_moves(runner, domain):
    with pytest.raises(GAMError) as refused:
        await runner.run_authenticated(domain, GAMCommands.move_user_to_org_unit("/Nope", ALICE))
    assert "Organizational Unit: /Nope, Does not exist" in refused.value.stderr


# --- the person's page --------------------------------------------------------------------------------------------------

def _preview(client, org_unit, email=ALICE):  # noqa: F811
    r = client.post("/users/ou/preview", data={"email": email, "org_unit": org_unit})
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _apply(client, token, org_unit, email=ALICE):  # noqa: F811
    return client.post("/users/ou/apply", data={"email": email, "org_unit": org_unit, "preview": token, "confirmed": "1"})


def test_the_page_offers_it_and_reads_the_tree_only_when_asked(client, gam_calls):  # noqa: F811
    page = client.get("/users/detail", params={"email": ALICE}).text
    assert "Move to another org unit…" in page and GAMCommands.print_org_units() not in gam_calls()
    card = client.get("/users/ou", params={"email": ALICE}).text
    assert '<option value="/Sales">' in card and "/Staff (now)" in card


def test_the_preview_reads_again_says_policy_follows_and_writes_nothing(client, gam_calls):  # noqa: F811
    page, token = _preview(client, "/Sales")
    assert "Move alice@example.com from /Staff to /Sales?" in page and "policies follow the org unit" in page and token
    assert gam_writes(gam_calls()) == []


@pytest.mark.parametrize("org_unit, said", [("/Nope", "isn't an org unit here"), ("/Staff", "is already in /Staff")])
def test_an_unknown_unit_or_their_own_previews_nothing(client, org_unit, said):  # noqa: F811
    page, token = _preview(client, org_unit)
    assert said in page and not token


def test_the_move_runs_once_through_the_chokepoint_and_updates_the_list(client, gam_calls):  # noqa: F811
    _, token = _preview(client, "/Sales")
    r = _apply(client, token, "/Sales")
    assert "Moved alice@example.com from /Staff to /Sales." in r.text
    assert gam_writes(gam_calls()) == [GAMCommands.move_user_to_org_unit("/Sales", ALICE)]
    rec = client.app.state.gamgui.connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("move_org_unit", ALICE, True)
    again = _apply(client, token, "/Sales")
    assert "expired or was already run" in again.text and len(gam_writes(gam_calls())) == 1


def test_a_unit_edited_after_the_preview_moves_nobody(client, gam_calls):  # noqa: F811
    _, token = _preview(client, "/Sales")
    r = _apply(client, token, "/Students")
    assert "changed after the preview" in r.text and gam_writes(gam_calls()) == []


def test_a_tree_that_cant_be_read_offers_nothing(client, gam_state):  # noqa: F811
    (gam_state / "org_units_refused").write_text("")
    assert "Couldn't read the org units" in unescape(client.get("/users/ou", params={"email": ALICE}).text)
    page, token = _preview(client, "/Sales")
    assert "Couldn't read the org units or their account again" in page and not token
