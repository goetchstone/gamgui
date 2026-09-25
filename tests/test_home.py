"""Home (plan U4): a dashboard that makes no directory call of its own.

The counts come from the cache as it is; a cold cache shows a Load control rather than holding the page
on `gam print users` (the mock's argv log proves which calls ran). Jobs come from the tray's registry
and failures from the audit log, newest first, linking to Audit filtered to failures.
"""

from __future__ import annotations

import re
from html import unescape

from gamgui.web.jobs import start_job

from .test_users_web import client, unconnected_client  # noqa: F401 — the mock-backed TestClient fixtures


def _state(client):  # noqa: F811
    return client.app.state.gamgui


def test_cold_cache_offers_load_and_calls_no_gam(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    html = client.get("/").text
    assert gam_calls()[before:] == [], "Home must not call gam on load (version probes aside)"
    assert 'hx-get="/home/directory"' in html and "Load the directory" in html
    assert 'href="/reports#report-' not in html


def test_warm_cache_counts_without_a_call(client, gam_calls):  # noqa: F811
    users = _state(client).user_cache.cached
    assert users is None
    client.get("/users")                       # fills the cache the way any screen does
    users = _state(client).user_cache.cached
    before = len(gam_calls())
    html = unescape(client.get("/").text)
    assert gam_calls()[before:] == []
    assert re.search(rf">{len(users)} <span[^>]*>accounts", html)
    assert 'href="/reports#report-no_2sv"' in html and "No 2-step verification" in html
    assert "as of just now" in html and 'hx-get="/home/directory?refresh=1"' in html


def test_load_control_reads_the_directory_once(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    html = client.get("/home/directory").text
    calls = gam_calls()[before:]
    assert len(calls) == 1 and calls[0][:2] == ["print", "users"], calls
    assert 'id="home-directory"' in html and 'href="/reports#report-admins"' in html
    client.get("/home/directory")                   # fresh: served from the cache
    assert len(gam_calls()) == before + 1
    client.get("/home/directory?refresh=1")         # Refresh re-reads
    assert len(gam_calls()) == before + 2


def test_recent_failures_link_to_audit_filtered(client):  # noqa: F811
    audit = _state(client).connector.audit
    for i in range(5):
        audit.record("update_user", target=f"u{i}@example.com", ok=False, extra={"error": f"boom {i}"})
    audit.record("update_user", target="fine@example.com", ok=True)
    html = client.get("/").text
    assert html.count("boom ") == 3, "the last few, not the whole log"
    assert html.index("boom 4") < html.index("boom 3") < html.index("boom 2")
    assert "fine@example.com" not in html
    assert 'href="/audit?failed=1"' in html
    page = client.get("/audit?failed=1").text
    assert 'value="1" checked' in page and "fine@example.com" not in page and "u4@example.com" in page


def test_jobs_come_from_the_tray(client):  # noqa: F811
    job = start_job(_state(client).jobs, total=3, title="Apply signature", kind="signatures")
    html = client.get("/").text
    assert f'href="/jobs/{job.id}"' in html and "Apply signature" in html and "1 running" in html


def test_unconnected_home_points_at_setup(unconnected_client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    html = unconnected_client.get("/").text
    assert gam_calls()[before:] == []
    assert 'href="/setup"' in html and "Load the directory" not in html
    assert "Not connected" in html
    assert "Connect a domain" in unconnected_client.get("/home/directory").text
