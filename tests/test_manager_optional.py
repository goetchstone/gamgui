"""Offboarding without a manager (roadmap 3.4, plan P1.3): needed only when a step that runs acts on them."""

from __future__ import annotations

from html import unescape

from gamgui.core import lifecycle
from gamgui.core.gam.models import GAMUser

from .helpers import gam_writes
from .test_offboarding_added_steps import CAROL, FORM, _preview, _run, _token, client, state  # noqa: F401

BOB = "bob@example.com"
NONE = frozenset()
NOT_THE_LOCK = frozenset(lifecycle.OPTIONAL_STEPS)


def test_the_routine_needs_one_for_each_step_that_acts_on_them():
    assert lifecycle.needs_manager(NONE, NONE, NONE, "") == [
        "Set delegate", "Auto-reply", lifecycle.STEP_NAMES["transfer"], "Manager reminder"]


def test_a_step_left_out_done_or_handed_to_the_recipient_doesnt():
    assert lifecycle.needs_manager(NONE, NOT_THE_LOCK, NONE, "") == []
    assert lifecycle.needs_manager(frozenset({"delegate", "vacation"}), frozenset({"reminder"}), NONE, BOB) == []
    assert lifecycle.needs_manager(NONE, frozenset({"delegate", "vacation", "reminder"}), NONE, "") == [lifecycle.STEP_NAMES["transfer"]]
    assert lifecycle.needs_manager(NONE, NOT_THE_LOCK, frozenset({"secondary_calendars"}), "") == [
        lifecycle.STEP_NAMES["secondary_calendars"]]
    assert lifecycle.needs_manager(NONE, NOT_THE_LOCK, frozenset({"secondary_calendars"}), BOB) == []


def test_no_manager_is_looked_up_when_none_is_given():
    check = lifecycle.check_addresses([GAMUser(primary_email=CAROL)], CAROL, "  ")
    assert check.errors == [] and check.manager is None and check.user.primary_email == CAROL


def test_the_lock_alone_runs_without_a_manager(client, state, gam_calls):  # noqa: F811
    form = {"manager": "", "na": list(lifecycle.OPTIONAL_STEPS)}
    page = _preview(client, **form)
    assert _token(page) and "for the manager" in unescape(page)
    _run(client, state, _token(page), **form)
    writes = gam_writes(gam_calls())
    assert len(writes) == 3 and all(CAROL in w for w in writes)     # the reset, the sign-out, forwarding off


def test_a_transfer_to_the_recipient_needs_no_manager(client, state, gam_calls):  # noqa: F811
    form = {"manager": "", "recipient": BOB, "na": ["delegate", "vacation", "reminder", "calacls"]}
    page = _preview(client, **form)
    _run(client, state, _token(page), **form)
    assert ["create", "datatransfer", CAROL, "drive,calendar", BOB, "all"] in gam_writes(gam_calls())


def test_a_step_that_needs_one_is_named_and_nothing_previews(client):  # noqa: F811
    page = unescape(_preview(client, manager="", na=["vacation", "reminder", "calacls"]))
    assert "Enter the manager: “Set delegate”, “Transfer Drive & Calendar” need one" in page and not _token(page)
