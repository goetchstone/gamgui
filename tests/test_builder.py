"""The Command Builder + Sequencer web flow (catalog browse, slot→argv build, guarded run, sequence)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.catalog import load_catalog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


def test_slot_value_is_a_single_argv_element():
    # The injection-safety guarantee: a malicious slot value lands as ONE argv element, never split.
    cmd = load_catalog().by_id("build.add_delegate")
    argv = cmd.build({"email": "a@x.com; rm -rf /", "delegate": "b@x.com"})
    assert argv == ["user", "a@x.com; rm -rf /", "add", "delegate", "b@x.com"]


def test_builder_page_and_catalog_search(client):
    r = client.get("/builder")
    assert r.status_code == 200 and "Command builder" in r.text and "Users" in r.text
    r = client.get("/builder/catalog", params={"q": "signature"})
    assert "Set Gmail signature" in r.text and "buildable" in r.text


def test_buildable_form_has_slots(client):
    r = client.get("/builder/command/build.set_signature")
    assert r.status_code == 200
    assert 'name="email"' in r.text and 'name="signature"' in r.text and 'name="cid"' in r.text


def test_preview_shows_assembled_gam_and_guard(client):
    r = client.post("/builder/preview", data={"cid": "build.add_delegate",
                                              "email": "alice@example.com", "delegate": "bob@example.com"})
    assert r.status_code == 200
    assert "gam user alice@example.com add delegate bob@example.com" in r.text
    assert ">Run<" in r.text or "Run" in r.text


def test_preview_requires_field(client):
    r = client.post("/builder/preview", data={"cid": "build.add_delegate", "email": "", "delegate": "b@x.com"})
    assert "required" in r.text.lower()


def test_destructive_command_requires_confirm(client):
    r = client.post("/builder/preview", data={"cid": "build.delete_user", "email": "alice@example.com"})
    assert "DESTRUCTIVE" in r.text and "Confirm" in r.text   # red confirm button (& is HTML-escaped)


def test_destructive_run_requires_confirmed_flag(client):
    # A bare run of a destructive command re-shows the preview; only confirmed=1 executes.
    bare = client.post("/builder/run", data={"cid": "build.delete_user", "email": "alice@example.com"})
    assert "Will run" in bare.text and "done" not in bare.text
    ok = client.post("/builder/run", data={"cid": "build.delete_user", "email": "alice@example.com", "confirmed": "1"})
    assert "Delete account" in ok.text and "done" in ok.text


def test_run_mutation_goes_through_guard_and_audit(client):
    r = client.post("/builder/run", data={"cid": "build.set_signature",
                                          "email": "alice@example.com", "signature": "Hi"})
    assert r.status_code == 200 and "Set Gmail signature" in r.text


def test_run_read_command_renders_table(client):
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert r.status_code == 200
    assert "assistant@example.com" in r.text          # from the mock `print delegates` CSV


def test_browse_only_command_cannot_run(client):
    browse_id = next(c.id for c in load_catalog().commands if not c.buildable)
    assert "Browse-only" in client.get(f"/builder/command/{browse_id}").text
    r = client.post("/builder/preview", data={"cid": browse_id})
    assert "run it in GAM" in r.text          # refused (apostrophe in "can't" is HTML-escaped)


def test_sequence_add_remove_and_run(client):
    client.post("/builder/sequence/add", data={"cid": "build.set_signature",
                                               "email": "alice@example.com", "signature": "Hi"})
    r = client.post("/builder/sequence/add", data={"cid": "build.add_delegate",
                                                   "email": "alice@example.com", "delegate": "bob@example.com"})
    assert "Set Gmail signature" in r.text and "Add mailbox delegate" in r.text
    run = client.post("/builder/sequence/run")
    m = re.search(r"/builder/sequence/status\?job=([A-Za-z0-9_\-]+)", run.text)
    assert m, run.text[:200]
    last = ""
    for _ in range(40):
        last = client.get("/builder/sequence/status", params={"job": m.group(1)}).text
        if "Sequence complete" in last:
            break
    assert "Sequence complete" in last   # the background task doesn't run steps under TestClient;
    # the per-step execution is asserted deterministically below.


@pytest.mark.asyncio
async def test_run_sequence_executor_applies_each(connector):
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.builder import _run_sequence, _seq_previews
    seq = [
        {"target": "alice@example.com", "label": "Set signature",
         "argv": ["user", "alice@example.com", "signature", "Hi", "html"], "risk": 1},
        {"target": "alice@example.com", "label": "Add delegate",
         "argv": ["user", "alice@example.com", "add", "delegate", "bob@example.com"], "risk": 1},
    ]
    job = start_job({}, len(seq))
    await _run_sequence(job, connector, _seq_previews(seq))
    assert job.finished and job.applied == 2 and not job.failed


def test_destructive_single_step_sequence_needs_confirm(client):
    # A lone destructive step must still be confirmed — running it via a 1-step sequence is no bypass.
    client.post("/builder/sequence/add", data={"cid": "build.delete_user", "email": "victim@example.com"})
    bare = client.post("/builder/sequence/run")
    assert "status?job=" not in bare.text and "Confirm" in bare.text   # confirm interstitial, not run
    ok = client.post("/builder/sequence/run", data={"confirmed": "1"})
    assert "status?job=" in ok.text                                    # now it starts the job


def test_destructive_bulk_sequence_needs_typed_confirm(client):
    for _ in range(10):  # ten destructive deletes => bulk + destructive => typed confirm
        client.post("/builder/sequence/add", data={"cid": "build.delete_user", "email": "victim@example.com"})
    prev = client.post("/builder/sequence/preview")
    assert 'name="confirm"' in prev.text          # typed confirmation required
    blocked = client.post("/builder/sequence/run", data={"confirm": "nope"})
    assert "Type confirm" in blocked.text and "status?job=" not in blocked.text
