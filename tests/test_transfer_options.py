"""Offboarding's transfer options (roadmap 3.4, plan P1.3 and P1.5): a recipient other than the manager, and what the
transfer moves. The default routine sends the same argv, byte for byte; GAM refuses the privacy level without Drive."""

from __future__ import annotations

from datetime import date
from html import unescape

import pytest

from gamgui.core import lifecycle
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser
from gamgui.web.routes.lifecycle import _form_key

from .helpers import gam_writes
from .test_offboarding_added_steps import ALICE, CAROL, FORM, _preview, _run, _token, client, state  # noqa: F401

BOB = "bob@example.com"
TODAY = date(2026, 11, 19)


def _transfer(**kw):
    steps = lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, TODAY, **kw)
    return next(s for s in steps if s.key == "transfer")


# --- the step -----------------------------------------------------------------------------------------------------------

def test_the_default_transfer_is_the_routine_s_own_byte_for_byte():
    assert _transfer().commands == [["create", "datatransfer", CAROL, "drive,calendar", ALICE, "all"]]
    assert _transfer(recipient=ALICE, services={"calendar", "drive"}).commands == _transfer().commands
    assert _transfer().label == "Transfer Drive & Calendar ownership"


@pytest.mark.parametrize("services, sent", [
    ({"drive"}, ["drive", BOB, "all"]),
    ({"calendar"}, ["calendar", BOB]),                                  # the privacy level is Drive's: never without it
    ({"lookerstudio", "calendar", "drive"}, ["drive,calendar,lookerstudio", BOB, "all"]),   # always the same order
])
def test_the_recipient_and_services_are_what_the_transfer_sends(services, sent):
    step = _transfer(recipient=BOB, services=services)
    assert step.commands == [["create", "datatransfer", CAROL, *sent]] and BOB in step.summary


def test_the_secondary_calendars_and_the_reminder_follow_the_recipient():
    cal = [lifecycle.OwnedCalendar("c_1@group.calendar.google.com", "Team")]
    steps = lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, TODAY, recipient=BOB, services={"drive"},
                                           added=frozenset({"secondary_calendars"}), calendars=cal)
    hand_over = next(s for s in steps if s.key == "secondary_calendars")
    assert hand_over.commands == [GAMCommands.transfer_calendar_ownership(["c_1@group.calendar.google.com"], BOB)]
    reminder = next(s for s in steps if s.key == "reminder")
    assert "Drive transferred to bob@example.com" in str(reminder.commands) and reminder.commands[0][1] == ALICE


def test_a_transfer_that_moves_nothing_is_refused():
    with pytest.raises(ValueError, match="at least one service"):
        _transfer(services=set())


# --- the recipient against the directory ----------------------------------------------------------------------------

def _directory():
    return [GAMUser(primary_email=CAROL), GAMUser(primary_email=ALICE),
            GAMUser(primary_email=BOB, suspended=True, aliases=["robert@example.com"])]


@pytest.mark.parametrize("recipient, said", [
    ("nobody@example.com", "nobody@example.com isn't in the directory — check the transfer recipient's address"),
    ("robert@example.com", "robert@example.com is an alias of bob@example.com — enter the primary address as the transfer recipient"),
    (CAROL, "The departing user and the transfer recipient are the same account"),
])
def test_the_recipient_is_checked_as_the_manager_is(recipient, said):
    check = lifecycle.check_addresses(_directory(), CAROL, ALICE, recipient=recipient)
    assert any(said in e for e in check.errors), check.errors


def test_a_suspended_recipient_is_a_warning_and_the_manager_needs_no_second_check():
    check = lifecycle.check_addresses(_directory(), CAROL, ALICE, recipient=BOB)
    assert not check.errors and check.recipient.primary_email == BOB and any("recipient" in w for w in check.warnings)
    assert lifecycle.check_addresses(_directory(), CAROL, ALICE, recipient=ALICE.upper()).recipient is None


# --- through the page and the mock ------------------------------------------------------------------------------------

def test_the_preview_and_run_send_the_transfer_to_the_recipient(client, state, gam_calls):  # noqa: F811
    page = _preview(client, recipient=BOB, services_shown="1", service=["drive"])
    assert "Transfer carol@example.com's Drive to bob@example.com" in unescape(page)
    _run(client, state, _token(page), recipient=BOB, services_shown="1", service=["drive"])
    assert ["create", "datatransfer", CAROL, "drive", BOB, "all"] in gam_writes(gam_calls())


def test_a_recipient_edited_after_the_preview_runs_nothing(client, state, gam_calls):  # noqa: F811
    token = _token(_preview(client, recipient=BOB))
    out = _run(client, state, token, recipient=ALICE)
    assert "changed after the preview" in unescape(out) and gam_writes(gam_calls()) == []


def test_no_service_ticked_is_refused_unless_the_transfer_is_left_out(client):  # noqa: F811
    assert "The transfer moves nothing" in unescape(_preview(client, services_shown="1"))
    assert _token(_preview(client, services_shown="1", na=["transfer"]))


def test_an_unknown_recipient_previews_nothing(client):  # noqa: F811
    page = unescape(_preview(client, recipient="nobody@example.com"))
    assert "isn't in the directory — check the transfer recipient's address" in page and not _token(page)


# --- P1.5: every option is part of what the preview held -----------------------------------------------------------------

_BASE = dict(user=CAROL, manager=ALICE, subject="s", message="m", days="30", notify="", done=frozenset(),
             not_applicable=frozenset(), added=frozenset(), recipient="", services=lifecycle.DEFAULT_SERVICES)


@pytest.mark.parametrize("change", [
    {"user": BOB}, {"manager": BOB}, {"subject": "S2"}, {"message": "M2"}, {"days": "7"}, {"notify": "it@example.com"},
    {"done": frozenset({"password"})}, {"not_applicable": frozenset({"calacls"})}, {"added": frozenset({"imap_off"})},
    {"recipient": BOB}, {"services": frozenset({"drive"})},
], ids=lambda c: next(iter(c)))
def test_changing_any_option_changes_the_form_key(change):
    assert _form_key(**{**_BASE, **change}) != _form_key(**_BASE)
