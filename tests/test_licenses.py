"""One person's license seats on their page (roadmap 4.3): read from every seat in the domain, a change previewed from a
fresh read and held, a removal DESTRUCTIVE (address typed) and refused while a data transfer is unfinished or unknown, and
each write one audited `gam user <email> add|update|delete license nv:<productId>:<skuId>`. The mock refuses what Google
refuses, so a refusal reaches the screen and is never "done"."""

from __future__ import annotations

import re
from html import unescape

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gamgui.core import licenses
from gamgui.core.gam.commands import GAMCommands, license_sku
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.models import GAMUser, LicenseSeat
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes
from .test_users_web import FIXTURES, client  # noqa: F401  (the fixture)

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"
STANDARD = LicenseSeat(ALICE, "Google-Apps", "1010020028", "Google Workspace", "Google Workspace Business Standard")
VFE = ("Google-Vault", "Google-Vault-Former-Employee")
PLUS_ROW = "carol@example.com,Google-Apps,Google Workspace,1010020025,Google Workspace Business Plus\n"


# --- argv ------------------------------------------------------------------------------------------------------------------

def test_each_write_names_the_seat_by_its_ids_as_one_element():
    assert GAMCommands.add_license(ALICE, *VFE) == ["user", ALICE, "add", "license", "nv:Google-Vault:Google-Vault-Former-Employee"]
    assert GAMCommands.swap_license(ALICE, "Google-Apps", "1010020028", "1010020025") == [
        "user", ALICE, "update", "license", "nv:Google-Apps:1010020025", "from", "nv:Google-Apps:1010020028"]
    assert GAMCommands.remove_license(ALICE, "Google-Apps", "1010020028") == [
        "user", ALICE, "delete", "license", "nv:Google-Apps:1010020028"]


@pytest.mark.parametrize("product, sku", [("", "S"), ("P", ""), ("a:b", "S"), ("a,b", "S"), ("P", "S,T"), (" P", "S"),
                                          ("P", "S ")])
def test_an_id_gam_would_split_or_strip_is_refused(product, sku):
    # getProductAndSKU splits `nv:` at its first `:`, the add/delete list splits at `,`, getGoogleSKU strips: any of those
    # would name another seat than the one read.
    with pytest.raises(ValueError):
        license_sku(product, sku)


def test_a_sku_may_hold_a_colon_since_only_the_first_splits():
    assert license_sku("nv-product", "a:b") == "nv:nv-product:a:b"


def _gam_reads(token: str):
    # getProductAndSKU (glskus.py at the 7.52.04 tag) for an `nv:` token, after getGoogleSKU's strip and the list's `,` split.
    (one,) = token.strip().split(",")
    rest = one[3:]
    return tuple(rest.split(":", 1)) if ":" in rest else (None, one)


@given(st.text(min_size=1), st.text(min_size=1))
def test_gam_reads_back_exactly_the_ids_given_or_the_ids_are_refused(product, sku):
    try:
        token = license_sku(product, sku)
    except ValueError:
        return
    assert token.lower().startswith("nv:") and _gam_reads(token) == (product, sku)


# --- the read --------------------------------------------------------------------------------------------------------------

async def test_the_seats_are_read_with_their_ids_and_names(connector):
    seats = await connector.license_seats()
    assert STANDARD in seats and len(seats) == 5
    assert LicenseSeat(BOB, *VFE, "Google Vault", "Google Vault - Former Employee") in seats


@pytest.mark.parametrize("out", ["", "A notice GAM printed\n", "user,sku\nalice@example.com,1\n"])
async def test_output_without_the_titles_is_incomplete_never_no_seats(connector, monkeypatch, out):
    async def no_titles(domain, argv, timeout=None, serialize=False):
        return out

    monkeypatch.setattr(connector.runner, "run_authenticated", no_titles)
    with pytest.raises(Incomplete):
        await connector.license_seats()


async def test_a_seat_without_its_ids_is_incomplete(connector, monkeypatch):
    async def no_sku(domain, argv, timeout=None, serialize=False):
        return "userId,productId,productDisplay,skuId,skuDisplay\nalice@example.com,Google-Apps,Google Workspace,,\n"

    monkeypatch.setattr(connector.runner, "run_authenticated", no_sku)
    with pytest.raises(Incomplete):
        await connector.license_seats()


async def test_a_token_without_the_licensing_scope_raises(connector, gam_state):
    (gam_state / "nolicensing").write_text("")
    with pytest.raises(GAMError):
        await connector.license_seats()


# --- the choices -----------------------------------------------------------------------------------------------------------

def _seats(*rows):
    return [LicenseSeat(u, p, s, p, s) for u, p, s in rows]


def test_what_they_hold_and_what_can_be_added_come_from_the_read():
    seats = _seats((ALICE, "Google-Apps", "A"), (BOB, "Google-Apps", "B"), (BOB, "Google-Vault", "V"))
    assert [s.sku_id for s in licenses.held_by(seats, " Alice@Example.com ")] == ["A"]
    alice = licenses.choices(seats, ALICE)
    assert alice.held == [(seats[0], [seats[1]])]                 # B is a change of A: the same product
    assert alice.adds == [seats[2]]                               # one SKU per product: B isn't an add for her
    assert licenses.choices(seats, BOB).adds == [] and licenses.choices(seats, CAROL).adds == seats


def test_a_transfer_unfinished_or_unknown_refuses_a_removal_and_a_failed_one_is_named():
    assert licenses.transfer_refusal(ALICE, []) == ""
    failed = [{"application": "Drive and Docs", "status": "failed"}]
    assert licenses.transfer_refusal(ALICE, failed) == "" and licenses.failed_transfers(failed) == ["Drive and Docs"]
    assert "hasn't finished (calendar (new))" in licenses.transfer_refusal(ALICE, [*failed, {"application": "calendar",
                                                                                            "status": "new"}])
    assert "hasn't finished (drive (inProgress))" in licenses.transfer_refusal(ALICE, [{"application": "drive",
                                                                                       "status": "inProgress"}])
    assert "Couldn't check whether a data transfer" in licenses.transfer_refusal(ALICE, None)


# --- the page --------------------------------------------------------------------------------------------------------------

def _preview(client, email=ALICE, **form):  # noqa: F811
    r = client.post("/users/licenses/preview", data={"email": email, **form})
    token = re.search(r'name="preview" value="([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _apply(client, token, email=ALICE, **form):  # noqa: F811
    return client.post("/users/licenses/apply", data={"email": email, "preview": token, "confirmed": "1", **form})


def test_the_page_offers_it_and_reads_the_seats_only_when_asked(client, gam_calls):  # noqa: F811
    page = client.get("/users/detail", params={"email": ALICE}).text
    assert "Show their licenses" in page and GAMCommands.print_licenses() not in gam_calls()
    card = unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    assert "Google Workspace Business Standard" in card and "Remove…" in card
    assert '<option value="Google-Vault:Google-Vault-Former-Employee">' in card   # a license the domain holds
    assert "a license missing here may only be unread" in card


def test_a_card_for_someone_holding_none_found_says_so_not_none(client):  # noqa: F811
    card = unescape(client.get("/users/licenses", params={"email": "nobody@example.com"}).text)
    assert "The seats read list no license for nobody@example.com." in card and "may only be unread" in card


def test_seats_that_cant_be_read_offer_nothing(client, gam_state, gam_calls):  # noqa: F811
    (gam_state / "nolicensing").write_text("")
    assert "Couldn't read the license seats" in unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    page, token = _preview(client, action="add", seat="Google-Vault:Google-Vault-Former-Employee")
    assert "Couldn't read the license seats or their account again" in page and not token
    assert gam_writes(gam_calls()) == []


def test_an_add_runs_once_from_its_preview_and_the_seats_then_list_it(client, gam_state, gam_calls):  # noqa: F811
    page, token = _preview(client, action="add", seat="Google-Vault:Google-Vault-Former-Employee")
    assert "Add Google Vault - Former Employee (Google Vault) to alice@example.com?" in page and token
    assert gam_writes(gam_calls()) == []
    r = _apply(client, token, action="add", product_id=VFE[0], sku_id=VFE[1])
    assert "Added Google Vault - Former Employee to alice@example.com." in r.text
    assert gam_writes(gam_calls()) == [GAMCommands.add_license(ALICE, *VFE)]
    rec = client.app.state.gamgui.connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("add_license", ALICE, True)
    again = _apply(client, token, action="add", product_id=VFE[0], sku_id=VFE[1])
    assert "expired or was already run" in again.text and len(gam_writes(gam_calls())) == 1
    card = unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    assert card.count("Remove…") == 2


@pytest.mark.parametrize("form, said", [
    ({"action": "add", "seat": "Google-Apps:1010020028"}, "alice@example.com already holds Google Workspace Business Standard."),
    ({"action": "add", "seat": "Google-Apps:1010020025"}, "already holds a license of that product: change it instead"),
    ({"action": "add", "seat": "Nope:1"}, "That isn't a license anyone in this domain holds"),
    ({"action": "remove", "product_id": VFE[0], "sku_id": VFE[1]}, "don't list that license for alice@example.com"),
    ({"action": "swap", "product_id": "Google-Apps", "sku_id": "1010020028", "new_sku_id": "1010020025"},
     "can only change to another Google Workspace license"),
    ({"action": "swap", "product_id": "Google-Apps", "sku_id": "1010020028", "new_sku_id": VFE[1]},
     "can only change to another Google Workspace license"),
    ({"action": "sync", "seat": "Google-Apps:1010020028"}, "Pick add, change or remove."),
    ({"action": "add", "seat": ""}, "Pick a license first."),
])
def test_what_the_read_doesnt_support_previews_nothing(client, gam_calls, form, said):  # noqa: F811
    page, token = _preview(client, **form)
    assert said in page and not token and gam_writes(gam_calls()) == []


def test_a_swap_stays_within_the_product_and_runs_as_one_update(client, gam_state, gam_calls):  # noqa: F811
    # The domain also has Business Plus (carol's seat), seeded into the seats the mock keeps.
    (gam_state / "licenses.csv").write_text((FIXTURES / "licenses.csv").read_text() + PLUS_ROW)
    card = unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    assert '<option value="1010020025">Google Workspace Business Plus</option>' in card
    form = {"action": "swap", "product_id": "Google-Apps", "sku_id": "1010020028", "new_sku_id": "1010020025"}
    page, token = _preview(client, **form)
    assert "Change alice@example.com's Google Workspace Business Standard to Google Workspace Business Plus?" in page
    r = _apply(client, token, **form)
    assert "Changed alice@example.com's Google Workspace Business Standard to Google Workspace Business Plus." in unescape(r.text)
    assert gam_writes(gam_calls()) == [GAMCommands.swap_license(ALICE, "Google-Apps", "1010020028", "1010020025")]
    assert client.app.state.gamgui.connector.audit.tail()[-1]["action"] == "swap_license"
    card = unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    assert "Google Workspace Business Plus" in card and "Business Standard <span" not in card


# --- a removal ---------------------------------------------------------------------------------------------------------------

REMOVE = {"action": "remove", "product_id": "Google-Apps", "sku_id": "1010020028"}


def test_a_removal_is_destructive_says_google_s_words_and_needs_the_address_typed(client, gam_state, gam_calls):  # noqa: F811
    page, token = _preview(client, **REMOVE)
    assert "DESTRUCTIVE." in page and licenses.MIGHT_LOSE_DATA in page and licenses.SOURCE in page and token
    untyped = unescape(_apply(client, token, **REMOVE).text)
    assert "Type the exact email address to confirm." in untyped and gam_writes(gam_calls()) == []
    fresh = re.search(r'name="preview" value="([^"]+)"', untyped).group(1)   # the same step again, a fresh token
    r = _apply(client, fresh, confirm_email=" Alice@Example.com ", **REMOVE)
    assert "Removed Google Workspace Business Standard from alice@example.com." in r.text and "24 hours" in r.text
    assert "Show their licenses again" in r.text
    assert gam_writes(gam_calls()) == [GAMCommands.remove_license(ALICE, "Google-Apps", "1010020028")]
    rec = client.app.state.gamgui.connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("remove_license", ALICE, True)
    card = unescape(client.get("/users/licenses", params={"email": ALICE}).text)
    assert "The seats read list no license for alice@example.com." in card


def _transfers(monkeypatch, client, answers):  # noqa: F811
    calls = iter(answers)

    async def incomplete(email):
        return next(calls)

    monkeypatch.setattr(client.app.state.gamgui.connector, "incomplete_transfers_for", incomplete)


@pytest.mark.parametrize("answer, said", [([{"application": "Drive and Docs", "status": "inProgress"}], "hasn't finished"),
                                          (None, "Couldn't check whether a data transfer")])
def test_a_removal_is_refused_while_a_transfer_is_unfinished_or_unknown(client, monkeypatch, gam_calls, answer, said):  # noqa: F811
    _transfers(monkeypatch, client, [answer])
    page, token = _preview(client, **REMOVE)
    assert said in page and not token and gam_writes(gam_calls()) == []


def test_a_failed_transfer_is_named_on_the_confirm_step_not_a_refusal(client, monkeypatch, gam_state, gam_calls):  # noqa: F811
    _transfers(monkeypatch, client, [[{"application": "Drive and Docs", "status": "failed"}], []])
    page, token = _preview(client, **REMOVE)
    assert "A data transfer from them <strong>failed</strong> (Drive and Docs)" in " ".join(page.split()) and token
    r = _apply(client, token, confirm_email=ALICE, **REMOVE)
    assert "Removed Google Workspace Business Standard" in r.text
    assert gam_writes(gam_calls()) == [GAMCommands.remove_license(ALICE, "Google-Apps", "1010020028")]


def test_a_transfer_started_after_the_preview_refuses_the_click(client, monkeypatch, gam_calls):  # noqa: F811
    _transfers(monkeypatch, client, [[], [{"application": "Drive and Docs", "status": "inProgress"}]])
    _, token = _preview(client, **REMOVE)
    r = unescape(_apply(client, token, confirm_email=ALICE, **REMOVE).text)
    assert "hasn't finished" in r and gam_writes(gam_calls()) == []


def test_a_change_edited_after_the_preview_runs_nothing(client, gam_calls):  # noqa: F811
    _, token = _preview(client, **REMOVE)
    r = _apply(client, token, confirm_email=ALICE, **{**REMOVE, "sku_id": "Google-Vault-Former-Employee"})
    assert "changed after the preview" in r.text and gam_writes(gam_calls()) == []


# --- what Google refuses ----------------------------------------------------------------------------------------------------

def test_no_seat_left_reaches_the_screen_and_is_not_done(client, gam_state):  # noqa: F811
    (gam_state / "no_seats").write_text("")
    _, token = _preview(client, action="add", seat="Google-Vault:Google-Vault-Former-Employee")
    r = unescape(_apply(client, token, action="add", product_id=VFE[0], sku_id=VFE[1]).text)
    assert "Couldn't add Google Vault - Former Employee to alice@example.com." in r and "Added" not in r
    assert "There aren't enough available licenses" in r
    assert client.app.state.gamgui.connector.audit.tail()[-1]["ok"] is False


@pytest.mark.parametrize("argv, said, code", [
    (GAMCommands.remove_license(ALICE, *VFE), "Delete Failed: User does not have a license", 50),
    (GAMCommands.add_license(ALICE, "Google-Apps", "1010020028"), "Add Failed: User already has a license", 50),
    (GAMCommands.add_license(ALICE, "Google-Apps", "1010020027"), "Add Failed: Invalid sku", 50),
    (GAMCommands.swap_license(ALICE, "Google-Apps", "1010020028", VFE[1]), "Update Failed: Invalid sku", 50),
    (GAMCommands.swap_license(ALICE, *VFE, "x"), "Update Failed: User does not have a license", 50),
    (GAMCommands.add_license("nobody@example.com", *VFE), "User: nobody@example.com, Does not exist", 56),
])
async def test_the_mock_refuses_what_google_and_gam_refuse(runner, domain, argv, said, code):
    with pytest.raises(GAMError) as refused:
        await runner.run_authenticated(domain, argv)
    assert said in refused.value.stderr and refused.value.exit_code == code


@pytest.mark.hand_built_argv
@pytest.mark.parametrize("argv, said", [
    (["user", ALICE, "delete", "license", "nv:Google-Apps:1010020028", "now"], "Unknown argument: now"),
    (["user", ALICE, "add", "license", "nv::1010020028"], "Invalid choice"),
])
async def test_the_mock_refuses_a_line_gam_refuses(runner, domain, argv, said):
    with pytest.raises(GAMError) as refused:
        await runner.run_authenticated(domain, argv)
    assert said in refused.value.stderr and refused.value.exit_code == 2


def test_a_seat_gam_cant_be_given_is_refused_at_the_preview_not_at_the_click(client, monkeypatch, gam_calls):  # noqa: F811
    # A quoted CSV cell can carry a `,` that GAM's SKU list would split on: nothing is held, so the click can't 500.
    odd = LicenseSeat(ALICE, "Google-Apps", "Odd,Sku", "Google Workspace", "Odd")

    async def seats():
        return [odd]

    monkeypatch.setattr(client.app.state.gamgui.connector, "license_seats", seats)
    page, token = _preview(client, action="remove", product_id="Google-Apps", sku_id="Odd,Sku")
    assert "GAM can't be given this license's IDs" in page and not token and gam_writes(gam_calls()) == []


def test_an_alias_changes_and_audits_the_account_it_belongs_to(client, monkeypatch, gam_calls):  # noqa: F811
    async def get_user(email, fields=None):
        return GAMUser.from_json({"primaryEmail": ALICE})

    monkeypatch.setattr(client.app.state.gamgui.connector, "get_user", get_user)
    page, token = _preview(client, email="a.anders@example.com", action="add", seat="Google-Vault:Google-Vault-Former-Employee")
    assert f"to {ALICE}?" in page
    _apply(client, token, action="add", product_id=VFE[0], sku_id=VFE[1])
    assert gam_writes(gam_calls()) == [GAMCommands.add_license(ALICE, *VFE)]
    assert client.app.state.gamgui.connector.audit.tail()[-1]["target"] == ALICE
