"""The Command Builder + Sequencer web flow (catalog browse, slot→argv build, guarded run, sequence)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.catalog import load_catalog
from gamgui.core.connectors.base import RiskLevel
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
    assert "Set Gmail signature" in r.text and "Build" in r.text


# --- generic read builder: every read-only command is runnable ------------------------

def test_every_command_has_a_description():
    # Each row explains itself: curated text where authored, else a grammar-derived gloss.
    cat = load_catalog()
    assert all(c.description for c in cat.commands)
    by = {c.id: c for c in cat.commands}
    assert by["build.delete_user"].description.startswith("Permanently delete")   # curated
    # the verified noun glossary feeds the gloss for parsed commands
    ve = next(c for c in cat.commands if c.id.startswith("raw.") and c.raw_syntax.startswith("gam print vaultexports"))
    assert "Vault" in ve.description


def test_every_read_command_is_buildable():
    # The whole read surface is runnable; mutations stay curated-only.
    cat = load_catalog()
    reads = [c for c in cat.commands if c.risk == RiskLevel.READ_ONLY and not c.uncertain]
    assert reads and all(c.buildable for c in reads)
    assert len([c for c in reads if c.id.startswith("raw.")]) > 400   # the bulk of the catalog


def test_only_read_commands_became_generically_buildable():
    # The safety boundary: the generic builder may ONLY make read-only commands runnable. A
    # mis-classified LOW/DESTRUCTIVE shallow line must never gain a run path.
    cat = load_catalog()
    for c in cat.commands:
        if c.buildable and c.id.startswith("raw."):
            assert c.risk == RiskLevel.READ_ONLY and not c.uncertain, c.raw_syntax


def test_generic_read_argv_is_injection_safe():
    # A poisoned slot value lands as ONE argv element, exactly like the curated builders.
    cat = load_catalog()
    cmd = next(c for c in cat.commands
               if c.id.startswith("raw.") and c.raw_syntax.startswith("gam <UserTypeEntity> print"))
    argv = cmd.build({s.key: "a@x.com; rm -rf /" for s in cmd.slots})
    assert argv[:2] == ["user", "a@x.com; rm -rf /"] and argv[2] == "print"   # value not split


def test_generic_read_drops_optional_flags_keeps_required():
    cat = load_catalog()
    pg = next(c for c in cat.commands if c.id.startswith("raw.") and c.raw_syntax.startswith("gam print groups"))
    assert pg.build({}) == ["print", "groups"]            # `[todrive …]` dropped, runs bare


def test_generic_read_runs_end_to_end(client):
    # Build + run a generic (non-curated) read through the real run path → rendered output.
    cat = load_catalog()
    pg = next(c for c in cat.commands if c.id.startswith("raw.") and c.raw_syntax.startswith("gam print groups"))
    r = client.post("/builder/run", data={"cid": pg.id})
    assert r.status_code == 200 and "gam print groups" in r.text


# --- curated "Search a mailbox" (find an email, show Return-Path/headers) --------------

def test_search_messages_query_is_one_argv_element_and_capped():
    cmd = load_catalog().by_id("build.search_messages")
    assert cmd is not None and cmd.buildable and cmd.risk == RiskLevel.READ_ONLY
    poison = "rfc822msgid:x; rm -rf /"
    argv = cmd.build({"email": "alice@example.com", "query": poison, "detail": "Headers"})
    assert argv[:4] == ["user", "alice@example.com", "print", "messages"]
    assert argv.count(poison) == 1                 # the whole Gmail query rides as ONE element
    assert "headers" in argv and "all" in argv     # full headers surface Return-Path/Received
    assert "max_to_print" in argv and "50" in argv  # bounded so it can't dump a whole mailbox
    assert argv[-1] == "formatjson"


def test_search_messages_detail_modes():
    cmd = load_catalog().by_id("build.search_messages")
    assert "showbody" in cmd.build({"email": "u@x.com", "detail": "Headers + body"})
    summary = cmd.build({"email": "u@x.com", "detail": "Summary"})
    assert "showbody" not in summary and "showsnippet" in summary


def test_search_messages_runs_and_surfaces_return_path(client):
    r = client.post("/builder/run", data={
        "cid": "build.search_messages", "email": "alice@example.com",
        "query": "after:2026/06/23 before:2026/06/24", "detail": "Headers"})
    assert r.status_code == 200
    assert "amazonses.com" in r.text   # the SES Return-Path is visible in the result table
    assert "gam user alice@example.com print messages" in r.text


def test_generic_read_never_emits_grammar_junk():
    # No built read command may contain raw grammar punctuation — a value is the only free part, and
    # literal tokens come from the grammar. Worst case is an incomplete (but valid-token) command.
    cat = load_catalog()
    JUNK = set("()<>|*[]")
    for c in cat.commands:
        if not (c.buildable and c.id.startswith("raw.")):
            continue
        argv = c.build({s.key: "VALUE" for s in c.slots})
        for tok in argv:
            assert tok == "VALUE" or not (set(tok) & JUNK), (c.raw_syntax, argv)


def test_generic_read_keeps_hyphenated_noun():
    # `gam print course-participants` — the hyphenated subcommand must survive (not be dropped).
    cat = load_catalog()
    cp = next(c for c in cat.commands if c.id.startswith("raw.") and "course-participants" in c.raw_syntax)
    assert cp.build({}) == ["print", "course-participants"]


def test_generic_read_leading_useritem_gets_user_keyword():
    # `gam <UserItem> show meetconferences` runs as `gam user <x> show meetconferences`.
    cat = load_catalog()
    cmd = next(c for c in cat.commands
               if c.id.startswith("raw.") and c.raw_syntax.startswith("gam <UserItem> show meetconferences"))
    assert cmd.build({s.key: "joe@x.com" for s in cmd.slots})[:3] == ["user", "joe@x.com", "show"]


def test_catalog_buildable_only_filter(client):
    # The default landing lists only runnable commands — every row is a Build, none a Copy.
    r = client.get("/builder/catalog", params={"buildable": "1"})
    assert "Build" in r.text and "Copy" not in r.text


def test_user_picker_searches_directory(client):
    # The slot picker returns matches from the cached directory, capped — scales to large domains.
    r = client.get("/builder/pick", params={"kind": "users", "q": "al"})
    assert r.status_code == 200 and "alice@example.com" in r.text
    assert "No matches" in client.get("/builder/pick", params={"kind": "users", "q": "zzznope"}).text
    assert "sales@example.com" in client.get("/builder/pick", params={"kind": "groups"}).text


def test_builder_form_renders_picker_not_datalist(client):
    # User slots use the server-backed picker widget, not a <datalist>.
    r = client.get("/builder/command/build.add_delegate")
    assert 'class="upick' in r.text and 'data-kind="users"' in r.text and "datalist" not in r.text


def test_builder_page_groups_into_areas(client):
    r = client.get("/builder")
    assert "Users &amp; Identity" in r.text and "Calendars" in r.text   # area dropdown, not 53 cats


def test_area_browse_is_a_flat_paginated_list(client):
    # Browsing an area returns one flat, paginated list (no tree, no scroll box) with category headers.
    r = client.get("/builder/catalog", params={"area": "Users & Identity", "buildable": ""})
    assert "<details" not in r.text and "Page 1 of" in r.text   # flat list with a pager, no tree
    assert "<ul" in r.text and "uppercase" in r.text            # category headers still group rows


def test_catalog_paginates_with_prev_next(client):
    # Pages are small (fit a 13"); page 1 has Next not Prev, a later page has Prev.
    r1 = client.get("/builder/catalog", params={"area": "Users & Identity", "buildable": ""})
    assert "Next ›" in r1.text and "of" in r1.text
    r2 = client.get("/builder/catalog", params={"area": "Users & Identity", "buildable": "", "page": 2})
    assert "‹ Prev" in r2.text and "Page 2 of" in r2.text


def test_read_command_export_to_drive(client):
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com",
                                          "td_export": "1", "td_user": "boss@example.com", "td_title": "Delegates"})
    assert "Exported to a Google Sheet" in r.text and "boss@example.com" in r.text
    assert "todrive tduser boss@example.com tdtitle Delegates" in r.text


def test_row_action_prefills_the_form(client):
    # Clicking a person in a result opens the chosen command pre-filled with that email.
    r = client.get("/builder/command/build.suspend_user", params={"email": "alice@example.com"})
    assert r.status_code == 200 and 'value="alice@example.com"' in r.text


def test_result_emails_are_actionable(client):
    # A read result makes email cells clickable, and the page ships the quick-actions menu
    # with both user and group actions.
    page = client.get("/builder").text
    assert 'id="row-actions"' in page and ">Suspend<" in page
    assert "As a user" in page and "As a group" in page and "Add a member" in page
    res = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert 'class="cell-act' in res.text and "assistant@example.com" in res.text


def test_row_action_prefills_group_slot(client):
    # Clicking an address as a group pre-fills the group slot (different slot key than the user case).
    r = client.get("/builder/command/build.add_group_member", params={"group": "sales@example.com"})
    assert r.status_code == 200 and 'value="sales@example.com"' in r.text


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
    # Assert the run STARTED (a polling panel). We don't poll the bg job to completion under
    # TestClient — that task + subprocess can deadlock; per-step execution is covered deterministically
    # by test_run_sequence_executor_applies_each below.
    assert re.search(r"/builder/sequence/status\?job=[A-Za-z0-9_\-]+", run.text), run.text[:200]


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
