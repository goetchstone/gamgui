"""The offboarding auto-reply's end (roadmap 3.4, plan P1.4): a day, held as that date from the preview to the run. GAM
sends the start of that day (getYYYYMMDD, read from the vendored build), so the first day it can be is tomorrow."""

from __future__ import annotations

from datetime import date, timedelta
from html import unescape

import pytest

from gamgui.core import lifecycle
from gamgui.web.routes.lifecycle import _form_key

from .helpers import gam_writes
from .test_offboarding_added_steps import ALICE, CAROL, FORM, _preview, _run, _token, client, state  # noqa: F401

TODAY = date(2026, 11, 20)


def _vacation(**kw):
    steps = lifecycle.build_offboard_steps(CAROL, ALICE, "s", "m", 30, TODAY, **kw)
    return next(s for s in steps if s.key == "vacation")


def test_no_end_is_the_routine_s_own_argv():
    assert _vacation().commands[0][-4:] == ["start", "Started", "end", "NotSpecified"]
    assert _vacation(autoreply_end="").commands == _vacation().commands


def test_an_end_is_sent_as_that_date_and_said():
    step = _vacation(autoreply_end="2026-12-31")
    assert step.commands[0][-4:] == ["start", "Started", "end", "2026-12-31"] and "until 2026-12-31" in step.summary


@pytest.mark.parametrize("value, said", [
    ("", ""),
    ("2026-11-21", ""),
    ("2026-11-20", "after today"),                      # today's would switch it off as it's set
    ("2026-11-01", "after today"),
    ("2036-11-21", "more than ten years"),
    ("next week", "isn't a date"),
    ("2026-13-01", "isn't a date"),
    ("20261231", "isn't a date"),                       # Python reads it; the form never sends it
])
def test_the_end_must_be_a_day_after_today(value, said):
    problem = lifecycle.autoreply_end_problem(value, TODAY)
    assert (said in problem) if said else problem == ""


def test_the_preview_holds_the_date_and_the_run_sends_it(client, state, gam_calls):  # noqa: F811
    end = (date.today() + timedelta(days=14)).isoformat()
    page = _preview(client, autoreply_end=end)
    assert f"until {end}" in unescape(page)
    _run(client, state, _token(page), autoreply_end=end)
    sent = [a for a in gam_writes(gam_calls()) if a[2:3] == ["vacation"]]
    assert sent and sent[0][-2:] == ["end", end]


def test_an_end_edited_after_the_preview_runs_nothing(client, state, gam_calls):  # noqa: F811
    token = _token(_preview(client, autoreply_end=(date.today() + timedelta(days=14)).isoformat()))
    out = _run(client, state, token, autoreply_end=(date.today() + timedelta(days=15)).isoformat())
    assert "changed after the preview" in unescape(out) and gam_writes(gam_calls()) == []


def test_a_bad_end_previews_nothing_unless_the_auto_reply_is_left_out(client):  # noqa: F811
    today = date.today().isoformat()
    assert "after today" in unescape(_preview(client, autoreply_end=today))
    assert _token(_preview(client, autoreply_end=today, na=["vacation"]))


def test_the_end_is_part_of_the_form_key():
    base = dict(user=CAROL, manager=ALICE, subject="s", message="m", days="30", notify="", done=frozenset())
    assert _form_key(**base, autoreply_end="2026-12-31") != _form_key(**base)
