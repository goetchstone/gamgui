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

from .helpers import TEST_HOSTS, assert_ok_partial, gam_writes, wait_for_job

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
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def test_slot_value_is_a_single_argv_element():
    # The injection-safety guarantee: a malicious slot value lands as ONE argv element, never split.
    cmd = load_catalog().by_id("build.add_delegate")
    argv = cmd.build({"email": "a@x.com; rm -rf /", "delegate": "b@x.com"})
    assert argv == ["user", "a@x.com; rm -rf /", "add", "delegate", "b@x.com"]


def test_builder_page_and_catalog_search(client):
    r = client.get("/builder")
    assert r.status_code == 200 and "Command builder" in r.text and "Users" in r.text


def test_curated_search_commands_build_safely():
    cat = load_catalog()
    poison = "x; rm -rf /"
    # Find users (domain-wide)
    u = cat.by_id("build.find_users")
    assert u and u.buildable and u.risk == RiskLevel.READ_ONLY and u.supports_export
    argv = u.build({"query": poison})
    assert argv[:2] == ["print", "users"] and argv.count(poison) == 1 and argv[-1] == "formatjson"
    assert any(s.key == "query" and "isSuspended=true" in s.hints for s in u.slots)
    # Find Chromebooks (domain-wide)
    c = cat.by_id("build.find_cros")
    assert c and c.buildable and c.supports_export
    cargv = c.build({"query": poison})
    assert cargv[:2] == ["print", "cros"] and cargv.count(poison) == 1 and "formatjson" in cargv
    assert any("status:provisioned" in s.hints for s in c.slots if s.key == "query")
    # Find Drive files (per-user)
    f = cat.by_id("build.find_files")
    assert f and f.buildable and f.supports_export
    fargv = f.build({"email": "alice@example.com", "query": poison})
    assert fargv[:4] == ["user", "alice@example.com", "print", "filelist"]
    assert fargv.count(poison) == 1 and "formatjson" in fargv
    assert any("'me' in owners" in s.hints for s in f.slots if s.key == "query")


def test_curated_search_commands_run(client):
    # Each renders a result table through the real run path + mock GAM.
    r = client.post("/builder/run", data={"cid": "build.find_users", "query": "isSuspended=true"})
    assert r.status_code == 200 and "gam print users" in r.text
    rc = client.post("/builder/run", data={"cid": "build.find_cros", "query": "status:ACTIVE"})
    assert rc.status_code == 200 and "5CD123" in rc.text          # a mock device serial
    rf = client.post("/builder/run", data={"cid": "build.find_files",
                                           "email": "alice@example.com", "query": "trashed=false"})
    assert rf.status_code == 200 and "Q4 Budget" in rf.text       # a mock file name


def test_export_offered_exactly_for_todrive_reads(client):
    # Export-to-Sheet shows iff the GAM command actually supports `todrive`.
    cat = load_catalog()
    for c in cat.commands:
        if c.id.startswith("raw."):   # generic rows carry the grammar line — accurate signal
            expected = c.risk == RiskLevel.READ_ONLY and "todrive" in c.raw_syntax
            assert c.supports_export == expected, c.raw_syntax
    # curated print-based read now offers export (its hand-authored syntax omits `todrive`)
    assert cat.by_id("build.search_messages").supports_export
    assert "Export to a Google Sheet" in client.get("/builder/command/build.search_messages").text
    # a non-read curated command never offers export
    assert not cat.by_id("build.set_signature").supports_export


def test_hardening_csp_and_local_assets(client):
    # The UI loads no remote executable scripts (vendored locally) and ships security headers.
    page = client.get("/builder")
    assert "/static/vendor/tailwind-play.js" in page.text
    assert "/static/vendor/htmx-1.9.12.min.js" in page.text
    assert "cdn.tailwindcss.com" not in page.text and "unpkg.com" not in page.text
    h = page.headers
    assert "frame-ancestors 'none'" in h.get("content-security-policy", "")
    assert "object-src 'none'" in h.get("content-security-policy", "")
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy") == "no-referrer"
    # the vendored assets are actually served
    assert client.get("/static/vendor/htmx-1.9.12.min.js").status_code == 200


def test_dense_pages_use_full_window_width(client):
    # Dense screens drop the centered 1024px cap so the catalog/results use the whole window.
    assert "max-w-none" in client.get("/builder").text
    assert "max-w-none" in client.get("/users").text
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
    assert "formatjson" not in argv                # `print messages` has no JSON mode — CSV only


def test_search_messages_detail_modes():
    cmd = load_catalog().by_id("build.search_messages")
    assert "showbody" in cmd.build({"email": "u@x.com", "detail": "Headers + body"})
    summary = cmd.build({"email": "u@x.com", "detail": "Summary"})
    assert "showbody" not in summary and "showsnippet" in summary


def test_search_messages_query_has_insertable_hints(client):
    # The query slot offers click-to-insert operator chips (incl. the date format) and the form
    # renders them wired to the insert helper.
    qslot = next(s for s in load_catalog().by_id("build.search_messages").slots if s.key == "query")
    for op in ("rfc822msgid:", "from:", "after:2026/06/23", "before:2026/06/25", "newer_than:7d"):
        assert op in qslot.hints
    html = client.get("/builder/command/build.search_messages").text
    assert 'onclick="ggHint(this)"' in html and 'data-ins="rfc822msgid:"' in html
    assert "YYYY/MM/DD" in html   # the date-format note


def test_search_messages_runs_and_surfaces_return_path(client):
    r = client.post("/builder/run", data={
        "cid": "build.search_messages", "email": "alice@example.com",
        "query": "after:2026/06/23 before:2026/06/24", "detail": "Headers"})
    assert r.status_code == 200
    # Asserts on rendered output, not a URL/host check: the fixture's envelope-sender id is a
    # non-domain token, so this pins the actual Return-Path cell rather than any substring of a host.
    assert "0101019ef4e29302-4b960d36-aba1-4a59-9f22-123f07e3fce8-000000" in r.text
    assert "gam user alice@example.com print messages" in r.text


def test_read_error_surfaces_gam_details(client):
    # A failing read shows the friendly remediation AND the raw GAM stderr — no more
    # "see details below" with nothing below it.
    r = client.post("/builder/run", data={
        "cid": "build.search_messages", "email": "alice@example.com", "query": "FAILME", "detail": "Headers"})
    assert r.status_code == 200
    assert "See details below" in r.text                      # the UNKNOWN-kind remediation
    assert "a representative GAM failure detail" in r.text     # …and the actual stderr is now shown


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


def _audit(client):
    return client.app.state.gamgui.connector.audit.tail()


def test_read_command_export_to_drive(client):
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com",
                                          "td_export": "1", "td_user": "boss@example.com", "td_title": "Delegates"})
    assert "Exported to a Google Sheet" in r.text and "boss@example.com" in r.text
    assert "todrive tduser boss@example.com tdtitle Delegates" in r.text
    # The export creates a file in boss's Drive — a write, so the chokepoint audits it (plan Q6).
    [entry] = _audit(client)
    assert (entry["action"], entry["target"], entry["ok"]) == ("export_to_sheet", "boss@example.com", True)
    assert entry["argv"] == ["user", "alice@example.com", "print", "delegates",
                             "todrive", "tduser", "boss@example.com", "tdtitle", "Delegates"]


def test_a_failed_export_is_audited_and_shown(client, monkeypatch):
    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    async def refused(domain, argv, **kw):
        raise GAMError(GAMErrorKind.PERMISSION_DENIED, exit_code=1, stderr="ERROR: 403: Insufficient permissions")

    monkeypatch.setattr(client.app.state.gamgui.connector.runner, "run_authenticated", refused)
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com",
                                          "td_export": "1", "td_user": "boss@example.com"})
    assert "export to a Google Sheet failed" in r.text and "403" in r.text
    [entry] = _audit(client)
    assert (entry["action"], entry["target"], entry["ok"]) == ("export_to_sheet", "boss@example.com", False)


SENSITIVE_HEADS = {
    "gam <UserTypeEntity> show backupcodes|verificationcodes",
    "gam <UserTypeEntity> print backupcodes|verificationcodes",
    "gam show browsertokens",
    "gam print browsertokens",
    "gam <UserTypeEntity> get drivefile <DriveFileEntity>",
    "gam <UserTypeEntity> get document <DriveFileEntity>",
}


def _sensitive(needle):
    return next(c for c in load_catalog().commands if c.sensitive and needle in c.raw_syntax)


def test_sensitive_reads_are_flagged_and_still_buildable():
    # Operator decision D3: keep them runnable, audit every run. Pinned by syntax (the raw.<line> ids
    # move on a GAM bump), so a bump that renames one fails here instead of silently losing its audit.
    flagged = [c for c in load_catalog().commands if c.sensitive]
    assert {c.raw_syntax.split("[")[0].strip() for c in flagged} == SENSITIVE_HEADS
    assert len(flagged) == len(SENSITIVE_HEADS)
    assert all(c.buildable and c.risk == RiskLevel.READ_ONLY for c in flagged)


def test_a_sensitive_read_is_audited_without_its_output(client):
    cmd = _sensitive("show backupcodes")
    r = client.post("/builder/run", data={"cid": cmd.id, "a0": "alice@example.com"})
    assert "11112222" in r.text                              # the operator still sees the codes
    [entry] = _audit(client)
    assert (entry["action"], entry["target"], entry["ok"]) == ("sensitive_read", "alice@example.com", True)
    assert entry["extra"] == {"command": cmd.id}
    assert entry["argv"] == ["user", "alice@example.com", "show", "backupcodes"]
    log = client.app.state.gamgui.connector.audit.path.read_text()
    assert "11112222" not in log and "33334444" not in log   # never the output


def test_a_failed_sensitive_read_is_audited(client):
    cmd = _sensitive("show backupcodes")
    r = client.post("/builder/run", data={"cid": cmd.id, "a0": "missing@example.com"})
    assert "Does not exist" in r.text
    [entry] = _audit(client)
    assert (entry["action"], entry["target"], entry["ok"]) == ("sensitive_read", "missing@example.com", False)
    assert entry["extra"]["command"] == cmd.id and "Does not exist" in entry["extra"]["error"]


async def test_a_sensitive_export_is_audited_as_such(connector, monkeypatch):
    async def uploaded(domain, argv, **kw):
        return "Data uploaded to Drive File: https://docs.google.com/spreadsheets/d/abc"

    monkeypatch.setattr(connector.runner, "run_authenticated", uploaded)
    cmd = _sensitive("print backupcodes")
    res = await connector.export_to_sheet(cmd, cmd.build({"a0": "alice@example.com"}), "boss@example.com")
    assert res.ok and "spreadsheets/d/abc" in res.output
    [entry] = connector.audit.tail()
    assert (entry["action"], entry["target"]) == ("sensitive_export", "boss@example.com")
    assert entry["argv"] == ["user", "alice@example.com", "print", "backupcodes", "todrive", "tduser",
                             "boss@example.com"]


def test_a_plain_read_writes_no_audit_entry(client):
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert "assistant@example.com" in r.text and _audit(client) == []


async def test_catalog_read_and_export_refuse_a_write_command(connector, gam_calls):
    # The Builder's read entry points can't become a second write path, whatever a caller passes.
    write = load_catalog().by_id("build.delete_user")
    with pytest.raises(ValueError):
        await connector.catalog_read(write, write.build({"email": "alice@example.com"}))
    with pytest.raises(ValueError):
        await connector.export_to_sheet(write, write.build({"email": "alice@example.com"}))
    assert gam_calls() == [] and connector.audit.tail() == []


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


def _builder_preview(client, **form):
    """Preview a Builder command; return the page and its Run button's single-use token ("" if none)."""
    r = client.post("/builder/preview", data=form)
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', r.text)
    return r.text, (m.group(1) if m else "")


def _builder_run(client, token, **form):
    """Post Run as the preview's button does: the live form (hx-include) + confirmed + the token."""
    return client.post("/builder/run", data={**form, "confirmed": "1", "preview": token})


@pytest.mark.parametrize("previewed, ran, shown", [
    # Preview an undelete, load "Delete account", type another address, click the stale blue Run:
    # the account was deleted with no preview and no confirm dialog.
    ({"cid": "build.undelete_user", "email": "carol@example.com"},
     {"cid": "build.delete_user", "email": "alice@example.com", "confirm_email": "alice@example.com"},
     "gam delete user alice@example.com"),
    # Preview a suspend for carol, retype the address, click Confirm & run: alice was suspended.
    ({"cid": "build.suspend_user", "email": "carol@example.com"},
     {"cid": "build.suspend_user", "email": "alice@example.com"},
     "gam update user alice@example.com suspended on"),
])
def test_builder_run_executes_only_the_previewed_command(client, gam_calls, previewed, ran, shown):
    _, token = _builder_preview(client, **previewed)
    r = _builder_run(client, token, **ran)
    assert "The form changed after the preview" in r.text and "done" not in r.text
    assert "Will run" in r.text and shown in r.text          # the new command's own preview, to check first
    assert gam_writes(gam_calls()) == []


def test_builder_run_is_single_use(client, gam_calls):
    form = {"cid": "build.set_signature", "email": "alice@example.com", "signature": "Hi"}
    _, token = _builder_preview(client, **form)
    assert "done" in _builder_run(client, token, **form).text
    r = _builder_run(client, token, **form)                          # a replayed click
    assert "expired or was already run" in r.text and "done" not in r.text
    assert len(gam_writes(gam_calls())) == 1


def test_builder_mutation_runs_only_from_its_preview(client, gam_calls):
    # Even a single-target LOW write: a POST that didn't come from a preview shows the preview instead.
    r = client.post("/builder/run", data={"cid": "build.set_signature", "email": "alice@example.com",
                                          "signature": "Hi", "confirmed": "1"})
    assert "Will run" in r.text and "done" not in r.text
    assert gam_writes(gam_calls()) == []


def test_builder_delete_needs_the_email_typed(client, gam_calls):
    # The Builder's "Delete account" (a row action on every result table) once ran on the Confirm
    # click alone, while the Users delete zone demanded the exact email typed. guard.enforce owns
    # that rule now: the preview asks for the address, and the run refuses without it.
    form = {"cid": "build.delete_user", "email": "alice@example.com"}
    shown, _ = _builder_preview(client, **form)
    assert 'name="confirm_email"' in shown and "#builder-confirm-email" in shown
    for typed in ({}, {"confirm_email": "carol@example.com"}):
        _, token = _builder_preview(client, **form)
        r = _builder_run(client, token, **form, **typed)
        assert "Type the exact email" in r.text and "Will run" in r.text and "done" not in r.text
    assert gam_writes(gam_calls()) == []
    _, token = _builder_preview(client, **form)
    ok = _builder_run(client, token, **form, confirm_email="alice@example.com")
    assert "Delete account" in ok.text and "done" in ok.text
    assert gam_writes(gam_calls()) == [["delete", "user", "alice@example.com"]]


def test_a_data_transfer_is_confirmed_like_a_destructive_change(client, gam_calls):
    # Ownership handed over can't be taken back by a second transfer (the offboarding runbook warns so),
    # and the README promises a data transfer runs behind a confirmation: a red Confirm & run, and
    # the typed "confirm" once a sequence holds ten.
    assert load_catalog().by_id("build.transfer_data").risk == RiskLevel.DESTRUCTIVE
    form = {"cid": "build.transfer_data", "old_owner": "carol@example.com", "service": "drive",
            "new_owner": "alice@example.com"}
    shown, token = _builder_preview(client, **form)
    assert "DESTRUCTIVE" in shown and "Confirm &amp; run" in shown
    r = client.post("/builder/run", data={**form, "preview": token})           # the token without the click
    assert "done" not in r.text and gam_writes(gam_calls()) == []
    _, token = _builder_preview(client, **form)
    assert "done" in _builder_run(client, token, **form).text
    assert gam_writes(gam_calls()) == [["create", "datatransfer", "carol@example.com", "drive", "alice@example.com"]]


def test_builder_delete_warns_on_a_pending_data_transfer(client):
    # Deleting before an offboarding's Drive transfer finishes loses the rest: the Builder warns like
    # the Users delete zone does (both read `print datatransfers olduser <address>`).
    r = client.post("/builder/preview", data={"cid": "build.delete_user", "email": "xferpending@example.com"})
    assert "Data transfer still in progress" in r.text and "permanently loses" in r.text
    r = client.post("/builder/preview", data={"cid": "build.delete_user", "email": "alice@example.com"})
    assert "Data transfer still in progress" not in r.text


def test_run_mutation_goes_through_guard_and_audit(client, gam_calls):
    form = {"cid": "build.set_signature", "email": "alice@example.com", "signature": "Hi"}
    _, token = _builder_preview(client, **form)
    r = _builder_run(client, token, **form)
    assert_ok_partial(r)
    assert "Set Gmail signature — done" in r.text
    argv = ["user", "alice@example.com", "signature", "Hi", "html"]
    assert gam_writes(gam_calls()) == [argv]
    rec = client.app.state.gamgui.connector.audit.tail()[-1]
    assert (rec["action"], rec["target"], rec["ok"]) == ("apply", "alice@example.com", True)
    assert rec["argv"] == ["user", "alice@example.com", "signature", "***redacted***", "html"]  # body kept out of the log


def test_run_read_command_renders_table(client):
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert r.status_code == 200
    assert "assistant@example.com" in r.text          # from the mock `print delegates` CSV


def test_read_results_export_csv(client):
    # Run a read command, then download the SAME result set as CSV.
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert "Download CSV" in r.text                    # the table offers the download
    e = client.get("/builder/export.csv")
    assert e.status_code == 200
    assert e.headers["content-type"].startswith("text/csv")
    assert "attachment" in e.headers["content-disposition"]
    assert "assistant@example.com" in e.text           # same records the table showed


def test_export_csv_without_a_run_is_friendly(client):
    e = client.get("/builder/export.csv")
    assert e.status_code == 404
    assert "run a read command first" in e.text


def test_export_csv_is_formula_injection_safe_and_handles_ragged_rows(client):
    import csv as _csv
    import io as _io

    # Stash a crafted result set directly: a formula-leading cell + a column only row 2 has.
    client.app.state.gamgui.builder_last_result = {
        "records": [
            {"email": "a@x.com", "name": "=HYPERLINK(\"evil\")"},
            {"email": "b@x.com", "name": "Bea", "extra": "+more"},
        ],
        "gam": "gam print users",
    }
    e = client.get("/builder/export.csv")
    assert e.status_code == 200
    rows = list(_csv.reader(_io.StringIO(e.text)))
    assert rows[0] == ["email", "name", "extra"]       # union columns, first-record order first
    assert rows[1] == ["a@x.com", "'=HYPERLINK(\"evil\")", ""]   # formula neutralized, gap filled
    assert rows[2] == ["b@x.com", "Bea", "'+more"]


def test_browse_only_command_cannot_run(client):
    browse_id = next(c.id for c in load_catalog().commands if not c.buildable)
    assert "Browse-only" in client.get(f"/builder/command/{browse_id}").text
    r = client.post("/builder/preview", data={"cid": browse_id})
    assert "run it in GAM" in r.text          # refused (apostrophe in "can't" is HTML-escaped)


def test_sequence_add_remove_and_run(client, gam_calls):
    client.post("/builder/sequence/add", data={"cid": "build.set_signature",
                                               "email": "alice@example.com", "signature": "Hi"})
    r = client.post("/builder/sequence/add", data={"cid": "build.add_delegate",
                                                   "email": "alice@example.com", "delegate": "bob@example.com"})
    assert "Set Gmail signature" in r.text and "Add mailbox delegate" in r.text
    _, token = _seq_preview(client)
    run = client.post("/builder/sequence/run", data={"preview": token})
    m = re.search(r"/builder/sequence/status\?job=([A-Za-z0-9_\-]+)", run.text)
    assert m, run.text[:200]
    # Await the job on the client's own loop (never poll the status endpoint), then check each step
    # really ran and was accepted — a failed step also advances `done`.
    job = client.app.state.gamgui.jobs[m.group(1)]
    wait_for_job(client, job)
    assert (job.applied, job.failed) == (2, [])
    assert gam_writes(gam_calls()) == [
        ["user", "alice@example.com", "signature", "Hi", "html"],
        ["user", "alice@example.com", "add", "delegate", "bob@example.com"],
    ]
    assert [e["ok"] for e in client.app.state.gamgui.connector.audit.tail()[-2:]] == [True, True]
    done = client.get("/builder/sequence/status", params={"job": job.id})
    assert_ok_partial(done)
    assert "Sequence complete — 2 of 2 steps succeeded." in done.text


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
    client.post("/builder/sequence/add", data={"cid": "build.suspend_user", "email": "victim@example.com"})
    _, token = _seq_preview(client)
    unconfirmed = client.post("/builder/sequence/run", data={"preview": token})
    assert "status?job=" not in unconfirmed.text and "Confirm" in unconfirmed.text   # the interstitial again
    _, token = _seq_preview(client)
    ok = client.post("/builder/sequence/run", data={"confirmed": "1", "preview": token})
    assert "status?job=" in ok.text                                    # now it starts the job


def _seq_preview(client):
    """Preview the sequence; return the page and the single-use token its Run form carries ("" if none)."""
    r = client.post("/builder/sequence/preview")
    m = re.search(r'name="preview" value="([A-Za-z0-9_\-]+)"', r.text)
    return r.text, (m.group(1) if m else "")


def test_sequence_run_executes_only_the_previewed_sequence(client, gam_calls):
    # The sequence lives on the server, so the stale case is a step added (or removed or moved) after
    # Preview: Run once ran whatever the sequence held at click time.
    client.post("/builder/sequence/add", data={"cid": "build.set_signature", "email": "alice@example.com",
                                               "signature": "Hi"})
    _, token = _seq_preview(client)
    client.post("/builder/sequence/add", data={"cid": "build.suspend_user", "email": "carol@example.com"})
    r = client.post("/builder/sequence/run", data={"confirmed": "1", "preview": token})
    assert "The sequence changed after the preview" in r.text and "status?job=" not in r.text
    assert gam_writes(gam_calls()) == []


def test_sequence_run_is_single_use(client, gam_calls):
    client.post("/builder/sequence/add", data={"cid": "build.set_signature", "email": "alice@example.com",
                                               "signature": "Hi"})
    _, token = _seq_preview(client)
    run = client.post("/builder/sequence/run", data={"preview": token})
    job = client.app.state.gamgui.jobs[re.search(r"status\?job=([A-Za-z0-9_\-]+)", run.text).group(1)]
    wait_for_job(client, job)
    again = client.post("/builder/sequence/run", data={"preview": token})     # a replayed click
    assert "expired or was already run" in again.text and "status?job=" not in again.text
    assert len(gam_writes(gam_calls())) == 1


def test_a_sequence_that_deletes_an_account_needs_the_email_typed(client, gam_calls):
    client.post("/builder/sequence/add", data={"cid": "build.delete_user", "email": "victim@example.com"})
    shown, token = _seq_preview(client)
    assert 'name="confirm_email"' in shown and "victim@example.com" in shown
    r = client.post("/builder/sequence/run", data={"confirmed": "1", "preview": token})
    assert "status?job=" not in r.text and "exact email" in r.text
    assert gam_writes(gam_calls()) == []
    _, token = _seq_preview(client)
    ok = client.post("/builder/sequence/run", data={"confirmed": "1", "confirm_email": "victim@example.com",
                                                    "preview": token})
    assert "status?job=" in ok.text


def test_destructive_bulk_sequence_needs_typed_confirm(client):
    for _ in range(10):  # ten destructive deletes => bulk + destructive => typed confirm
        client.post("/builder/sequence/add", data={"cid": "build.delete_user", "email": "victim@example.com"})
    prev, token = _seq_preview(client)
    assert 'name="confirm"' in prev               # typed confirmation required
    assert prev.count('name="confirm_email"') == 1               # and the one deleted address, once
    blocked = client.post("/builder/sequence/run", data={"confirm": "nope", "confirm_email": "victim@example.com",
                                                         "preview": token})
    assert "Type confirm" in blocked.text and "status?job=" not in blocked.text
