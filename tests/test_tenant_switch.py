"""A tenant switch (plan U10b) leaves nothing of the old tenant usable under the new one: a confirm step
previewed before it, a directory read queued across it, the Builder's last result (review 2: R5, R9,
R11). Plus two directory-cache follow-ups: offboarding drops the list when its run finishes, and a
user's page serves the cached list with its age instead of blocking on a full re-read."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from gamgui.core import clock
from gamgui.core import lifecycle as core_lifecycle
from gamgui.core.gam.models import GAMUser
from gamgui.web.previews import Previews
from gamgui.web.server import AppState

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401 — the mock-backed TestClient fixture

OTHER = "example.org"


@pytest.fixture
def t(monkeypatch):
    now = {"now": 1000.0}
    monkeypatch.setattr(clock, "now", lambda: now["now"])
    return now


def _u(email: str) -> GAMUser:
    return GAMUser.from_json({"primaryEmail": email, "name": {"fullName": email.split("@")[0]}})


def _token(html: str) -> str:
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', html)
    assert m, html[:300]
    return m.group(1)


def _switch_to_other(client, monkeypatch):  # noqa: F811
    """Put a second domain in the Keychain and switch to it, with verify stubbed to pass."""
    from gamgui.core.setup import VerifyResult

    st = client.app.state.gamgui
    oauth2 = json.dumps({"token": "tok", "decoded_id_token": {"email": f"boss@{OTHER}"}})
    st.vault.set_all(OTHER, {"oauth2": oauth2, "oauth2service": json.dumps({"client_id": "x"})})

    async def ok_verify(self, domain, admin):
        return VerifyResult(ok=True, summary="verified")
    monkeypatch.setattr("gamgui.core.setup.SetupService.verify", ok_verify)
    r = client.post("/setup/switch", data={"tenant": OTHER})
    assert r.status_code == 200 and st.connector.domain == OTHER


# --- R5: a preview is bound to the tenant it was made on -------------------------------------------

def test_a_held_preview_is_refused_once_the_active_domain_changes():
    tenant = {"d": "example.com"}
    store = Previews()
    store.bind(lambda: tenant["d"])
    token = store.hold("f", ("form",), "value")
    tenant["d"] = OTHER
    value, why = store.take("f", token, ("form",), again="click Preview again")
    assert value is None and "domain changed" in why and "click Preview again" in why
    kept = store.hold("f", ("form",), "value")          # a preview made on the new tenant runs on it
    assert store.take("f", kept, ("form",)) == ("value", None)


def test_clear_drops_every_held_preview():
    store = Previews()
    store.hold("a", 1, 1)
    store.hold("b", 2, 2)
    store.clear()
    assert store.count("a") == store.count("b") == 0


@pytest.mark.parametrize("route, preview, body", [
    ("/signatures/apply", "/signatures/preview",
     {"template": "{name}", "scope_type": "user", "scope_value": "alice@example.com"}),
    ("/builder/run", "/builder/preview", {"cid": "build.suspend_user", "email": "alice@example.com"}),
])
def test_a_confirm_step_previewed_before_a_switch_runs_nothing_after_it(client, monkeypatch, gam_calls,  # noqa: F811
                                                                        route, preview, body):
    st = client.app.state.gamgui
    token = _token(client.post(preview, data=body).text)
    _switch_to_other(client, monkeypatch)
    before = len(gam_writes(gam_calls()))
    r = client.post(route, data={**body, "confirmed": "1", "preview": token})
    assert "again" in r.text and "?job=" not in r.text, r.text[:300]
    assert len(gam_writes(gam_calls())) == before and not st.jobs


# --- R11: nothing the old tenant returned is served under the new one --------------------------------

def test_a_switch_drops_the_builder_result_and_held_previews(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    st.builder_last_result = {"id": "r1", "records": [{"primaryEmail": "alice@example.com"}]}
    client.post("/signatures/preview", data={"template": "{name}", "scope_type": "company", "scope_value": ""})
    assert st.previews.count("signatures") == 1
    _switch_to_other(client, monkeypatch)
    assert st.builder_last_result is None and st.previews.count("signatures") == 0


# --- R9: a read bound to the old connector never fills the new tenant's cache ------------------------

def _conn(domain: str, gate: asyncio.Event, calls: list) -> SimpleNamespace:
    async def list_users(fields=None):
        calls.append(domain)
        await gate.wait()
        return [_u(f"user@{domain}")]

    async def list_groups():
        calls.append(domain)
        await gate.wait()
        return [f"group@{domain}"]
    return SimpleNamespace(domain=domain, list_users=list_users, list_groups=list_groups)


@pytest.mark.parametrize("read, cache", [("users", "user_cache"), ("groups", "group_cache")])
async def test_a_read_queued_on_the_cache_lock_across_a_switch_is_not_kept(t, read, cache):
    gate, calls = asyncio.Event(), []
    st = AppState(vault=None, runner=None, audit_domain="example.com",   # type: ignore[arg-type]
                  connector=_conn("example.com", gate, calls))
    r1 = asyncio.create_task(getattr(st, read)())     # holds the lock, reading example.com
    for _ in range(3):
        await asyncio.sleep(0)
    r2 = asyncio.create_task(getattr(st, read)())     # its fetch is bound to example.com; it queues
    for _ in range(3):
        await asyncio.sleep(0)
    assert calls == ["example.com"]
    st.activate(_conn(OTHER, gate, calls))             # the switch lands while r2 waits
    gate.set()
    await asyncio.gather(r1, r2)
    assert getattr(st, cache).cached is None           # neither old-tenant read was stored as the new one's
    fresh = await getattr(st, read)()
    assert calls[-1] == OTHER and OTHER in str(fresh[0])


# --- Follow-up: offboarding drops the directory list when its run finishes, too -----------------------

def test_offboarding_drops_the_directory_cache_when_its_run_finishes(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui
    real = core_lifecycle.run_offboard

    async def run_with_a_page_load_mid_run(job, conn, steps, done=frozenset()):
        st.user_cache._items, st.user_cache._at = ["read before the run changed it"], clock.now()
        await real(job, conn, steps, done=done)
    monkeypatch.setattr(core_lifecycle, "run_offboard", run_with_a_page_load_mid_run)
    body = {"user": "carol@example.com", "manager": "alice@example.com", "subject": "s", "message": "m",
            "days": "30", "notify": ""}
    token = _token(client.post("/lifecycle/offboard/preview", data=body).text)
    client.post("/lifecycle/offboard/run", data={**body, "confirmed": "1", "preview": token})
    [job] = st.jobs.values()
    wait_for_job(client, job)
    assert st.user_cache.cached is None


# --- Follow-up: a user's page serves the cached list with its age, past the TTL too ------------------

def test_user_detail_serves_a_stale_list_with_its_age_and_refreshes_behind_it(client, t, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    client.get("/users/table")                         # the directory is read once ...
    t["now"] += 301                                    # ... and is now past its TTL
    page = client.get("/users/detail", params={"email": "alice@example.com"}).text
    assert "Directory as of 5 min ago" in page         # served at once, and says how old
    assert 'href="/users/detail?email=alice%40example.com&amp;refresh=1"' in page
    wait_for_job(client, SimpleNamespace(task=st.user_cache._refresh))   # one refresh ran behind it
    assert st.user_cache.age_seconds == 0


def test_user_detail_refresh_reads_just_that_account(client, gam_calls):  # noqa: F811
    client.get("/users/table")
    reads = len(gam_calls())
    page = client.get("/users/detail", params={"email": "alice@example.com", "refresh": "1"}).text
    new = gam_calls()[reads:]
    assert new and all("print" not in c[:3] for c in new)                  # not the whole directory
    assert any("info" in c and "alice@example.com" in c for c in new)
    assert "alice@example.com" in page and "Directory as of" not in page   # fresh: no age to show
