"""The transfer's `release_resources` (roadmap 3.4, plan P1.3): a Calendar parameter. GAM refuses a transfer parameter no
listed service takes (_assignAppParameter, a usage error, read from the vendored build); the mock refuses it the same way."""

from __future__ import annotations

from datetime import date
from html import unescape

import pytest

from gamgui.core import lifecycle
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError
from gamgui.core.offboard_templates import OffboardTemplate
from gamgui.web.routes.lifecycle import _form_key

from .helpers import gam_writes
from .test_offboarding_added_steps import ALICE, CAROL, FORM, _preview, _run, _token, client, state  # noqa: F401

TODAY = date(2026, 11, 20)


async def test_the_mock_refuses_it_without_calendar_as_gam_does(runner, domain):
    with pytest.raises(GAMError) as refused:
        await runner.run_authenticated(domain, GAMCommands.create_datatransfer(CAROL, "drive", ALICE, "all", release_resources=True))
    assert "No data transfer application for key RELEASE_RESOURCES" in refused.value.stderr
    out = await runner.run_authenticated(domain, GAMCommands.create_datatransfer(CAROL, "calendar", ALICE, release_resources=True))
    assert "Transfer Request Performed" in out


def test_off_by_default_and_last_on_the_line_when_on():
    steps = lambda **kw: lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, TODAY, **kw)  # noqa: E731
    default = next(s for s in steps() if s.key == "transfer").commands[0]
    assert "release_resources" not in default
    on = next(s for s in steps(release_resources=True) if s.key == "transfer")
    assert on.commands[0] == default + ["release_resources"] and "rooms and resources" in on.summary


def test_without_calendar_it_is_refused_before_gam():
    with pytest.raises(ValueError, match="Calendar transfer"):
        lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, TODAY, services={"drive"}, release_resources=True)


def test_the_page_sends_it_and_refuses_it_without_calendar(client, state, gam_calls):  # noqa: F811
    assert "Calendar transfer: tick Calendar too" in unescape(
        _preview(client, release_resources="1", services_shown="1", service=["drive"]))
    page = _preview(client, release_resources="1")
    _run(client, state, _token(page), release_resources="1")
    assert ["create", "datatransfer", CAROL, "drive,calendar", ALICE, "all", "release_resources"] in gam_writes(gam_calls())


def test_it_is_part_of_the_form_key_and_of_a_template():
    base = dict(user=CAROL, manager=ALICE, subject="s", message="m", days="30", notify="", done=frozenset())
    assert _form_key(**base, release=True) != _form_key(**base)
    template = OffboardTemplate(release_resources=True)
    assert OffboardTemplate.from_json(template.as_json()) == template
    assert OffboardTemplate.from_json({k: v for k, v in template.as_json().items() if k != "release_resources"}).release_resources is False
    assert "Calendar" in OffboardTemplate(release_resources=True, services=frozenset({"drive"})).problem
