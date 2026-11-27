"""A Builder sequence exported as a `gam batch` file (roadmap 5.3).

The quoting is held to GAM 7.52.05's own reader: doBatch iterates the file's lines (text mode, so `\\r` ends one too),
skips `#` lines and splits each with `shlex.split`. `_gam_batch_reads` below is that loop, so a value that would
come back changed, split or cut fails here. tests/fixtures/sequence_export.txt is the exact bytes SwiftGamGui
must write for the same steps.
"""

from __future__ import annotations

import html
import io
import re
import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.sequence_export import ExportRefused, batch_file, batch_line
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"

# The steps tests/fixtures/sequence_export.txt was written from (SwiftGamGui's parity test uses the same).
FIXTURE_STEPS = [
    {"cid": "build.reset_password", "label": "Reset password (random)",
     "argv": ["update", "user", "alice@example.com", "password", "random", "changepassword", "off"]},
    {"cid": "build.rename_shared_drive", "label": "Rename a shared drive",
     "argv": ["update", "shareddrive", "shareddriveid", "0AFINANCEuk9PVA", "name", "Finance — 2027 'Q1' $budget"]},
    {"cid": "build.delete_group", "label": "Delete a group",
     "argv": ["delete", "group", "old-team@example.com", "noactionifalias"]},
]


def _gam_batch_reads(text: str) -> list:
    """GAM's doBatch, as far as reading the file goes: lines, `#` skipped, shlex.split, blank lines dropped."""
    items = []
    for line in io.StringIO(text, newline=None):   # open(..., newline=None): \n, \r and \r\n each end a line
        if line.startswith("#"):
            continue
        argv = shlex.split(line)
        if argv:
            items.append(argv)
    return items


HARD_VALUES = [
    "Finance — 2027 'Q1' $budget", 'say "hi"', "a\\b", "`whoami`", "$(rm -rf /)", "50%datetime%",
    "# not a comment", "tab\there", "", " leading and trailing ", "Café", "Café", "a;b|c&d>e", "*?[]{}~",
]


@pytest.mark.parametrize("value", HARD_VALUES)
def test_every_value_comes_back_as_one_argument(value):
    argv = ["update", "shareddrive", "shareddriveid", "0AX", "name", value]
    assert _gam_batch_reads(batch_line(argv) + "\n") == [["gam", *argv]]


def test_the_file_is_the_steps_in_order_each_waiting_for_the_last():
    items = _gam_batch_reads(batch_file(FIXTURE_STEPS))
    assert items == [["gam", *FIXTURE_STEPS[0]["argv"]], ["commit-batch"],
                     ["gam", *FIXTURE_STEPS[1]["argv"]], ["commit-batch"],
                     ["gam", *FIXTURE_STEPS[2]["argv"]]]
    # GAM refuses the whole file for any line that isn't one of its batch words (BATCH_COMMANDS)
    assert {i[0] for i in items} <= {"gam", "commit-batch"}


def test_the_file_matches_the_fixture_byte_for_byte():
    assert batch_file(FIXTURE_STEPS).encode() == (FIXTURES / "sequence_export.txt").read_bytes()


@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", "\0"])
def test_a_value_with_a_line_break_is_refused(brk):
    with pytest.raises(ExportRefused, match="Step 1 .*line break"):
        batch_file([{"label": "Rename", "argv": ["update", "shareddrive", "shareddriveid", "0AX", "name", f"a{brk}gam delete user bob"]}])


@pytest.mark.parametrize("argv", [
    ["update", "user", "a@example.com", "password", "Hunter2!", "changepassword", "on"],
    ["create", "user", "a@example.com", "firstname", "A", "lastname", "B", "PASSWORD", "s3cret"],
    ["update", "user", "a@example.com", "password"],
])
def test_a_password_is_refused_not_written(argv):
    with pytest.raises(ExportRefused, match="password"):
        batch_file([{"label": "Set password", "argv": argv}, *FIXTURE_STEPS])


def test_gam_s_own_password_words_are_not_a_password():
    for word in ("random", "uniquerandom", "blocklogin"):
        batch_file([{"label": "Reset", "argv": ["update", "user", "a@example.com", "password", word]}])


def test_an_empty_sequence_is_refused():
    with pytest.raises(ExportRefused):
        batch_file([])


# --- the route ---------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _audit(client):
    return client.app.state.gamgui.connector.audit.tail()


def _export_link(client) -> str:
    """Preview the queued sequence and return its export link, as the page shows it."""
    page = client.post("/builder/sequence/preview").text
    m = re.search(r'href="(/builder/sequence/export\.txt\?preview=[^"]+)" download', page)
    assert m, page
    return html.unescape(m.group(1))


def test_the_export_downloads_the_previewed_steps_and_is_audited(client, gam_calls):
    client.post("/builder/sequence/add", data={"cid": "build.delete_group", "group": "old-team@example.com"})
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "alice@example.com"})
    link = _export_link(client)
    before = len(gam_calls())                           # the preview's own reads (an alias check)
    r = client.get(link)
    assert r.status_code == 200 and "attachment; filename=gamgui-sequence.txt" in r.headers["content-disposition"]
    assert _gam_batch_reads(r.text) == [["gam", "delete", "group", "old-team@example.com", "noactionifalias"],
                                        ["commit-batch"],
                                        ["gam", "update", "user", "alice@example.com", "password", "random",
                                         "changepassword", "off"]]
    [entry] = _audit(client)
    assert (entry["action"], entry["target"], entry["ok"]) == ("sequence_export", "sequence", True)
    assert entry["extra"]["commands"] == ["build.delete_group", "build.reset_password"]
    assert entry["extra"]["argvs"][0] == ["delete", "group", "old-team@example.com", "noactionifalias"]
    assert len(gam_calls()) == before                   # the export ran nothing


def test_a_step_added_after_the_preview_is_not_in_its_export(client):
    # The review's probe: a destructive step queued after Preview rode out in the file the preview's link wrote.
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "alice@example.com"})
    link = _export_link(client)
    client.post("/builder/sequence/add", data={"cid": "build.delete_group", "group": "old-team@example.com"})
    r = client.get(link)
    assert r.status_code == 200 and "delete" not in r.text
    assert [e["extra"]["commands"] for e in _audit(client)] == [["build.reset_password"]]


def test_the_export_leaves_the_preview_s_run_alone(client):
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "alice@example.com"})
    link = _export_link(client)
    assert client.get(link).status_code == 200
    token = link.split("preview=", 1)[1]
    run = client.post("/builder/sequence/run", data={"preview": token})
    assert "status?job=" in run.text                    # peeked, not spent
    assert client.get(link).status_code == 409          # the run spent it


@pytest.mark.parametrize("link", ["/builder/sequence/export.txt", "/builder/sequence/export.txt?preview=nope"])
def test_no_export_without_a_live_preview(client, link):
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "alice@example.com"})
    r = client.get(link)
    assert r.status_code == 409 and "preview" in r.text
    assert _audit(client) == []


def test_a_step_that_sets_a_password_is_refused_with_no_record(client):
    st = client.app.state.gamgui
    st.builder_sequence.append({"cid": "x", "label": "Set password", "target": "a@example.com", "risk": 1,
                                "argv": ["update", "user", "a@example.com", "password", "Hunter2!"]})
    page = client.post("/builder/sequence/preview").text
    assert "Can't export as a gam batch file" in page and "/builder/sequence/export.txt" not in page
    token = re.search(r'name="preview" value="([^"]+)"', page).group(1)
    r = client.get(f"/builder/sequence/export.txt?preview={token}")
    assert r.status_code == 409 and "password" in r.text and "Hunter2!" not in r.text
    assert _audit(client) == []


def test_no_export_without_a_connector_to_record_it(client):
    client.post("/builder/sequence/add", data={"cid": "build.delete_group", "group": "old-team@example.com"})
    link = _export_link(client)
    client.app.state.gamgui.connector = None            # no domain: the preview's is gone too, so it's refused first
    r = client.get(link)
    assert r.status_code == 409 and "gam delete" not in r.text


def test_no_file_when_the_record_can_t_be_written(client, monkeypatch):
    client.post("/builder/sequence/add", data={"cid": "build.delete_group", "group": "old-team@example.com"})
    link = _export_link(client)

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(client.app.state.gamgui.connector.audit, "record", full)
    r = client.get(link)
    assert r.status_code == 409 and "audit log" in r.text and "gam delete" not in r.text and "No space" not in r.text


def test_the_preview_says_the_file_runs_outside_the_app(client):
    client.post("/builder/sequence/add", data={"cid": "build.reset_password", "email": "alice@example.com"})
    r = client.post("/builder/sequence/preview")
    assert "outside this app" in r.text and "no audit record" in r.text
