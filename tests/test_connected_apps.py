"""Reports' connected apps and agents: access said in words with its reach, an AI assistant or agent known by whole
words of its name, the grants grouped by app, the summary counted by code, and the read that runs only when asked.
The pure cases are SwiftGamGui's `ConnectedAppsTests`, so the two apps say the same thing. Offline only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import clock
from gamgui.core.audit import AuditLog
from gamgui.core.connected_apps import (
    AppsRead,
    Area,
    Reach,
    Summary,
    apps_from,
    describe,
    grants_from,
    groups_of,
    looks_like_ai,
    read_from,
)
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.parser import parse_records
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.jobs import PANELS, UNSTOPPABLE
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
HEADER = "user,clientId,displayText,anonymous,nativeApp,userKey,scopes\n"
SCOPE = "https://www.googleapis.com/auth/"
ROW = "b@example.com,1-x,Zoom,False,False,2,openid\n"


def _apps(csv_text: str):
    return apps_from(grants_from(parse_records(csv_text)))


def test_print_tokens_argv_is_swift_gamgui_s():
    # SwiftGamGui's GamCommands.printTokens() with no user. The grammar: `gam print tokens|token ...`.
    assert GAMCommands.print_tokens() == ["print", "tokens"]


def test_access_is_said_with_its_reach():
    assert describe("https://mail.google.com/") == ("all of Gmail (read, send, delete)", Reach.BROAD, Area.MAIL)
    assert describe(f"{SCOPE}gmail.settings.sharing")[1] == Reach.BROAD
    assert describe(f"{SCOPE}drive.readonly") == ("read all of Drive", Reach.SOME, None)
    assert describe(f"{SCOPE}admin.directory.user") == ("admin: directory.user", Reach.BROAD, Area.ADMIN)
    assert describe("openid")[1] == Reach.LITTLE
    assert describe("https://example.com/auth/thing/")[0] == "thing"


@pytest.mark.parametrize("name", ["ChatGPT", "ChatGPT for Gmail", "Read AI Notetaker", "Claude", "Microsoft Copilot",
                                  "Inbox Agent", "GPT-4o mailer", "my_agent"])
def test_an_ai_assistant_is_known_by_whole_words_of_its_name(name):
    assert looks_like_ai(name)


@pytest.mark.parametrize("name", ["Zoom", "Promotion Manager", "Harry Potter Quiz", "Airtable", "Mailchimp", "Painting Pro", ""])
def test_a_word_inside_a_word_is_not_ai(name):
    assert not looks_like_ai(name)


def test_grants_are_grouped_by_app_ai_first_then_reach():
    apps = _apps(HEADER
                 + f"a@example.com,1-zoom,Zoom,False,False,1,{SCOPE}calendar.readonly\n"
                 + f"b@example.com,1-zoom,Zoom,False,False,2,{SCOPE}calendar.readonly\n"
                 + "a@example.com,2-sync,Mail Sync,False,False,1,https://mail.google.com/\n"
                 + f"b@example.com,3-gpt,ChatGPT,False,False,2,{SCOPE}gmail.readonly\n")
    assert [a.name for a in apps] == ["ChatGPT", "Mail Sync", "Zoom"]
    assert apps[2].users == ("a@example.com", "b@example.com") and apps[2].access == ("read calendars",)
    assert apps[1].reach == Reach.BROAD and apps[0].looks_like_ai
    assert [title for title, _ in groups_of(apps)] == ["AI assistants and agents, by name", "Broad access", "Other apps"]


def test_nothing_read_is_no_apps():
    assert _apps(HEADER) == [] and _apps("") == []
    assert groups_of([]) == []


def test_an_app_with_no_name_or_scopes_still_lists():
    # GAM writes what the API returned: a blank name is the client ID, and an empty scope list has no reach to show.
    app = _apps(HEADER + "a@example.com,1-blank,,False,False,1,\n")[0]
    assert app.name == "1-blank" and app.access == () and app.reach == Reach.LITTLE and not app.looks_like_ai


def test_a_name_with_gam_s_csv_escapes_reads_whole():
    # GAM writes a quote in its escaped dialect (failure log 2026-10-30): the name must not lose its row.
    apps = _apps(HEADER + 'a@example.com,1-x,"Bob\\\\"" ""Sync, Inc",False,False,1,openid\n')
    assert len(apps) == 1 and "Sync, Inc" in apps[0].name


def test_every_broad_scope_has_a_kind():
    for scope in ["https://mail.google.com/", "gmail.modify", "gmail.settings.sharing", "drive", "cloud-platform",
                  "admin.directory.user", "gmail.readonly", "drive.readonly", "drive.file", "calendar", "openid",
                  "gmail.labels", "something.else"]:
        _, reach, area = describe(scope)
        assert (reach == Reach.BROAD) == (area is not None), scope


def test_the_summary_counts_the_apps():
    apps = _apps(HEADER
                 + f"a@example.com,1-zoom,Zoom,False,False,1,{SCOPE}calendar.readonly\n"
                 + f"B@example.com,1-zoom,Zoom,False,False,2,{SCOPE}calendar.readonly\n"
                 + f"c@example.com,1-zoom,Zoom,False,False,3,{SCOPE}calendar.readonly\n"
                 + f"a@example.com,2-sync,Mail Sync,False,False,1,https://mail.google.com/ {SCOPE}drive\n"
                 + f"b@example.com,3-gpt,ChatGPT,False,False,2,{SCOPE}gmail.readonly\n"
                 + f"d@example.com,4-agent,Inbox Agent,False,False,4,{SCOPE}gmail.settings.sharing\n"
                 + f"c@example.com,5-admin,Directory Sync,False,False,3,{SCOPE}admin.directory.user\n"
                 + f"a@example.com,5-admin,Directory Sync,False,False,1,{SCOPE}admin.directory.user\n")
    assert next(a for a in apps if a.name == "Mail Sync").areas == (Area.MAIL, Area.DRIVE)
    summary = Summary.of(apps)
    assert (summary.apps, summary.people, summary.ai, summary.ai_people) == (5, 4, 2, 2)   # addresses' case aside
    assert (summary.broad, summary.ai_broad) == (3, 1)
    assert summary.areas == {Area.MAIL: 1, Area.DRIVE: 1, Area.FORWARDING: 1, Area.ADMIN: 1}
    assert (summary.one_person, summary.one_person_ai, summary.one_person_broad, summary.most_people) == (3, 2, 2, 3)
    assert summary.sentences == [
        "4 people have given 5 apps access.",
        "2 apps look like an AI assistant or agent by name, granted by 2 people; 1 with broad access.",
        "3 apps have broad access: 1 to all of Gmail or reading and changing mail, 1 to Gmail forwarding and delegates, "
        "1 to all of Drive, 1 to admin access.",
        "3 apps were granted by one person only, among them 2 that look like AI and 2 with broad access.",
        "At most 3 people granted any one app.",
    ]
    assert Summary.of([]).sentences == ["No account has given an app access."]


def test_one_kind_of_broad_access_is_said_without_its_count():
    apps = _apps(HEADER + f"a@example.com,1-x,Files,False,False,1,{SCOPE}drive\n")
    assert "1 app has broad access, to all of Drive." in Summary.of(apps).sentences


def test_the_summary_says_when_nothing_is_ai_or_broad():
    sentences = Summary.of(_apps(HEADER + f"a@example.com,1-x,Zoom,False,False,1,{SCOPE}calendar.readonly\n"
                                 + f"b@example.com,1-x,Zoom,False,False,2,{SCOPE}calendar.readonly\n")).sentences
    assert "No app looks like an AI assistant or agent by its name." in sentences
    assert "No app has broad access (all of Gmail or Drive, forwarding, admin or Google Cloud)." in sentences
    assert not any("one person only" in s for s in sentences)


# --- the read, through the mock ----------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch) -> AppState:
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    return AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")


@pytest.fixture
def client(state):
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _read(client, state) -> str:
    r = client.post("/reports/connected-apps/read")
    assert r.status_code == 200
    job = state.jobs[state.connected_apps_job_id]
    wait_for_job(client, job)
    return client.get(f"/reports/connected-apps?job={job.id}").text


def _deny(gam_state: Path) -> None:
    (gam_state / "denied").mkdir()
    (gam_state / "denied" / "tokens").write_text("")


def test_gam_s_progress_stays_out_of_the_read(state):
    # GAM's progress goes to stderr, so stdout stays CSV.
    read = asyncio.run(state.connector.connected_apps())
    assert read.app_count == 5 and read.complete


def test_gam_s_progress_is_not_a_refusal_s_message(state, gam_state):
    # GAM says "Getting Access Tokens for …" for the account before it is refused (failure log 2026-10-30).
    _deny(gam_state)
    with pytest.raises(GAMError) as caught:
        asyncio.run(state.connector.connected_apps())
    assert "Getting" in caught.value.stderr
    assert "Getting" not in caught.value.message and "Not Authorized" in caught.value.stderr


def _gone(gam_state: Path, address: str) -> None:
    (gam_state / "tokens_gone").mkdir()
    (gam_state / "tokens_gone" / address).write_text("")


def test_an_account_gam_cannot_read_leaves_the_others(client, state, gam_state):
    # entityUnknownWarning: GAM carries on, writes the CSV and exits 56. Every other account is there.
    _gone(gam_state, "alice@example.com")
    panel = _read(client, state)
    assert "GAM couldn’t read at least one account" in panel
    assert "ChatGPT" in panel and "alice@example.com" not in panel and "macOS" not in panel
    assert "Couldn’t read the connected apps." not in panel
    read, _ = state.connected_apps
    assert not read.complete and read.app_count == 4


def test_a_complete_read_does_not_warn(client, state):
    assert "GAM couldn’t read at least one account" not in _read(client, state)


@pytest.mark.parametrize("exit_code,stdout", [(12, HEADER + ROW), (2, HEADER + ROW), (56, ""), (73, ""), (56, HEADER),
                                               (73, HEADER), (56, "User: x, Does not exist\nmore\n"), (None, HEADER + ROW)])
def test_only_a_one_account_warning_with_a_csv_keeps_the_read(state, monkeypatch, exit_code, stdout):
    async def fails(*args, **kwargs):
        raise GAMError.from_run(exit_code, "ERROR: it failed\n", ["print", "tokens"], stdout=stdout)

    monkeypatch.setattr(state.runner, "run_authenticated", fails)
    with pytest.raises(GAMError):
        asyncio.run(state.connector.connected_apps())


def test_exit_73_is_a_one_account_warning_too(state, monkeypatch):
    # entityUnknownWarning for an address outside the primary domain: SERVICE_NOT_APPLICABLE_RC.
    async def fails(*args, **kwargs):
        raise GAMError.from_run(73, "User: a@other.example, Service not applicable/Does not exist (1/2)\n", ["print", "tokens"],
                                stdout=HEADER + ROW)

    monkeypatch.setattr(state.runner, "run_authenticated", fails)
    read = asyncio.run(state.connector.connected_apps())
    assert not read.complete and read.app_count == 1


def test_a_non_gam_failure_says_one_thing_once(client, state):
    async def broken():
        raise RuntimeError("the secret detail")

    state.connector.connected_apps = broken
    panel = _read(client, state)
    assert panel.count("Couldn’t read the connected apps.") == 1 and "the secret detail" not in panel
    assert "Couldn't read" not in panel


def test_reports_does_not_announce_an_old_read_again(client, state):
    # The panel loads by htmx after the page, so app.js would speak anything marked on every visit to Reports.
    job = client.post("/reports/connected-apps/read") and state.jobs[state.connected_apps_job_id]
    wait_for_job(client, job)
    assert "data-announce" not in client.get("/reports/connected-apps").text
    assert f'data-announce="{job.id}"' in client.get(f"/reports/connected-apps?job={job.id}").text


def test_the_refusal_of_an_old_read_is_not_shown_beside_a_newer_one(client, state, gam_state):
    _deny(gam_state)
    _read(client, state)
    old = state.connected_apps_job_id
    (gam_state / "denied" / "tokens").unlink()
    _read(client, state)
    panel = client.get(f"/reports/connected-apps?job={old}").text
    assert "Couldn’t read the connected apps." not in panel and "ChatGPT" in panel


def test_another_job_s_id_is_not_this_panel_s(client, state):
    from gamgui.web.jobs import start_job

    other = start_job(state.jobs, 1, kind="index", title="Calendar index rebuild")
    other.error = "the index failed"
    other.finish()
    assert "the index failed" not in client.get(f"/reports/connected-apps?job={other.id}").text


def test_a_failed_read_after_a_switch_does_not_follow_to_the_new_tenant(client, state, gam_state, tmp_path):
    _deny(gam_state)
    _read(client, state)
    old = state.connected_apps_job_id
    state.activate(GAMConnector(runner=state.runner, domain="other.example", audit=AuditLog(tmp_path / "o.jsonl")))
    assert "Couldn’t read the connected apps." not in client.get(f"/reports/connected-apps?job={old}").text


@pytest.mark.hand_built_argv
def test_the_mock_refuses_an_option_it_does_not_model(state):
    # GAM has clientid, delimiter, orderby and more. A builder that grows one must model it here first.
    with pytest.raises(GAMError):
        asyncio.run(state.runner.run_authenticated(DOMAIN, ["print", "tokens", "clientid", "x"]))


def test_reports_never_reads_by_itself(client, gam_calls):
    assert 'hx-get="/reports/connected-apps"' in client.get("/reports").text
    panel = client.get("/reports/connected-apps").text
    assert "Read connected apps" in panel and "hx-disabled-elt" in panel
    assert not [argv for argv in gam_calls() if "tokens" in argv]


def test_a_read_lists_every_account_but_the_suspended(client, state, gam_calls):
    panel = _read(client, state)
    assert ["print", "tokens"] in gam_calls()
    assert "Only Bob Has This" not in panel and "bob@example.com" not in panel
    assert "ChatGPT" in panel and "Mail Sync Pro" in panel and "Zoom" in panel
    assert panel.index("ChatGPT") < panel.index("Mail Sync Pro") < panel.index("Zoom")
    assert "2 people have given 5 apps access." in panel
    assert "AI assistants and agents, by name (2)" in panel and "Broad access (1)" in panel and "Other apps (2)" in panel
    assert "all of Gmail (read, send, delete)" in panel
    assert 'href="/users/detail?email=carol%40example.com"' in panel
    assert "Read again" in panel


def test_the_read_is_kept_and_not_repeated(client, state, gam_calls):
    _read(client, state)
    before = len(gam_calls())
    assert "ChatGPT" in client.get("/reports/connected-apps").text
    assert len(gam_calls()) == before


def test_a_second_read_waits_for_the_first(client, state):
    release = client.portal.call(asyncio.Event)

    async def held():
        await release.wait()
        return AppsRead.of([])

    state.connector.connected_apps = held
    panel = client.post("/reports/connected-apps/read").text
    first = state.connected_apps_job_id
    assert "Reading every account" in panel and f"job={first}" in panel
    assert "Reading every account" in client.post("/reports/connected-apps/read").text
    assert state.connected_apps_job_id == first
    client.portal.call(release.set)
    wait_for_job(client, state.jobs[first])


def test_a_refused_read_says_what_to_do_and_keeps_nothing(client, state, gam_state):
    _deny(gam_state)
    panel = _read(client, state)
    assert "Couldn’t read the connected apps." in panel and "Try again" in panel
    assert state.connected_apps is None


def test_a_refused_read_is_gam_s_exit_12(state, gam_state):
    _deny(gam_state)
    with pytest.raises(GAMError) as caught:
        asyncio.run(state.connector.connected_apps())
    assert caught.value.exit_code == 12 and caught.value.remediation


def test_a_failed_read_again_keeps_showing_the_last_read(client, state, gam_state):
    _read(client, state)
    _deny(gam_state)
    panel = _read(client, state)
    assert "Couldn’t read the connected apps." in panel and "ChatGPT" in panel


def test_an_app_s_name_is_only_ever_text(client, state):
    # An app's maker writes its name. `<img src=x onerror=…>` must arrive escaped.
    apps = apps_from(grants_from([{"user": "a@example.com", "clientId": "1-x", "displayText": "<img src=x onerror=alert(1)>",
                                   "scopes": "<script>alert(2)</script>"}]))
    state.connected_apps = (AppsRead.of(apps), clock.now())
    panel = client.get("/reports/connected-apps").text
    assert "<img" not in panel and "<script" not in panel and "&lt;img" in panel


def test_who_granted_it_stops_at_six_names(client, state):
    grants = [{"user": f"u{i}@example.com", "clientId": "1-x", "displayText": "Zoom", "scopes": "openid"} for i in range(8)]
    state.connected_apps = (read_from(grants), clock.now())
    panel = client.get("/reports/connected-apps").text
    assert "8 people:" in panel and "u5@example.com" in panel and "u6@example.com" not in panel and "and 2 more" in panel


def test_another_connection_drops_the_read(client, state):
    _read(client, state)
    state.activate(state.connector)
    panel = client.get("/reports/connected-apps").text
    assert "ChatGPT" not in panel and "Read connected apps" in panel


def test_a_read_that_ends_after_a_switch_is_not_kept(client, state, tmp_path):
    release = client.portal.call(asyncio.Event)
    other = GAMConnector(runner=state.runner, domain="other.example", audit=AuditLog(tmp_path / "other.jsonl"))

    async def slow():
        await release.wait()
        return read_from([{"user": "a@example.com", "clientId": "1-x", "displayText": "Old Tenant App", "scopes": ""}])

    state.connector.connected_apps = slow
    client.post("/reports/connected-apps/read")
    job = state.jobs[state.connected_apps_job_id]
    state.activate(other)
    client.portal.call(release.set)
    wait_for_job(client, job)
    assert state.connected_apps is None and state.connected_apps_job_id == ""
    assert "Old Tenant App" not in client.get("/reports/connected-apps").text
    assert "Not kept" in job.error and "Done" not in client.get("/jobs/status").text   # the tray says why


def test_a_read_that_runs_out_of_time_does_not_suggest_the_same_again(client, state, monkeypatch):
    async def timed_out(*args, **kwargs):
        raise GAMError.from_run(None, "", ["print", "tokens"])

    monkeypatch.setattr(state.runner, "run_authenticated", timed_out)
    panel = _read(client, state)
    assert "didn’t finish" in panel.replace("&#39;", "’").replace("'", "’") and "retry" not in panel.lower()


def test_not_connected_says_so_and_reads_nothing(tmp_path, gam_calls):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        assert "Connect a domain on Setup first." in c.get("/reports/connected-apps").text
        assert "Connect a domain on Setup first." in c.post("/reports/connected-apps/read").text
    assert state.connected_apps_job_id == "" and not gam_calls()


def test_the_job_has_a_page_and_no_stop(client, state):
    assert "connected_apps" in PANELS and "connected_apps" in UNSTOPPABLE
    _read(client, state)
    job = state.jobs[state.connected_apps_job_id]
    assert job.status_url == f"/reports/connected-apps?job={job.id}"
    assert not job.can_stop
    assert 'hx-get="/reports/connected-apps?job=' in client.get(f"/jobs/{job.id}").text
