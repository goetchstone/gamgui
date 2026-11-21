"""Plan F4: a Builder read exported to a new Google Sheet, a curated write, and Files, the read-only list of what the app made.

GAM's todrive path was read from its source at the pin (7.52.04, CSVPrintFile.GetTodriveParameters and writeCSVfile; see
core/sheet_export.py), and the mock prints what it prints: the link on stdout instead of the CSV, exit 73 with "Drive API v3"
or "Sheets API … Service/App not enabled" when the service account isn't allowed one, the file made already in the Sheets
case. Nothing here has run against a real tenant: the first live export waits on the operator's yes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import files, sheet_export
from gamgui.core.audit import AuditLog
from gamgui.core.catalog import load_catalog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident.store import IncidentStore
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import ADMIN, OAUTH2_FAKE, TEST_HOSTS, gam_writes

FIXTURES = Path(__file__).parent / "fixtures"
GRAMMAR = Path(__file__).parent.parent / "gamgui" / "resources" / "gam7" / "GamCommands.txt"
DOMAIN = "example.com"
DRIVE, SHEETS = sheet_export.DRIVE_SCOPE, sheet_export.SHEETS_SCOPE
READ = GAMCommands.print_delegates("alice@example.com")
TITLE = "example.com - List mailbox delegates"   # sheet_export.title: the domain and the read's catalog name
LINK = re.compile(r"https://docs\.google\.com/spreadsheets/d/1MockSheet\d+/edit\?usp=drivesdk")


def _refuse(state: Path, *scopes: str) -> None:
    """The simulated tenant hasn't authorized these delegated scopes (the mock's GAM_MOCK_STATE/dwd_missing)."""
    (state / "dwd_missing").write_text("".join(s + "\n" for s in scopes))


def _made(state: Path) -> list:
    path = state / "drive_files"
    return path.read_text().splitlines() if path.exists() else []


# --- the argv -------------------------------------------------------------------------------------------------------------

def test_the_export_tail_is_the_admin_s_own_unshared_sheet():
    assert GAMCommands.todrive_args(TITLE) == ["todrive", "tdtitle", TITLE, "tdnobrowser", "true", "tdnoemail", "true"]
    assert GAMCommands.todrive_args() == ["todrive", "tdnobrowser", "true", "tdnoemail", "true"]
    tail = GAMCommands.todrive_args("a; rm -rf / tduser x@evil.example")   # a title is one element, whatever it says
    assert tail[2] == "a; rm -rf / tduser x@evil.example" and len(tail) == 7
    # Never another Drive, a share, an alert or a notice: owned by the connected admin, shared with nobody.
    assert not {"tduser", "tdshare", "tdalert", "tdnotify", "tdfrom", "tdparent", "tdfileid"} & set(tail[:2] + tail[3:])


@pytest.mark.skipif(not GRAMMAR.exists(), reason="the vendored GAM grammar isn't here")
def test_the_grammar_takes_each_option_the_tail_sends():
    block = GRAMMAR.read_text(errors="replace").split("<ToDriveAttribute> ::=", 1)[1].split("\n<", 1)[0]
    for option in ("(tdtitle <String>)", "(tdnobrowser [<Boolean>])", "(tdnoemail [<Boolean>])"):
        assert option in block, option


# --- the mock prints what GAM prints --------------------------------------------------------------------------------------

async def test_the_mock_uploads_the_table_and_prints_only_the_link(runner, domain, gam_state):
    res = await runner.run_any_exit(domain, READ + GAMCommands.todrive_args(TITLE))
    assert res.returncode == 0
    assert re.fullmatch(r"Data uploaded to Drive File:\n" + LINK.pattern + r"\n", res.stdout), res.stdout
    assert "delegateAddress" not in res.stdout           # the CSV goes to Drive, never to stdout
    plain = await runner.run_any_exit(domain, READ)
    [made] = _made(gam_state)
    assert made.split("\t")[1:] == [TITLE, str(len(plain.stdout.strip().splitlines()) - 1)]   # the file, its name, its rows
    assert sheet_export.sheet_url(res.stdout) == LINK.search(res.stdout).group(0)


async def test_the_mock_refuses_the_export_without_drive_and_makes_nothing(runner, domain, gam_state):
    _refuse(gam_state, DRIVE)
    res = await runner.run_any_exit(domain, READ + GAMCommands.todrive_args(TITLE))
    assert (res.returncode, res.stdout.strip()) == (73, "")
    assert f"User: {ADMIN}, Drive API v3 Service/App not enabled" in res.stderr
    assert _made(gam_state) == []


async def test_without_sheets_the_mock_makes_the_file_and_prints_no_link(runner, domain, gam_state):
    _refuse(gam_state, SHEETS)
    res = await runner.run_any_exit(domain, READ + GAMCommands.todrive_args(TITLE))
    assert (res.returncode, res.stdout.strip()) == (73, "")
    assert f"User: {ADMIN}, Sheets API Service/App not enabled" in res.stderr
    assert len(_made(gam_state)) == 1                     # made before the Sheets step: there, with no link


async def test_a_warning_exit_after_the_table_still_uploads_it(runner, domain, gam_state, connector):
    # GAM's warning (entityUnknownWarning, 56) sets the exit code and the read carries on; writeCSVfile uploads what it got,
    # prints the link, and 56 stands. The record keeps the link, and the result says the Sheet may be short.
    tokens = GAMCommands.print_tokens_of(["alice@example.com", "ghost@example.com"]) + GAMCommands.todrive_args(TITLE)
    res = await runner.run_any_exit(domain, tokens)
    assert res.returncode == 56 and LINK.search(res.stdout) and "ghost@example.com, Does not exist" in res.stderr
    result = await connector.export_to_sheet(_cmd(), tokens[:4], TITLE)
    [entry] = connector.audit.tail()
    assert not result.ok and LINK.fullmatch(result.noted["url"]) and entry["extra"]["url"] == result.noted["url"]
    shown = sheet_export.outcome(result.ok, result.noted["url"], result.detail, ADMIN)
    assert shown.made == "yes" and "may not hold every row" in shown.said


async def test_a_read_that_fails_uploads_nothing(runner, domain, gam_state):
    res = await runner.run_any_exit(domain, GAMCommands.print_delegates("missing@example.com") + GAMCommands.todrive_args())
    assert res.returncode != 0 and res.stdout.strip() == "" and _made(gam_state) == []


@pytest.mark.hand_built_argv
@pytest.mark.parametrize("tail", [["todrive"], ["todrive", "tduser", "boss@example.com", "tdnobrowser", "true", "tdnoemail", "true"],
                                  ["todrive", "tdnobrowser", "true", "tdnoemail", "true", "tdshare", "x@example.com", "reader"],
                                  ["todrive", "tdnoemail", "true", "tdnobrowser", "true"]])
async def test_the_mock_takes_only_the_tail_the_app_sends(runner, domain, gam_state, tail):
    res = await runner.run_any_exit(domain, READ + tail)
    assert res.returncode == 2 and "ERROR: Invalid argument" in res.stderr and _made(gam_state) == []


async def test_the_scope_check_and_the_upload_agree(runner, domain, gam_state):
    _refuse(gam_state, SHEETS)
    res = await runner.run_any_exit(domain, GAMCommands.check_svcacct(ADMIN, [DRIVE, SHEETS]))
    assert res.returncode == 10
    assert re.search(r"auth/drive\s+PASS", res.stdout) and re.search(r"auth/spreadsheets\s+FAIL", res.stdout)


# --- what GAM's output means ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,url", [
    ("Data uploaded to Drive File:\nhttps://docs.google.com/spreadsheets/d/abc/edit?usp=drivesdk\n",
     "https://docs.google.com/spreadsheets/d/abc/edit?usp=drivesdk"),
    ("WARNING: Results are too large for Google Spreadsheets. Uploading as a regular CSV file.\n"
     "Data uploaded to Drive File:\nhttps://drive.google.com/file/d/abc/view?usp=drivesdk\n",
     "https://drive.google.com/file/d/abc/view?usp=drivesdk"),
    ("Data uploaded to Drive File:\nhttps://evil.example/spreadsheets/d/abc\n", ""),
    ("Data uploaded to Drive File:\nhttp://docs.google.com/spreadsheets/d/abc\n", ""),
    ("Data uploaded to Drive File:\nhttps://x@docs.google.com/spreadsheets/d/abc\n", ""),
    ("Data uploaded to Drive File:\njavascript:alert(1)\n", ""),
    ("Data uploaded to Drive File:\nhttps://docs.google.com:evil/spreadsheets/d/abc\n", ""),
    ("Data uploaded to Drive File:\nhttps://docs.google.com:8443/spreadsheets/d/abc\n", ""),
    ("Data uploaded to Drive File:\nhttps://docs.google.com/spreadsheets/d/a\x7fbc\n", ""),
    ("delegateAddress\nboss@example.com\n", ""),
])
def test_only_a_google_file_link_is_a_link(text, url):
    assert sheet_export.sheet_url(text) == url
    assert sheet_export.noted(text) == ({"url": url} if url else {})
    assert bool(sheet_export.verify(text)) is (not url)


@pytest.mark.parametrize("error,made", [
    ("GAM failed (service_not_enabled, exit=73): User: a@x, Sheets API Service/App not enabled", "yes"),
    ("GAM failed (service_not_enabled, exit=73): User: a@x, Drive API v3 Service/App not enabled", "no"),
    ("GAM failed (service_not_enabled, exit=73): User: a@x, Drive Service/App not enabled", "no"),
    ("GAM failed (unknown, exit=67): WARNING: Insufficient permissions to perform this task", "no"),
    ("GAM failed (unknown, exit=50): Drive Folder: root, Failed: The user's Drive storage quota has been exceeded.", "no"),
    ('GAM failed (unknown, exit=50): Drive File: T, Failed: Google reported "x" but the file was probably uploaded, check that it has 3 rows', "unknown"),
    ("GAM failed (timeout, exit=None)", "unknown"),
    ("", "unknown"),
])
def test_a_failed_export_says_whether_a_file_was_made(error, made):
    shown = sheet_export.outcome(False, "", error, ADMIN)
    assert shown.made == made and shown.said and not shown.url
    if made == "unknown":
        assert "can't be told" in shown.said or "probably" in shown.said


def test_a_link_printed_before_a_failed_exit_is_a_made_file():
    shown = sheet_export.outcome(False, "https://docs.google.com/spreadsheets/d/a", "GAM failed (not_found, exit=56)", ADMIN)
    assert shown.made == "yes" and shown.url and "may not hold every row" in shown.said


def test_the_scope_check_in_words():
    assert sheet_export.scope_state({DRIVE: True, SHEETS: True}) == ""
    assert "couldn't be checked" in sheet_export.scope_state(None)          # unknown, never "allowed"
    assert SHEETS in sheet_export.scope_state({DRIVE: True, SHEETS: False})
    assert "can't make the file" in sheet_export.scope_state({DRIVE: False, SHEETS: True})


# --- the write, through the chokepoint ------------------------------------------------------------------------------------

def _cmd(cid="build.print_delegates"):
    return load_catalog().by_id(cid)


async def test_an_export_is_audited_with_its_link_and_domain(connector, gam_state):
    res = await connector.export_to_sheet(_cmd(), READ, TITLE)
    assert res.ok and LINK.fullmatch(res.noted["url"])
    [entry] = connector.audit.tail()
    assert (entry["action"], entry["target"], entry["ok"]) == ("export_to_sheet", ADMIN, True)
    assert entry["argv"] == READ + ["todrive", "tdtitle", TITLE, "tdnobrowser", "true", "tdnoemail", "true"]
    assert entry["extra"] == {"command": "build.print_delegates", "url": res.noted["url"], "domain": DOMAIN}
    assert "boss@example.com" not in str(entry)          # never the rows


async def test_a_sensitive_read_s_export_is_a_sensitive_export(connector, monkeypatch):
    async def uploaded(domain, argv, **kw):   # the mock doesn't model `print backupcodes`
        return "Data uploaded to Drive File:\nhttps://docs.google.com/spreadsheets/d/abc/edit?usp=drivesdk\n"
    monkeypatch.setattr(connector.runner, "run_authenticated", uploaded)
    cmd = next(c for c in load_catalog().commands if c.sensitive and "print backupcodes" in c.raw_syntax)
    res = await connector.export_to_sheet(cmd, cmd.build({"a0": "alice@example.com"}), "x")
    [entry] = connector.audit.tail()
    assert res.ok and entry["action"] == "sensitive_export" and entry["extra"]["url"] == res.noted["url"]


async def test_without_sheets_the_record_says_failed_and_the_file_is_made(connector, gam_state):
    _refuse(gam_state, SHEETS)
    res = await connector.export_to_sheet(_cmd(), READ, TITLE)
    assert not res.ok and res.noted == {} and len(_made(gam_state)) == 1
    [entry] = connector.audit.tail()
    assert entry["ok"] is False and "Sheets API Service/App not enabled" in entry["extra"]["error"] and "url" not in entry["extra"]
    assert sheet_export.outcome(res.ok, "", res.detail, res.preview.target).made == "yes"


async def test_a_link_before_a_warning_exit_stays_on_the_record(connector, monkeypatch):
    async def warned(domain, argv, **kw):
        raise GAMError(GAMErrorKind.NOT_FOUND, exit_code=56, stderr="User: bob@example.com, Does not exist",
                       stdout="Data uploaded to Drive File:\nhttps://docs.google.com/spreadsheets/d/w/edit\n")
    monkeypatch.setattr(connector.runner, "run_authenticated", warned)
    res = await connector.export_to_sheet(_cmd(), READ, TITLE)
    [entry] = connector.audit.tail()
    assert not res.ok and res.noted == {"url": "https://docs.google.com/spreadsheets/d/w/edit"}
    assert entry["ok"] is False and entry["extra"]["url"] == res.noted["url"] and "Does not exist" in entry["extra"]["error"]


async def test_an_exit_0_without_a_link_is_no_answer(connector, monkeypatch):
    async def quiet(domain, argv, **kw):
        return "delegateAddress\nboss@example.com\n"
    monkeypatch.setattr(connector.runner, "run_authenticated", quiet)
    res = await connector.export_to_sheet(_cmd(), READ, TITLE)
    [entry] = connector.audit.tail()
    assert not res.ok and "can't be told" in res.detail and entry["ok"] is False


@pytest.mark.parametrize("spelling", ["todrive", "ToDrive", "to_drive", "TODRIVE", " t_o_d_r_i_v_e "])
async def test_a_read_that_already_exports_is_refused(connector, gam_calls, spelling):
    # GAM reads a keyword lowercased with every "_" dropped (getArgument): each spelling is todrive to it. A plain read
    # carrying one would make a Sheet from Run with no preview; the export would make a second one.
    with pytest.raises(ValueError):
        await connector.export_to_sheet(_cmd(), READ + [spelling], TITLE)
    with pytest.raises(ValueError):
        await connector.catalog_read(_cmd(), READ + [spelling])
    assert gam_calls() == [] and connector.audit.tail() == []


async def test_the_scope_check_reads_both_scopes_or_says_unknown(connector, gam_state, monkeypatch):
    assert await connector.sheet_export_scopes() == {DRIVE: True, SHEETS: True}
    _refuse(gam_state, SHEETS)
    assert await connector.sheet_export_scopes() == {DRIVE: True, SHEETS: False}
    assert connector.audit.tail() == []                   # the app's own read: not recorded (F1)

    async def broken(domain, argv, **kw):
        raise GAMError(GAMErrorKind.NOT_AUTHENTICATED, exit_code=16, stderr="ERROR: Service Account OAuth2 File")
    monkeypatch.setattr(connector.runner, "run_authenticated", broken)
    assert await connector.sheet_export_scopes() is None
    monkeypatch.setattr(connector, "sheet_owner", lambda: "")
    assert await connector.sheet_export_scopes() is None


# --- the screens ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, gam_state):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "123"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    state.incident_store = IncidentStore(tmp_path / "incidents")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _read(client) -> str:
    r = client.post("/builder/run", data={"cid": "build.print_delegates", "email": "alice@example.com"})
    assert "Export to a Google Sheet…" in r.text and "Download CSV" in r.text
    return client.app.state.gamgui.builder_last_result["id"]


def _token(html: str) -> str:
    return re.search(r'"preview": "([^"]+)"', html).group(1)


def test_the_preview_says_what_runs_where_and_that_it_is_a_fresh_read(client, gam_calls):
    rid = _read(client)
    r = client.post("/builder/sheet/preview", data={"rid": rid})
    shown = f"gam user alice@example.com print delegates todrive tdtitle &#39;{TITLE}&#39; tdnobrowser true tdnoemail true"
    assert shown in r.text                                # quoted as a shell would: the title is one argument
    assert ADMIN in r.text and "shared with nobody" in r.text and "opens no browser and sends no email" in r.text
    rows = len(client.app.state.gamgui.builder_last_result["records"])
    assert "fresh read" in r.text and f"can differ from the {rows} rows on screen" in r.text
    assert r.text.count("Allowed") == 2 and 'hx-post="/builder/sheet/export"' in r.text
    assert gam_writes(gam_calls()) == []                  # a read and the scope check: nothing made yet


def test_a_scope_the_service_account_lacks_offers_no_export(client, gam_state):
    _refuse(gam_state, SHEETS)
    r = client.post("/builder/sheet/preview", data={"rid": _read(client)})
    assert "Not allowed" in r.text and "stop before finishing it as a Sheet" in r.text
    assert 'hx-post="/builder/sheet/export"' not in r.text
    assert "admin.google.com/ac/owl/domainwidedelegation" in r.text and "spreadsheets" in r.text   # Setup's link, every scope


def test_the_export_makes_one_sheet_and_shows_its_link(client, gam_calls, gam_state):
    rid = _read(client)
    token = _token(client.post("/builder/sheet/preview", data={"rid": rid}).text)
    r = client.post("/builder/sheet/export", data={"rid": rid, "preview": token, "confirmed": "1"})
    url = LINK.search(r.text).group(0)
    assert f'href="{url}"' in r.text and "shared with nobody" in r.text and len(_made(gam_state)) == 1
    entry = client.app.state.gamgui.connector.audit.tail()[-1]
    assert (entry["action"], entry["ok"], entry["extra"]["url"]) == ("export_to_sheet", True, url)
    again = client.post("/builder/sheet/export", data={"rid": rid, "preview": token, "confirmed": "1"})
    assert "already run" in again.text and len(_made(gam_state)) == 1          # a token runs once
    files_page = client.get("/files")
    assert f'href="{url}"' in files_page.text and "List mailbox delegates" in files_page.text and DOMAIN in files_page.text


def test_an_export_without_its_confirmation_runs_nothing(client, gam_calls, gam_state):
    rid = _read(client)
    token = _token(client.post("/builder/sheet/preview", data={"rid": rid}).text)
    r = client.post("/builder/sheet/export", data={"rid": rid, "preview": token})
    assert "needs confirmation" in r.text and _made(gam_state) == [] and gam_writes(gam_calls()) == []


def test_a_file_made_without_its_link_is_said(client, gam_state):
    rid = _read(client)
    token = _token(client.post("/builder/sheet/preview", data={"rid": rid}).text)
    _refuse(gam_state, SHEETS)    # taken away after the preview checked it
    r = client.post("/builder/sheet/export", data={"rid": rid, "preview": token, "confirmed": "1"})
    assert "made the file" in r.text and "Find the file in that Drive" in r.text and "href=\"https://docs" not in r.text
    assert len(_made(gam_state)) == 1


def test_a_kept_read_carrying_todrive_is_refused_at_the_preview_and_the_export(client, gam_calls, gam_state):
    rid = _read(client)
    st = client.app.state.gamgui
    st.builder_last_result["read"]["argv"] = READ + ["to_drive"]
    r = client.post("/builder/sheet/preview", data={"rid": rid})
    assert "already says todrive" in r.text and 'hx-post="/builder/sheet/export"' not in r.text
    # A token held for one anyway (the export's own refusal is the second line): an answer in words, never a 500.
    token = st.previews.hold("builder_sheet", rid, ("build.print_delegates", READ + ["to_drive"], TITLE), tenant=st.tenant_key())
    r = client.post("/builder/sheet/export", data={"rid": rid, "preview": token, "confirmed": "1"})
    assert r.status_code == 200 and "already carries todrive" in r.text and "Nothing ran" in r.text
    assert gam_writes(gam_calls()) == [] and _made(gam_state) == []
    assert [e["action"] for e in st.connector.audit.tail()] == ["read"]


def test_an_export_the_audit_log_missed_says_so(client, gam_state, monkeypatch):
    rid = _read(client)
    token = _token(client.post("/builder/sheet/preview", data={"rid": rid}).text)
    log = client.app.state.gamgui.connector.audit
    original = log.record

    def full(action, *a, **kw):
        if action == "export_to_sheet":
            raise OSError(28, "No space left on device")
        return original(action, *a, **kw)
    monkeypatch.setattr(log, "record", full)
    r = client.post("/builder/sheet/export", data={"rid": rid, "preview": token, "confirmed": "1"})
    assert LINK.search(r.text) and len(_made(gam_state)) == 1           # made: its link is the only trace
    assert "audit log couldn" in r.text and "Files won't list it" in r.text and "Kept in the audit log" not in r.text


def test_a_result_replaced_since_can_t_be_exported(client):
    rid = _read(client)
    _read(client)                                         # a newer run replaced the table
    r = client.post("/builder/sheet/preview", data={"rid": rid})
    assert "replaced by a newer run" in r.text


# --- Files --------------------------------------------------------------------------------------------------------------------

def test_files_runs_no_gam_and_says_what_it_can_t_list(client, gam_calls):
    r = client.get("/files")
    assert r.status_code == 200 and gam_calls() == []
    assert "No export to a Google Sheet is in the audit log" in r.text
    assert "No incident kept here has saved evidence" in r.text
    assert "GamGUI keeps no list of them" in r.text
    from gamgui.core.secrets.ephemeral import app_runtime_dir
    from gamgui.core.setup import SetupService
    st = client.app.state.gamgui
    for private in (app_runtime_dir(), SetupService(st.vault, st.runner).managed_setup_dir()):   # never a run or setup folder
        assert str(private) not in r.text


def test_files_lists_the_newest_exports_bounded(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(files.SHEETS_SHOWN + 5):
        log.record("export_to_sheet", target=ADMIN, argv=READ + GAMCommands.todrive_args(TITLE), ok=True,
                   extra={"command": "build.print_delegates", "url": f"https://docs.google.com/spreadsheets/d/{i}/edit"},
                   domain=DOMAIN)
    log.record("read", target="", argv=["print", "users"], ok=True, domain=DOMAIN)
    log.record("sensitive_export", target=ADMIN, argv=["x"], ok=True, extra={"url": "https://evil.example/d/1"}, domain="other.org")
    sheets = files.sheets_made(log.path)
    assert len(sheets.made) == files.SHEETS_SHOWN and sheets.more
    newest = sheets.made[0]
    assert (newest.sensitive, newest.domain, newest.outcome.url) == (True, "other.org", "")   # not a Google link: no link
    assert sheets.made[1].outcome.url.endswith(f"/{files.SHEETS_SHOWN + 4}/edit")


def test_files_says_an_unreadable_log_is_unknown(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.mkdir()                                          # can't be opened as a file: never "nothing exported"
    sheets = files.sheets_made(path)
    assert sheets.problem and sheets.made == ()
    assert files.sheets_made(tmp_path / "none.jsonl") == files.Sheets((), False, True)   # no log yet: nothing made


def test_files_lists_each_incident_s_evidence_folder(client, tmp_path):
    from datetime import datetime, timezone
    store = client.app.state.gamgui.incident_store
    kept = store.create(DOMAIN, "Phished", datetime(2026, 11, 20, tzinfo=timezone.utc))
    empty = store.create(DOMAIN, "Nothing saved", datetime(2026, 11, 21, tzinfo=timezone.utc))
    evidence = Path(store.root) / kept.id / "evidence"
    (evidence / "2026-11-20 1200").mkdir(parents=True, mode=0o700)
    r = client.get("/files")
    assert "Phished" in r.text and "1 saved read" in r.text and str(evidence) in r.text
    assert "Nothing saved" not in r.text and empty.id not in r.text


def test_files_says_when_the_incidents_can_t_be_read(client):
    st = client.app.state.gamgui
    st.incident_store, st.incident_store_problem = None, ""
    from gamgui.web.routes import _common
    original = _common.IncidentStore

    class Refused(original):
        def __init__(self, *a, **kw):
            raise _common.IncidentProblem("Couldn't read the incidents: Permission denied.")
    _common.IncidentStore = Refused
    try:
        r = client.get("/files")
    finally:
        _common.IncidentStore = original
    assert "Permission denied" in r.text and "What evidence is kept can't be told" in r.text
