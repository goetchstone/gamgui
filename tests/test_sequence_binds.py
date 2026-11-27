"""Step-to-step dataflow in a Builder sequence (roadmap 5.2): `=stepN.Column` in a later step's slot.

Only a read binds; the read runs at the preview, and the steps it fills in, one per distinct row and each built by the
command's own builder, are what the preview shows, holds and runs. A read that fell short blocks it (invariant 10).
"""

from __future__ import annotations

import html
import re

import pytest

from gamgui.core import sequence_binds as sb
from gamgui.core.gam.commands import GAMCommands

from .helpers import gam_writes, wait_for_job
from .test_builder import client  # noqa: F401

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


# --- the pure part ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,bind", [
    ("=step1.primaryEmail", (1, "primaryEmail")), (" =step12.name.fullName ", (12, "name.fullName")),
    ("=step1.", None), ("step1.primaryEmail", None), ("=stepx.a", None), ("a@example.com", None), ("=)", None),
])
def test_only_the_bind_syntax_binds(value, bind):
    assert sb.parse_bind(value) == bind


def test_values_are_distinct_rows_first_seen_first():
    rows = [{"e": "b@x"}, {"e": "a@x"}, {"e": "b@x"}]
    assert sb.values_from(rows, ["e"], 1, 2) == ([("b@x",), ("a@x",)], "")


@pytest.mark.parametrize("rows,needle", [
    (None, "didn't print a table"),
    ([{"e": "a@x"}, {"f": "b@x"}], "a row with no e"),
    ([{"e": "a@x"}, {"e": "  "}], "a row with no e"),
    ([{"e": ["a@x", "b@x"]}], "a row with no e"),        # a list isn't one value
])
def test_a_read_that_fell_short_is_unknown_never_none(rows, needle):
    values, why = sb.values_from(rows, ["e"], 1, 2)
    assert values == [] and needle in why and "Nothing will run" in why


def test_an_empty_table_is_none_said_aloud():
    assert sb.values_from([], ["e"], 1, 2) == ([], "")


# --- the screen ------------------------------------------------------------------------------------------------------

def _queue(client, cid, **slots):  # noqa: F811
    r = client.post("/builder/sequence/add", data={"cid": cid, **slots})
    assert r.status_code == 200
    return r.text


def _preview(client):  # noqa: F811
    page = html.unescape(client.post("/builder/sequence/preview").text)
    m = re.search(r'name="preview" value="([^"]*)"', page)
    return page, (m.group(1) if m else "")


def test_a_write_runs_once_for_each_row_of_an_earlier_read(client, gam_calls):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    panel = _queue(client, "build.signout_user", email="=step1.primaryEmail")
    assert "each primaryEmail from step 1" in panel
    page, token = _preview(client)
    assert token, page
    assert f"gam user {ALICE} signout" in page and f"gam user {CAROL} signout" in page   # shown before it runs
    held = client.app.state.gamgui.previews.peek("builder_sequence", token)
    # the mock's directory read answers every fixture user (it doesn't filter on the query)
    assert [s["argv"] for s in held[1:]] == [GAMCommands.signout_user(u) for u in (ALICE, BOB, CAROL)]
    run = client.post("/builder/sequence/run", data={"preview": token})
    job = client.app.state.gamgui.jobs[re.search(r"status\?job=([A-Za-z0-9_\-]+)", run.text).group(1)]
    wait_for_job(client, job)
    assert gam_writes(gam_calls()) == [GAMCommands.signout_user(u) for u in (ALICE, BOB, CAROL)]
    assert (job.total, job.applied) == (4, 4)


def test_only_a_read_can_feed_a_later_step(client):  # noqa: F811
    _queue(client, "build.signout_user", email=ALICE)
    r = client.post("/builder/sequence/add", data={"cid": "build.signout_user", "email": "=step1.primaryEmail"})
    assert "only a read's result" in html.unescape(r.text) and len(client.app.state.gamgui.builder_sequence) == 1


@pytest.mark.parametrize("value,needle", [("=step2.primaryEmail", "no step 2 before"),
                                          ("=step0.primaryEmail", "no step 0 before")])
def test_a_step_that_isn_t_before_it_is_refused(client, value, needle):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    r = client.post("/builder/sequence/add", data={"cid": "build.signout_user", "email": value})
    assert needle in r.text


def test_moving_the_read_after_its_step_blocks_the_preview(client, gam_calls):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")
    client.post("/builder/sequence/move", data={"index": 0, "to": 1})
    page, token = _preview(client)
    assert token == "" and "isn't before it any more" in page and gam_writes(gam_calls()) == []


def test_a_column_the_read_lacks_blocks_the_preview(client):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.managerEmail")
    page, token = _preview(client)
    assert token == "" and "a row with no managerEmail" in page and "Its columns:" in page


def test_a_read_that_fails_blocks_the_preview(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")

    async def broken(*a, **k):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", broken)
    page, token = _preview(client)
    assert token == "" and "Step 1's read failed" in page and "Input/output" not in page


def test_a_value_the_builder_refuses_blocks_the_preview(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.delete_group", group="=step1.primaryEmail")

    async def odd(*a, **k):
        return 'primaryEmail,name\n"a@example.com, b@example.com",A\n'   # two addresses in one cell: delete_group's check refuses
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", odd)
    page, token = _preview(client)
    assert token == "" and "can't take" in page


def test_an_empty_read_runs_the_step_for_no_one_and_says_so(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")

    async def nobody(*a, **k):
        return "primaryEmail,name\n"
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", nobody)
    page, token = _preview(client)
    assert token and "Step 1 found nothing, so step 2 runs for no one." in page


def test_the_fan_out_counts_for_the_guard(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.suspend_user", email="=step1.primaryEmail")

    async def many(*a, **k):
        return "primaryEmail,name\n" + "".join(f"u{i}@example.com,U\n" for i in range(12))
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", many)
    page, token = _preview(client)
    assert token and 'placeholder="type: confirm"' in page            # ten-plus destructive steps: the typed word
    r = client.post("/builder/sequence/run", data={"preview": token, "confirmed": "1"})
    assert "status?job=" not in r.text                                 # the click alone isn't enough


def test_too_many_steps_once_filled_in_is_refused(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")

    async def huge(*a, **k):
        return "primaryEmail,name\n" + "".join(f"u{i}@example.com,U\n" for i in range(sb.MAX_RESOLVED_STEPS + 1))
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", huge)
    page, token = _preview(client)
    assert token == "" and "use a CSV on Users" in page


def test_the_export_writes_the_filled_in_steps(client):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")
    page, token = _preview(client)
    body = client.get(f"/builder/sequence/export.txt?preview={token}").text
    assert f"gam user {ALICE} signout" in body and "=step1" not in body


def test_a_long_fan_out_keeps_the_poll_bounded(client, monkeypatch):  # noqa: F811
    # Review finding: the run's feed was sized to every filled-in step, so each 1s poll grew with the read (invariant 9).
    from gamgui.web.jobs import RECENT_WINDOW
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")

    async def many(*a, **k):
        return "primaryEmail,name\n" + "".join(f"u{i}@example.com,U\n" for i in range(60))
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", many)
    page, token = _preview(client)
    assert 'placeholder="type: 61"' in page          # the guard's count of targets: 60 people and the read's step
    refused = client.post("/builder/sequence/run", data={"preview": token, "confirmed": "1"})
    assert "status?job=" not in refused.text and "type 61" in html.unescape(refused.text)
    _, token = _preview(client)
    run = client.post("/builder/sequence/run", data={"preview": token, "confirmed": "1", "confirm_count": "61"})
    job = client.app.state.gamgui.jobs[re.search(r"status\?job=([A-Za-z0-9_\-]+)", run.text).group(1)]
    wait_for_job(client, job)
    assert job.total == 61 and job.window == RECENT_WINDOW and len(job.recent) <= RECENT_WINDOW


def test_steps_after_a_fan_out_count_toward_the_cap(client, monkeypatch):  # noqa: F811
    _queue(client, "build.find_users", query="isSuspended=false")
    _queue(client, "build.signout_user", email="=step1.primaryEmail")
    _queue(client, "build.signout_user", email=ALICE)

    async def full(*a, **k):   # 1 read + 499 filled in = 500, then one more
        return "primaryEmail,name\n" + "".join(f"u{i}@example.com,U\n" for i in range(sb.MAX_RESOLVED_STEPS - 1))
    monkeypatch.setattr(client.app.state.gamgui.connector, "catalog_read", full)
    page, token = _preview(client)
    assert token == "" and "use a CSV on Users" in page
