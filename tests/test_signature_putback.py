"""Put Back (roadmap 4.1): set a kept run's old signatures again, through the chokepoint. Who can't go back exactly is left
out and said; anyone whose signature changed since that run is left as it is; what Put Back replaces is kept too."""

from __future__ import annotations

import json
import re
from html import unescape

import pytest

from gamgui.core import signature_backups as sb
from gamgui.core.gam.commands import GAMCommands

from .helpers import gam_writes, wait_for_job
from .test_users_web import _job, _sig_apply, _sig_preview, client  # noqa: F401

ALICE, CAROL = "alice@example.com", "carol@example.com"


@pytest.mark.parametrize("previous, goes_back", [
    ("Best,<br>Alice", True), ("", True), ("  spaced\nlines\n", True),
    ("file", False), (" HTML_File ", False),             # GAM would read it as a file to load
    ("line\r\nbreak", False), ("a\\nb", False),          # GAM would change it on the way in
])
def test_only_what_can_go_back_exactly_is_put_back(previous, goes_back):
    run = sb.Run("r", 1.0, "x", [sb.Entry(ALICE, previous, "new")])
    puts, left_out = sb.put_back_plan(run)
    assert (puts == run.entries) is goes_back and (len(left_out) == 1) is (not goes_back)


def test_gam_s_rewrite_on_the_way_in():
    assert sb.gam_stored("a\r\nb\\nc") == "a\nb<br/>c" and sb.gam_stored("") == ""


# --- through the screen and the mock --------------------------------------------------------------------------------------

def _run(client, template):  # noqa: F811
    form = {"template": template, "scope_type": "user", "scope_value": ALICE}
    _, token = _sig_preview(client, **form)
    job = _job(client, _sig_apply(client, token, **form).text, "/signatures/apply/status")
    wait_for_job(client, job)
    return job


def _kept(client):  # noqa: F811
    return client.app.state.gamgui.signature_backups


def _put_back(client, run_id, **extra):  # noqa: F811
    shown = client.post("/signatures/putback/preview", data={"run": run_id}).text
    token = re.search(r'name="preview" value="([^"]+)"', shown)
    r = client.post("/signatures/putback", data={"run": run_id, "preview": token.group(1) if token else "", "confirmed": "1", **extra})
    job = None
    if "apply/status" in r.text:
        job = _job(client, r.text, "/signatures/apply/status")
        wait_for_job(client, job)
    return unescape(shown), r, job


async def _now(client, email):  # noqa: F811
    return await client.app.state.gamgui.connector.signature_exact(email)


def test_a_run_s_old_signature_goes_back_and_what_it_replaces_is_kept(client, gam_calls, gam_state):  # noqa: F811
    _run(client, "<b>{name}</b>")
    [first] = _kept(client).runs()
    assert "alice@example.com" in client.get("/signatures/kept").text and "Put back…" in client.get("/signatures/kept").text
    shown, _, job = _put_back(client, first.id)
    assert "Put back the signatures “alice@example.com” replaced: 1 person." in shown
    assert job.applied == 1
    assert gam_writes(gam_calls())[-1] == GAMCommands.set_signature(ALICE, "Best,<br>Alice")
    assert client.portal.call(_now, client, ALICE) == "Best,<br>Alice"
    newest = _kept(client).runs()[0]
    assert newest.what == "put back of alice@example.com"
    assert newest.entries == [sb.Entry(ALICE, "<b>Alice Anders</b>", "Best,<br>Alice")]   # so Put Back can be undone too


def test_someone_changed_since_that_run_is_left_as_they_are(client, gam_calls, gam_state):  # noqa: F811
    _run(client, "<b>{name}</b>")
    run_id = _kept(client).runs()[0].id
    _run(client, "<i>changed later</i>")              # someone else's change after that run
    writes = len(gam_writes(gam_calls()))
    _, _, job = _put_back(client, run_id)
    assert job.failed_items == [ALICE] and "Changed since that run" in job.failed[0].reason
    assert len(gam_writes(gam_calls())) == writes and client.portal.call(_now, client, ALICE) == "<i>changed later</i>"


def test_an_empty_old_signature_goes_back_as_empty(client, gam_state):  # noqa: F811
    from gamgui.web.routes._common import signature_backups
    store = signature_backups(client.app.state.gamgui)
    store.folder.mkdir(parents=True, exist_ok=True)
    (store.folder / "0000000000002-empty.jsonl").write_text(
        json.dumps({"run": "0000000000002-empty", "when": 1.0, "what": "carol"}) + "\n"
        + json.dumps({"email": CAROL, "previous": "", "set": "Carol Clark<br>Operations"}) + "\n")
    _, _, job = _put_back(client, "0000000000002-empty")
    assert job.applied == 1 and client.portal.call(_now, client, CAROL) == ""


def test_a_run_that_cant_be_read_or_doesnt_exist_puts_nothing_back(client, gam_calls):  # noqa: F811
    for bad in ("nope", "../x"):
        shown, r, job = _put_back(client, bad)
        assert "can't be put back" in shown and job is None
    assert gam_writes(gam_calls()) == []


def test_a_put_back_runs_once(client, gam_calls, gam_state):  # noqa: F811
    _run(client, "<b>{name}</b>")
    run_id = _kept(client).runs()[0].id
    shown = client.post("/signatures/putback/preview", data={"run": run_id}).text
    token = re.search(r'name="preview" value="([^"]+)"', shown).group(1)
    first = client.post("/signatures/putback", data={"run": run_id, "preview": token, "confirmed": "1"})
    wait_for_job(client, _job(client, first.text, "/signatures/apply/status"))
    again = client.post("/signatures/putback", data={"run": run_id, "preview": token, "confirmed": "1"})
    assert "expired or was already run" in again.text
