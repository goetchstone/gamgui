from __future__ import annotations

from starlette.datastructures import FormData

from gamgui.core import guard
from gamgui.core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from gamgui.core.gam.commands import GAMCommands


def _preview(target: str, risk: RiskLevel) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=target, summary="x", risk=risk)


def test_empty_no_confirmation():
    d = guard.evaluate([])
    assert d.requires_confirmation is False
    assert d.affected_count == 0


def test_single_low_risk_no_confirmation():
    d = guard.evaluate([_preview("a@e.com", RiskLevel.LOW)])
    assert d.requires_confirmation is False
    assert d.max_risk == RiskLevel.LOW


def test_single_destructive_confirms_without_typing():
    d = guard.evaluate([_preview("a@e.com", RiskLevel.DESTRUCTIVE)])
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is False
    assert d.affected == ["a@e.com"]


def test_bulk_destructive_requires_typed_confirmation():
    previews = [_preview(f"u{i}@e.com", RiskLevel.DESTRUCTIVE) for i in range(12)]
    d = guard.evaluate(previews)
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is True


def test_bulk_low_risk_confirms_but_no_typing():
    previews = [_preview(f"u{i}@e.com", RiskLevel.LOW) for i in range(12)]
    d = guard.evaluate(previews)
    assert d.requires_confirmation is True
    assert d.requires_typed_confirmation is False


def test_over_hard_cap_warns():
    previews = [_preview(f"u{i}@e.com", RiskLevel.DESTRUCTIVE) for i in range(5)]
    d = guard.evaluate(previews, hard_cap=3)
    assert d.over_hard_cap is True
    assert d.warnings


# --- enforce: the server-side half, against the posted form -------------------------------------

def test_enforce_destructive_needs_exactly_confirmed_1():
    one = guard.changes(["a@e.com"], RiskLevel.DESTRUCTIVE, "Suspend")
    assert guard.enforce(one, {}) and guard.enforce(one, {"confirmed": "yes"})
    assert guard.enforce(one, {"confirm": "1"})           # the typed field is not the Confirm click
    assert guard.enforce(one, {"confirmed": "1"}) is None


def test_enforce_bulk_destructive_needs_the_typed_word():
    many = guard.changes([f"u{i}@e.com" for i in range(10)], RiskLevel.DESTRUCTIVE, "Delete")
    assert guard.enforce(many, {"confirmed": "1"})        # a click is not enough at bulk scale
    assert guard.enforce(many, {"confirm": "nope"})
    assert guard.enforce(many, {"confirm": " Confirm "}) is None


def test_enforce_single_low_write_needs_nothing_unless_it_has_a_confirm_step():
    one = guard.changes(["a@e.com"], RiskLevel.LOW, "Set signature")
    assert guard.enforce(one, {}) is None
    assert guard.enforce(one, {}, confirm_step=True)      # a bulk job / routine always previews first
    assert guard.enforce(one, {"confirmed": "1"}, confirm_step=True) is None


def test_enforce_typed_count_only_above_the_opted_in_threshold():
    # A large signature overwrite: the operator types how many people it changes, on top of the click.
    few = guard.changes([f"u{i}@e.com" for i in range(3)], RiskLevel.LOW, "Set signature")
    many = guard.changes([f"u{i}@e.com" for i in range(4)], RiskLevel.LOW, "Set signature")
    assert not guard.evaluate(few, typed_count_above=3).requires_typed_count
    assert guard.evaluate(many, typed_count_above=3).requires_typed_count
    assert not guard.evaluate(many).requires_typed_count                # opt-in only
    assert guard.enforce(few, {"confirmed": "1"}, confirm_step=True, typed_count_above=3) is None
    assert guard.enforce(many, {"confirmed": "1"}, confirm_step=True, typed_count_above=3)
    assert guard.enforce(many, {"confirmed": "1", "confirm_count": "3"}, confirm_step=True,
                         typed_count_above=3)                           # a stale or wrong count
    assert guard.enforce(many, {"confirm_count": "4"}, confirm_step=True, typed_count_above=3)  # and the click
    assert guard.enforce(many, {"confirmed": "1", "confirm_count": " 4 "}, confirm_step=True,
                         typed_count_above=3) is None


def _delete(addr: str) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=addr, summary="Delete account",
                         risk=RiskLevel.DESTRUCTIVE, argv=GAMCommands.delete_user(addr))


def test_enforce_an_account_delete_needs_its_address_typed():
    # The Users delete zone asked for the exact email; the Builder's "Delete account" row action took
    # one Confirm click. The rule lives here now, so every route whose previews delete an account
    # gets it — recognised by the argv GAMCommands.delete_user builds, whatever the route calls it.
    one = [_delete("alice@example.com")]
    assert guard.evaluate(one).typed_emails == ["alice@example.com"]
    assert guard.enforce(one, {"confirmed": "1"})                                   # a click is not enough
    assert guard.enforce(one, {"confirmed": "1", "confirm_email": "carol@example.com"})
    assert guard.enforce(one, {"confirm_email": "alice@example.com"})               # nor the typing alone
    assert guard.enforce(one, {"confirmed": "1", "confirm_email": " Alice@Example.com "}) is None
    assert guard.evaluate([_preview("a@e.com", RiskLevel.DESTRUCTIVE)]).typed_emails == []   # not a delete
    undelete = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target="a@e.com", summary="x",
                             risk=RiskLevel.LOW, argv=GAMCommands.undelete_user("a@e.com"))
    assert guard.evaluate([undelete]).typed_emails == []


def test_enforce_each_deleted_account_is_typed():
    # A Builder sequence can delete several accounts: each address is typed, once.
    seq = [_delete("a@example.com"), _preview("x@e.com", RiskLevel.LOW), _delete("b@example.com"),
           _delete("a@example.com")]
    assert guard.evaluate(seq).typed_emails == ["a@example.com", "b@example.com"]
    assert guard.enforce(seq, FormData([("confirmed", "1"), ("confirm_email", "a@example.com")]))
    assert guard.enforce(seq, FormData([("confirmed", "1"), ("confirm_email", "a@example.com"),
                                        ("confirm_email", "b@example.com")])) is None
