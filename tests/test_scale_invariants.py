"""What has to stay true as the directory grows (docs/scale.md; scripts/scale_bench.py measures it at 1k–20k by hand).

Small enough for CI: a 600-person synthetic tenant (scripts/scale_fixtures.py) against the strict mock, next to a
60-person one. One directory read serves every screen; a table page and a bulk run's poll cost the same at either size
(invariant 9); a person's own page never reads the whole directory again.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, gam_writes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scale_fixtures  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


def _client(tmp_path, monkeypatch, n: int):
    fixtures = scale_fixtures.generate(n, tmp_path / f"fixtures{n}")
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(fixtures))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / f"audit{n}.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    c = TestClient(create_app(state, allowed_hosts=TEST_HOSTS))
    c.__enter__()
    c.get("/?token=t")
    return c


@pytest.fixture
def small(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, 60)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def large(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, 600)
    yield c
    c.__exit__(None, None, None)


def _reads(calls, verb):
    return [c for c in calls if c[:2] == ["print", verb]]


def test_one_directory_read_serves_every_screen(large, gam_calls):
    for path in ("/users", "/users/table?page=1&size=50", "/users/table?page=12&size=50", "/users/table?q=user01",
                 "/users/detail?email=user00042%40example.com", "/reports", "/reports/fill-in.csv?key=no_title"):
        assert large.get(path).status_code == 200, path
    assert len(_reads(gam_calls(), "users")) == 1


def test_a_table_page_costs_the_same_at_any_size(small, large):
    page = lambda c: len(c.get("/users/table?page=1&size=50").content)   # noqa: E731
    assert page(large) <= page(small) * 1.15


def test_a_bulk_run_s_poll_stays_bounded(large, gam_calls):
    preview = large.post("/reports/inactive/preview", data={"days": "90"}).text
    token = re.search(r'name="preview" value="([^"]+)"', preview).group(1)
    held = large.app.state.gamgui.previews.peek("suspend_inactive", token)
    assert len(held) > 50                                  # enough people that an unbounded feed would show
    run = large.post("/reports/inactive/apply", data={"preview": token, "days": "90", "confirmed": "1",
                                                      "confirm": "confirm", "confirm_count": str(len(held))})
    job_id = re.search(r"status\?job=([\w-]+)", run.text).group(1)
    job = large.app.state.gamgui.jobs[job_id]
    largest = 0
    while not job.finished:
        largest = max(largest, len(large.get(f"/reports/inactive/status?job={job_id}").content))
    largest = max(largest, len(large.get(f"/reports/inactive/status?job={job_id}").content))
    assert len(gam_writes(gam_calls())) == len(held)
    assert largest < 12_000                                # the 12-row feed and the capped failure list, not N rows


def test_reports_costs_about_the_same_at_any_size(small, large):
    # Every member of every card was listed on the page: 12.5 MB of HTML at 20,000 people (docs/scale.md). A card now
    # lists its first REPORT_SHOWN and offers the rest as a CSV.
    assert len(large.get("/reports").content) <= len(small.get("/reports").content) * 1.6
    page = large.get("/reports").text
    assert "Download all" in page and 'href="/reports/list.csv?key=no_title" download' in page


def test_a_card_s_csv_has_everyone_it_counts(large):
    rows = large.get("/reports/list.csv?key=no_title").text.strip().splitlines()
    page = large.get("/reports").text
    count = int(re.search(r'id="report-no_title".*?font-serif[^>]*>(\d+)<', page, re.S).group(1))
    assert rows[0] == "email,name,org_unit,suspended" and len(rows) - 1 == count > 50
    assert large.get("/reports/list.csv?key=nope").status_code == 400
