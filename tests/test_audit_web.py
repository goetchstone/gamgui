from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog, generation_paths, iter_records, read_records, rolled_path
from gamgui.core.calendar_index import CalendarIndex
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Mirrors tests/test_users_web.py's client fixture, but keeps the audit path handy so tests
    can seed records the /audit routes will actually read (the route resolves the path from
    ``connector.audit.path``, same object this fixture constructs)."""
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(audit_path))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    c = TestClient(create_app(state))
    c.get("/?token=t")
    c.audit_path = audit_path  # type: ignore[attr-defined]
    return c


def _seed(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _hx_get(body: str, label: str) -> str:
    """The URL htmx would request for the button rendered as ``label`` (entities unescaped)."""
    m = re.search(r'hx-get="([^"]+)"[^>]*>\s*' + re.escape(label) + r'\s*</button>', body)
    assert m, f"no {label!r} button in the rendered rows"
    return html.unescape(m.group(1))


# --- unit: read_records ------------------------------------------------------------------

def test_read_records_newest_first_and_skips_malformed(tmp_path):
    path = tmp_path / "audit.jsonl"
    lines = [
        json.dumps({"ts": "2026-06-23T15:00:00+00:00", "action": "set_vacation", "target": "a@example.com", "ok": True}),
        "not json at all",
        "",
        json.dumps({"ts": "2026-06-23T15:01:00+00:00", "action": "suspend", "target": "b@example.com", "ok": False, "extra": {"error": "boom"}}),
        json.dumps({"ts": "2026-06-23T15:02:00+00:00", "action": "delete_user", "target": "c@example.com", "ok": True}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = read_records(path)
    assert len(records) == 3
    # most-recent-first
    assert [r["action"] for r in records] == ["delete_user", "suspend", "set_vacation"]
    failures = [r for r in records if r.get("ok") is False]
    assert len(failures) == 1
    assert failures[0]["extra"]["error"] == "boom"


def test_read_records_missing_file_returns_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []


def test_read_records_respects_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path, [{"ts": str(i), "action": "noop", "ok": True} for i in range(10)])
    records = read_records(path, limit=3)
    assert len(records) == 3
    assert records[0]["ts"] == "9"  # newest first


def test_read_records_uncapped_by_default(tmp_path):
    # The old 2000 cap silently dropped history; the default now reads everything.
    path = tmp_path / "audit.jsonl"
    _seed(path, [{"ts": str(i), "action": "noop", "ok": True} for i in range(2500)])
    records = read_records(path)
    assert len(records) == 2500
    assert records[-1]["ts"] == "0"


def test_limited_read_tails_the_newest_records_across_chunks(tmp_path):
    # The tail read seeks back from EOF in 64K chunks — a log bigger than one chunk must still
    # come back newest-first with no record split across the seam.
    path = tmp_path / "audit.jsonl"
    _seed(path, [{"ts": str(i), "action": "noop", "target": "x" * 200, "ok": True} for i in range(2000)])
    assert path.stat().st_size > 64 * 1024
    records = read_records(path, limit=5)
    assert [r["ts"] for r in records] == ["1999", "1998", "1997", "1996", "1995"]


def test_tail_returns_newest_records_oldest_first(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(10):
        log.record("noop", target=f"u{i}@example.com", ok=True)
    entries = log.tail(limit=3)
    assert [e["target"] for e in entries] == ["u7@example.com", "u8@example.com", "u9@example.com"]


# --- rotation ------------------------------------------------------------------------------

def _rotating_log(path: Path, tmp_path: Path, generation: int = 12) -> AuditLog:
    """An AuditLog whose threshold is ``generation`` records wide, measured from a real record."""
    probe = tmp_path / "probe.jsonl"
    AuditLog(probe).record("noop", target="u0@example.com", ok=True)
    return AuditLog(path, max_bytes=probe.stat().st_size * generation)


def test_rotation_rolls_to_generation_1_with_0600(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _rotating_log(path, tmp_path)
    for i in range(20):
        log.record("noop", target=f"u{i}@example.com", ok=True)

    rolled = rolled_path(path)
    assert rolled.exists()
    assert os.stat(rolled).st_mode & 0o777 == 0o600
    assert os.stat(path).st_mode & 0o777 == 0o600
    # rotation must not lose or reorder anything: both generations, still newest-first
    targets = [r["target"] for r in read_records(path)]
    assert targets == [f"u{i}@example.com" for i in reversed(range(20))]


def test_repeated_rotation_shifts_generations_instead_of_overwriting(tmp_path):
    # Rolling always into .1 would destroy the previous generation on every roll but the first.
    path = tmp_path / "audit.jsonl"
    log = _rotating_log(path, tmp_path)
    for i in range(40):
        log.record("noop", target=f"u{i}@example.com", ok=True)

    assert rolled_path(path, 2).exists()  # more than one roll happened
    targets = [r["target"] for r in read_records(path)]
    assert targets == [f"u{i}@example.com" for i in reversed(range(40))]


def test_every_surviving_generation_is_0600(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = _rotating_log(path, tmp_path)
    for i in range(40):
        log.record("noop", target=f"u{i}@example.com", ok=True)

    generations = generation_paths(path)
    assert len(generations) >= 4  # live file + at least three rolled
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in generations)


def test_iter_records_snapshots_generations_against_a_roll_mid_read(tmp_path):
    # A roll landing mid-stream must not make the reader re-emit the generation it just drained
    # (now .1) and skip the one before it.
    path = tmp_path / "audit.jsonl"
    log = _rotating_log(path, tmp_path)
    for i in range(20):
        log.record("noop", target=f"u{i}@example.com", ok=True)
    assert rolled_path(path, 1).exists()

    stream = iter_records(path)
    first = next(stream)  # generations are pinned here, before the roll below
    AuditLog(path, max_bytes=1).record("noop", target="rolled@example.com", ok=True)

    targets = [first["target"]] + [r["target"] for r in stream]
    assert targets == [f"u{i}@example.com" for i in reversed(range(20))]


# --- routes -------------------------------------------------------------------------------

def test_audit_page_renders_seeded_records(client):
    _seed(client.audit_path, [
        {"ts": "2026-06-23T15:02:51+00:00", "connector": "google_workspace", "action": "set_vacation",
         "target": "alice@example.com", "argv": ["user", "alice@example.com"], "exit_code": 0, "ok": True, "actor": None},
        {"ts": "2026-06-23T15:03:00+00:00", "connector": "google_workspace", "action": "suspend",
         "target": "bob@example.com", "argv": ["update", "user", "bob@example.com"], "exit_code": 1, "ok": False,
         "actor": None, "extra": {"error": "Permission denied", "tolerated": False}},
    ])
    r = client.get("/audit")
    assert r.status_code == 200
    assert "Audit log" in r.text
    assert "2 actions logged" in r.text
    assert "1 failure" in r.text
    assert "alice@example.com" in r.text
    assert "bob@example.com" in r.text
    assert "Failed" in r.text


def test_audit_rows_failed_filter(client):
    _seed(client.audit_path, [
        {"ts": "t1", "action": "set_vacation", "target": "alice@example.com", "ok": True},
        {"ts": "t2", "action": "suspend", "target": "bob@example.com", "ok": False, "extra": {"error": "boom"}},
    ])
    r = client.get("/audit/rows", params={"failed": 1})
    assert r.status_code == 200
    assert "bob@example.com" in r.text
    assert "alice@example.com" not in r.text


def test_audit_rows_query_filter(client):
    _seed(client.audit_path, [
        {"ts": "t1", "action": "set_vacation", "target": "alice@example.com", "ok": True},
        {"ts": "t2", "action": "suspend", "target": "bob@example.com", "ok": False, "extra": {"error": "boom"}},
    ])
    r = client.get("/audit/rows", params={"q": "vacation"})
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "bob@example.com" not in r.text


def test_audit_rows_pager_params_with_the_filter_off(client):
    # The pager spells "failures-only is off" as an empty value; that must not 422.
    _seed(client.audit_path, [{"ts": "t1", "action": "suspend", "target": "alice@example.com", "ok": True}])
    r = client.get("/audit/rows?q=&failed=&page=1")
    assert r.status_code == 200
    assert "alice@example.com" in r.text


def test_clear_filters_link_works(client):
    _seed(client.audit_path, [
        {"ts": "t1", "action": "set_vacation", "target": "alice@example.com", "ok": True},
        {"ts": "t2", "action": "suspend", "target": "bob@example.com", "ok": False},
    ])
    empty = client.get("/audit/rows", params={"q": "nothing-matches-this"})
    assert "No matches." in empty.text

    cleared = client.get(_hx_get(empty.text, "Clear filters"))
    assert cleared.status_code == 200
    assert "alice@example.com" in cleared.text and "bob@example.com" in cleared.text


def test_pager_next_link_reaches_older_records(client):
    _seed(client.audit_path, [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "set_signature",
                               "target": f"u{i:03d}@example.com", "ok": True} for i in range(60)])
    page1 = client.get("/audit/rows")
    assert page1.status_code == 200
    assert "u059@example.com" in page1.text and "u000@example.com" not in page1.text

    page2 = client.get(_hx_get(page1.text, "Next"))
    assert page2.status_code == 200
    assert "u034@example.com" in page2.text and "u059@example.com" not in page2.text

    page3 = client.get(_hx_get(page2.text, "Next"))
    assert page3.status_code == 200
    assert "u000@example.com" in page3.text  # the pager reaches the oldest record


def test_pager_next_link_keeps_the_failures_only_filter(client):
    _seed(client.audit_path, [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "suspend",
                               "target": f"u{i:03d}@example.com", "ok": i % 2 == 1} for i in range(80)])
    page1 = client.get("/audit/rows", params={"failed": "1"})
    assert "failed=1" in page1.text

    page2 = client.get(_hx_get(page1.text, "Next"))
    assert page2.status_code == 200
    assert "u028@example.com" in page2.text
    assert "u029@example.com" not in page2.text  # still filtered to ok:false records only
    assert "u030@example.com" not in page2.text  # and page 1's slice is gone


def test_audit_rows_empty_state(client):
    r = client.get("/audit/rows")
    assert r.status_code == 200
    assert "No audited actions yet." in r.text


def test_audit_export_csv(client):
    _seed(client.audit_path, [
        {"ts": "2026-06-23T15:02:51+00:00", "action": "set_vacation", "target": "alice@example.com",
         "argv": ["user", "alice@example.com"], "exit_code": 0, "ok": True},
    ])
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "audit-export.csv" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "ts,action,target,ok,exit_code,error,argv"
    assert "alice@example.com" in lines[1]


def test_audit_export_csv_neutralises_formula_injection(client):
    # A target/argv that begins with =/+/-/@ must not export as a live spreadsheet formula.
    _seed(client.audit_path, [
        {"ts": "2026-06-26T00:00:00+00:00", "action": "set_signature",
         "target": "=HYPERLINK(\"http://evil\")", "ok": True,
         "argv": ["@SUM(1+1)", "signature", "x"], "extra": {"error": "-2+3"}},
    ])
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    body = r.text
    # every cell that STARTS with a formula leader is prefixed with a single quote
    assert "'=HYPERLINK" in body          # target
    assert "'-2+3" in body                # error
    assert "'@SUM(1+1)" in body           # argv (joined string starts with @)
    # no raw formula leader survives at a cell boundary (right after a comma/quote)
    assert ",=HYPERLINK" not in body and ',"=HYPERLINK' not in body


def test_audit_export_covers_history_beyond_the_old_cap(client):
    # One bulk apply is thousands of records; the export must still contain what came before it.
    _seed(client.audit_path, [{"ts": "2026-01-01T00:00:00+00:00", "action": "delete_user",
                               "target": "ancient@example.com", "ok": True}]
          + [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "set_signature",
              "target": f"bulk{i}@example.com", "ok": True} for i in range(2500)])
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    assert "ancient@example.com" in r.text
    assert len(r.text.splitlines()) == 2502  # header + every record


def test_audit_export_filters_over_the_full_log(client):
    # The filter has to run over everything, not over a pre-truncated newest-N window.
    _seed(client.audit_path, [{"ts": "2026-01-01T00:00:00+00:00", "action": "suspend",
                               "target": "ancient@example.com", "ok": False,
                               "extra": {"error": "boom"}}]
          + [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "set_signature",
              "target": f"bulk{i}@example.com", "ok": True} for i in range(2500)])
    r = client.get("/audit/export.csv", params={"failed": 1})
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert len(lines) == 2  # header + the one old failure
    assert "ancient@example.com" in lines[1]


def test_audit_export_includes_rolled_records(client, tmp_path):
    log = _rotating_log(client.audit_path, tmp_path)
    for i in range(20):
        log.record("suspend", target=f"u{i}@example.com", ok=True)
    assert rolled_path(client.audit_path).exists()

    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    body = r.text
    assert all(f"u{i}@example.com" in body for i in range(20))


def test_audit_export_spans_every_generation_exactly_once(client, tmp_path):
    log = _rotating_log(client.audit_path, tmp_path)
    for i in range(40):
        log.record("suspend", target=f"u{i:03d}@example.com", ok=True)
    assert rolled_path(client.audit_path, 2).exists()  # at least two rolls behind us

    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    rows = r.text.splitlines()[1:]
    # every record once — no duplicates, no gaps — and still newest-first across the boundaries
    targets = [row.split(",")[2] for row in rows]
    assert targets == [f"u{i:03d}@example.com" for i in reversed(range(40))]


def test_audit_rows_filter_finds_records_beyond_the_old_cap(client):
    _seed(client.audit_path, [{"ts": "2026-01-01T00:00:00+00:00", "action": "suspend",
                               "target": "ancient@example.com", "ok": True}]
          + [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "set_signature",
              "target": f"bulk{i}@example.com", "ok": True} for i in range(2500)])
    r = client.get("/audit/rows", params={"q": "ancient@example.com"})
    assert r.status_code == 200
    assert "ancient@example.com" in r.text
    assert "1 total (filtered)" in r.text


def test_audit_page_counts_the_whole_log(client):
    _seed(client.audit_path, [{"ts": "2026-01-01T00:00:00+00:00", "action": "suspend",
                               "target": "ancient@example.com", "ok": False}]
          + [{"ts": f"2026-06-23T00:00:{i:02d}+00:00", "action": "set_signature",
              "target": f"bulk{i}@example.com", "ok": True} for i in range(2500)])
    r = client.get("/audit")
    assert r.status_code == 200
    assert "2501 actions logged" in r.text
    assert "1 failure" in r.text


def test_audit_export_csv_no_records_still_200(client):
    r = client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "ts,action,target,ok,exit_code,error,argv"


# --- unconnected: routes shouldn't 500 even with no connector -----------------------------

@pytest.fixture
def unconnected_client(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    c = TestClient(create_app(state))
    c.get("/?token=t")
    return c


def test_audit_page_without_connector(unconnected_client):
    r = unconnected_client.get("/audit")
    assert r.status_code == 200


def test_audit_rows_without_connector(unconnected_client):
    r = unconnected_client.get("/audit/rows")
    assert r.status_code == 200


def test_audit_export_without_connector(unconnected_client):
    r = unconnected_client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
