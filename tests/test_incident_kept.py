"""A kept incident (back-port plan E4): made by hand or from an export, its evidence saved and read back, and the file handling that keeps it here. Ported from SwiftGamGui's IncidentStoreTests and
IncidentEvidenceTests."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import stat
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident import logs, pull, store
from gamgui.core.incident.store import IncidentStore, Note, Problem, Record, Review, Tick, Warning
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
HEADER = ",".join(logs.TABLE_HEADER)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _export(name="Incident logs 2026-10-20 120000", about="Incident logs exported x.\nOver the last 3 days.\nAccounts: every account.\nFrom IP address: any.\n\n", rows=()):
    csv = "\r\n".join([HEADER, *rows]) + "\r\n"
    return store.check_export(name, {"about.txt": about.encode(), "login-log.csv": csv.encode()})


ROW = "login,2026-10-19T14:00:00Z,carol@example.com,login_success,198.51.100.23,yes,,type: login | login_type: google_password"


@pytest.fixture
def kept(tmp_path) -> IncidentStore:
    return IncidentStore(tmp_path / "incidents")


def _new(kept: IncidentStore, title: str = "Carol's account") -> Record:
    return kept.create(DOMAIN, title, NOW, "admin", "An alert")


# --- the record -------------------------------------------------------------------------------------------------------------


def test_an_incident_survives_a_restart(kept, tmp_path):
    record = kept.save(replace(_new(kept), notes=(Note(NOW, "Called the bank"),), warnings=(Warning("a@b.c", "d@e.f"),), phish="p@q.r",
                               ticks=(Tick("look", "See it", "b1", "By phone", NOW, "admin"),), marks=(store.Mark(CAROL, "192.0.2.1", "her phone"),),
                               review=Review(kind="Unauthorized access", severity="High", meeting=NOW, cause="A phish", summary="s")))
    again = IncidentStore(tmp_path / "incidents")
    assert again.records == [record] and again.unreadable == []
    assert again.for_domain("EXAMPLE.com") == [record] and again.for_domain("other.example") == []


def test_the_files_are_private(kept, tmp_path):
    record = _new(kept)
    root = tmp_path / "incidents"
    assert _mode(root) == 0o700 and _mode(root / record.id) == 0o700 and _mode(root / record.id / "incident.json") == 0o600
    name = kept.keep(_export(rows=[ROW]), record.id)
    folder = root / record.id / "evidence" / name
    assert _mode(root / record.id / "evidence") == 0o700 and _mode(folder) == 0o700 and all(_mode(f) == 0o600 for f in folder.iterdir())


def test_an_id_is_never_one_kept_already(kept):
    assert len({_new(kept).id for _ in range(50)}) == 50


@pytest.mark.parametrize("bad", ["", "../x", "2026-10-20-ABCDEF", "2026-10-20-abcde", "2026-10-20-abcdef/..", "2026-10-20-abcdef\n"])
def test_an_id_that_is_not_one_reaches_no_file(kept, bad):
    assert not store.is_id(bad)
    with pytest.raises(Problem):
        kept.keep(_export(), bad)
    with pytest.raises(Problem):
        kept.exports(bad)


@pytest.mark.parametrize("change,says", [({"title": "  "}, "title"), ({"title": "x" * 201}, "title"), ({"opened_by": "x" * 101}, "name"), ({"reported": "x" * 2001}, "reported"),
                                         ({"notes": (Note(NOW, ""),)}, "note"), ({"phish": "has space@x.y"}, "picked"), ({"warnings": (Warning("a b", "c"),)}, "warning"),
                                         ({"review": Review(kind="x" * 101)}, "type"), ({"ticks": (Tick("k", "w", "b", "", NOW),)}, "how it was done")])
def test_what_is_kept_is_bounded(kept, change, says):
    with pytest.raises(Problem, match=says):
        kept.save(replace(_new(kept), **change))


def test_at_most_so_many_incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CAP", 2)
    kept = IncidentStore(tmp_path / "i")
    _new(kept), _new(kept)
    with pytest.raises(Problem, match="2 incidents"):
        _new(kept)


def test_a_note_takes_its_own_time_and_sorts(kept):
    record = _new(kept)
    kept.add_note(record.id, "Later", NOW)
    kept.add_note(record.id, " Earlier ", NOW - timedelta(hours=3))
    assert [n.text for n in kept.get(record.id).notes] == ["Earlier", "Later"]


def test_an_unreadable_record_is_moved_aside_and_named_never_saved_over(kept, tmp_path):
    record = _new(kept)
    path = tmp_path / "incidents" / record.id / "incident.json"
    path.write_text("not json")
    again = IncidentStore(tmp_path / "incidents")
    assert again.records == [] and again.unreadable == [record.id] and not path.exists()
    aside = list(path.parent.glob("incident.json.unreadable-*"))
    assert len(aside) == 1 and aside[0].read_text() == "not json"


@pytest.mark.parametrize("data", [{"version": 2}, {"version": 1, "id": "2026-10-20-000000"}, []])
def test_a_record_of_another_shape_is_unreadable(kept, tmp_path, data):
    record = _new(kept)
    (tmp_path / "incidents" / record.id / "incident.json").write_text(json.dumps(data))
    assert IncidentStore(tmp_path / "incidents").unreadable == [record.id]


def test_a_record_saying_another_id_is_refused(kept, tmp_path):
    first, second = _new(kept), _new(kept, "Second")
    a = tmp_path / "incidents" / first.id / "incident.json"
    a.write_text((tmp_path / "incidents" / second.id / "incident.json").read_text())
    assert first.id in IncidentStore(tmp_path / "incidents").unreadable


def test_a_folder_that_can_t_be_read_opens_nothing(tmp_path):
    (tmp_path / "incidents").write_text("a file")
    with pytest.raises(Problem):
        IncidentStore(tmp_path / "incidents")


def test_a_linked_store_is_refused_rather_than_followed(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "incidents").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Problem):
        IncidentStore(tmp_path / "incidents")


def test_a_linked_incident_is_no_incident(kept, tmp_path):
    record = _new(kept)
    root = tmp_path / "incidents"
    (root / record.id).rename(tmp_path / "moved")
    (root / record.id).symlink_to(tmp_path / "moved")
    assert IncidentStore(root).records == []


def test_a_linked_record_file_is_not_read(kept, tmp_path):
    record = _new(kept)
    path = tmp_path / "incidents" / record.id / "incident.json"
    secret = tmp_path / "secret"
    secret.write_text(path.read_text())
    path.unlink()
    path.symlink_to(secret)
    assert IncidentStore(tmp_path / "incidents").unreadable == [record.id]
    assert secret.exists()


def test_a_save_never_writes_through_a_linked_folder(kept, tmp_path):
    record = _new(kept)
    root = tmp_path / "incidents"
    (root / record.id).rename(tmp_path / "moved")
    (tmp_path / "attack").mkdir()
    (root / record.id).symlink_to(tmp_path / "attack")
    with pytest.raises(Problem):
        kept.save(replace(record, title="x"))
    assert list((tmp_path / "attack").iterdir()) == []


def test_a_fifo_in_the_evidence_doesn_t_hang_the_read(kept, tmp_path):
    record = _new(kept)
    name = kept.keep(_export(rows=[ROW]), record.id)
    folder = tmp_path / "incidents" / record.id / "evidence" / name
    (folder / "login-log.csv").unlink()
    os.mkfifo(folder / "login-log.csv")
    assert kept.exports(record.id) == ([], [name])


def test_a_linked_file_in_the_evidence_is_not_read(kept, tmp_path):
    record = _new(kept)
    name = kept.keep(_export(rows=[ROW]), record.id)
    folder = tmp_path / "incidents" / record.id / "evidence" / name
    (folder / "login-log.csv").unlink()
    secret = tmp_path / "secret.csv"
    secret.write_text(HEADER + "\n")
    (folder / "login-log.csv").symlink_to(secret)
    assert kept.exports(record.id) == ([], [name])
    with pytest.raises(Problem):
        list(kept.evidence_files(record.id))


# --- the evidence -----------------------------------------------------------------------------------------------------------


def test_a_saved_read_reads_back_byte_for_byte_with_its_hash(kept):
    import hashlib

    record = _new(kept)
    export = _export(rows=[ROW])
    name = kept.keep(export, record.id)
    exports, unreadable = kept.exports(record.id)
    assert unreadable == [] and [e.name for e in exports] == [name] and exports[0].logs == export.logs and exports[0].about == export.about
    files = {path: (data, sha) for path, data, sha in kept.evidence_files(record.id)}
    assert set(files) == {f"{name}/about.txt", f"{name}/login-log.csv"}
    assert all(hashlib.sha256(data).hexdigest() == sha for data, sha in files.values())


def test_a_second_read_of_one_name_is_kept_beside_it(kept):
    record = _new(kept)
    assert kept.keep(_export(), record.id) != kept.keep(_export(), record.id)
    assert len(kept.exports(record.id)[0]) == 2


@pytest.mark.parametrize("name", ["../../escape", "a/b", ".hidden", "..", "x\ny", "/etc/passwd"])
def test_a_saved_read_s_name_is_never_a_path(kept, tmp_path, name):
    record = _new(kept)
    saved = kept.keep(_export(name=name), record.id)
    assert "/" not in saved and not saved.startswith(".")
    evidence = tmp_path / "incidents" / record.id / "evidence"
    assert (evidence / saved).is_dir() and sorted(p.name for p in (tmp_path / "incidents").iterdir()) == [record.id]


def test_a_read_that_fails_partway_leaves_nothing(kept, tmp_path, monkeypatch):
    record = _new(kept)
    real = store._write_new

    def fails_second(folder, name, data):
        if name != "about.txt":
            raise OSError(28, "No space left on device")
        real(folder, name, data)

    monkeypatch.setattr(store, "_write_new", fails_second)
    with pytest.raises(Problem, match="No space"):
        kept.keep(_export(rows=[ROW]), record.id)
    assert list((tmp_path / "incidents" / record.id / "evidence").iterdir()) == []


def test_at_most_so_many_saved_reads(kept, monkeypatch):
    monkeypatch.setattr(store, "EXPORTS_CAP", 1)
    record = _new(kept)
    kept.keep(_export(), record.id)
    with pytest.raises(Problem, match="1 reads"):
        kept.keep(_export(), record.id)


def _zip(files) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


GOOD = {"about.txt": "Incident logs exported x.\n", "login-log.csv": HEADER + "\n"}


def test_an_export_zip_reads_as_its_files_and_leaves_the_rest_out():
    export = store.read_zip("Export", _zip({**GOOD, "other.txt": "x", "evil-log.csv": HEADER + "\n"}))
    assert sorted(n for n, _ in export.files) == ["about.txt", "login-log.csv"]


@pytest.mark.parametrize("files,says", [({"about.txt": "Incident logs exported x.\n"}, "needs about.txt"), ({"login-log.csv": HEADER + "\n"}, "needs about.txt"),
                                        ({"about.txt": "Something else\n", "login-log.csv": HEADER + "\n"}, "about.txt isn't"), ({**GOOD, "login-log.csv": "a,b\n"}, "isn't one of this app's")])
def test_what_is_not_an_export_is_refused(files, says):
    with pytest.raises(Problem, match=says):
        store.read_zip("x", _zip(files))


def test_a_zip_s_paths_reach_nothing(tmp_path):
    export = store.read_zip("x", _zip({**GOOD, "../../../evil-log.csv": HEADER + "\n", "/abs/about.txt": "x", "evidence/../login-log.csv": "x"}))
    assert sorted(n for n, _ in export.files) == ["about.txt", "login-log.csv"]


def test_not_a_zip_is_said():
    with pytest.raises(Problem, match="isn't a zip"):
        store.read_zip("x", b"not a zip")


def test_a_zip_bomb_is_refused_by_what_it_holds(monkeypatch):
    monkeypatch.setattr(store, "FILE_CAP", 1000)
    with pytest.raises(Problem, match="larger than"):
        store.read_zip("x", _zip({**GOOD, "login-log.csv": HEADER + "\n" + "x" * 100_000}))


def test_a_zip_lying_about_its_size_is_still_bounded(monkeypatch):
    monkeypatch.setattr(store, "FILE_CAP", 1000)
    data = bytearray(_zip({**GOOD, "login-log.csv": HEADER + "\n" + "x" * 100_000}))
    # The central directory's uncompressed size, rewritten small.
    at = data.find(b"PK\x01\x02", data.find(b"login-log.csv") - 60)
    while at >= 0 and b"login-log.csv" not in data[at:at + 80]:
        at = data.find(b"PK\x01\x02", at + 4)
    data[at + 24:at + 28] = (10).to_bytes(4, "little")
    with pytest.raises(Problem):
        store.read_zip("x", bytes(data))


def test_a_file_that_isn_t_text_is_refused():
    with pytest.raises(Problem, match="isn't text"):
        store.check_export("x", {"about.txt": b"\xff\xfe", "login-log.csv": HEADER.encode()})


# --- reading back ------------------------------------------------------------------------------------------------------------


def test_a_pull_reads_back_as_itself():
    rows = logs.rows_from((FIXTURES / "report_login.csv").read_text(), "login")
    log = pull.Log.read("login", rows, 72)
    back = logs.rows_from_export(pull.csv_of(log), "login")
    assert [(r.time, r.who, r.event, r.ip, r.flagged, r.parameters) for r in back] == [(r.time, r.who, r.event, r.ip, r.flagged, r.parameters) for r in rows]


def test_reading_back_undoes_only_what_the_export_did():
    row = logs.Row("i", "drive", "2026-10-19T00:00:00Z", None, "=cmd()", "view", "", (("subject", "a | b"), ("note", "=x")), False, "the AI agent H", "Mail Sync")
    back = logs.rows_from_export(pull.csv_of(pull.Log.read("drive", [row], 72)), "drive")[0]
    assert back.who == "=cmd()" and back.parameters == (("subject", "a | b"), ("note", "=x")) and back.agent == "the AI agent H" and back.app == "Mail Sync"
    assert logs.rows_from_export("a,b\n", "drive") is None
    assert logs.rows_from_export(f"{HEADER}\nlogin,t,w,e,i,,,\n", "drive") is None, "a row naming another log"


def test_a_saved_read_says_how_far_it_read():
    held = pull.Pull(pull.Query(("login", "gmail", "drive"), (CAROL,), 4320, "198.51.100.23"))
    through = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = logs.rows_from((FIXTURES / "report_login.csv").read_text(), "login")
    held.logs = [pull.Log.read("login", rows, 4320), pull.Log.read("gmail", [], 720), pull.Log.read("drive", rows[:0] + [], 4320, True, None)]
    held.logs[0] = pull.Log.read("login", rows, 4320, True, through)
    note = pull.About.of(pull.about(held, "now"))
    assert note.users == (CAROL,) and note.ip == "198.51.100.23" and note.hours == 4320 and not note.every_account
    assert note.windows["gmail"] == 720 and note.through["login"] == through and "login" in note.capped


def test_saved_reads_put_together():
    t = datetime(2026, 10, 19, tzinfo=timezone.utc)
    a = logs.Row("a", "login", "t", t, CAROL, "e", "", (), False)
    b = logs.Row("b", "login", "t", t + timedelta(hours=1), CAROL, "e", "", (), False)
    older = pull.Pull(pull.Query(("login",), (), 72), [pull.Log.read("login", [a], 72, True, t)], ended=True)
    newer = pull.Pull(pull.Query(("login",), (CAROL,), 240, "1.2.3.4"), [pull.Log.read("login", [a, b], 240)], ended=True)
    together = pull.merged([older, newer])
    log = together.log("login")
    assert [r.identity for r in log.rows] == ["b", "a"], "an event two reads hold, once; newest first"
    assert not log.capped and log.through is None, "one read whole: whole"
    assert together.query.users == () and together.query.hours == 240 and together.query.ip == "1.2.3.4", "every account if any was"
    both_short = pull.merged([older, pull.Pull(pull.Query(("login",), (), 72), [pull.Log.read("login", [b], 72, True, t + timedelta(days=1))], ended=True)])
    assert both_short.log("login").capped and both_short.log("login").through == t + timedelta(days=1)


def test_a_log_one_read_couldn_t_read_comes_from_the_other():
    t = datetime(2026, 10, 19, tzinfo=timezone.utc)
    good = pull.Pull(pull.Query(("login",), (), 72), [pull.Log.read("login", [logs.Row("a", "login", "t", t, CAROL, "e", "", (), False)], 72)], ended=True)
    bad = pull.Pull(pull.Query(("login",), (), 72), [pull.Log("login", None, "refused")], ended=True)
    assert pull.merged([good, bad]).log("login").rows and pull.merged([bad, good]).log("login").rows


def test_a_saved_read_of_every_account_by_an_address_is_its_sweep():
    assert pull.swept_address("Incident logs exported x.\nAccounts: every account.\nFrom IP address: 1.2.3.4.\n") == "1.2.3.4"
    assert pull.swept_address("Accounts: a@b.c.\nFrom IP address: 1.2.3.4.\n") is None
    assert pull.swept_address("From IP address: 1.2.3.4.\n") is None, "every account is said, never taken from a line left out"


def test_an_export_from_another_tenant_is_told_apart():
    theirs = pull.pull_of_export(_export(rows=["login,2026-10-19T14:00:00Z,x@other.example,login_success,,,,"]))
    assert pull.from_another_tenant(theirs, {"example.com"}) and not pull.from_another_tenant(pull.pull_of_export(_export(rows=[ROW])), {"example.com"})
    assert not pull.from_another_tenant(pull.pull_of_export(_export()), {"example.com"}), "no actors: it can't say"


# --- through the page and the mock ----------------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    st = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    st.incident_store = IncidentStore(tmp_path / "incidents")
    return st


@pytest.fixture
def client(state):
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _settle(client, state):
    job = state.jobs.get(state.incident_open_job)
    if job:
        wait_for_job(client, job)


def _open_new(client, state, title="Carol's account", **files) -> Record:
    r = client.post("/incident/kept/new", data={"title": title, "reported": "An alert", "opened_by": "admin", "opened": "2026-10-20"}, **files)
    assert r.status_code == 200
    _settle(client, state)
    return state.incident_store.get(state.incident_open)


def _pull(client, state, *wanted, accounts=CAROL, ip=""):
    client.post("/incident/logs/pull", data={"accounts": accounts, "ip": ip, "hours": 72, "log": list(wanted or ("login", "token"))})
    wait_for_job(client, state.jobs[state.incident_pull_job])


def test_a_new_incident_opens(client, state):
    record = _open_new(client, state)
    assert record.title == "Carol's account" and record.domain == DOMAIN and record.opened.strftime("%Y") == "2026"
    page = client.get("/incident/kept").text
    assert "Carol&#39;s account" in page and "Close it here" in page


def test_a_pull_while_it_s_open_is_saved_and_a_second_adds_to_it(client, state, gam_calls):
    record = _open_new(client, state)
    _pull(client, state, "login")
    assert len(state.incident_saved) == 1 and state.incident_pull.from_evidence
    first = len(state.incident_pull.log("login").rows)
    _pull(client, state, "token")
    assert len(state.incident_saved) == 2
    together = state.incident_pull
    assert together.log("login") and together.log("token") and len(together.log("login").rows) == first, "a narrower read never takes away what the wider one found"
    exports, unreadable = state.incident_store.exports(record.id)
    assert len(exports) == 2 and not unreadable


def test_a_pull_is_saved_without_reading_every_saved_one_again(client, state, monkeypatch):
    _open_new(client, state)
    _pull(client, state, "login")
    monkeypatch.setattr(IncidentStore, "exports", lambda *a: pytest.fail("each pull re-read every saved one"))
    _pull(client, state, "token")


def test_what_is_added_in_memory_is_what_reading_it_back_gives(client, state):
    record = _open_new(client, state)
    _pull(client, state, "login")
    _pull(client, state, "login", "token")
    shown = state.incident_pull
    back, _ = pull.evidence(state.incident_store.exports(record.id)[0])
    for application in ("login", "token"):
        assert [r.identity for r in shown.log(application).rows] == [r.identity for r in back.log(application).rows]


def test_it_reopens_without_reading_google(client, state, tmp_path, gam_calls):
    record = _open_new(client, state)
    _pull(client, state, "login")
    rows = len(state.incident_pull.log("login").rows)
    # A restart: a new state on the same folder.
    state.incident_open = ""
    state.incident_pull = None
    state.incident_store = IncidentStore(tmp_path / "incidents")
    before = len(gam_calls())
    client.get("/incident/kept")
    _settle(client, state)
    assert state.incident_open == record.id and len(state.incident_pull.log("login").rows) == rows
    assert not [a for a in gam_calls()[before:] if a[0] == "report"]


def test_read_the_logs_reads_from_the_saved_evidence(client, state):
    _open_new(client, state)
    _pull(client, state, "login", "token", "admin", accounts="")
    assert "Where each account stands" in client.get("/incident/logs/reading").text


def test_a_new_incident_takes_the_pull_just_made(client, state):
    _pull(client, state, "login")
    record = _open_new(client, state)
    assert len(state.incident_store.exports(record.id)[0]) == 1 and state.incident_pull.log("login").rows


def test_what_is_shown_from_the_evidence_is_never_saved_again(client, state):
    first = _open_new(client, state)
    _pull(client, state, "login")
    second = _open_new(client, state, "Another")
    assert second.id != first.id and state.incident_store.exports(second.id)[0] == []


def test_a_pull_that_can_t_be_saved_is_still_shown_and_said(client, state, monkeypatch):
    _open_new(client, state)

    def full(*args, **kwargs):
        raise Problem("no room")

    monkeypatch.setattr(IncidentStore, "keep", full)
    _pull(client, state, "login")
    assert state.incident_pull.log("login").rows and not state.incident_pull.from_evidence
    assert "This pull wasn&#39;t saved to the incident: no room" in client.get(f"/incident/logs/status?job={state.incident_pull_job}").text


def test_an_export_comes_in_byte_for_byte(client, state):
    record = _open_new(client, state)
    data = _zip({"about.txt": "Incident logs exported x.\nOver the last 3 days.\nAccounts: every account.\nFrom IP address: any.\n", "login-log.csv": f"{HEADER}\r\n{ROW}\r\n"})
    r = client.post("/incident/kept/import", files={"export": ("Saved read.zip", data, "application/zip")})
    assert r.status_code == 200
    _settle(client, state)
    assert state.incident_saved == ["Saved read"] and state.incident_pull.log("login").rows
    files = {p: d for p, d, _ in state.incident_store.evidence_files(record.id)}
    assert files["Saved read/login-log.csv"] == f"{HEADER}\r\n{ROW}\r\n".encode()


def test_an_export_from_another_tenant_is_refused(client, state):
    record = _open_new(client, state)
    data = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\r\nlogin,2026-10-19T14:00:00Z,x@other.example,e,,,,\r\n"})
    page = client.post("/incident/kept/import", files={"export": ("x.zip", data, "application/zip")}).text
    assert "another domain" in page and state.incident_store.exports(record.id)[0] == []


def test_a_new_incident_from_an_export(client, state):
    data = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\r\n{ROW}\r\n"})
    record = _open_new(client, state, files={"export": ("Saved.zip", data, "application/zip")})
    assert [e.name for e in state.incident_store.exports(record.id)[0]] == ["Saved"]


def test_a_saved_read_that_can_t_be_read_is_named_and_the_rest_shown(client, state, tmp_path):
    record = _open_new(client, state)
    _pull(client, state, "login")
    name = state.incident_saved[0]
    bad = tmp_path / "incidents" / record.id / "evidence" / "zzz broken"
    bad.mkdir()
    (bad / "about.txt").write_text("nope")
    client.post("/incident/kept/open", data={"incident": record.id})
    _settle(client, state)
    assert state.incident_saved == [name] and state.incident_saved_unreadable == ["zzz broken"]
    assert "zzz broken" in client.get("/incident/kept").text and state.incident_pull.log("login").rows


def test_notes_and_details_are_saved(client, state):
    record = _open_new(client, state)
    client.post("/incident/kept/note", data={"text": "Told the insurer", "at": "2026-10-20T09:00"})
    client.post("/incident/kept/details", data={"title": "New title", "reported": "A person", "opened_by": "me", "opened": "2026-10-19T08:00"})
    kept = state.incident_store.get(record.id)
    assert [n.text for n in kept.notes] == ["Told the insurer"] and kept.title == "New title" and kept.reported == "A person"
    assert "That incident" not in client.post("/incident/kept/note", data={"text": "x" * 2001}).text and len(state.incident_store.get(record.id).notes) == 1


def test_another_domain_s_incident_doesn_t_open(client, state):
    other = state.incident_store.create("other.example", "Theirs", NOW)
    page = client.post("/incident/kept/open", data={"incident": other.id}).text
    assert "isn&#39;t one of this domain" in page and not state.incident_open
    assert "Theirs" not in client.get("/incident/kept").text


def test_it_closes_and_goes_with_the_connection(client, state):
    _open_new(client, state)
    client.post("/incident/kept/close")
    assert not state.incident_open and state.incident_store.remembered_open() == ""
    _open_new(client, state)
    state.activate(state.connector)
    assert not state.incident_open and state.incident_evidence is None


def test_nothing_changes_while_a_pull_is_saving_where_it_was_meant(client, state):
    from gamgui.web.jobs import start_job

    _open_new(client, state)
    state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
    assert "A pull of the logs is going on" in client.post("/incident/kept/close").text and state.incident_open
    state.jobs[state.incident_pull_job].finish()


def test_what_a_person_typed_is_only_ever_text(client, state):
    _open_new(client, state, title="<script>x</script>")
    page = client.get("/incident/kept").text
    assert "<script>x</script>" not in page and "&lt;script&gt;x" in page


def _no_log(gam_state, application: str) -> None:
    """The edition lacks this log: GAM's refusal (the mock's nolog)."""
    (gam_state / "nolog").mkdir(exist_ok=True)
    (gam_state / "nolog" / application).write_text("")


def test_a_log_this_pull_couldn_t_read_is_still_said_once_the_evidence_shows(client, state, gam_state):
    _no_log(gam_state, "token")
    _open_new(client, state)
    _pull(client, state, "login", "token")
    assert state.incident_pull.log("login").rows and state.incident_pull.log("token").rows is None
    assert state.incident_evidence.log("token") is None, "the evidence stays what reading it back gives"
    assert "couldn't be read" in client.get("/incident/logs").text


def test_a_log_the_evidence_holds_and_this_pull_couldn_t_read_keeps_its_rows_and_says_so(client, state, gam_state):
    _open_new(client, state)
    _pull(client, state, "token")
    rows = len(state.incident_pull.log("token").rows)
    _no_log(gam_state, "token")
    _pull(client, state, "login", "token")
    assert len(state.incident_pull.log("token").rows) == rows and state.incident_pull.log("login").rows
    assert "This pull didn&#39;t read the" in client.get(f"/incident/logs/status?job={state.incident_pull_job}").text


def test_a_pull_that_read_nothing_leaves_the_evidence_shown(client, state, gam_state):
    _open_new(client, state)
    _pull(client, state, "login")
    _no_log(gam_state, "login")
    _pull(client, state, "login")
    assert state.incident_pull.log("login").rows and len(state.incident_saved) == 1


def test_read_the_rest_is_saved_and_reads_back_whole(client, state):
    record = _open_new(client, state)
    state.incident_store.keep(_export(about=(
        "Incident logs exported x.\nOver the 3 days up to 2026-10-19T00:00:00Z.\nAccounts: carol@example.com.\nFrom IP address: any.\n\n"
        "login-log.csv: the Sign-ins (user log) log, 1 events; read completely through 2026-10-19T14:00:00Z, later events weren't read.\n"), rows=[ROW]), record.id)
    client.post("/incident/kept/open", data={"incident": record.id})
    _settle(client, state)
    stopped = state.incident_pull.log("login")
    assert stopped.capped and stopped.through
    client.post("/incident/logs/rest", data={"application": "login"})
    wait_for_job(client, state.jobs[state.incident_pull_job])
    assert len(state.incident_saved) == 2 and not state.incident_pull.log("login").capped
    back, _ = pull.evidence(state.incident_store.exports(record.id)[0])
    assert not back.log("login").capped and [r.identity for r in back.log("login").rows] == [r.identity for r in state.incident_pull.log("login").rows]


def test_a_pull_waits_for_the_evidence_it_adds_to(client, state):
    record = _open_new(client, state)
    _pull(client, state, "login")
    # Reopened, and a pull ending while that's still being read: both saved reads are shown.
    client.post("/incident/kept/open", data={"incident": record.id})
    _pull(client, state, "token")
    _settle(client, state)
    assert state.incident_pull.log("login").rows and state.incident_pull.log("token").rows and len(state.incident_saved) == 2


def test_a_read_named_before_those_saved_reads_them_all_back(client, state, monkeypatch):
    record = _open_new(client, state)
    state.incident_store.keep(_export(name="zzz Saved", rows=[ROW]), record.id)
    client.post("/incident/kept/open", data={"incident": record.id})
    _settle(client, state)
    _pull(client, state, "token")
    assert state.incident_saved[-1] == "zzz Saved" and len(state.incident_saved) == 2, "in the order reading them back gives"
    assert state.incident_pull.log("token").rows and state.incident_pull.log("login").rows


def test_opened_by_starts_as_the_admin_signed_in_and_a_refusal_leaves_it_empty(client, state, monkeypatch):
    monkeypatch.setattr(state.vault, "oauth_admin_email", lambda domain: "admin@example.com")
    assert 'value="admin@example.com"' in client.get("/incident/kept").text

    def refused(domain):
        raise OSError("keychain")

    monkeypatch.setattr(state.vault, "oauth_admin_email", refused)
    assert client.get("/incident/kept").status_code == 200


# --- what the adversarial check of the file handling proved ------------------------------------------------------------------


def test_a_hostile_about_line_reads_quickly():
    # A pattern over this line backtracked for seconds on a few KB, holding the GIL: a 734-byte zip froze the app at every open.
    started = time.monotonic()
    line = "a-log.csv: the " + " log, 1 events" * 40_000
    assert pull.About.of(f"Incident logs exported x.\n{line}\n").capped == frozenset()
    cell = " | ".join(["x"] * 200_000)
    assert len(logs.parameters_from_cell("a: b | " + cell)[0][1]) > 100_000
    assert time.monotonic() - started < 1


def test_about_txt_is_bounded():
    with pytest.raises(Problem, match="about.txt is larger"):
        store.check_export("x", {"about.txt": b"Incident logs exported " + b"x" * store.ABOUT_CAP, "login-log.csv": HEADER.encode()})


def test_a_saved_read_is_held_to_so_many_events(monkeypatch):
    monkeypatch.setattr(pull, "EXPORT_ROWS", 2)
    two = store.check_export("x", {"about.txt": b"Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n{ROW}\n".encode(), "token-log.csv": HEADER.encode() + b"\n"})
    assert pull.pull_of_export(two) is not None
    three = store.check_export("x", {"about.txt": b"Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n{ROW}\n".encode(),
                                     "token-log.csv": f"{HEADER}\n{ROW.replace('login,', 'token,', 1)}\n".encode()})
    assert pull.pull_of_export(three) is None, "all its logs together"


@pytest.mark.parametrize("line", ["Over the " + "9" * 5000 + " hours.", "login-log.csv: the Sign-ins log, 1 events, the last " + "9" * 5000 + " hours (the most it reads).",
                                  "login-log.csv: the Sign-ins log, 1 events; read completely through 0001-01-01T00:00:00+14:00, later events weren't read.",
                                  "login-log.csv: the Sign-ins log, 1 events; read completely through 9999-12-31T23:59:59-14:00, later events weren't read.",
                                  "From 0001-01-01T00:00:00+14:00 to 9999-12-31T23:59:59-14:00."])
def test_a_hostile_about_never_hides_the_incident(client, state, line):
    record = _open_new(client, state)
    state.incident_store.keep(_export(name="a good one", rows=[ROW]), record.id)
    state.incident_store.keep(_export(name="b hostile", about=f"Incident logs exported x.\n{line}\n", rows=[ROW.replace("14:00", "15:00")]), record.id)
    client.post("/incident/kept/open", data={"incident": record.id})
    _settle(client, state)
    assert state.incident_evidence is not None and not state.jobs[state.incident_open_job].error
    for path in ("/incident/logs", f"/incident/logs/status?job={state.incident_open_job}", "/incident/logs/export.zip", "/incident/kept"):
        assert client.get(path).status_code == 200, path


def test_one_saved_read_failing_any_way_leaves_the_rest_shown(monkeypatch):
    real = pull.About.of

    def fails(text):
        if "hostile" in text:
            raise RecursionError("whatever it was")
        return real(text)

    monkeypatch.setattr(pull.About, "of", fails)
    together, broken = pull.evidence([_export(name="good", rows=[ROW]), _export(name="bad", about="Incident logs exported hostile.\n", rows=[ROW])])
    assert broken == ["bad"] and together.log("login").rows


def test_a_read_that_can_t_be_read_never_says_an_address_was_swept(client, state, tmp_path):
    record = _open_new(client, state)
    bad = tmp_path / "incidents" / record.id / "evidence" / "z bad"
    bad.mkdir(parents=True)
    (bad / "about.txt").write_text("Incident logs exported x.\nAccounts: every account.\nFrom IP address: 198.51.100.23.\n")
    (bad / "login-log.csv").write_text(f"{HEADER}\nlogin,only,three\n")
    client.post("/incident/kept/open", data={"incident": record.id})
    _settle(client, state)
    assert state.incident_saved_unreadable == ["z bad"] and state.incident_swept == set()


def test_a_read_is_written_where_readers_skip_it_until_it_is_whole(kept, tmp_path, monkeypatch):
    record = _new(kept)
    evidence = tmp_path / "incidents" / record.id / "evidence"
    seen = []
    real = store._write_new

    def looks(folder, name, data):
        seen.append(sorted(p.name for p in evidence.iterdir()))
        assert kept.exports(record.id) == ([], [])
        real(folder, name, data)

    monkeypatch.setattr(store, "_write_new", looks)
    name = kept.keep(_export(rows=[ROW]), record.id)
    assert seen and all(len(names) == 1 and names[0].startswith(".saving-") for names in seen)
    assert [p.name for p in evidence.iterdir()] == [name] and len(kept.exports(record.id)[0]) == 1


def test_what_a_crash_left_is_no_evidence_and_the_next_save_clears_it(kept, tmp_path):
    record = _new(kept)
    kept.keep(_export(name="first", rows=[ROW]), record.id)
    evidence = tmp_path / "incidents" / record.id / "evidence"
    left = evidence / ".saving-abc"
    left.mkdir()
    (left / "about.txt").write_text("Incident logs exported x.\n")
    (left / "login-log.csv").write_text(HEADER + "\n")
    assert [e.name for e in kept.exports(record.id)[0]] == ["first"] and kept.names(record.id) == ["first"]
    kept.keep(_export(name="second", rows=[ROW]), record.id)
    assert sorted(p.name for p in evidence.iterdir()) == ["first", "second"]


def test_a_taken_name_is_never_written_over_even_an_empty_folder_or_a_link(kept, tmp_path):
    record = _new(kept)
    kept.keep(_export(name="first", rows=[ROW]), record.id)
    evidence = tmp_path / "incidents" / record.id / "evidence"
    (evidence / "empty").mkdir()
    (evidence / "linked").symlink_to(tmp_path / "nowhere")
    assert kept.keep(_export(name="empty", rows=[ROW]), record.id) == "empty (2)"
    assert kept.keep(_export(name="linked", rows=[ROW]), record.id) == "linked (2)"
    assert not (tmp_path / "nowhere").exists()


def test_a_folder_sync_failing_after_the_rename_isn_t_a_failed_save(kept, tmp_path, monkeypatch):
    record = _new(kept)
    real = os.fsync

    def refuses_folders(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        real(fd)

    monkeypatch.setattr(store.os, "fsync", refuses_folders)
    assert kept.save(replace(record, title="New title")).title == "New title"
    assert kept.get(record.id).title == "New title" and IncidentStore(tmp_path / "incidents").get(record.id).title == "New title"


@pytest.mark.parametrize("data", [b"[" * 200_000, b'{"version": 1, "id": "%ID%", "domain": "example.com", "title": "\\ud800", "opened": "2026-10-20T12:00:00+00:00", "openedBy": "", "reported": ""}',
                                  b'{"version": 1, "id": "%ID%", "domain": "example.com", "title": "x", "opened": "0001-01-01T00:00:00+14:00", "openedBy": "", "reported": ""}',
                                  b'{"version": 1, "id": "%ID%", "domain": "example.com", "title": "x", "opened": "2026-10-20T12:00:00+00:00", "openedBy": "", "reported": "", "notes": [{"at": "9999-12-31T23:59:59-14:00", "text": "n"}]}'])
def test_a_hostile_record_is_moved_aside_and_the_screens_stay_up(client, state, tmp_path, data):
    record = _open_new(client, state)
    client.post("/incident/kept/close")
    (tmp_path / "incidents" / record.id / "incident.json").write_bytes(data.replace(b"%ID%", record.id.encode()))
    state.incident_store = IncidentStore(tmp_path / "incidents")
    assert state.incident_store.unreadable == [record.id]
    assert client.get("/incident/kept").status_code == 200 and client.get("/incident/logs").status_code == 200


def test_moved_aside_is_said_at_every_launch(kept, tmp_path):
    record = _new(kept)
    (tmp_path / "incidents" / record.id / "incident.json").write_text("not json")
    assert IncidentStore(tmp_path / "incidents").unreadable == [record.id]
    assert IncidentStore(tmp_path / "incidents").unreadable == [record.id], "else the incident and its evidence vanish without a word"


def _corrupt_deflate() -> bytes:
    data = bytearray(_zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": HEADER + "\n" + "abcdefgh12345678" * 400}))
    at = data.find(b"login-log.csv") + len(b"login-log.csv")
    data[at:at + 40] = bytes(b ^ 0xFF for b in data[at:at + 40])
    return bytes(data)


def test_whatever_a_zip_does_wrong_is_said_in_words():
    with pytest.raises(Problem):
        store.read_zip("x", _corrupt_deflate())
    good = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n"})
    random = Random(20261106)  # noqa: S311 - seeded so a failure repeats
    for _ in range(1500):
        data = bytearray(good)
        for _ in range(random.randint(1, 6)):
            data[random.randrange(len(data))] = random.randrange(256)
        try:
            store.read_zip("x", bytes(data))
        except Problem:
            pass


def test_a_member_with_an_empty_name_is_said_in_words(monkeypatch):
    # Python 3.10's ZipInfo.is_dir() raises IndexError on it; the fuzz above found that on CI's oldest Python.
    good = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n"})
    real = zipfile.ZipInfo.is_dir

    def raises(info):
        if info.filename == "login-log.csv":
            raise IndexError("string index out of range")
        return real(info)

    monkeypatch.setattr(zipfile.ZipInfo, "is_dir", raises)
    with pytest.raises(Problem, match="couldn't be read"):
        store.read_zip("x", good)


def test_a_corrupt_zip_is_refused_on_the_screen(client, state):
    _open_new(client, state)
    r = client.post("/incident/kept/import", files={"export": ("x.zip", _corrupt_deflate(), "application/zip")})
    assert r.status_code == 200 and "wasn&#39;t read from the zip" in r.text


def test_an_export_that_doesn_t_read_back_is_refused(client, state):
    record = _open_new(client, state)
    data = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\nlogin,2026-10-19T14:00:00Z,x@other.example,e,,,,\nlogin,only,three\n"})
    page = client.post("/incident/kept/import", files={"export": ("x.zip", data, "application/zip")}).text
    assert "can&#39;t be read back" in page and state.incident_store.names(record.id) == []


def test_what_this_app_exports_always_reads_back():
    row = logs.Row("i", "drive", "2026-10-19T00:00:00Z", None, CAROL, "view", "", (("doc_title", "x" * 200_000),), False)
    back = logs.rows_from_export(pull.csv_of(pull.Log.read("drive", [row], 72)), "drive")
    assert back and back[0].parameters == row.parameters
    assert csv.field_size_limit() == 131072, "the limit goes back"


@pytest.mark.parametrize("bad", ["0001-01-01", "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59", "9999-12-31T23:59:59-14:00", "not a date", "0026-11-05T09:00"])
def test_a_date_that_can_t_be_kept_is_said_not_swapped_for_now(client, state, bad):
    record = _open_new(client, state)
    assert "isn&#39;t one an incident can keep" in client.post("/incident/kept/note", data={"text": "x", "at": bad}).text
    assert "isn&#39;t one an incident can keep" in client.post("/incident/kept/details", data={"title": "New", "opened": bad}).text
    assert "isn&#39;t one an incident can keep" in client.post("/incident/kept/new", data={"title": "Another", "opened": bad}).text
    held = state.incident_store.get(record.id)
    assert not held.notes and held.title != "New" and len(state.incident_store.records) == 1


def test_a_new_incident_whose_logs_can_t_be_saved_is_open_and_says_so(client, state, monkeypatch):
    _pull(client, state, "login")

    def full(*args, **kwargs):
        raise Problem("no room")

    monkeypatch.setattr(IncidentStore, "keep", full)
    page = client.post("/incident/kept/new", data={"title": "One"}).text
    _settle(client, state)
    assert "its logs weren&#39;t saved to it: no room" in page and "Add an export" in page
    assert len(state.incident_store.records) == 1 and state.incident_open


# Each of these forces one interleaving by doing the other thing inside the awaited step.


def _connect_elsewhere(state):
    state.activate(GAMConnector(runner=state.runner, domain="other.example", audit=state.connector.audit))


def test_a_connection_change_during_an_import_adds_nothing(client, state, monkeypatch):
    record = _open_new(client, state)
    real = store.read_zip

    def switches(name, data):
        export = real(name, data)
        _connect_elsewhere(state)
        return export

    monkeypatch.setattr(store, "read_zip", switches)
    data = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n"})
    assert "nothing was added" in client.post("/incident/kept/import", files={"export": ("x.zip", data, "application/zip")}).text
    assert state.incident_store.names(record.id) == [] and not state.incident_open


def test_a_connection_change_during_a_new_incident_doesn_t_open_it(client, state, monkeypatch):
    real = IncidentStore.create

    def switches(self, *args, **kwargs):
        made = real(self, *args, **kwargs)
        _connect_elsewhere(state)
        return made

    monkeypatch.setattr(IncidentStore, "create", switches)
    page = client.post("/incident/kept/new", data={"title": "One"}).text
    assert "is kept" in page and not state.incident_open and len(state.incident_store.records) == 1


def test_a_pull_started_during_a_new_incident_stays_with_the_one_open(client, state, monkeypatch):
    from gamgui.web.jobs import start_job

    first = _open_new(client, state)
    real = IncidentStore.create

    def starts_a_pull(self, *args, **kwargs):
        state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
        return real(self, *args, **kwargs)

    monkeypatch.setattr(IncidentStore, "create", starts_a_pull)
    assert "is kept" in client.post("/incident/kept/new", data={"title": "Two"}).text
    assert state.incident_open == first.id and len(state.incident_store.records) == 2
    state.jobs[state.incident_pull_job].finish()


@pytest.mark.parametrize("then", ["closed", "opened another"])
def test_an_import_finishing_never_undoes_what_was_done_meanwhile(client, state, monkeypatch, then):
    first = _open_new(client, state)
    second = state.incident_store.create(DOMAIN, "Second", NOW)
    real = IncidentStore.keep

    def meanwhile(self, export, record_id):
        state.incident_open = "" if then == "closed" else second.id
        return real(self, export, record_id)

    monkeypatch.setattr(IncidentStore, "keep", meanwhile)
    data = _zip({"about.txt": "Incident logs exported x.\n", "login-log.csv": f"{HEADER}\n{ROW}\n"})
    client.post("/incident/kept/import", files={"export": ("x.zip", data, "application/zip")})
    assert state.incident_open == ("" if then == "closed" else second.id) and state.incident_store.names(first.id) == ["x"]


def test_a_pull_is_saved_to_the_incident_open_when_it_started(client, state):
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.incident_logs import _keep

    first = _open_new(client, state)
    second = state.incident_store.create(DOMAIN, "Second", NOW)
    client.post("/incident/kept/open", data={"incident": second.id})
    _settle(client, state)
    read = pull.Pull(pull.Query(("login",), (CAROL,), 72), [pull.Log.read("login", logs.rows_from_export(f"{HEADER}\n{ROW}\n", "login"), 72)], ended=True)
    job = start_job(state.jobs, 1, kind="incident_logs", title="x")
    asyncio.run(_keep(job, state, state.connector, read, first.id))
    job.finish()
    assert len(state.incident_store.names(first.id)) == 1 and state.incident_store.names(second.id) == []
    assert state.incident_open == second.id and state.incident_pull is None, "shown only where it was saved"


def test_a_pull_made_after_a_restart_is_saved_to_the_remembered_incident(client, state, tmp_path):
    record = _open_new(client, state)
    state.incident_open = ""
    state.incident_store = IncidentStore(tmp_path / "incidents")
    _pull(client, state, "login")
    assert state.incident_open == record.id and len(state.incident_store.names(record.id)) == 1


def test_nothing_reopens_over_a_pull_going_on(client, state):
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.incident_kept import reopen_remembered

    _open_new(client, state)
    state.incident_open = ""
    state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
    reopen_remembered(state)
    assert not state.incident_open
    state.jobs[state.incident_pull_job].finish()


def test_an_export_added_during_a_pull_is_in_what_the_pull_then_shows(client, state):
    from gamgui.web.jobs import start_job
    from gamgui.web.routes.incident_logs import _keep

    record = _open_new(client, state)
    state.incident_store.keep(_export(name="added meanwhile", rows=[ROW]), record.id)     # beside it, unknown to the screen
    read = pull.Pull(pull.Query(("token",), (CAROL,), 72), [pull.Log.read("token", logs.rows_from_export(HEADER + "\n", "token"), 72)], ended=True)
    job = start_job(state.jobs, 1, kind="incident_logs", title="x")
    asyncio.run(_keep(job, state, state.connector, read, record.id))
    job.finish()
    assert "added meanwhile" in state.incident_saved and state.incident_pull.log("login").rows


def test_an_empty_incident_leaves_a_pull_that_wasn_t_saved_but_not_another_s_evidence(client, state):
    _pull(client, state, "login")
    empty = state.incident_store.create(DOMAIN, "Empty", NOW)
    fresh = state.incident_pull
    client.post("/incident/kept/open", data={"incident": empty.id})
    _settle(client, state)
    assert state.incident_pull is fresh
    full = _open_new(client, state, "Full")
    assert state.incident_pull.from_evidence and full.id != empty.id
    client.post("/incident/kept/open", data={"incident": empty.id})
    _settle(client, state)
    assert state.incident_pull is None
